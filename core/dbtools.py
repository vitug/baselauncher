# -*- coding: utf-8 -*-
"""Операции над файловой базой: загрузка из .dt и проверка базы данных (chdbfl).

Только stdlib. Долгие операции идут в фоновом потоке, состояние читается из REST.
"""
import datetime
import os
import shutil
import subprocess
import threading
from pathlib import Path

# guid -> {state, dt, started, finished, log, error, backup, cmd}
RESTORE = {}
_LOCK = threading.Lock()


def _ts() -> str:
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def _decode(raw: bytes) -> str:
    for enc in ('utf-8', 'cp866'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('cp1251', errors='replace')


def tool_bin(exe: str) -> Path:
    """Каталог bin платформы, которой запускается база ('' -> пусто)."""
    if not exe:
        return Path()
    p = Path(exe)
    return p.parent if p.parent.is_dir() else Path()


def find_tool(exe: str, name: str) -> str:
    """Ищем утилиту платформы: рядом с клиентом базы, иначе в самой новой установке 8.3/8.5."""
    b = tool_bin(exe)
    if b and (b / name).is_file():
        return str(b / name)
    roots = [Path(r'C:\Program Files\1cv8'), Path(r'C:\Program Files (x86)\1cv8')]
    cands = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in root.iterdir():
            f = d / 'bin' / name
            if f.is_file():
                try:
                    key = tuple(int(x) for x in d.name.split('.'))
                except ValueError:
                    key = (0,)
                cands.append((key, str(f)))
    if not cands:
        return ''
    return sorted(cands, key=lambda c: c[0])[-1][1]


def disk_base(base: dict) -> dict:
    """Файловая база: есть каталог базы и 1Cv8.1CD. Возвращает {ok, path, cd, why}."""
    path = (base.get('path') or '').strip()
    if not path:
        return {'ok': False, 'why': 'это не файловая база (нет пути на диске)'}
    d = Path(path)
    if not d.is_dir():
        return {'ok': False, 'why': 'каталога базы нет: %s' % path}
    cds = sorted(d.glob('1Cv8.1CD'))
    if not cds:
        return {'ok': False, 'why': 'в каталоге нет файла 1Cv8.1CD'}
    return {'ok': True, 'path': str(d), 'cd': str(cds[0])}


def _sessions(base: dict) -> list:
    pids = base.get('running_pids') or {}
    return [m for m, p in pids.items() if p]


def state_of(guid: str) -> dict:
    with _LOCK:
        return dict(RESTORE.get(guid) or {})


def start_restore(guid: str, base: dict, dt: str, backup: bool = True) -> dict:
    """Запустить загрузку из .dt в фоне. Возвращает {ok} или {error}."""
    with _LOCK:
        cur = RESTORE.get(guid) or {}
        if cur.get('state') == 'running':
            return {'error': 'загрузка этой базы уже идёт'}
    info = disk_base(base)
    if not info['ok']:
        return {'error': info['why']}
    p = Path(dt.strip().strip('"'))
    if not p.is_file():
        return {'error': 'файла .dt нет: %s' % dt}
    if p.suffix.lower() != '.dt':
        return {'error': 'ожидается файл выгрузки .dt, а не %s' % (p.suffix or '(без расширения)')}
    if _sessions(base):
        return {'error': 'база открыта (%s) — закройте сеансы и повторите' % ', '.join(_sessions(base))}
    ibcmd = find_tool(base.get('exe') or '', 'ibcmd.exe')
    if not ibcmd:
        return {'error': 'не найден ibcmd.exe (нужна платформа 8.3.17+)'}
    with _LOCK:
        RESTORE[guid] = {'state': 'running', 'dt': str(p), 'ibcmd': ibcmd,
                         'started': datetime.datetime.now().isoformat(timespec='seconds'),
                         'backup': '', 'log': '', 'error': ''}

    def work():
        log = []
        bdir = Path(info['path'])
        bak = ''
        try:
            if backup:
                bak = '%s.restore-bak-%s' % (bdir, _ts())
                bdir.rename(bak)                     # старую базу отодвигаем целиком
                log.append('бэкап базы: %s' % bak)
            bdir.mkdir(parents=True, exist_ok=True)
            cmd = [ibcmd, 'infobase', 'restore', '--db-path=%s' % bdir,
                   '--create-database', '--force', str(p)]
            log.append(' '.join('"%s"' % c if ' ' in c else c for c in cmd))
            pr = subprocess.run(cmd, capture_output=True, timeout=6 * 3600,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            out = (_decode(pr.stdout or b'') + _decode(pr.stderr or b'')).strip()
            if out:
                log.append(out[-4000:])
            if pr.returncode != 0:
                raise RuntimeError('ibcmd вернул код %s: %s' % (pr.returncode, out[-800:] or 'без вывода'))
            with _LOCK:
                RESTORE[guid].update(state='ok', backup=bak, log='\n'.join(log))
        except Exception as e:                        # noqa: BLE001
            # вернуть старую базу на место, неудачную — в сторону
            try:
                if bdir.is_dir() and any(bdir.iterdir()):
                    bdir.rename('%s.restore-failed-%s' % (bdir, _ts()))
                if bak and Path(bak).is_dir() and not bdir.exists():
                    Path(bak).rename(bdir)
                    log.append('старая база возвращена на место')
            except Exception as e2:                   # noqa: BLE001
                log.append('не удалось вернуть бэкап: %s' % e2)
            with _LOCK:
                RESTORE[guid].update(state='error', error=str(e)[:1500], backup=bak,
                                     log='\n'.join(log))
        finally:
            with _LOCK:
                RESTORE[guid]['finished'] = datetime.datetime.now().isoformat(timespec='seconds')

    threading.Thread(target=work, daemon=True, name='bl-restore').start()
    return {'ok': True, 'started': RESTORE[guid]['started'], 'dt': str(p), 'ibcmd': ibcmd}


def start_check(guid: str, base: dict) -> dict:
    """Проверка базы данных файловой версии — утилита 1С chdbfl.exe (открывается окном)."""
    info = disk_base(base)
    if not info['ok']:
        return {'error': info['why']}
    if _sessions(base):
        return {'error': 'база открыта (%s) — проверка требует закрыть сеансы' % ', '.join(_sessions(base))}
    ch = find_tool(base.get('exe') or '', 'chdbfl.exe')
    if not ch:
        return {'error': 'не найден chdbfl.exe (утилита проверки входит в платформу)'}
    try:
        pr = subprocess.Popen([ch, info['cd']],
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as e:                            # noqa: BLE001
        return {'error': str(e)}
    return {'ok': True, 'pid': pr.pid, 'file': info['cd'], 'tool': ch,
            'note': 'в окне утилиты отметьте «Исправлять обнаруженные ошибки», если нужно исправление'}


def list_dir(path: str, exts=('.dt',), limit: int = 400) -> dict:
    """Содержимое каталога для выбора файла .dt: папки + файлы нужных расширений."""
    p = Path((path or '').strip().strip('"') or 'C:\\')
    if not p.is_dir():
        parent = p.parent if str(p.parent) != str(p) else Path('C:\\')
        p = parent if parent.is_dir() else Path('C:\\')
    dirs, files = [], []
    try:
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith('$') or e.name in ('System Volume Information', 'RECYCLE.BIN'):
                continue
            if e.is_dir():
                dirs.append({'name': e.name, 'path': str(e)})
            elif e.suffix.lower() in exts:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                files.append({'name': e.name, 'path': str(e), 'size': size,
                              'mtime': datetime.datetime.fromtimestamp(
                                  e.stat().st_mtime).strftime('%d.%m.%Y %H:%M')})
    except PermissionError:
        return {'error': 'нет доступа к %s' % p}
    up = str(p.parent) if str(p.parent) != str(p) else ''
    return {'ok': True, 'dir': str(p), 'parent': up,
            'dirs': dirs[:limit], 'files': files[:limit]}
