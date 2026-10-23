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
import json
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
    import re
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


def t_detect_platform(a: dict) -> dict:
    """Версия сервера 1С через COM (как обновлятор) + закрепление её платформы за базой."""
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
                  'users_error': res.get('users_error') or ''})


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
    ('list_sessions', 'Открытые сеансы 1С: база, режим, PID, заголовок окна, есть ли видимое окно.',
     {}, t_sessions),
    ('activate_session', 'Переключиться на окно открытого сеанса: по base (guid/имя) или pid; '
     'mode — если база открыта в двух режимах.',
     {'base': {'type': 'string'}, 'pid': {'type': 'integer'},
      'mode': {'type': 'string', 'enum': ['enterprise', 'designer']}}, t_activate),
    ('set_platform', 'Закрепить платформу за базой (как выбор платформы в SM): '
     'platform = версия ("8.3.25.1336", "8.3.25"), семейство ("8.5") или полный путь к exe; '
     'auto=true — вернуть автоматический подбор. Дальше база всегда запускается этой платформой.',
     {'base': {'type': 'string'},
      'platform': {'type': 'string', 'description': 'версия, семейство или путь к 1cv8.exe'},
      'auto': {'type': 'boolean'},
      'mode': {'type': 'string', 'enum': ['enterprise', 'designer'],
               'description': 'для какого режима важнее (designer требует толстый клиент)'}},
     t_set_platform),
    ('detect_platform', 'Узнать версию сервера 1С по COM-подключению (как обновлятор, без окна) '
     'и закрепить за базой платформу этой версии. Работает и когда версии не совпали: 1С в тексте '
     'ошибки отдаёт версию сервера. pin=false — только показать, не закреплять.',
     {'base': {'type': 'string'}, 'pin': {'type': 'boolean'}}, t_detect_platform),
    ('base_cominfo', 'Подключиться к базе по COM (comcntr.dll, как обновлятор) и получить: имя и '
     'версию конфигурации, режим совместимости, пользователей ИБ (всего/активных, список), '
     'текст ошибки подключения. Нужны логин/пароль пользователя ИБ (можно save=true — сохранить).',
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
    ('base_users', 'Список пользователей ИБ через COM-подключение (справочник Пользователи) — '
     'чтобы выбрать логин и записать его через set_credentials. Нужны логин/пароль пользователя, '
     'у которого есть права на чтение справочника.',
     {'base': {'type': 'string'}, 'user': {'type': 'string'}, 'pwd': {'type': 'string'},
      'timeout': {'type': 'number'}}, t_base_users),
    ('base_cache', 'Кэш клиента 1С для одной базы: what=info (размеры), user (кэш текущего '
     'пользователя Windows: %LOCALAPPDATA%/1C/1cv8/<guid> + %APPDATA%/1C/1cv8/<guid>), '
     'program (общий для всех пользователей ПК: %PROGRAMDATA%/1C/1cv8/<guid>), both. '
     'Данные базы не затрагиваются; очищать только при закрытых сеансах.',
     {'base': {'type': 'string'},
      'what': {'type': 'string', 'enum': ['info', 'user', 'program', 'both']}}, t_base_cache),
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
        return _err(f'неизвестный инструмент: {name}')
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
