# -*- coding: utf-8 -*-
"""Перенос имён пользователей ИБ из SM (v8config.smc) в наш data/config.json.

SM хранил логин для Предприятия (UserLoginEnt/EntUser) и отдельно для Конфигуратора
(UserLoginCnf/CfgUser). BaseLauncher использует одно поле user (/N) на оба режима, поэтому
переносим только те базы, где в SM включён вход в Предприятие (UserLoginEnt=1) — иначе логин
конфигуратора уехал бы в командную строку Предприятия с «чужим» паролем.

Пароли SM хранит в собственной обфускации, наш формат другой — их не трогаем.

Запуск:  python tools/import_sm_users.py [--apply]
Без --apply только показывает, что будет записано. Перед записью делает бэкап config.json.
"""
import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import parsers  # noqa: E402

CFG = ROOT / 'data' / 'config.json'


def main(apply_: bool) -> int:
    cfg = json.load(open(CFG, encoding='utf-8'))
    smc = parsers.parse_v8config()
    names = {str(r.get('ID', '')).strip().lower(): r['__name__'] for r in parsers.parse_v8i()}

    ent, cnf = [], []
    for guid, our in cfg.get('bases', {}).items():
        sm = smc.get(guid.strip().lower()) or {}
        if not sm or str(our.get('user') or '').strip():
            continue
        ue = (sm.get('EntUser') or '').strip()
        uc = (sm.get('CfgUser') or '').strip()
        if sm.get('UserLoginEnt') == '1' and ue:
            ent.append((guid, names.get(guid, ''), ue))
        elif uc and sm.get('UserLoginCnf') == '1':
            cnf.append((guid, names.get(guid, ''), uc))

    print('баз, где в SM включён вход в Предприятие:', len(ent))
    for guid, name, user in ent:
        print('  %-58s → %s' % (name[:58], user))
    print('\nтолько логин Конфигуратора (вход Предприятия выключен) — не переносим:', len(cnf))
    for guid, name, user in cnf:
        print('  %-58s → %s' % (name[:58], user))

    if not apply_:
        print('\nэто превью; повторите с --apply, чтобы записать в', CFG)
        return 0
    if not ent:
        print('нечего записывать')
        return 0

    bak = CFG.with_name(CFG.name + '.bak-smusers-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy(CFG, bak)
    for guid, _name, user in ent:
        cfg['bases'][guid]['user'] = user
    json.dump(cfg, open(CFG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\nзаписано имён:', len(ent), '| бэкап:', bak)
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='записать в data/config.json (иначе только показать)')
    sys.exit(main(ap.parse_args().apply))
