# -*- coding: utf-8 -*-
"""Свой формат конфигурации BaseLauncher — data/config.json.

SM больше не используется: всё, что раньше бралось из `v8config.smc`
и `settings.cnf`, живёт здесь (ibases.v8i по-прежнему читаем — это формат 1С,
его понимает и штатный стартер).

Пароли не хранятся открытым текстом: `seal()` — base64 + XOR с ключом от
SHA256(host|user|salt). Это не криптография, а «защита от дурака»: в файле нет
читаемых паролей, при копировании конфига на другую машину/пользователя
значения просто не расшифруются (вернётся '') и пароль надо ввести заново.

Формат:
{
  "version": 1,
  "updated": "2026-10-22T18:00:00",
  "crypto": {"alg": "xor-b64-v1"},
  "default_family": "8.3",
  "families": {"8.3": "", "8.5": ""},     // "" = авто (новейшая установленная)
  "bases": {
    "<guid>": {
      "name": "...", "descr": "...", "user": "...",
      "pwd": "bl1:...", "pwd_cfg": "bl1:...",   // ИБ: Предприятие / Конфигуратор
      "exe": "C:\\...\\1cv8.exe", "thin": false,
      "mode": "enterprise|designer", "fav": true, "extra_keys": ["/..."],
      "storage": {"dir": "...", "user": "...", "pwd": "bl1:..."}
    }
  }
}
"""
import base64
import datetime
import getpass
import hashlib
import json
import platform
from pathlib import Path

from . import parsers, smcrypt

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
CONFIG_PATH = DATA_DIR / 'config.json'
LEGACY_OVERRIDES = DATA_DIR / 'overrides.json'

VERSION = 1
PREFIX = 'bl1:'
_SALT = b'baselauncher/2026/config'


# ── «защита от дурака» ───────────────────────────────────────────────────────

def _key() -> bytes:
    who = '|'.join((platform.node(), getpass.getuser(), 'baselauncher')).encode('utf-8', 'replace')
    return hashlib.sha256(who + _SALT).digest()


def _stream(n: int, key: bytes) -> bytes:
    out = b''
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + i.to_bytes(4, 'little')).digest()
        i += 1
    return out[:n]


def seal(text: str) -> str:
    """Пароль -> 'bl1:<base64 xor>'. Пустая строка остаётся пустой."""
    if not text:
        return ''
    raw = text.encode('utf-8')
    key = _key()
    x = bytes(a ^ b for a, b in zip(raw, _stream(len(raw), key)))
    return PREFIX + base64.b64encode(x).decode('ascii')


def unseal(value: str) -> str:
    """'bl1:...' -> пароль; не расшифровалось (другая машина/юзер) — ''."""
    if not value:
        return ''
    if not value.startswith(PREFIX):
        return value          # значение не наше (старый файл/ручная правка) — как есть
    try:
        x = base64.b64decode(value[len(PREFIX):])
    except Exception:
        return ''
    key = _key()
    try:
        # strict: на чужой машине ключ другой -> мусор -> честно возвращаем ''
        return bytes(a ^ b for a, b in zip(x, _stream(len(x), key))).decode('utf-8')
    except UnicodeDecodeError:
        return ''


def is_sealed(value: str) -> bool:
    return bool(value) and value.startswith(PREFIX)


# ── файл ─────────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {'version': VERSION, 'updated': '', 'crypto': {'alg': 'xor-b64-v1'},
            'default_family': '8.3', 'families': {}, 'bases': {}}


def load() -> dict:
    """Прочитать config.json; при первом запуске — собрать из старых источников."""
    cfg = None
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            cfg = None
    if not isinstance(cfg, dict) or 'bases' not in cfg:
        cfg = migrate()
    cfg.setdefault('version', VERSION)
    cfg.setdefault('crypto', {'alg': 'xor-b64-v1'})
    cfg.setdefault('default_family', '8.3')
    cfg.setdefault('families', {})
    cfg.setdefault('bases', {})
    return cfg


def save(cfg: dict, touch: bool = True) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if touch:
        cfg['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    cfg['version'] = VERSION
    tmp = CONFIG_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(CONFIG_PATH)
    return cfg


def get_base(cfg: dict, guid: str) -> dict:
    return (cfg.get('bases') or {}).get((guid or '').lower(), {})


_FIELDS = ('name', 'descr', 'user', 'user_cfg', 'exe', 'mode', 'extra_keys', 'family', 'learnt')


def set_base(guid: str, patch: dict) -> dict:
    """Точечно обновить настройки базы. Пароли принимаются открытым текстом, кладутся sealed.

    None/'' в полях — удалить ключ (вернуться к авто). {'clear': true} — удалить базу.
    """
    cfg = load()
    bases = cfg.setdefault('bases', {})
    guid = (guid or '').lower()
    if patch.get('clear'):
        bases.pop(guid, None)
        save(cfg)
        return cfg
    cur = bases.setdefault(guid, {})
    for fld in _FIELDS:
        if fld in patch:
            v = patch[fld]
            if fld == 'mode':
                if v in ('enterprise', 'designer'):
                    cur['mode'] = v
                else:
                    cur.pop('mode', None)
            elif fld == 'extra_keys':
                ks = [k for k in (v or []) if k]
                if ks:
                    cur['extra_keys'] = ks
                else:
                    cur.pop('extra_keys', None)
            elif v:
                cur[fld] = v
            else:
                cur.pop(fld, None)
    for fld in ('thin', 'fav'):
        if fld in patch:
            if patch[fld]:
                cur[fld] = True
            else:
                cur.pop(fld, None)
    for fld in ('pwd', 'pwd_cfg'):        # пароли ИБ: Предприятие / Конфигуратор
        if fld in patch:
            v = (patch[fld] or '').strip()
            if v:
                cur[fld] = seal(v)
            else:
                cur.pop(fld, None)
    if 'storage' in patch:
        s = patch['storage'] or {}
        if s.get('dir'):
            st = {'dir': s.get('dir', ''), 'user': s.get('user', '')}
            pwd = (s.get('pwd') or s.get('password') or '').strip()
            if pwd:
                st['pwd'] = seal(pwd)
            cur['storage'] = st
        else:
            cur.pop('storage', None)
    if not cur:
        bases.pop(guid, None)
    save(cfg)
    return cfg


def family_exe(cfg: dict, family: str) -> str:
    """Закреплённый пользователем exe семейства ('' = авто)."""
    return ((cfg.get('families') or {}).get(family) or '') if family else ''


def set_family_exe(family: str, exe: str) -> dict:
    cfg = load()
    fams = cfg.setdefault('families', {})
    if exe:
        fams[family] = exe
    else:
        fams.pop(family, None)
    return save(cfg)


# ── миграция из SM (один раз) ──────────────────────────────────────

def migrate() -> dict:
    """Собрать config.json из overrides.json + v8config.smc + settings.cnf.

    Вызывается автоматически, пока config.json нет. Старый overrides.json
    переименовывается в overrides.migrated.json (бэкап, читать больше не нужно).
    """
    cfg = _empty()
    legacy = {}
    if LEGACY_OVERRIDES.is_file():
        try:
            legacy = json.loads(LEGACY_OVERRIDES.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            legacy = {}
    cfg['default_family'] = legacy.get('default_family') or '8.3'

    sm_cfg, slots = {}, {}
    try:
        sm_cfg = parsers.parse_v8config()
        slots = parsers.parse_settings_slots()
        cfg['families'] = {fam: exe for fam, exe in (slots or {}).items() if exe}
    except Exception:
        pass

    old_bases = legacy.get('bases') or {}
    for guid in set(old_bases) | set(sm_cfg):
        our = old_bases.get(guid, {}) or {}
        sm = sm_cfg.get(guid, {}) or {}
        ent = {}
        for fld in ('name', 'descr', 'user', 'exe'):
            if our.get(fld):
                ent[fld] = our[fld]
        if our.get('mode') in ('enterprise', 'designer'):
            ent['mode'] = our['mode']
        if our.get('thin'):
            ent['thin'] = True
        # fav: пишем и False, иначе после миграции «избранное» потеряется
        if 'fav' in our:
            ent['fav'] = bool(our['fav'])
        if our.get('extra_keys'):
            ent['extra_keys'] = [k for k in our['extra_keys'] if k]
        # пароли: наш (открытый) -> SM (расшифрованный crypto-module)
        pwd = (our.get('pwd') or '').strip()
        pwd_cfg = (our.get('pwd_cfg') or '').strip()
        if not pwd and sm.get('EntPassword'):
            pwd = smcrypt.decrypt(sm['EntPassword'])
        if not pwd_cfg and sm.get('CfgPassword'):
            pwd_cfg = smcrypt.decrypt(sm['CfgPassword'])
        if pwd:
            ent['pwd'] = seal(pwd)
        if pwd_cfg:
            ent['pwd_cfg'] = seal(pwd_cfg)
        # прочее из SM
        if not ent.get('descr') and sm.get('Description'):
            ent['descr'] = sm['Description']
        if not ent.get('user') and sm.get('UserLogin'):
            ent['user'] = sm['UserLogin']
        if not ent.get('exe') and str(sm.get('V8UseAppFile', '0')) == '1' and sm.get('V8AppFile'):
            ent['exe'] = sm['V8AppFile']
        st = dict(our.get('storage') or {})
        if not st.get('dir') and sm.get('StorageDir'):
            st = {'dir': sm['StorageDir'], 'user': sm.get('StorageUser', '')}
        if st.get('dir'):
            st_out = {'dir': st['dir'], 'user': st.get('user', '')}
            spwd = (st.get('pwd') or st.get('password') or '').strip()
            if not spwd and sm.get('StoragePassword'):
                spwd = smcrypt.decrypt(sm['StoragePassword'])
            if spwd:
                st_out['pwd'] = seal(spwd)
            ent['storage'] = st_out
        if ent:
            cfg['bases'][(guid or '').lower()] = ent

    save(cfg)
    if LEGACY_OVERRIDES.is_file():
        try:
            LEGACY_OVERRIDES.replace(DATA_DIR / 'overrides.migrated.json')
        except OSError:
            pass
    return cfg


if __name__ == '__main__':
    c = load()
    print('конфиг:', CONFIG_PATH)
    print('баз в конфиге:', len(c['bases']), '| семейства:', c['families'])
    for g, v in list(c['bases'].items())[:5]:
        print(' ', g, {k: ('***' if k in ('pwd', 'pwd_cfg') else v2) for k, v2 in v.items()})
