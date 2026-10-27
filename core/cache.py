"""Очистка кэшей: клиентский кэш 1С у пользователя и служебные файлы самого стартера.

Кэш 1С лежит двумя деревьями, имена подкаталогов — GUID информационной базы:
  %LOCALAPPDATA%\\1C\\1cv8\\<guid>   — кэш конфигурации (самый большой, десятки ГБ)
  %APPDATA%\\1C\\1cv8\\<guid>        — пользовательские настройки и кэш интерфейса
Удалять их можно только при закрытых сеансах 1С, иначе часть файлов занята.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

HUMAN = ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ')


def human(n: float) -> str:
    n = float(n or 0)
    for unit in HUMAN:
        if n < 1024 or unit == HUMAN[-1]:
            return ('%d %s' % (n, unit)) if unit == 'Б' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return '%.1f ТБ' % n


def local_root() -> Path:
    return Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / '1C' / '1cv8'


def roaming_root() -> Path:
    return Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming')) / '1C' / '1cv8'


def size_of(path: Path, budget: float = 2.5) -> dict:
    """Размер дерева с ограничением по времени: на 120 ГБ полный обход слишком долгий."""
    total = 0
    files = 0
    partial = False
    started = time.time()
    stack = [str(path)]
    while stack:
        if time.time() - started > budget:
            partial = True
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return {'bytes': total, 'human': human(total), 'files': files, 'partial': partial}


def one_c_dirs(guid: str | None = None) -> list[Path]:
    """Каталоги кэша 1С: все (guid=None) или только по конкретной базе."""
    out: list[Path] = []
    for root in (local_root(), roaming_root()):
        if not root.is_dir():
            continue
        if guid:
            p = root / guid
            if p.is_dir():
                out.append(p)
            continue
        try:
            for e in os.scandir(root):
                if e.is_dir(follow_symlinks=False):
                    out.append(Path(e.path))
        except OSError:
            continue
    return out


def one_c_info(guid: str | None = None) -> dict:
    dirs = one_c_dirs(guid)
    total = 0
    files = 0
    partial = False
    for d in dirs:
        s = size_of(d, budget=1.2)
        total += s['bytes']
        files += s['files']
        partial = partial or s['partial']
    return {'dirs': len(dirs), 'bytes': total, 'human': human(total), 'files': files,
            'partial': partial, 'paths': [str(d) for d in dirs[:50]]}


def _rmtree(path: Path) -> dict:
    """Удалить каталог, не падая на занятых файлах (1С может держать кэш открытым)."""
    errors = []
    before = size_of(path, budget=1.0)['bytes']

    def onerror(func, p, exc):          # noqa: ANN001
        errors.append('%s: %s' % (p, exc))

    try:
        shutil.rmtree(path, onerror=onerror)
    except Exception as e:              # noqa: BLE001
        errors.append(str(e))
    return {'removed': not path.exists(), 'bytes': before, 'errors': errors[:5],
            'error_count': len(errors)}


def clear_one_c(guid: str | None = None) -> dict:
    dirs = one_c_dirs(guid)
    removed = 0
    freed = 0
    errors = []
    for d in dirs:
        r = _rmtree(d)
        freed += r['bytes']
        if r['removed']:
            removed += 1
        errors.extend(r['errors'])
    return {'ok': not errors or removed > 0, 'what': 'one_c', 'guid': guid,
            'dirs_found': len(dirs), 'dirs_removed': removed,
            'freed': freed, 'freed_human': human(freed), 'errors': errors[:10]}


# ---- служебные файлы стартера -------------------------------------------------

APP_CACHE_FILES = ('bases.json', 'server.log', 'boot.log', 'focus.log', 'launches.log',
                   'pythonw.log', 'pwtest.txt')
APP_CACHE_DIRS = ('install',)


def app_info(root: Path) -> dict:
    data = Path(root) / 'data'
    total = 0
    items = []
    for name in APP_CACHE_FILES:
        p = data / name
        if p.is_file():
            n = p.stat().st_size
            total += n
            items.append({'name': name, 'bytes': n, 'human': human(n)})
    for name in APP_CACHE_DIRS:
        p = data / name
        if p.is_dir():
            s = size_of(p, budget=0.8)
            total += s['bytes']
            items.append({'name': name + '/', 'bytes': s['bytes'], 'human': s['human']})
    return {'path': str(data), 'bytes': total, 'human': human(total), 'items': items}


def clear_app(root: Path) -> dict:
    """Убрать производные файлы (кэш состояния, логи). Настройки — config.json, window.json — не трогаем."""
    data = Path(root) / 'data'
    freed = 0
    removed = []
    errors = []
    for name in APP_CACHE_FILES:
        p = data / name
        if p.is_file():
            try:
                n = p.stat().st_size
                p.unlink()
                freed += n
                removed.append(name)
            except OSError as e:
                errors.append('%s: %s' % (name, e))
    for name in APP_CACHE_DIRS:
        p = data / name
        if p.is_dir():
            r = _rmtree(p)
            freed += r['bytes']
            if r['removed']:
                removed.append(name + '/')
            errors.extend(r['errors'])
    return {'ok': True, 'what': 'app', 'removed': removed, 'freed': freed,
            'freed_human': human(freed), 'errors': errors[:10]}


def info(root: Path) -> dict:
    return {'one_c': {'path': str(local_root()), 'roaming': str(roaming_root()),
                      **one_c_info()},
            'app': app_info(root)}

# ---- кэш конкретной базы: пользователь / общий для всех пользователей ПК ----------
# Из справки 1С (и разбора реального каталога на этом хосте):
#   %LOCALAPPDATA%\1C\1cv8\<guid>\  — Config, ConfigSave, SICache, cfgrepo, cgf-cache,
#                                       <guid>\vrs-cache (кэш версий), dbg.pfl
#   %APPDATA%\1C\1cv8\<guid>\        — настройки интерфейса, lognhist.reg
#   %PROGRAMDATA%\1C\1cv8\<guid>\    — общий кэш для всех пользователей ПК
# Данные ИБ не затрагиваются: они в файле базы или на сервере 1С.


def programdata_root() -> Path:
    base = os.environ.get('PROGRAMDATA') or r'C:\ProgramData'
    return Path(base) / '1C' / '1cv8'


def base_dirs(guid: str) -> dict:
    """Каталоги кэша одной базы, по уровням 1С.

    config — %LOCALAPPDATA%\\1C\\1cv8\\<guid>: кэш программы (конфигурации): Config,
             ConfigSave, SICache, cgf-cache, cfgrepo, КэшМодулей — пересоздаётся сам;
             настройками не является, «кэш конфигуратора» живёт здесь же (ConfigSave/cgf-cache).
    user   — %APPDATA%\\1C\\1cv8\\<guid>: настройки пользователя (1cv8.pfl, def.usr,
             положения окон/форм) — удаление сбрасывает интерфейсные настройки.
    program — %PROGRAMDATA%\\1C\\1cv8\\<guid>: общий кэш для всех пользователей ПК.
    """
    cfg = []
    p = local_root() / guid
    if p.is_dir():
        cfg.append(p)
    usr = []
    p = roaming_root() / guid
    if p.is_dir():
        usr.append(p)
    prog = []
    p = programdata_root() / guid
    if p.is_dir():
        prog.append(p)
    return {'config': cfg, 'user': usr, 'program': prog}


def base_info(guid: str, budget: float = 2.0) -> dict:
    """Размеры кэша базы по уровням. budget — сколько секунд тратить на обход каждого каталога."""
    dirs = base_dirs(guid)
    out = {}
    for what in ('config', 'user', 'program'):
        total = files = 0
        partial = False
        for d in dirs[what]:
            s = size_of(d, budget=budget)
            total += s['bytes']
            files += s['files']
            partial = partial or s['partial']
        out[what] = {'bytes': total, 'human': human(total), 'files': files,
                     'dirs': len(dirs[what]), 'partial': partial,
                     'paths': [str(d) for d in dirs[what]]}
    return out


def clear_base(guid: str, what: str = 'config') -> dict:
    """Удалить кэш базы: what=config (кэш программы/конфигурации, LOCALAPPDATA)
    | user (настройки, APPDATA) | program (PROGRAMDATA) | both (всё)."""
    guid = (guid or '').strip()
    if not guid:
        raise ValueError('не передан guid базы')
    want = ('config', 'user', 'program') if what in ('both', 'all', '') else (what,)
    for w in want:
        if w not in ('config', 'user', 'program'):
            raise ValueError("what: config | user | program | both")
    dirs = base_dirs(guid)
    freed = 0
    removed = []
    errors = []
    found = 0
    for w in want:
        for d in dirs[w]:
            found += 1
            r = _rmtree(d)
            freed += r['bytes']
            if r['removed']:
                removed.append(str(d))
            errors.extend(r['errors'])
    return {'ok': not errors or bool(removed), 'what': ','.join(want), 'guid': guid,
            'dirs_found': found, 'dirs_removed': len(removed), 'removed': removed[:10],
            'freed': freed, 'freed_human': human(freed), 'errors': errors[:10]}


# ---- мёртвые кэши: GUID-каталоги, не привязанные ни к одной базе ----------------
# Имена подкаталогов 1cv8 — это ID информационных баз из ibases.v8i (их же пишет 1С
# в свои кэши). Если GUID-каталог есть, а секции с таким ID в списке баз нет —
# база удалена/переименована давно, кэш остался («мёртвый»).

_GUID_DIR = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _cache_roots() -> list[Path]:
    return [r for r in (local_root(), roaming_root(), programdata_root()) if r.is_dir()]


def dead_caches(known: set[str], limit: int = 400) -> dict:
    """Кэш-каталоги, чей GUID не в known (ID баз и папок из ibases.v8i + config)."""
    known = {g.strip().lower() for g in (known or set()) if g}
    items = []
    total = 0
    for root in _cache_roots():
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or not _GUID_DIR.match(e.name.strip().lower()):
                    continue
                if e.name.strip().lower() in known:
                    continue
                s = size_of(Path(e.path), budget=1.0)
                mtime = datetime.fromtimestamp(e.stat(follow_symlinks=False).st_mtime).strftime('%Y-%m-%d')
                items.append({'guid': e.name.strip().lower(), 'path': e.path,
                              'bytes': s['bytes'], 'human': s['human'], 'files': s['files'],
                              'mtime': mtime})
                total += s['bytes']
            except OSError:
                continue
    items.sort(key=lambda x: -x['bytes'])
    return {'count': len(items), 'bytes': total, 'human': human(total),
            'items': items[:limit], 'total_count': len(items)}


def clear_dead(known: set[str], guids: list[str] | None = None) -> dict:
    """Удалить мёртвые кэши (все, либо только перечисленные в guids)."""
    known = {g.strip().lower() for g in (known or set()) if g}
    want = {g.strip().lower() for g in (guids or [])} if guids else None
    freed = 0
    removed = 0
    errors = []
    for root in _cache_roots():
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                g = e.name.strip().lower()
                if not e.is_dir(follow_symlinks=False) or not _GUID_DIR.match(g):
                    continue
                if g in known:
                    continue
                if want is not None and g not in want:
                    continue
                r = _rmtree(Path(e.path))
                freed += r['bytes']
                if r['removed']:
                    removed += 1
                errors.extend(r['errors'])
            except OSError:
                continue
    return {'ok': True, 'removed': removed, 'freed': freed, 'freed_human': human(freed),
            'errors': errors[:10]}
