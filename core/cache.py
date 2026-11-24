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


def _is_settings_file(name: str) -> bool:
    """Файлы настроек пользователя 1С: *.pfl (1cv8.pfl, dbg.pfl, ...) и def.usr."""
    n = (name or '').lower()
    return n.endswith('.pfl') or n == 'def.usr'


def _backup_settings(path: Path) -> list[str]:
    """Перенести *.pfl/def.usr из удаляемого каталога в соседний <имя>_backup.

    Возврат — список перенесённых файлов (пути уже в бэкапе)."""
    moved = []
    bdir = path.parent / (path.name + '_backup')
    try:
        for f in path.rglob('*'):
            try:
                if not f.is_file() or not _is_settings_file(f.name):
                    continue
                dest = bdir / f.relative_to(path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest.unlink()
                shutil.move(str(f), str(dest))
                moved.append(str(dest))
            except OSError:
                continue
    except OSError:
        pass
    return moved


def _rmtree(path: Path, backup_settings: bool = False) -> dict:
    """Удалить каталог, не падая на занятых файлах (1С может держать кэш открытым).

    backup_settings=True — перед удалением перенести файлы настроек пользователя
    (*.pfl, def.usr) в соседний <имя>_backup, чтобы очистка кэша не сбрасывала их."""
    errors = []
    before = size_of(path, budget=1.0)['bytes']
    backed_up = _backup_settings(path) if backup_settings else []

    def onerror(func, p, exc):          # noqa: ANN001
        errors.append('%s: %s' % (p, exc))

    try:
        shutil.rmtree(path, onerror=onerror)
    except Exception as e:              # noqa: BLE001
        errors.append(str(e))
    return {'removed': not path.exists(), 'bytes': before, 'errors': errors[:5],
            'error_count': len(errors), 'backed_up': backed_up}


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


def clear_base(guid: str, what: str = 'config', no_backup: bool = False) -> dict:
    """Удалить кэш базы: what=config (кэш программы/конфигурации, LOCALAPPDATA)
    | user (настройки, APPDATA) | program (PROGRAMDATA) | both (всё).

    Настройки пользователя (*.pfl, def.usr) по умолчанию НЕ теряются: перед удалением
    они переносятся в соседний <guid>_backup. no_backup=True — удалять без бэкапа
    (полный сброс настроек, как раньше)."""
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
    backed_up = []
    for w in want:
        for d in dirs[w]:
            found += 1
            # бэкап настроек: уровень user — всегда (кроме no_backup), прочие — тоже
            # защищаем (dbg.pfl в Local и общие настройки в ProgramData)
            r = _rmtree(d, backup_settings=not no_backup)
            freed += r['bytes']
            backed_up += r.get('backed_up') or []
            if r['removed']:
                removed.append(str(d))
            errors.extend(r['errors'])
    return {'ok': not errors or bool(removed), 'what': ','.join(want), 'guid': guid,
            'dirs_found': found, 'dirs_removed': len(removed), 'removed': removed[:10],
            'freed': freed, 'freed_human': human(freed), 'errors': errors[:10],
            'backed_up_count': len(backed_up), 'backed_up': backed_up[:20],
            'no_backup': bool(no_backup)}


# ---- мёртвые кэши: GUID-каталоги, не привязанные ни к одной базе ----------------
# Имена подкаталогов 1cv8 — это ID информационных баз из ibases.v8i (их же пишет 1С
# в свои кэши). Если GUID-каталог есть, а секции с таким ID в списке баз нет —
# база удалена/переименована давно, кэш остался («мёртвый»).

_GUID_DIR = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _cache_roots() -> list[Path]:
    """Корни кэшей ДЛЯ УДАЛЕНИЯ: только профиль текущего пользователя
    (Local/Roaming/ProgramData). Кэши чужих пользователей ПК не удаляем никогда —
    даже под админом: их «мёртвость» определяется по нашему ibases.v8i, а у юзера
    может быть свой список баз."""
    return [r for r in (local_root(), roaming_root(), programdata_root()) if r.is_dir()]


def _scan_roots() -> list[tuple[Path, str]]:
    """Корни кэшей для осмотра: свой профиль (owner='') + Local-кэши ДРУГИХ
    пользователей этого ПК (C:\\Users\\<юзер>\\AppData\\Local\\1C\\1cv8) — админ
    видит чужие кэши в списке, но удалить их нельзя (см. _cache_roots).
    Чужие каталоги могут требовать прав — недоступные молча пропускаем."""
    roots = [(r, '') for r in (local_root(), roaming_root(), programdata_root()) if r.is_dir()]
    users = Path.home().parent          # C:\Users
    mine = Path.home().name.lower()
    try:
        entries = list(os.scandir(users))
    except OSError:
        return roots
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False) or e.name.lower() in (mine, 'public', 'default', 'defaultuser', 'all users'):
                continue
            p = Path(e.path) / 'AppData' / 'Local' / '1C' / '1cv8'
            if p.is_dir():
                roots.append((p, e.name))
        except OSError:
            continue
    return roots


def dead_caches(known: set[str], limit: int = 400) -> dict:
    """Кэш-каталоги, чей GUID не в known (ID баз и папок из ibases.v8i + config).

    Осматриваются и чужие профили (админ видит), в item поле user — чей профиль
    ('' = текущий пользователь). Удаляются чужие никогда (clear_dead — только свои)."""
    known = {g.strip().lower() for g in (known or set()) if g}
    items = []
    total = 0
    for root, owner in _scan_roots():
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
                              'user': owner,
                              'deletable': not owner,
                              'bytes': s['bytes'], 'human': s['human'], 'files': s['files'],
                              'mtime': mtime})
                total += s['bytes']
            except OSError:
                continue
    items.sort(key=lambda x: -x['bytes'])
    return {'count': len(items), 'bytes': total, 'human': human(total),
            'items': items[:limit], 'total_count': len(items)}


def clear_dead(known: set[str], guids: list[str] | None = None,
               no_backup: bool = False) -> dict:
    """Удалить мёртвые кэши (все, либо только перечисленные в guids).

    Только профиль текущего пользователя (см. _cache_roots) — чужие кэши не удаляются.
    Настройки (*.pfl, def.usr) перед удалением переносятся в <guid>_backup рядом,
    no_backup=True — без бэкапа."""
    known = {g.strip().lower() for g in (known or set()) if g}
    want = {g.strip().lower() for g in (guids or [])} if guids else None
    freed = 0
    removed = 0
    errors = []
    backed_up = []
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
                r = _rmtree(Path(e.path), backup_settings=not no_backup)
                freed += r['bytes']
                backed_up += r.get('backed_up') or []
                if r['removed']:
                    removed += 1
                errors.extend(r['errors'])
            except OSError:
                continue
    return {'ok': True, 'removed': removed, 'freed': freed, 'freed_human': human(freed),
            'errors': errors[:10], 'backed_up_count': len(backed_up),
            'backed_up': backed_up[:20], 'no_backup': bool(no_backup)}
