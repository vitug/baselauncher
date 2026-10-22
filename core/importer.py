# -*- coding: utf-8 -*-
"""Импорт данных SM 1.4 + 1CEStart в свой формат (data/bases.json) и сборка состояния.

Приоритет эффективного exe платформы для базы:
  1. data/overrides.json: bases[guid].exe      (наши правки — высший приоритет)
  2. v8config.smc: V8UseAppFile=1 + V8AppFile  (пер-базовый обход из SM)
  3. settings.cnf: слот семейства (V8xAppFile; слот 8.4 = 8.5 на этом хосте)
  4. новейшая установленная платформа семейства по умолчанию (default_family)
"""
import datetime
import json
from pathlib import Path

from . import launcher, parsers, scanner
from .model import BaseEntry

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
OVERRIDES_PATH = DATA_DIR / 'overrides.json'
STATE_PATH = DATA_DIR / 'bases.json'

# семейство -> какие ключи слотов settings.cnf ему соответствуют
_SLOT_FAMILIES = {'8.1': '8.1', '8.2': '8.2', '8.3': '8.3', '8.4': '8.4', '8.5': '8.5'}


def load_overrides() -> dict:
    if OVERRIDES_PATH.is_file():
        try:
            return json.loads(OVERRIDES_PATH.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            pass
    return {'default_family': '8.3', 'bases': {}}


def _family_of_exe(exe: str, platforms: list) -> str:
    for p in platforms:
        if p.exe.lower() == (exe or '').lower():
            return p.family
    # из пути: ...\1cv8\8.5.1.1580\bin\1cv8.exe
    parts = (exe or '').replace('/', '\\').split('\\')
    for part in parts:
        bits = part.split('.')
        if len(bits) >= 3 and bits[0].startswith('8') and bits[1].isdigit():
            return '.'.join(bits[:2])
    return ''


def _resolve_exe(guid: str, sm: dict, overrides: dict, slots: dict, platforms: list) -> tuple[str, str]:
    our = (overrides.get('bases') or {}).get(guid, {})
    if our.get('exe'):
        return our['exe'], 'our'
    if str(sm.get('V8UseAppFile', '0')) == '1' and sm.get('V8AppFile'):
        return sm['V8AppFile'], 'sm'
    fam = overrides.get('default_family', '8.3')
    slot_exe = slots.get(_SLOT_FAMILIES.get(fam, fam), '')
    if slot_exe:
        return slot_exe, 'slot'
    for p in platforms:  # platforms отсортированы от новейших
        if p.family == fam:
            return p.exe, 'auto'
    return '', 'none'


def build_state() -> dict:
    v8i_secs = parsers.parse_v8i()
    sm_cfg = parsers.parse_v8config()
    slots = parsers.parse_settings_slots()
    overrides = load_overrides()
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
        path, srvr, ref = parsers.parse_connect(connect)
        guid = keys.get('ID', '').strip().lower()
        sm = sm_cfg.get(guid, {})
        our = (overrides.get('bases') or {}).get(guid, {})
        exe, exe_src = _resolve_exe(guid, sm, overrides, slots, platforms)
        if our.get('thin') and exe:  # тонкий клиент: 1cv8c.exe рядом с выбранным exe
            alt = str(Path(exe).with_name('1cv8c.exe'))
            if Path(alt).is_file():
                exe = alt
        storage = dict(our.get('storage') or {})
        if not storage.get('dir') and sm.get('StorageDir'):
            storage = {'dir': sm.get('StorageDir', ''), 'user': sm.get('StorageUser', '')}
        # наличиe папки файловой базы и файла базы внутри
        if path:
            if Path(path).is_dir():
                has_cd = any(Path(path).glob('1Cv8.1CD')) or any(Path(path).glob('*.1CD'))
                path_ok, path_info = bool(has_cd), ('ok' if has_cd else 'нет файла 1Cv8.1CD')
            else:
                path_ok, path_info = False, 'папки базы нет'
        else:
            path_ok, path_info = None, 'серверная'
        exe_ok = bool(exe) and Path(exe).is_file()
        bases.append(BaseEntry(
            guid=guid,
            name=(our.get('name') or name),
            descr=(our.get('descr') or sm.get('Description', '')),
            user=(our.get('user') or sm.get('UserLogin', '')),
            pwd=str(our.get('pwd') or ''),
            folder=keys.get('Folder', '/'),
            connect=connect,
            path=path, server=srvr, ref=ref,
            app=keys.get('App', 'Auto'),
            order=keys.get('OrderInTree', ''),
            sm={'Description': sm.get('Description', ''),
                'LaunchDate': sm.get('LaunchDate', ''),
                'V8AppFile': sm.get('V8AppFile', ''),
                'UserLogin': sm.get('UserLogin', ''),
                'StorageDir': sm.get('StorageDir', ''),
                'StorageUser': sm.get('StorageUser', '')},
            exe=exe, exe_src=exe_src,
            family=_family_of_exe(exe, platforms),
            thin=bool(our.get('thin')),
            storage=storage,
            extra_keys=list(our.get('extra_keys') or []),
            our=our,
            fav=bool(our.get('fav')),
            default_mode=(our.get('mode') if our.get('mode') in ('enterprise', 'designer') else ''),
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
        loc = b['path'] or ((b['server'] + ('\\' + b['ref'] if b['ref'] else '')) if b['server'] else '')
        recent.append({'guid': b['guid'], 'name': b['name'], 'path': loc,
                       'mode': m['mode'], 'last_ts': m['last_ts'],
                       'count': m['count'], 'day': day})
    recent.sort(key=lambda r: r['last_ts'] or '', reverse=True)

    state = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'defaults': {'default_family': overrides.get('default_family', '8.3')},
        'slots': slots,
        'platforms': [p.as_dict() for p in platforms],
        'folders': folders,
        'bases': bases,
        'recent': recent[:20],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    return state


def find_base(state: dict, guid: str) -> dict | None:
    for b in state['bases']:
        if b['guid'] == guid.lower():
            return b
    return None


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
