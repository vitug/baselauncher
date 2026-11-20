# -*- coding: utf-8 -*-
"""MCP-сервер внутри BaseLauncher (JSON-RPC 2.0 поверх POST /mcp).

Часть приложения: отдельного процесса не нужно, инструменты работают с тем же состоянием,
что и UI (ibases.v8i, data/config.json, живые сеансы 1С). Протокол — MCP `2025-06-18`
(совместим и с `2024-11-05`), транспорт — streamable HTTP без SSE: клиент делает POST,
получает application/json.

Для клиентов, которым нужен только stdio, в корне репозитория лежит `mcp_server.py` —
тонкий мост «stdio → http://127.0.0.1:8797/mcp».

Пароли наружу НЕ отдаются: в ответах только флаги `pwd_set`/`pwd_cfg_set`/`storage.pwd_set`.
"""
import re
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

from core import config, importer, launcher, procs, v8i_writer

SERVER_NAME = 'baselauncher'
SERVER_VERSION = '1.0.0'
DEFAULT_PROTOCOL = '2025-06-18'
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')


# ---------------------------------------------------------------- утилиты
def _text(obj) -> dict:
    return {'content': [{'type': 'text',
                         'text': json.dumps(obj, ensure_ascii=False, indent=1)}]}


def _err(msg: str) -> dict:
    return {'content': [{'type': 'text', 'text': str(msg)}], 'isError': True}


def _state() -> dict:
    return importer.build_state()


def _where(b: dict) -> str:
    """Строка «где лежит» для компактного вывода."""
    return b.get('path') or b.get('ws') or (
        f"{b.get('server')}\\{b.get('ref')}" if b.get('server') else b.get('connect') or '')


def _resolve(state: dict, ref: str) -> dict:
    """guid | точное имя | уникальная подстрока имени. Ошибка — ValueError с подсказкой."""
    ref = (ref or '').strip()
    if not ref:
        raise ValueError('укажите базу: guid или имя')
    b = importer.find_base(state, ref)
    if b:
        return b
    low = ref.lower()
    hits = [x for x in state['bases'] if low in (x.get('name') or '').lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f'база не найдена: {ref}')
    names = '; '.join(f"{x['name']} ({x['guid']})" for x in hits[:10])
    raise ValueError(f'под «{ref}» подходит {len(hits)} баз — уточните: {names}')


def _brief(b: dict) -> dict:
    d = {
        'guid': b['guid'], 'name': b['name'], 'folder': b.get('folder') or '/',
        'where': _where(b),
        'kind': 'web' if b.get('ws') else ('file' if b.get('path') else 'server'),
        'family': b.get('family') or '', 'exe': b.get('exe') or '',
        'has_designer': bool(b.get('has_designer')),
        'default_mode': b.get('default_mode') or '',
        'running': b.get('running_modes') or [],
        'fav': bool(b.get('fav')),
        'last_launch': b.get('last_launch') or '', 'launches': b.get('launches') or 0,
    }
    if b.get('path_ok') is False:
        d['missing'] = b.get('path_info') or 'нет папки базы'
    if b.get('exe_ok') is False:
        d['exe_missing'] = True
    return d


def _full(b: dict) -> dict:
    d = _brief(b)
    d.update({
        'connect': b.get('connect') or '',
        'path_ok': b.get('path_ok'), 'path_info': b.get('path_info') or '',
        'exe_ok': b.get('exe_ok'), 'exe_src': b.get('exe_src') or '',
        'thin': bool(b.get('thin')),
        'user': b.get('user') or '',
        'user_cfg': b.get('user_cfg') or '',
        'pwd_set': bool(b.get('pwd')), 'pwd_cfg_set': bool(b.get('pwd_cfg')),
        'pwd_note': b.get('pwd_note') or '',
        'settled_in_config': bool(b.get('our')),
        'extra_keys': b.get('extra_keys') or [],
        'storage': {
            'dir': (b.get('storage') or {}).get('dir') or '',
            'user': (b.get('storage') or {}).get('user') or '',
            'pwd_set': bool((b.get('storage') or {}).get('pwd')),
        },
        'running_pids': b.get('running_pids') or {},
        'running_win': b.get('running_win') or {},
        'launch_day': b.get('launch_day') or '',
        'descr': b.get('descr') or '',
    })
    return d


def _mask(cmd: str) -> str:
    """Не отдавать пароли в командной строке (/P, /ConfigurationRepositoryP)."""
    return re.sub(r'(/(?:P|ConfigurationRepositoryP)\s+)("[^"]*"|\S+)', r'\1"***"', cmd)


# ---------------------------------------------------------------- инструменты
def t_status(_a: dict) -> dict:
    st = _state()
    return _text({
        'server': {'name': SERVER_NAME, 'version': SERVER_VERSION},
        'config_path': st.get('config_path'),
        'bases': len(st['bases']), 'folders': len(st['folders']),
        'platforms': len(st['platforms']),
        'sessions': len(st.get('sessions') or []),
        'defaults': st.get('defaults') or {},
        'generated_at': st.get('generated_at'),
    })


def t_list_bases(a: dict) -> dict:
    st = _state()
    q = (a.get('query') or '').strip().lower()
    folder = (a.get('folder') or '').strip().lower()
    fam = (a.get('family') or '').strip()
    running_only = bool(a.get('running_only'))
    limit = int(a.get('limit') or 100)
    out = []
    for b in st['bases']:
        if fam and (b.get('family') or '') != fam:
            continue
        if folder and not (b.get('folder') or '/').lower().startswith(folder.rstrip('/') or '/'):
            continue
        if running_only and not (b.get('running_modes') or []):
            continue
        if q:
            hay = ' '.join(str(b.get(k) or '') for k in
                           ('name', 'descr', 'path', 'ws', 'server', 'ref', 'folder')).lower()
            if q not in hay:
                continue
        out.append(_brief(b))
    return _text({'count': len(out), 'shown': min(len(out), limit), 'bases': out[:limit]})


def t_get_base(a: dict) -> dict:
    st = _state()
    b = _resolve(st, a.get('base'))
    return _text(_full(b))


def t_add_base(a: dict) -> dict:
    name = (a.get('name') or '').strip()
    folder = (a.get('folder') or '/').strip() or '/'
    path = (a.get('path') or '').strip()
    server = (a.get('server') or '').strip()
    ref = (a.get('ref') or '').strip()
    if not name:
        return _err('укажите имя базы')
    if path:
        connect = 'File="%s"' % path.rstrip('\\/')
    elif server and ref:
        connect = 'Srvr="%s";Ref="%s"' % (server, ref)
    else:
        return _err('нужен path (файловая база) либо server + ref (серверная)')
    try:
        guid = v8i_writer.add_base(name, connect, folder)
    except ValueError as e:
        return _err(e)
    st = _state()
    return _text({'ok': True, 'guid': guid, 'base': _brief(importer.find_base(st, guid) or {}),
                  'note': 'записано в ibases.v8i — 1С увидит базу в своём списке'})


def t_add_folder(a: dict) -> dict:
    name = (a.get('name') or '').strip()
    parent = (a.get('parent') or '/').strip() or '/'
    if not name:
        return _err('укажите имя папки')
    try:
        guid = v8i_writer.add_folder(name, parent)
    except ValueError as e:
        return _err(e)
    st = _state()
    return _text({'ok': True, 'guid': guid, 'folders': len(st['folders']),
                  'path': ('' if parent == '/' else parent) + '/' + name})


def t_base_cache(a: dict) -> dict:
    """Размеры либо очистка кэша базы: what=info|user|program|both."""
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    if not b:
        return _err('база не найдена: %s' % (a.get('base') or ''))
    what = str(a.get('what') or 'info').strip().lower()
    from core import cache as _cache
    if what in ('info', '', 'size'):
        info = _cache.base_info(b['guid'])
        return _text({'base': b['name'], 'guid': b['guid'], 'running': bool(b.get('running')), **info})
    try:
        res = _cache.clear_base(b['guid'], what)
    except ValueError as e:
        return _err(e)
    res['base'] = b['name']
    res['running'] = bool(b.get('running'))
    return _text(res)


def t_move_base(a: dict) -> dict:
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    if not b:
        return _err('база не найдена: %s' % (a.get('base') or ''))
    folder = str(a.get('folder') or '/').strip() or '/'
    try:
        r = v8i_writer.move(b['guid'], folder)
    except ValueError as e:
        return _err(e)
    importer._BASES_CACHE.clear()
    st = _state()
    path = [f for f in st['folders']
            if (('' if (f.get('folder') or '/') == '/' else f['folder']) + '/' + f['name']) == folder]
    return _text({'ok': True, 'name': r['name'], 'from': r['from'], 'to': r['to'],
                  'guid': b['guid'], 'folder_guid': (path[0]['guid'] if path else None),
                  'bases': len(st['bases'])})


def t_delete_base(a: dict) -> dict:
    st = _state()
    ref = (a.get('base') or '').strip()
    try:
        b = _resolve(st, ref)
    except ValueError:
        # не база — возможно, папка дерева (у неё тоже есть guid-секция в ibases.v8i)
        f = _find_folder(st, ref)
        if not f:
            raise
        b = f
    try:
        name = v8i_writer.delete(b['guid'])
    except ValueError as e:
        return _err(e)
    return _text({'ok': True, 'deleted': name, 'guid': b['guid'],
                  'kind': b.get('kind') or 'base'})


def _find_folder(state: dict, ref: str):
    """Папка дерева по полному пути, имени или уникальной подстроке пути."""
    ref = (ref or '').strip().strip('/')
    if not ref:
        return None
    low = ref.lower()
    rows = []
    for f in state.get('folders') or []:
        parent = f.get('folder') or '/'
        p = ('' if parent == '/' else parent) + '/' + f['name']
        rows.append((p, f))
    exact = [f for p, f in rows if p.lower() == '/' + low or f['name'].lower() == low]
    if len(exact) == 1:
        return _folder_dict(exact[0])
    hits = [_folder_dict(f) for p, f in rows if low in p.lower()]
    return hits[0] if len(hits) == 1 else None


def _folder_dict(f: dict) -> dict:
    parent = f.get('folder') or '/'
    return {'guid': f['guid'], 'name': f['name'],
            'path': ('' if parent == '/' else parent) + '/' + f['name'], 'kind': 'folder'}


def _build_cmd(b: dict, a: dict):
    """Собрать команду. Логика повторяет REST-слой (exe по режиму, пароль из конфига)."""
    mode = a.get('mode') or 'enterprise'
    extra = a.get('extra_keys') or []
    if a.get('platform') and not a.get('exe'):      # разовый запуск конкретной версией платформы
        a = dict(a)
        a['exe'] = _platform_exe(_state(), a['platform'], mode)
        if not a['exe']:
            raise ValueError('платформа %r не найдена' % a['platform'])
    storage = a.get('storage') if a.get('storage') is not None else (b.get('storage') or {})
    user = a.get('user') or b.get('user') or ''
    pwd = (a.get('pwd') or '').strip()
    if not pwd:
        pwd = (b.get('pwd_cfg') or b.get('pwd') or '') if mode == 'designer' \
            else (b.get('pwd') or b.get('pwd_cfg') or '')
    if mode == 'designer' and b.get('has_designer') is False:
        raise ValueError('веб-база: конфигуратора у сайта нет' if b.get('ws')
                         else 'у платформы только тонкий клиент — конфигуратора нет')
    exe = a.get('exe') or b.get('exe') or ''
    if b.get('ws') and not a.get('exe'):
        if mode == 'enterprise' and exe and not exe.lower().endswith('1cv8c.exe'):
            alt = Path(exe).with_name('1cv8c.exe')
            if alt.is_file():
                exe = str(alt)
        elif mode == 'designer' and exe and exe.lower().endswith('1cv8c.exe'):
            alt = Path(exe).with_name('1cv8.exe')
            if alt.is_file():
                exe = str(alt)
    if not exe or not Path(exe).is_file():
        raise ValueError(f'exe платформы не найден: {exe or "(не определён)"}')
    p = (b.get('path') or '').strip()
    if p and not Path(p).is_dir():
        raise ValueError(f'папки базы нет на диске: {p}')
    return launcher.build_cmd(exe, mode, b['connect'], extra, storage, user, pwd), mode


def _check_execute(extra) -> str | None:
    for k in extra or []:
        s = str(k).strip()
        if not s.lower().startswith('/execute'):
            continue
        p = s[len('/execute'):].strip().strip('"')
        if not p:
            return 'пустой путь в /Execute'
        if not Path(p).is_file():
            return f'файл обработки не найден: {p}'
        if Path(p).suffix.lower() not in ('.epf', '.erf'):
            return f'/Execute ждёт .epf или .erf, а не {Path(p).suffix or "(без расширения)"}'
    return None


def _slice_log(path: Path, limit: int = 6000) -> str:
    """Прочитать /Out-файл 1С: UTF-8 с BOM, иначе cp1251. Длинный — хвост."""
    if not path.is_file():
        return ''
    raw = path.read_bytes()
    for enc in ('utf-8-sig', 'utf-16', 'cp1251'):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        txt = raw.decode('utf-8', 'replace')
    txt = txt.replace('\r\n', '\n').strip()
    return txt if len(txt) <= limit else '…\n' + txt[-limit:]


def _journal_path(guid: str) -> Path:
    from core.paths import DATA_DIR
    return Path(DATA_DIR) / 'manifests' / (guid + '.changes.json')


def _journal_read(guid: str) -> dict:
    try:
        j = json.loads(_journal_path(guid).read_text(encoding='utf-8'))
        if isinstance(j, dict):
            j.setdefault('entries', [])
            return j
    except (OSError, ValueError):
        pass
    return {'entries': []}


def _journal_add(guid: str, entry: dict) -> None:
    """Дописать запись журнала загрузок базы (рядом с манифестом, <guid>.changes.json)."""
    j = _journal_read(guid)
    j['entries'].append(entry)
    j['entries'] = j['entries'][-200:]            # журнал не растёт бесконечно
    p = _journal_path(guid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
    except OSError:
        pass                                     # журнал — не критично для команды


def _repo_version_from_log(log):
    """Ревизия хранилища из /Out-лога («Версия 5», «версии №12», «Версией 3») → int|null."""
    m = re.findall(r'[Вв]ерси\w*\s*№?\s*(\d+)', log or '')
    return int(m[-1]) if m else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return ''


def _git(dirpath, *args, timeout: float = 30.0):
    """git -C <dir> … → (exit, вывод stdout+stderr)."""
    import subprocess
    try:
        r = subprocess.run(['git', '-C', str(dirpath), *args],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def _git_status_names(dirpath) -> list:
    """Изменённые/новые/удалённые файлы рабочей копии (git status --porcelain).

    -uall — новые каталоги разворачиваются в файлы (иначе схлопываются в 'dir/');
    core.quotepath=false — кириллица в путях без октет-экранирования.
    """
    code, out = _git(dirpath, '-c', 'core.quotepath=false', 'status', '--porcelain', '-uall')
    if code != 0:
        return []
    names = []
    for line in out.splitlines():
        line = line.rstrip('\r')
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] and not parts[0].startswith('##'):
            p = parts[1].strip('"')
            if ' -> ' in p:
                p = p.split(' -> ')[-1]
            names.append(p)
    return names


def _objects_xml(objs: list) -> str:
    """XML для -objects хранилища: <Objects><Object fullName="…"/>…</Objects>.

    «Конфигурация» — особый случай (тег <Configuration/>, без fullName). Имена —
    отсортированный уникальный список: платформа не принимает плоский -list
    («Ошибка разбора XML»), а дубли в XML ей тоже ни к чему.
    """
    lines = ['<Objects xmlns="http://v8.1c.ru/8.3/config/objects" version="1.0">']
    for n in sorted(set(objs)):
        if n.lower() in ('конфигурация', 'configuration'):
            lines.append('    <Configuration includeChildObjects="false"/>')
        else:
            lines.append(f'    <Object fullName="{n}" includeChildObjects="true"/>')
    lines.append('</Objects>')
    return '\n'.join(lines) + '\n'


def t_git_baseline(a: dict) -> dict:
    """Сделать каталог XML-выгрузки git-репозиторием: ветка xml-state, коммит baseline.

    Дальше контроль изменений — git status/diff: фантомные правки платформы (макеты/формы
    при загрузке поддерева) видны до помещения в хранилище.
    """
    st = _state()
    b = _resolve(st, a.get('base'))
    root = (a.get('root') or '').strip().strip('"')
    if not root:
        root = str(((b.get('our') or {}).get('dump_dir') or '')).strip()
    if not root:
        jr = _journal_read(b['guid'])
        if jr['entries']:
            root = str(jr['entries'][-1].get('src') or '')
    if not root:
        return _err('укажите root (каталог XML-выгрузки) или задайте базе dump_dir')
    src = Path(root)
    if not src.is_dir():
        return _err('каталога нет: %s' % src)
    if (src / '.git').is_dir():
        code, out = _git(src, 'log', '--oneline', '-1')
        code2, stat = _git(src, 'status', '--porcelain')
        dirty = [l for l in stat.splitlines() if l.strip()]
        return _text({'base': b['name'], 'src': str(src), 'already_repo': True,
                      'head': out, 'dirty_count': len(dirty)})
    code, out = _git(src, 'init', '-b', 'xml-state')
    if code != 0:
        return _err('git init: %s' % out)
    _git(src, 'add', '-A')
    msg = 'baseline %s — выгрузка XML базы «%s»' % (time.strftime('%Y-%m-%d %H:%M'), b['name'])
    code, out = _git(src, '-c', 'user.name=BaseLauncher', '-c', 'user.email=baselauncher@local',
                      'commit', '-m', msg)
    if code != 0 and 'nothing to commit' not in out:
        return _err('git commit: %s' % out)
    code, head = _git(src, 'log', '--oneline', '-1')
    return _text({'base': b['name'], 'src': str(src), 'ok': True, 'branch': 'xml-state',
                  'head': head, 'message': msg})


def t_run_batch(a: dict) -> dict:
    """Пакетная команда Конфигуратора: запустить, дождаться выхода, вернуть код и /Out.

    Для DumpCfg / LoadConfigFromFiles / ConfigurationRepository* / UpdateDBCfg — того, что
    в интерактивном запуске не дождаться: launcher.launch() возвращает PID и отпускает окно,
    а здесь процесс ждётся и его результат (exit code + сообщения 1С) отдаётся вызывающему.
    """
    import subprocess
    st = _state()
    b = _resolve(st, a.get('base'))
    keys = [str(k) for k in (a.get('extra_keys') or []) if str(k).strip()]
    if not keys:
        return _err('extra_keys пуст: укажите ключи Конфигуратора, например '
                    '["/DumpCfgD:\\\\temp\\\\base.cf"] или '
                    '["/ConfigurationRepositoryUpdateCfg"]')
    # --- объекты хранилища: список полных имён (Catalog.X, ОбщийМодуль.X) ---
    # сервер сам собирает XML <Objects ...><Object fullName="..."/></Objects> (плоский список
    # платформа не принимает: "Ошибка разбора XML: Document is empty") и дописывает
    # -objects <файл> СРАЗУ за последним ключом extra_keys (обычно /ConfigurationRepositoryLock
    # или /ConfigurationRepositoryUnLock) — между командой и -objects не должно быть других ключей.
    # XML кладём в папку дампа базы (dump_dir), иначе во временный каталог.
    objs = [str(o).strip() for o in (a.get('objects') or []) if str(o).strip()]
    obj_file = (a.get('objects_file') or '').strip()
    if objs and not obj_file:
        dd = str(((b.get('our') or {}).get('dump_dir') or '')).strip()
        try:
            base_dir = Path(dd) if dd else Path(tempfile.gettempdir())
            base_dir.mkdir(parents=True, exist_ok=True)
            obj_file = str(base_dir / 'repository_objects.xml')
            lines = ['<Objects xmlns="http://v8.1c.ru/8.3/config/objects" version="1.0">']
            for n in sorted(set(objs)):
                if n.lower() in ('конфигурация', 'configuration'):
                    lines.append('    <Configuration includeChildObjects="false"/>')
                else:
                    lines.append(f'    <Object fullName="{n}" includeChildObjects="true"/>')
            lines.append('</Objects>')
            Path(obj_file).write_text('\n'.join(lines) + '\n', encoding='utf-8')
        except OSError:
            obj_file = ''
    if obj_file:
        keys.append(f'-objects "{obj_file}"')
    # --- частичная загрузка: files — rel-пути или имена объектов («Справочник.X») ---
    # разворачиваются в файлы (+поддерево) от root (по умолчанию dump_dir/git_dir базы) и
    # дописываются -files "p1,p2" СРАЗУ за последним ключом extra_keys (обычно
    # /LoadConfigFromFiles<dir>); для частичной загрузки обязателен -Format — добавляем
    # Hierarchical, если не передан.
    fls = [str(x).strip() for x in (a.get('files') or []) if str(x).strip()]
    if fls:
        from core import loadxml
        import re as _re
        # root: аргумент → каталог из ключа /LoadConfigFromFiles<dir> → dump_dir → git_dir
        froot = (a.get('root') or '').strip().strip('"')
        if not froot:
            for k in keys:
                m = _re.match(r'/LoadConfigFromFiles"?([^"]+?)"?\s*$', str(k))
                if m:
                    froot = m.group(1)
                    break
        if not froot:
            froot = str(((b.get('our') or {}).get('dump_dir') or '')).strip() \
                or str(((b.get('our') or {}).get('git_dir') or '')).strip()
        if not froot:
            return _err('для files укажите root (каталог XML-исходников) или задайте у базы '
                        'dump_dir/git_dir')
        fsrc = Path(froot)
        sub = fsrc / loadxml._safe_name(b['name'])
        fsrc = sub if sub.is_dir() else fsrc
        if not fsrc.is_dir():
            return _err('каталога нет: %s' % fsrc)
        exp = []
        for x in fls:
            if (fsrc / x).is_file():
                exp.append(x.replace('\\', '/'))
        if len(exp) < len(fls):
            exp += [p for p in loadxml.expand_objects(fsrc, fls) if p not in exp]
        if not exp:
            return _err('ни один из files не найден в %s (проверьте имена/типы объектов)' % fsrc)
        keys.append('-files "%s"' % ','.join(exp))
        if not any(k.lower().startswith('-format') for k in keys):
            keys.append('-Format')
            keys.append('Hierarchical')
        loaded_src, loaded_files = fsrc, list(exp)   # для журнала загрузок
        # --- верификация перед загрузкой (git-режим): в рабочей копии не должно быть
        #     изменений ВНЕ разрешённого списка -files — фантомные правки платформы
        #     (макеты/формы при загрузке поддерева) ловятся ДО помещения в хранилище ---
        if (fsrc / '.git').is_dir() and not a.get('allow_extra_files'):
            extras = sorted(set(_git_status_names(fsrc)) - set(exp))
            if extras:
                return _err('СТОП: в git-рабочей копии %s изменены файлы вне списка -files '
                            '(фантомные правки?): %s. Уберите лишние правки, расширьте files '
                            'или передайте allow_extra_files=true' % (fsrc, extras[:20]))
    else:
        loaded_src, loaded_files = None, []
    out_path = (a.get('out') or '').strip()
    if not out_path:
        # папка дампа базы (v0.38.4): туда по умолчанию пишем /Out, чтобы сообщения 1С не терялись
        dd = str(((b.get('our') or {}).get('dump_dir') or '')).strip()
        if dd:
            try:
                Path(dd).mkdir(parents=True, exist_ok=True)
                out_path = str(Path(dd) / 'run_batch_out.txt')
            except OSError:
                out_path = ''
    if out_path:
        keys.append(f'/Out"{out_path}"')
    if a.get('disable_dialogs', True) and not any('/disablestartupdialogs' in k.lower() for k in keys):
        keys.append('/DisableStartupDialogs')
    pids = (b.get('running_pids') or {}).get('designer')
    if pids and not a.get('allow_running'):
        return _text({'ok': False, 'already_running': True, 'pid': pids, 'mode': 'designer',
                      'message': f'база открыта в Конфигураторе (PID {pids}); монопольные операции '
                                 f'упадут. Закройте сеанс или передайте allow_running=true'})
    timeout = max(30.0, min(float(a.get('timeout') or 1800), 7200.0))
    cmd, mode = _build_cmd(b, {'mode': 'designer', 'extra_keys': keys})
    t0 = time.time()
    try:
        p = subprocess.run(cmd, timeout=timeout, capture_output=True)
        code = p.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        code, timed_out = -1, True
    log_txt = _slice_log(Path(out_path)) if out_path else ''
    # --- журнал загрузок: успешная загрузка через /LoadConfigFromFiles — что именно легло в базу ---
    if code == 0 and not timed_out and loaded_files and loaded_src:
        vers = _repo_version_from_log(log_txt)
        _journal_add(b['guid'], {
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'base': b['name'],
            'cmd': _mask(cmd)[:400],
            'src': str(loaded_src),
            'files': {p: _sha256(loaded_src / p) for p in loaded_files[:500]},
            'repo_version': vers,
            'exit': code, 'seconds': int(time.time() - t0),
        })
    return _text({'ok': code == 0 and not timed_out, 'exit': code, 'timed_out': timed_out,
                  'seconds': int(time.time() - t0), 'base': b['name'], 'mode': mode,
                  'out_file': out_path, 'log': log_txt, 'cmd': _mask(cmd)})


def _git_section(src: Path, a: dict) -> dict:
    """Git-режим dump_changes: если выгрузка — git-репозиторий, контроль через git.

    status --porcelain → список, diff --stat → сводка по строкам;
    diff_file → полный diff одного файла (до 8000 символов).
    """
    if not (src / '.git').is_dir():
        return {}
    names = _git_status_names(src)
    code, stat = _git(src, 'diff', '--stat')
    out = {'repo': True, 'changed_count': len(names), 'changed': names[:int(a.get('limit') or 200)]}
    if stat:
        out['diff_stat'] = stat[-3000:]
    f = (a.get('diff_file') or '').strip()
    if f:
        code, d = _git(src, 'diff', '--', f)
        out['diff_file'] = f
        out['diff'] = d[:8000] if d else '(нет отличий от baseline/HEAD)'
    return out


def t_dump_changes(a: dict) -> dict:
    """Изменённые файлы XML-выгрузки базы с прошлой выгрузки (по манифесту)."""
    from core import loadxml
    from core.paths import DATA_DIR
    st = _state()
    b = _resolve(st, a.get('base'))
    man = loadxml.read_manifest(b['guid'], str(DATA_DIR))
    root = (a.get('root') or '').strip().strip('"')
    if not root:
        root = str((man.get('root') or '')).strip() or str(((b.get('our') or {}).get('git_dir') or '')).strip()
    if not root:
        return _err('не указан root и не найдено ни манифеста выгрузки, ни git_dir базы')
    src = Path(root)
    sub = src / loadxml._safe_name(b['name'])
    src = sub if sub.is_dir() else src
    if not src.is_dir():
        return _err('каталога нет: %s' % src)
    if not man or not man.get('files'):
        jrn0 = _journal_read(b['guid'])
        if not jrn0['entries']:
            return _err('нет манифеста прошлой выгрузки базы — сначала выгрузите XML '
                        '(инструмент dumpxml / «Выгрузить конфигурации в XML…»)')
    cur = {}
    for f in src.rglob('*'):
        if f.is_file():
            try:
                stt = f.stat()
            except OSError:
                continue
            cur[f.relative_to(src).as_posix()] = (stt.st_mtime, stt.st_size)
    old = man.get('files') or {}
    if old:
        changed = sorted(p for p, v in cur.items() if p not in old or (old[p][0], old[p][1]) != v)
        deleted = sorted(p for p in old if p not in cur)
    else:
        changed, deleted = [], []                # манифеста нет — отвечаем только из журнала
    limit = int(a.get('limit') or 200)
    # --- журнал загрузок run_batch: что грузилось в базу и что в исходниках поменялось с той поры ---
    jrn = _journal_read(b['guid'])
    jout = {}
    if jrn['entries']:
        e = jrn['entries'][-1]
        jsrc = Path(e.get('src') or '')
        jfiles = e.get('files') or {}
        cur_h = {}
        for p in jfiles:
            f = jsrc / p
            cur_h[p] = _sha256(f) if f.is_file() else ''
        differs = sorted(p for p in jfiles if jfiles[p] and cur_h[p] != jfiles[p])
        vanished = sorted(p for p in jfiles if not cur_h[p])
        jout = {'last_load_ts': e.get('ts'), 'last_load_files': len(jfiles),
                'repo_version': e.get('repo_version'),
                'src': e.get('src'),
                'changed_since_load': differs[:limit], 'changed_since_load_count': len(differs),
                'vanished_since_load': vanished[:limit], 'vanished_since_load_count': len(vanished),
                'entries_total': len(jrn['entries'])}
    return _text({'base': b['name'], 'src': str(src), 'manifest_ts': man.get('ts'),
                  'changed_count': len(changed), 'deleted_count': len(deleted),
                  'changed': changed[:limit], 'deleted': deleted[:limit],
                  'journal': jout,
                  'git': _git_section(src, a),
                  'note': ('показаны первые %d' % limit) if len(changed) > limit else ''})


def t_dryrun(a: dict) -> dict:
    st = _state()
    b = _resolve(st, a.get('base'))
    err = _check_execute(a.get('extra_keys'))
    if err:
        return _err(err)
    cmd, mode = _build_cmd(b, a)
    return _text({'base': b['name'], 'mode': mode, 'cmd': _mask(cmd)})


def _wait_focus(b: dict, mode: str) -> None:
    def match(s):
        from app.main import _session_matches
        return _session_matches(s, b)
    procs.wait_and_focus(match, mode, timeout=120, settle=3)


def t_launch(a: dict) -> dict:
    st = _state()
    b = _resolve(st, a.get('base'))
    err = _check_execute(a.get('extra_keys'))
    if err:
        return _err(err)
    cmd, mode = _build_cmd(b, a)
    pids = (b.get('running_pids') or {}).get(mode)
    if pids and not a.get('force'):
        return _text({'ok': False, 'already_running': True, 'pid': pids, 'mode': mode,
                      'message': f'база уже открыта в этом режиме (PID {pids}). '
                                 f'Передайте force=true, чтобы открыть ещё один сеанс, '
                                 f'или mode="{"designer" if mode == "enterprise" else "enterprise"}"'})
    pid = launcher.launch(cmd)
    launcher.log_launch(b['name'], mode, pid, cmd, b['guid'])
    threading.Thread(target=_wait_focus, args=(b, mode), daemon=True).start()
    return _text({'ok': True, 'pid': pid, 'mode': mode, 'base': b['name'],
                  'cmd': _mask(cmd), 'focus': 'queued'})


def t_run_processing(a: dict) -> dict:
    """Запуск базы сразу с внешней обработкой (/Execute [+ /C])."""
    proc_path = (a.get('processing') or '').strip().strip('"')
    if not proc_path:
        return _err('укажите processing — путь к .epf/.erf')
    extra = [f'/Execute "{proc_path}"']
    if (a.get('params') or '').strip():
        extra.append('/C ' + a['params'].strip())
    body = dict(a)
    body['extra_keys'] = extra + list(a.get('extra_keys') or [])
    out = t_launch(body)
    if not out.get('isError'):
        obj = json.loads(out['content'][0]['text'])
        obj['processing'] = proc_path
        obj['params'] = a.get('params') or ''
        return _text(obj)
    return out


def t_sessions(_a: dict) -> dict:
    st = _state()
    return _text({'count': len(st.get('sessions') or []),
                  'sessions': st.get('sessions') or []})


def t_activate(a: dict) -> dict:
    pid = a.get('pid')
    if not pid:
        st = _state()
        b = _resolve(st, a.get('base'))
        pids = b.get('running_pids') or {}
        mode = a.get('mode')
        if mode:
            pid = pids.get(mode)
        if not pid and pids:
            pid = next(iter(pids.values()))
        if not pid:
            return _err(f"«{b['name']}» не открыта — сначала launch")
    sess = [s for s in (_state().get('sessions') or []) if s['pid'] == int(pid)]
    if sess and not sess[0].get('window'):
        return _text({'ok': False, 'no_window': True, 'pid': int(pid),
                      'message': 'у сеанса нет видимого окна (например, конфигуратор из EDT/агент)'})
    res = procs.focus_window(int(pid))
    return _text({'ok': bool(res.get('ok', res.get('foreground', True))) if isinstance(res, dict) else True,
                  'pid': int(pid), 'result': res})


def _platform_exe(st: dict, spec: str, mode: str = 'enterprise') -> str:
    """Найти exe платформы по описанию: полный путь, версия (8.3.25.1336 / 8.3.25) или семейство."""
    spec = (spec or '').strip()
    if not spec:
        return ''
    if (':' in spec or spec.startswith('\\\\')) and Path(spec).is_file():
        return spec
    plats = st.get('platforms') or []
    want_designer = (mode or '').lower() in ('designer', 'config', 'configurator')
    cand = []
    for p in plats:
        if p.get('key') == spec or (p.get('key') or '').startswith(spec.rstrip('.') ):
            cand.append(p)
        elif spec in ('8.3', '8.5') and p.get('family') == spec:
            cand.append(p)
    for p in cand:                       # сначала те, где есть толстый клиент (конфигуратор)
        exe = p.get('exe') or ('' if want_designer else p.get('exe_thin')) or ''
        if exe and Path(exe).is_file():
            return exe
    return ''


def t_set_platform(a: dict) -> dict:
    """Закрепить платформу за базой (или вернуть «авто»)."""
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    spec = str(a.get('platform') or a.get('version') or a.get('exe') or '').strip()
    if bool(a.get('auto')) or spec.lower() in ('', 'auto', 'авто'):
        config.set_base(b['guid'], {'exe': '', 'family': '', 'learnt': ''})
        st = _state()
        b = importer.find_base(st, b['guid']) or b
        return _text({'ok': True, 'base': b['name'], 'exe': b.get('exe') or '',
                      'exe_src': b.get('exe_src'), 'note': 'платформа снова подбирается автоматически'})
    mode = a.get('mode') or b.get('mode') or 'enterprise'
    exe = _platform_exe(st, spec, mode)
    if not exe:
        have = sorted({p.get('key') or '' for p in (st.get('platforms') or [])})
        return _err('платформа %r не найдена. Установлены: %s' % (spec, ', '.join(have)))
    config.set_base(b['guid'], {'exe': exe, 'family': ''})
    st = _state()
    b = importer.find_base(st, b['guid']) or b
    return _text({'ok': True, 'base': b['name'], 'exe': exe, 'exe_src': 'our',
                  'has_designer': bool(b.get('has_designer')),
                  'note': 'закреплено за базой: дальше запускается этой платформой'})


def t_maintenance(a: dict) -> dict:
    """Блокировка базы на обслуживание: запрет запуска из стартера + завершение сеансов."""
    from core import baselock
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    if a.get('off'):
        res = baselock.unlock(b['guid'])
    else:
        res = baselock.lock(b['guid'], float(a.get('minutes') or baselock.DEFAULT_TTL_MIN),
                            str(a.get('note') or ''), base=b,
                            kick_sessions=bool(a.get('kick_sessions')))
    res['maintenance'] = baselock.status(b)
    res['sessions_left'] = len(baselock.sessions_of(b))
    res['base'] = b['name']
    return _text(res)


def t_detect_platform(a: dict) -> dict:
    """Версия сервера 1С через COM + закрепление её платформы за базой."""
    from app.main import detect_platform
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    res = detect_platform(b, pin=bool(a.get('pin', True)))
    if res.get('server_version'):
        res['host'] = b.get('server') or b.get('where') or ''
    return _text({k: v for k, v in res.items() if k != 'base'} | {'base': b['name']})


def t_cominfo(a: dict) -> dict:
    """Сведения из базы по COM: конфигурация, режим совместимости, пользователи ИБ."""
    from core import com
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    user = str(a.get('user') or b.get('user') or '').strip()
    pwd = a.get('pwd')
    if pwd is None:
        pwd = b.get('pwd') or ''
    res = com.probe_async(b.get('connect') or '', user, str(pwd), b.get('exe') or '',
                          float(a.get('timeout') or 90))
    if a.get('save') and (a.get('user') is not None or a.get('pwd') is not None):
        patch = {}
        if a.get('user') is not None:
            patch['user'] = user
        if a.get('pwd') is not None:
            patch['pwd'] = str(a.get('pwd') or '')
        config.set_base(b['guid'], patch)
        res['saved'] = True
    res['base'] = b['name']
    return _text(res)


def t_set_storage(a: dict) -> dict:
    """Задать/снять хранилище конфигурации у базы (ключи конфигуратора)."""
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    d = str(a.get('dir') or a.get('storage') or '').strip()
    cfg = config.load()
    cur = (config.get_base(cfg, b['guid']).get('storage') or {})
    if not d or bool(a.get('clear')):
        config.set_base(b['guid'], {'storage': {}})
        st = _state()
        b = importer.find_base(st, b['guid']) or b
        return _text({'ok': True, 'base': b['name'], 'storage': None,
                      'note': 'хранилище снято'})
    user = a['user'] if 'user' in a else cur.get('user', '')
    if 'pwd' in a or 'password' in a:
        pwd = str(a.get('pwd') or a.get('password') or '').strip()
    else:                                   # не передали — сохранить прежний пароль
        pwd = config.unseal(cur.get('pwd') or '') if cur.get('pwd') else ''
    config.set_base(b['guid'], {'storage': {'dir': d, 'user': user, 'pwd': pwd}})
    st = _state()
    b = importer.find_base(st, b['guid']) or b
    s = b.get('storage') or {}
    return _text({'ok': True, 'base': b['name'],
                  'storage': {'dir': s.get('dir'), 'user': s.get('user'),
                              'pwd_set': bool(s.get('pwd'))},
                  'note': 'подставляется в конфигуратор: /ConfigurationRepositoryF/-N/-P'})


def t_set_credentials(a: dict) -> dict:
    """Задать пользователя/пароль базы (для входа и для COM-подключения)."""
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    patch = {}
    if 'user' in a:
        patch['user'] = str(a.get('user') or '').strip()
    if 'user_cfg' in a:
        patch['user_cfg'] = str(a.get('user_cfg') or '').strip()
    for k, fld in (('pwd', 'pwd'), ('password', 'pwd'), ('pwd_cfg', 'pwd_cfg')):
        if k in a and fld not in patch:
            patch[fld] = str(a.get(k) or '')
    if not patch:
        return _err('передайте user и/или pwd (пустая строка — стереть)')
    config.set_base(b['guid'], patch)
    st = _state()
    b = importer.find_base(st, b['guid']) or b
    return _text({'ok': True, 'base': b['name'], 'user': b.get('user') or '',
                  'user_cfg': b.get('user_cfg') or '',
                  'pwd_set': bool(b.get('pwd_set')), 'pwd_cfg_set': bool(b.get('pwd_cfg_set'))})


def t_base_users(a: dict) -> dict:
    """Список пользователей ИБ через COM — чтобы выбрать логин для базы."""
    from core import com
    st = _state()
    b = _resolve(st, str(a.get('base') or ''))
    user = str(a.get('user') or b.get('user') or '').strip()
    pwd = a.get('pwd')
    if pwd is None:
        pwd = b.get('pwd') or ''
    res = com.probe_async(b.get('connect') or '', user, str(pwd), b.get('exe') or '',
                          float(a.get('timeout') or 90), want_users=True)
    names = [str(r.get('Имя') or '').strip() for r in (res.get('users') or [])]
    return _text({'ok': bool(res.get('ok')), 'base': b['name'], 'users': [n for n in names if n],
                  'users_count': res.get('users_count'), 'platform': res.get('com_platform') or '',
                  'users_table': res.get('users_table') or '',
                  'users_source': res.get('users_source') or '',
                  'error': res.get('error') or '', 'hint': res.get('hint') or '',
                  'users_error': res.get('users_error') or '',
                  'users_ib_error': res.get('users_ib_error') or '',
                  'note': ('' if names else
                           'в базе нет зарегистрированных пользователей ИБ (это не ошибка подключения)')})


def t_platforms(_a: dict) -> dict:
    st = _state()
    fams = (st.get('defaults') or {}).get('families') or {}
    out = []
    for p in st['platforms']:
        exe = p.get('exe') or ''
        designer = bool(exe) and Path(exe).is_file()
        out.append({'version': p.get('key'), 'family': p.get('family'),
                    'bitness': p.get('bitness'), 'exe': exe,
                    'exe_thin': p.get('exe_thin') or '',
                    'has_designer': designer,
                    'default_for_family': fams.get(p.get('family') or '') == exe,
                    'source': p.get('source') or ''})
    return _text({'count': len(out), 'defaults': fams, 'platforms': out})


def t_storages(_a: dict) -> dict:
    st = _state()
    out = []
    for b in st['bases']:
        s = b.get('storage') or {}
        if not s.get('dir'):
            continue
        out.append({'guid': b['guid'], 'name': b['name'], 'folder': b.get('folder') or '/',
                    'dir': s['dir'], 'user': s.get('user') or '', 'pwd_set': bool(s.get('pwd'))})
    return _text({'count': len(out), 'storages': out})


def t_recent(a: dict) -> dict:
    st = _state()
    lim = int(a.get('limit') or 15)
    return _text({'count': len(st.get('recent') or []),
                  'recent': (st.get('recent') or [])[:lim]})


def t_open_web(a: dict) -> dict:
    st = _state()
    b = _resolve(st, a.get('base'))
    if not b.get('ws'):
        return _err(f"«{b['name']}» — не веб-база (нет ws=)")
    import os
    os.startfile(b['ws'])                                           # noqa: S606
    return _text({'ok': True, 'base': b['name'], 'url': b['ws']})


TOOLS = [
    ('launcher_status', 'Состояние BaseLauncher: версия, путь конфига, сколько баз/папок/платформ, '
     'число открытых сеансов, платформы по умолчанию.', {}, t_status),
    ('list_bases', 'Список баз 1С: guid, имя, папка, путь/URL, семейство, режим по умолчанию, '
     'открыта ли. Фильтры: query (подстрока по имени/пути/описанию), folder, family, running_only.',
     {'query': {'type': 'string'}, 'folder': {'type': 'string'},
      'family': {'type': 'string', 'description': '8.3 / 8.5'},
      'running_only': {'type': 'boolean'},
      'limit': {'type': 'integer', 'description': 'по умолчанию 100'}}, t_list_bases),
    ('get_base', 'Подробно о базе: где лежит, exe платформы, есть ли конфигуратор, хранилище '
     'конфигурации (каталог/пользователь/есть ли пароль), флаги паролей ИБ (сами пароли не отдаются), '
     'открытые режимы и PID, история запусков.',
     {'base': {'type': 'string', 'description': 'guid или имя (можно часть имени)'}},
     t_get_base),
    ('add_base', 'Добавить базу в список 1С (ibases.v8i). Файловая — path, серверная — server+ref; '
     'folder — куда в дереве (по умолчанию корень).',
     {'name': {'type': 'string'}, 'path': {'type': 'string'}, 'server': {'type': 'string'},
      'ref': {'type': 'string'}, 'folder': {'type': 'string'}}, t_add_base),
    ('add_folder', 'Создать папку (в т.ч. подпапку) в дереве ibases.v8i.',
     {'name': {'type': 'string'}, 'parent': {'type': 'string', 'description': 'по умолчанию /'}},
     t_add_folder),
    ('delete_base', 'Удалить базу или пустую папку из ibases.v8i (непустую папку 1С не даст удалить).',
     {'base': {'type': 'string'}}, t_delete_base),
    ('dryrun', 'Показать команду запуска, ничего не запуская (пароли в выводе скрыты).',
     {'base': {'type': 'string'}, 'mode': {'type': 'string', 'enum': ['enterprise', 'designer']},
      'platform': {'type': 'string', 'description': 'разово: версия/семейство/путь к exe'},
      'extra_keys': {'type': 'array', 'items': {'type': 'string'}}}, t_dryrun),
    ('launch_base', 'Запустить базу 1С (по умолчанию Предприятие). Если этот режим уже открыт — '
     'вернёт already_running с PID; force=true откроет второй сеанс. Через ~3 с окно базы '
     'выводится на передний план.',
     {'base': {'type': 'string'},
      'mode': {'type': 'string', 'enum': ['enterprise', 'designer']},
      'force': {'type': 'boolean'},
      'platform': {'type': 'string', 'description': 'разово запустить этой платформой (версия или путь)'},
      'extra_keys': {'type': 'array', 'items': {'type': 'string'},
                     'description': 'разовые ключи: /Debug, /C ..., /Execute "..."'},
      'user': {'type': 'string'}, 'pwd': {'type': 'string'}}, t_launch),
    ('run_processing', 'Открыть базу сразу с внешней обработкой/отчётом: /Execute "файл.epf|erf" '
     'и при необходимости /C "параметры".',
     {'base': {'type': 'string'}, 'processing': {'type': 'string'},
      'params': {'type': 'string'},
      'mode': {'type': 'string', 'enum': ['enterprise', 'designer']},
      'force': {'type': 'boolean'},
      'user': {'type': 'string'}, 'pwd': {'type': 'string'}}, t_run_processing),
    ('run_batch', 'Пакетная команда Конфигуратора: запустить и ДОЖДАТЬСЯ выхода, вернув exit code '
     'и текст /Out. Для DumpCfg, DumpConfigToFiles, LoadConfigFromFiles, UpdateDBCfg, '
     '/ConfigurationRepository* (захват/помещение/обновление), /CheckConfig. Ключи 1С — '
     'extra_keys (например ["/DumpCfgD:\\\\temp\\\\base.cf"]); out — путь для /Out, его содержимое '
     'вернётся в log. Хранилище: objects — список полных имён объектов '
     '(Catalog.ВидыДокументовЭДО, CommonModule.X) — сервер сам соберёт XML и допишет '
     '-objects к последнему ключу: extra_keys=["/ConfigurationRepositoryLock"] — захват, '
     '["/ConfigurationRepositoryUnLock","-force"] — освобождение; objects_file — готовый XML. '
     'Если база открыта в Конфигураторе, вернёт already_running (allow_running=true '
     '— запускать всё равно). timeout — секунды, по умолчанию 1800.',
     {'base': {'type': 'string'},
      'extra_keys': {'type': 'array', 'items': {'type': 'string'}},
      'objects': {'type': 'array', 'items': {'type': 'string'},
                  'description': 'полные имена объектов для -objects (захват/освобождение в хранилище)'},
      'objects_file': {'type': 'string', 'description': 'готовый XML-файл списка объектов'},
      'files': {'type': 'array', 'items': {'type': 'string'},
                'description': 'частичная загрузка: rel-пути (Catalogs/X.xml) или имена объектов '
                               '(Справочник.X → файл+поддерево); сервер допишет -files и -Format; '
                               'успешная загрузка пишется в журнал базы (changes.json: файлы+sha256)'},
      'root': {'type': 'string', 'description': 'каталог XML-исходников для files/dump_changes '
                                                '(по умолчанию dump_dir/git_dir базы)'},
      'out': {'type': 'string', 'description': 'путь для /Out (текст вернётся в log)'},
      'timeout': {'type': 'number'},
      'allow_running': {'type': 'boolean'},
      'disable_dialogs': {'type': 'boolean', 'description': 'по умолчанию true'},
      'allow_extra_files': {'type': 'boolean', 'description': 'разрешить загрузку, даже если в git-рабочей копии выгрузки есть изменения вне списка files (по умолчанию — стоп)'},
      'user': {'type': 'string'}, 'pwd': {'type': 'string'},
      'platform': {'type': 'string'}}, t_run_batch),
    ('git_baseline', 'Сделать каталог XML-выгрузки базы git-репозиторием: ветка xml-state, '
     'автокоммит baseline (после первичной выгрузки XML). Дальше контроль изменений — git: '
     'dump_changes показывает git status/diff, а run_batch с files блокирует загрузку, если '
     'в рабочей копии есть изменения вне списка -files (фантомные правки платформы).',
     {'base': {'type': 'string'},
      'root': {'type': 'string', 'description': 'каталог XML-выгрузки (по умолчанию dump_dir базы)'}},
     t_git_baseline),
    ('dump_changes', 'Изменённые файлы XML-выгрузки базы с момента прошлой выгрузки '
     '(сравнение с манифестом: новые/изменённые по mtime+size, удалённые). Каталог — root, '
     'иначе корень из манифеста прошлой выгрузки, иначе git_dir базы. Плюс журнал загрузок '
     'run_batch (changes.json): что грузилось в базу последний раз (файлы+sha256, ревизия '
     'хранилища) и что в исходниках поменялось/пропало с той загрузки. годится, чтобы понять, '
     'что нести в хранилище (список для run_batch -files / -objects).',
     {'base': {'type': 'string'},
      'root': {'type': 'string', 'description': 'каталог XML-исходников (по умолчанию — из манифеста/git_dir)'},
      'limit': {'type': 'number', 'description': 'макс. файлов в ответе, по умолчанию 200'},
      'diff_file': {'type': 'string', 'description': 'git-режим: полный diff одного файла (git diff -- <file>)'}},
     t_dump_changes),
    ('list_sessions', 'Открытые сеансы 1С: база, режим, PID, заголовок окна, есть ли видимое окно.',
     {}, t_sessions),
    ('activate_session', 'Переключиться на окно открытого сеанса: по base (guid/имя) или pid; '
     'mode — если база открыта в двух режимах.',
     {'base': {'type': 'string'}, 'pid': {'type': 'integer'},
      'mode': {'type': 'string', 'enum': ['enterprise', 'designer']}}, t_activate),
    ('set_platform', 'Закрепить платформу за базой: '
     'platform = версия ("8.3.25.1336", "8.3.25"), семейство ("8.5") или полный путь к exe; '
     'auto=true — вернуть автоматический подбор. Дальше база всегда запускается этой платформой.',
     {'base': {'type': 'string'},
      'platform': {'type': 'string', 'description': 'версия, семейство или путь к 1cv8.exe'},
      'auto': {'type': 'boolean'},
      'mode': {'type': 'string', 'enum': ['enterprise', 'designer'],
               'description': 'для какого режима важнее (designer требует толстый клиент)'}},
     t_set_platform),
    ('detect_platform', 'Узнать версию сервера 1С по COM-подключению (без окна 1С), перебором '
     'платформ до совпадения, и закрепить за базой платформу этой версии. Работает и когда '
     'версии не совпали: 1С в тексте ошибки отдаёт версию сервера, дальше подключение идёт '
     'платформой ровно этой версии. pin=false — только показать, не закреплять.',
     {'base': {'type': 'string'}, 'pin': {'type': 'boolean'}}, t_detect_platform),
    ('base_maintenance', 'Закрыть базу на обслуживание: запуск из стартера запрещается '
     '(с таймаутом авто-снятия, по умолчанию 12 ч), опционально принудительно завершаются '
     'живые сеансы этой базы (kick_sessions=true, только сеансы этой базы по её пути в '
     'командной строке). off=true — снять блокировку.',
     {'base': {'type': 'string'}, 'minutes': {'type': 'number'},
      'note': {'type': 'string'}, 'kick_sessions': {'type': 'boolean'},
      'off': {'type': 'boolean'}}, t_maintenance),
    ('base_cominfo', 'Подключиться к базе по COM (comcntr.dll) и получить: имя, версию и '
     'разработчика конфигурации, режим совместимости, поддержку расширений, пользователей ИБ '
     '(всего/активных, список), текст ошибки подключения. Нужны логин/пароль пользователя ИБ '
     '(можно save=true — сохранить).',
     {'base': {'type': 'string'}, 'user': {'type': 'string'}, 'pwd': {'type': 'string'},
      'save': {'type': 'boolean'}, 'timeout': {'type': 'number'}}, t_cominfo),
    ('set_storage', 'Задать хранилище конфигурации у базы (ключи конфигуратора '
     '/ConfigurationRepositoryF/-N/-P): dir вида tcp://host:2543/имя_хранилища или \\\\сервер\\папка, '
     'user, pwd (пустая строка — без пароля). clear=true — снять хранилище. '
     'Если pwd не передан, сохраняется прежний.',
     {'base': {'type': 'string'}, 'dir': {'type': 'string'},
      'user': {'type': 'string'}, 'pwd': {'type': 'string'},
      'clear': {'type': 'boolean'}}, t_set_storage),
    ('set_credentials', 'Задать пользователя и пароль базы 1С (/N /P и COM-подключение). '
     'Пустая строка стирает сохранённое значение. Для Конфигуратора можно задать свой логин '
     '(user_cfg) и пароль (pwd_cfg); пустой user_cfg = как для Предприятия.',
     {'base': {'type': 'string'}, 'user': {'type': 'string'},
      'user_cfg': {'type': 'string', 'description': 'пользователь Конфигуратора (необязательно)'},
      'pwd': {'type': 'string', 'description': 'пароль Предприятия'},
      'pwd_cfg': {'type': 'string', 'description': 'пароль Конфигуратора (если отличается)'}},
     t_set_credentials),
    ('base_users', 'Пользователи информационной базы через COM: штатный '
     'ПользователиИнформационнойБазы.ПолучитьПользователей() (список зарегистрированных '
     'пользователей ИБ, не зависит от наличия справочника Пользователи), с откатом на справочник '
     'Пользователи/ПользователиИБ конфигурации. В ответе users (имена), users_source (откуда взят), '
     'users_count, users_ib_error. Пустой список — в базе нет зарегистрированных пользователей, '
     'это не ошибка. Нужны логин/пароль с правами администрирования ИБ. Логин затем пишется '
     'через set_credentials.',
     {'base': {'type': 'string'}, 'user': {'type': 'string'}, 'pwd': {'type': 'string'},
      'timeout': {'type': 'number'}}, t_base_users),
    ('base_cache', 'Кэш клиента 1С для одной базы: what=info (размеры), config (кэш программы/конфигурации: '
     '%LOCALAPPDATA%/1C/1cv8/<guid> — Config, ConfigSave, SICache, cgf-cache, cfgrepo), '
     'user (настройки пользователя: %APPDATA%/1C/1cv8/<guid> — 1cv8.pfl, def.usr), '
     'program (общий для всех пользователей ПК: %PROGRAMDATA%/1C/1cv8/<guid>), both. '
     'Данные базы не затрагиваются; очищать только при закрытых сеансах.',
     {'base': {'type': 'string'},
      'what': {'type': 'string', 'enum': ['info', 'config', 'user', 'program', 'both']}}, t_base_cache),
    ('move_base', 'Перенести базу в папку дерева (пишется Folder= в ibases.v8i, как перетаскивание '
     'в интерфейсе). base — имя или guid базы; folder — полный путь папки ("/Разработка/Проекты") '
     'или "/", чтобы убрать из папок («Без папки»). Папка должна существовать (add_folder).',
     {'base': {'type': 'string'}, 'folder': {'type': 'string'}}, t_move_base),
    ('list_platforms', 'Установленные платформы 1С: версия, семейство 8.3/8.5, разрядность, '
     'exe и exe тонкого клиента, есть ли конфигуратор, какая платформа по умолчанию для семейства.',
     {}, t_platforms),
    ('list_storages', 'Базы, подключённые к хранилищу конфигурации: каталог хранилища, пользователь, '
     'есть ли сохранённый пароль (сам пароль не отдаётся).', {}, t_storages),
    ('recent_launches', 'История недавних запусков (что запускали, когда, сколько раз).',
     {'limit': {'type': 'integer', 'description': 'по умолчанию 15'}}, t_recent),
    ('open_web', 'Открыть веб-базу (ws=) в браузере по умолчанию.',
     {'base': {'type': 'string'}}, t_open_web),
]

TOOL_MAP = {name: (desc, schema, fn) for name, desc, schema, fn in TOOLS}

REQUIRED = {
    'get_base': ['base'], 'launch_base': ['base'], 'run_processing': ['base', 'processing'],
    'run_batch': ['base', 'extra_keys'], 'dump_changes': ['base'], 'git_baseline': ['base'],
    'delete_base': ['base'], 'dryrun': ['base'], 'open_web': ['base'],
    'add_base': ['name'], 'add_folder': ['name'], 'move_base': ['base', 'folder'],
}


def tools_list() -> list:
    out = []
    for name, desc, schema, _ in TOOLS:
        out.append({'name': name, 'description': desc,
                    'inputSchema': {'type': 'object', 'properties': schema,
                                    'additionalProperties': False,
                                    'required': REQUIRED.get(name, [])}})
    return out


def call_tool(name: str, args: dict) -> dict:
    item = TOOL_MAP.get(name)
    if not item:
        res = _err(f'неизвестный инструмент: {name}')
        res['meta'] = {'tool': name, 'ms': 0}
        return res
    _, _, fn = item
    t0 = time.time()
    try:
        res = fn(args or {})
    except ValueError as e:                       # осмысленные ошибки (не найдено/уже открыто)
        res = _err(e)
    except Exception as e:                        # noqa: BLE001  — наружу текстом, не 500
        res = _err(f'{type(e).__name__}: {e}')
    res['meta'] = {'tool': name, 'ms': int((time.time() - t0) * 1000)}
    return res


# ---------------------------------------------------------------- JSON-RPC
def handle(req: dict) -> dict | None:
    """Обработать одно JSON-RPC сообщение. None — для уведомлений (ответ не нужен)."""
    if not isinstance(req, dict) or req.get('jsonrpc') != '2.0':
        return {'jsonrpc': '2.0', 'id': (req or {}).get('id'),
                'error': {'code': -32600, 'message': 'Invalid Request'}}
    method = req.get('method') or ''
    rid = req.get('id')
    if rid is None and method.startswith('notifications/'):
        return None
    if method == 'initialize':
        want = ((req.get('params') or {}).get('protocolVersion') or '').strip()
        return {'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': want if want in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': 'BaseLauncher: список баз 1С, запуск в Предприятии/Конфигураторе, '
                            'запуск с внешней обработкой (/Execute), открытые сеансы и '
                            'переключение окон, добавление баз/папок в ibases.v8i, '
                            'хранилища конфигурации, установленные платформы.'}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': tools_list()}}
    if method == 'tools/call':
        p = req.get('params') or {}
        return {'jsonrpc': '2.0', 'id': rid,
                'result': call_tool(p.get('name') or '', p.get('arguments') or {})}
    if method in ('resources/list', 'prompts/list'):
        key = method.split('/')[0]
        return {'jsonrpc': '2.0', 'id': rid, 'result': {key: []}}
    return {'jsonrpc': '2.0', 'id': rid,
            'error': {'code': -32601, 'message': f'Method not found: {method}'}}


def handle_http(body):
    """Тело HTTP-запроса → тело ответа (dict / list / None=202)."""
    if isinstance(body, list):
        out = [r for r in (handle(x) for x in body) if r is not None]
        return out or None
    return handle(body)
