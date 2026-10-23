# -*- coding: utf-8 -*-
"""Предпроверка соединения с базой до запуска 1С (stdlib).

Зачем: 1С показывает ошибки своими окнами, текст из них не читается. Но самые частые
провалы («Сервер 1С:Предприятия не обнаружен», «Неправильный путь к файлу '1Cv8.cdn'»,
недоступная папка базы) видны заранее — по строке соединения.
"""
from __future__ import annotations

import socket
import urllib.parse
import re
from pathlib import Path
from urllib.request import getproxies
from urllib.request import urlopen, Request

DEFAULT_PORT = 1541
TIMEOUT = 2.0


def parse_conn(conn: str) -> dict:
    """Разбирает строку соединения 1С: Srvr/ws/File."""
    c = (conn or '').strip().rstrip(';')
    low = c.lower()
    out = {'kind': '', 'raw': conn or ''}
    # порядок важен: у Srvr= значение может содержать tcp://, поэтому проверяем его первым
    if 'file=' in low and 'srvr=' not in low:
        path = c.split('=', 1)[1].strip().strip('"').strip()
        out.update(kind='file', path=path, target=path)
        return out
    if 'srvr=' in low:
        srvr = ''
        for part in c.split(';'):
            if part.strip().lower().startswith('srvr='):
                srvr = part.split('=', 1)[1].strip().strip('"').strip()
                break
        ref = ''
        for part in c.split(';'):
            if part.strip().lower().startswith('ref='):
                ref = part.split('=', 1)[1].strip().strip('"').strip()
                break
        host, port = srvr, DEFAULT_PORT
        # частый вид: tcp://host:port или host:port (IPv6 — в скобках)
        if '://' in host:
            host = host.split('://', 1)[1]
        host = host.strip('\\/')
        if host.startswith('[') and ']' in host:
            h, _, rest = host[1:].partition(']')
            host, port = h, rest.lstrip(':') or port
        elif host.count(':') == 1:
            host, _, p = host.partition(':')
            port = p or port
        out.update(kind='server', host=host, port=int(port), ref=ref,
                   target='%s:%s' % (host, port))
        return out
    # веб-база: ws="url" или просто URL (base['ws'] в /api/precheck приходит без обёртки)
    if 'ws=' in low or '://' in c:
        url = c.split('=', 1)[1].strip() if 'ws=' in low else c
        url = url.strip().strip('"').strip()
        u = urllib.parse.urlparse(url if '://' in url else 'http://' + url)
        out.update(kind='web', url=url, host=u.hostname or '',
                   port=u.port or (443 if u.scheme == 'https' else 80),
                   target='%s://%s' % (u.scheme or 'http', u.netloc))
        return out
    return out


def check(conn: str, timeout: float = TIMEOUT) -> dict:
    """Проверяет доступность базы. Возвращает {'ok', 'kind', 'target', 'error', 'hint'}."""
    p = parse_conn(conn)
    kind = p.get('kind')
    res = {'ok': True, 'kind': kind, 'target': p.get('target', ''), 'error': '', 'hint': ''}
    if kind == 'file':
        path = p.get('path') or ''
        if not path:
            res.update(ok=False, error='в строке соединения нет пути к базе')
        elif not Path(path).is_dir():
            res.update(ok=False, error='папка базы не найдена',
                       hint='Проверь путь к базе и доступность диска.')
        else:
            res['ok'] = (Path(path) / '1Cv8.cdn').is_file() or any(Path(path).glob('1Cv8.1CD'))
            if not res['ok']:
                res.update(error='в папке нет файла базы 1Cv8.1CD',
                           hint='Похоже, папка базы указана неверно.')
        return res
    if kind == 'web':
        host, port = p.get('host'), p.get('port')
        ok, err = _tcp(host, port, timeout)
        if not ok:
            res.update(ok=False, error='веб-сервер недоступен: %s' % err,
                       hint='Сайт публикации не отвечает (VPN/сертификат/порт).')
        return res
    if kind == 'server':
        host, port = p.get('host'), p.get('port')
        if not host:
            res.update(ok=False, error='в строке соединения нет адреса сервера')
            return res
        ok, err = _tcp(host, port, timeout)
        if not ok:
            res.update(ok=False, error='сервер 1С:Предприятия не обнаружен (%s)' % err,
                       hint='Порт %s на «%s» не отвечает: сервер выключен, другой порт кластера, '
                            'нет VPN/маршрута или неверное имя.' % (port, host))
        return res
    res.update(ok=False, error='не удалось разобрать строку соединения',
               hint='Ожидается Srvr="host:port";Ref="база"; , File="путь"; или ws="адрес";')
    return res


def _tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """TCP-коннект: ok + текст ошибки. Использует getaddrinfo по всем адресам."""
    if not host or not port:
        return False, 'не указан адрес'
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return False, 'имя не разрешается: %s' % (e.strerror or e)
    last = ''
    for fam, typ, proto, _canon, addr in infos:
        s = socket.socket(fam, typ, proto)
        s.settimeout(timeout)
        try:
            if s.connect_ex(addr) == 0:
                return True, ''
            last = 'порт %s закрыт (connect refused)' % port
        except OSError as e:
            last = e.strerror or str(e)
        finally:
            s.close()
    return False, last or 'нет связи'


def web_version(url: str, timeout: float = 8.0) -> str:
    """Версия платформы веб-публикации из её стартовой страницы (var VERSION = "8.3.27.1989").

    Нужна, чтобы выбрать подходящий клиент: тонкий клиент НОВЕЕ публикации к ней не подключается
    («Не удается установить соединение»), а 1С ещё и перезапускается своим автообновлением.
    """
    try:
        u = (url or '').strip().rstrip(';').strip('"')
        if not u.lower().startswith(('http://', 'https://')):
            u = 'http://' + u
        req = Request(u, headers={'User-Agent': 'Mozilla/5.0 (BaseLauncher)'})
        html = urlopen(req, timeout=timeout).read(400_000).decode('utf-8', 'replace')
        m = re.search(r'VERSION\s*=\s*"(\d+\.\d+\.\d+\.\d+)"', html)
        if not m:
            m = re.search(r'sysver=(\d+\.\d+\.\d+\.\d+)', html)
        return m.group(1) if m else ''
    except Exception:                   # noqa: BLE001
        return ''
