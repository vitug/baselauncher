# -*- coding: utf-8 -*-
"""Сборка состояния BaseLauncher: ibases.v8i (список баз 1С) + свой конфиг (data/config.json).

SM не используется. Приоритет эффективного exe платформы для базы:
  1. config.json: bases[guid].exe        (наш выбор для базы)
  2. config.json: families[семейство]     (закреплённый exe семейства)
  3. новейшая установленная платформа семейства по умолчанию (default_family)
"""
import datetime
import json
from pathlib import Path

from . import config, launcher, parsers, procs, scanner
from .model import BaseEntry

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
STATE_PATH = DATA_DIR / 'bases.json'

# семейство -> какие ключи слотов settings.cnf ему соответствуют
_SLOT_FAMILIES = {'8.1': '8.1', '8.2': '8.2', '8.3': '8.3', '8.4': '8.4', '8.5': '8.5'}


def _family_of_exe(exe: str, platforms: list) -> str:
    for p in platforms:
        if exe and p.exe and p.exe.lower() == exe.lower():
            return p.family
    # из пути: ...\1cv8\8.5.1.1580\bin\1cv8.exe
    parts = (exe or '').replace('/', '\\').split('\\')
    for part in parts:
        bits = part.split('.')
        if len(bits) >= 3 and bits[0].startswith('8') and bits[1].isdigit():
            return '.'.join(bits[:2])
    return ''


def _resolve_exe(guid: str, cfg: dict, platforms: list) -> tuple[str, str]:
    our = config.get_base(cfg, guid)
    if our.get('exe'):
        return our['exe'], 'our'
    # платформа, которой базу открывали раньше (выучено из живого сеанса): сервер требует ровно её
    learnt = (our.get('learnt') or '').strip()
    if learnt and Path(learnt).is_file():
        return learnt, 'learnt'
    # семейство, закреплённое за базой (например 8.5, если сервер 8.5.1.x):
    # берём самую новую полную платформу этого семейства
    fam_pin = (our.get('family') or '').strip()
    if fam_pin:
        for p in platforms:
            if p.family == fam_pin and (p.exe or p.exe_thin):
                return (p.exe or p.exe_thin), 'our-family'
    fam = cfg.get('default_family') or '8.3'
    pinned = config.family_exe(cfg, fam)
    if pinned:
        return pinned, 'config'
    # платформы «только тонкий клиент» (1cv8c.exe без 1cv8.exe) в автоподбор НЕ берём:
    # это обычно 32-бит-клиенты и клиенты под конкретные публикации, и «самая новая» такая
    # платформа (напр. 8.3.28.949) ломает запуск серверных баз со старым кластером.
    # Они остаются в дереве платформ для явного выбора и для автоподбора ws-баз по версии публикации.
    for p in platforms:  # platforms отсортированы от новейших
        if p.family == fam and p.exe:
            return p.exe, 'auto'
    return '', 'none'


def build_state() -> dict:
    v8i_secs = parsers.parse_v8i()
    cfg = config.load()
    platforms = scanner.scan_platforms()

    bases = []
    folders = []
    for keys in v8i_secs:
        connect = keys.get('Connect', '')
        name = keys.get('__name__', '')
        if not connect.strip():
            # секция-папка стартера (без Connect; в живом файле у них OrderInList=-1)
            folders.append({'guid': keys.get('ID', '').strip().lower(), 'name': name,
                            'folder': keys.get('Folder', '/'), 'order': keys.get('OrderInTree', '')})
            continue
        path, srvr, ref, ws = parsers.parse_connect(connect)
        guid = keys.get('ID', '').strip().lower()
        our = config.get_base(cfg, guid)
        exe, exe_src = _resolve_exe(guid, cfg, platforms)
        if our.get('thin') and exe:  # тонкий клиент: 1cv8c.exe рядом с выбранным exe
            alt = str(Path(exe).with_name('1cv8c.exe'))
            if Path(alt).is_file():
                exe = alt
        # веб-база (ws=): по умолчанию тонкий клиент — так их и открывают
        # (толстый только грузит конфигурацию и перепоручает тонкому)
        ws_thin = False
        if ws and exe and not our.get('exe'):
            alt = str(Path(exe).with_name('1cv8c.exe'))
            if Path(alt).is_file():
                exe = alt
                ws_thin = True
        # хранилище конфигурации — из своего конфига (пароль расшифровывается на лету)
        storage = {}
        st_cfg = our.get('storage') or {}
        if st_cfg.get('dir'):
            storage = {'dir': st_cfg.get('dir', ''), 'user': st_cfg.get('user', ''),
                       'password': config.unseal(st_cfg.get('pwd') or '')}
        # пароли ИБ: Предприятие / Конфигуратор (в файле — sealed)
        pwd = config.unseal(our.get('pwd') or '')
        pwd_cfg = config.unseal(our.get('pwd_cfg') or '')
        pwd_note = 'set' if (pwd or pwd_cfg) else (
            'broken' if (our.get('pwd') or our.get('pwd_cfg')) else '')
        # конфигуратор есть не всегда: у веб-базы (ws) его нет в принципе,
        # у платформы «только тонкий клиент» — нет толстого 1cv8.exe
        has_designer = True
        if ws:
            has_designer = False
        elif exe and exe.lower().endswith('1cv8c.exe'):
            has_designer = Path(exe).with_name('1cv8.exe').is_file()
        # наличиe папки файловой базы и файла базы внутри
        if path:
            if Path(path).is_dir():
                has_cd = any(Path(path).glob('1Cv8.1CD')) or any(Path(path).glob('*.1CD'))
                path_ok, path_info = bool(has_cd), ('ok' if has_cd else 'нет файла 1Cv8.1CD')
            else:
                path_ok, path_info = False, 'папки базы нет'
        elif ws:
            path_ok, path_info = None, 'веб-база'
        else:
            path_ok, path_info = None, 'серверная'
        exe_ok = bool(exe) and Path(exe).is_file()
        bases.append(BaseEntry(
            guid=guid,
            name=(our.get('name') or name),
            ibname=name,
            descr=str(our.get('descr') or ''),
            user=str(our.get('user') or ''),
            pwd=pwd,
            pwd_cfg=pwd_cfg, pwd_note=pwd_note,
            folder=keys.get('Folder', '/'),
            connect=connect,
            path=path, server=srvr, ref=ref, ws=ws,
            app=keys.get('App', 'Auto'),
            order=keys.get('OrderInTree', ''),
            sm={},
            exe=exe, exe_src=exe_src,
            family=_family_of_exe(exe, platforms),
            thin=bool(our.get('thin')) or ws_thin,
            has_designer=has_designer,
            storage=storage,
            extra_keys=list(our.get('extra_keys') or []),
            our=our,
            fav=bool(our.get('fav')),
            default_mode=(str(our.get('mode')) if our.get('mode') in ('enterprise', 'designer') else ''),
            path_ok=path_ok,
            path_info=path_info,
            exe_ok=exe_ok,
        ).as_dict())

    # история запусков (data/launches.log): последний запуск, счётчик, «сегодня/вчера»
    hist = launcher.history()
    by_guid = {b['guid']: b for b in bases}
    today = datetime.date.today()
    for b in bases:
        b['last_launch'] = None
        b['launches'] = 0
        b['launch_day'] = ''
    # старые записи лога без guid приходят отдельной группой (по имени) — сводим к одной базе
    merged: dict[str, dict] = {}
    for h in hist:
        b = by_guid.get((h.get('guid') or '').lower())
        if not b:  # старые записи без guid — только при однозначном совпадении имени
            same = [x for x in bases if x['name'] == h.get('name')]
            b = same[0] if len(same) == 1 else None
        if not b:
            continue
        m = merged.get(b['guid'])
        if m is None:
            merged[b['guid']] = {'base': b, 'last_ts': h.get('last_ts') or '',
                                 'count': int(h.get('count') or 0),
                                 'mode': h.get('mode') or 'enterprise'}
        else:
            m['count'] += int(h.get('count') or 0)
            if (h.get('last_ts') or '') > m['last_ts']:
                m['last_ts'] = h.get('last_ts') or ''
                m['mode'] = h.get('mode') or m['mode']
    recent = []
    for g, m in merged.items():
        b = m['base']
        day = 'older'
        try:
            d = datetime.date.fromisoformat((m['last_ts'] or '')[:10])
        except ValueError:
            d = None
        if d == today:
            day = 'today'
        elif d == today - datetime.timedelta(days=1):
            day = 'yesterday'
        b['last_launch'] = m['last_ts'] or None
        b['launches'] = m['count']
        b['launch_day'] = day
        loc = b['path'] or b.get('ws') or \
            ((b['server'] + ('\\' + b['ref'] if b['ref'] else '')) if b['server'] else '')
        recent.append({'guid': b['guid'], 'name': b['name'], 'path': loc,
                       'mode': m['mode'], 'last_ts': m['last_ts'],
                       'count': m['count'], 'day': day})
    # «Недавние»: строго по времени последнего запуска, свежие сверху
    recent.sort(key=lambda r: (r['last_ts'] or '', r['count'] or 0), reverse=True)

    # живые сеансы 1С на хосте: какие базы уже открыты и в каком режиме
    sessions = procs.running_sessions()
    by_path: dict[str, list[dict]] = {}
    by_srv: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    by_ws: dict[str, list[dict]] = {}
    for b in bases:
        b['running_modes'] = []
        b['running_pids'] = {}
        b['running_win'] = {}
        b['running'] = False
        if b['path']:
            by_path.setdefault(procs._norm_path(b['path']), []).append(b)
        if b['server']:
            by_srv.setdefault('%s|%s' % (b['server'].lower(), (b['ref'] or '').lower()), []).append(b)
        if b.get('ws'):
            by_ws.setdefault(b['ws'].rstrip('/').lower(), []).append(b)
        by_name.setdefault(b['name'].strip().lower(), []).append(b)
    live = []
    # лучший pid на (база, режим): окно > без окна, настоящий заголовок > сплэш
    # (веб-базы: толстый клиент грузит «Загрузка конфигурационной информации...»
    # и перепоручает тонкому — реальное окно у тонкого клиента)
    best: dict[tuple, tuple] = {}

    def _prio(s: dict) -> tuple:
        t = (s.get('title') or '').strip()
        splash = t.startswith('Загрузка конфигурационной информации') \
            or t.startswith('Запуск 1С:Предприятия')
        return (1 if s.get('window') else 0, 0 if splash else 1)

    for s in sessions:
        cand = []
        if s['path']:
            cand = by_path.get(procs._norm_path(s['path']), [])
        elif s['server']:
            cand = by_srv.get('%s|%s' % (s['server'].lower(), (s['ref'] or '').lower()), [])
        elif s.get('ws'):
            cand = by_ws.get(s['ws'].rstrip('/').lower(), [])
        elif s['name']:
            cand = by_name.get(s['name'].strip().lower(), [])
        for b in cand:
            b['running'] = True
            if s['mode'] not in b['running_modes']:
                b['running_modes'].append(s['mode'])
            key = (b['guid'], s['mode'])
            pr = _prio(s)
            if key not in best or pr > best[key][0]:
                best[key] = (pr, s['pid'])
            b['running_win'][s['mode']] = bool(b['running_win'].get(s['mode'])) or bool(s.get('window'))
            live.append({'guid': b['guid'], 'name': b['name'], 'mode': s['mode'],
                         'pid': s['pid'], 'exe': s['exe'],
                         'window': bool(s.get('window')), 'title': s.get('title', '')})
    for (g, m), (_pr, pid) in best.items():
        for b in bases:
            if b['guid'] == g:
                b['running_pids'][m] = pid
                break

    state = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'defaults': {'default_family': cfg.get('default_family', '8.3'),
                     'families': cfg.get('families', {})},
        'config_path': str(config.CONFIG_PATH),
        'platforms': [p.as_dict() for p in platforms],
        'folders': folders,
        'bases': bases,
        'recent': recent[:20],
        'sessions': live,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    _BASES_CACHE[:] = bases        # для лёгкого опроса сеансов (/api/sessions)
    return state


def find_base(state: dict, guid: str) -> dict | None:
    for b in state['bases']:
        if b['guid'] == guid.lower():
            return b
    return None


# ── лёгкий опрос сеансов (без пересборки платформ/дерева) ────────────────────
_BASES_CACHE: list[dict] = []


def _index_bases(bases: list[dict]):
    """Индексы для матчинга сеансов к базам (кладём в кеш вместе с базой)."""
    by_path, by_srv, by_ws, by_name = {}, {}, {}, {}
    for b in bases:
        if b.get('path'):
            by_path.setdefault(procs._norm_path(b['path']), []).append(b)
        if b.get('server'):
            by_srv.setdefault('%s|%s' % (b['server'].lower(), (b.get('ref') or '').lower()), []).append(b)
        if b.get('ws'):
            by_ws.setdefault(b['ws'].rstrip('/').lower(), []).append(b)
        by_name.setdefault((b.get('name') or '').strip().lower(), []).append(b)
    return by_path, by_srv, by_ws, by_name


def sessions_light() -> dict:
    """Только живые сеансы: {running: {guid: {modes, pids, win}}, sessions: [...]}.

    Считается без сканирования платформ и парсинга v8i — по кешу последнего build_state.
    Нужно для частого опроса из UI (3 с), чтобы кнопки запуска не залипали после закрытия 1С.
    """
    bases = _BASES_CACHE
    by_path, by_srv, by_ws, by_name = _index_bases(bases)
    running: dict[str, dict] = {}
    live = []
    for s in procs.running_sessions():
        cand = []
        if s.get('path'):
            cand = by_path.get(procs._norm_path(s['path']), [])
        if not cand and s.get('server'):
            cand = by_srv.get('%s|%s' % (s['server'].lower(), (s.get('ref') or '').lower()), [])
        if not cand and s.get('ws'):
            cand = by_ws.get(s['ws'].rstrip('/').lower(), [])
        if not cand and s.get('name'):
            cand = by_name.get(s['name'].strip().lower(), [])
        for b in cand:
            cur = running.setdefault(b['guid'], {'modes': [], 'pids': {}, 'win': {}})
            m = s['mode']
            if m not in cur['modes']:
                cur['modes'].append(m)
            t = (s.get('title') or '').strip()
            splash = t.startswith('Загрузка конфигурационной информации') \
                or t.startswith('Запуск 1С:Предприятия')
            better = (1 if s.get('window') else 0, 0 if splash else 1)
            prev = cur.setdefault('_prio', {}).get(m)
            if prev is None or better > prev:
                cur['pids'][m] = s['pid']
                cur.setdefault('_prio', {})[m] = better
            cur['win'][m] = bool(cur['win'].get(m)) or bool(s.get('window'))
            live.append({'guid': b['guid'], 'name': b['name'], 'mode': m, 'pid': s['pid'],
                         'window': bool(s.get('window')), 'title': t})
    for cur in running.values():
        cur.pop('_prio', None)
    return {'running': running, 'sessions': live,
            'generated_at': datetime.datetime.now().isoformat(timespec='seconds')}


if __name__ == '__main__':
    st = build_state()
    fams = {}
    for b in st['bases']:
        fams[b['family']] = fams.get(b['family'], 0) + 1
    print(f"баз: {len(st['bases'])}, платформ найдено: {len(st['platforms'])}")
    for p in st['platforms']:
        print(f"  {p['key']:14} {p['bitness']} {p['family']:5} {p['source']}")
    print("семейства:", fams)
    print("слоты settings.cnf:", st['slots'])
    pinned = sum(1 for b in st['bases'] if b['exe_src'] in ('sm', 'our'))
    print(f"с явным exe (sm/our): {pinned}")
