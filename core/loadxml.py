# -*- coding: utf-8 -*-
"""Пакетная загрузка конфигурации в файловые базы из XML (ibcmd infobase config import).

Только stdlib. Одна фоновая задача на сервер. Режимы:
  full    — полная загрузка (замена всей конфигурации базы);
  changed — частичная: файлы, изменившиеся с прошлой ВЫГРУЗКИ этой же базы
            (манифест пишет core.dumpxml при каждой выгрузке: mtime+size по файлам);
  files   — частичная: вручную заданный список (относительные пути или имена объектов
            метаданных вида «Справочник.Номенклатура» — разворачиваются в файл+поддерево).
После загрузки по желанию применяется к БД (ibcmd infobase config apply --force).
"""
import datetime
import json
import subprocess
import threading
from pathlib import Path

from core import config, dbtools

JOB = {'state': 'idle'}
_LOCK = threading.Lock()
TIMEOUT_PER_BASE = 15 * 60                 # сек: ibcmd без --user может ждать ввода с консоли вечно

# имена объектов метаданных -> каталоги иерархической выгрузки
_TYPE_DIRS = {
    'справочник': 'Catalogs', 'документ': 'Documents', 'отчет': 'Reports',
    'отчёт': 'Reports', 'обработка': 'DataProcessors', 'перечисление': 'Enums',
    'константа': 'Constants', 'общиймодуль': 'CommonModules', 'общийреквизит': 'CommonAttributes',
    'регистрсведений': 'InformationRegisters', 'регистрнакопления': 'AccumulationRegisters',
    'регистрбухгалтерии': 'AccountingRegisters', 'регистррасчета': 'CalculationRegisters',
    'регистррасчёта': 'CalculationRegisters', 'плансчетов': 'ChartsOfAccounts',
    'планвидовхарактеристик': 'ChartsOfCharacteristicTypes', 'планвидоврасчета': 'ChartsOfCalculationTypes',
    'планвидоврасчёта': 'ChartsOfCalculationTypes', 'планобмена': 'ExchangePlans',
    'задача': 'Tasks', 'бизнеспроцесс': 'BusinessProcesses', 'характеристика': 'ChartsOfCharacteristicTypes',
    'роль': 'Roles', 'макет': 'CommonTemplates', 'регламентноезадание': 'ScheduledJobs',
    'внешнийисточникданных': 'ExternalDataSources', 'параметрсеанса': 'SessionParameters',
    'функциональнаяопция': 'FunctionalOptions', 'обменданными': 'ExchangePlans',
}


def state() -> dict:
    with _LOCK:
        return dict(JOB)


def _manifest_path(guid: str, data_dir: str) -> Path:
    return Path(data_dir) / 'manifests' / ('%s.json' % guid)


def write_manifest(guid: str, data_dir: str, dump_root: Path):
    """Манифест выгрузки: rel-путь -> [mtime, size] — база для режима «изменённое»."""
    files = {}
    for f in dump_root.rglob('*'):
        if f.is_file():
            try:
                st = f.stat()
            except OSError:
                continue
            files[f.relative_to(dump_root).as_posix()] = [st.st_mtime, st.st_size]
    mp = _manifest_path(guid, data_dir)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({'guid': guid, 'root': str(dump_root), 'ts': datetime.datetime.now().isoformat(timespec='seconds'),
                              'files': files}, ensure_ascii=False), encoding='utf-8')


def read_manifest(guid: str, data_dir: str) -> dict:
    mp = _manifest_path(guid, data_dir)
    if not mp.is_file():
        return {}
    try:
        return json.loads(mp.read_text(encoding='utf-8-sig'))
    except Exception:                                  # noqa: BLE001
        return {}


def expand_objects(src: Path, names: list) -> list:
    """«Справочник.Номенклатура» -> [Catalogs/Номенклатура.xml, Catalogs/Номенклатура/…].

    Узнаётся и готовый относительный путь (Catalogs/Номенклатура.xml). Возвращает rel-пути.
    """
    out = []
    for raw in names:
        s = (raw or '').strip().strip('"').replace('\\', '/')
        if not s:
            continue
        if '/' in s:                                   # уже путь
            if (src / s).exists():
                out.append(s)
            continue
        parts = s.split('.')
        if len(parts) != 2:
            continue
        tdir = _TYPE_DIRS.get(parts[0].lower())
        if not tdir:
            continue
        name = parts[1]
        head = '%s/%s.xml' % (tdir, name)
        if (src / head).is_file():
            out.append(head)
        sub = src / tdir / name
        if sub.is_dir():
            for f in sub.rglob('*'):
                if f.is_file():
                    out.append(f.relative_to(src).as_posix())
    # уникальные, с сохранением порядка
    seen, res = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res


def changed_files(src: Path, manifest: dict) -> list:
    """Файлы, изменившиеся с прошлой выгрузки: новые, изменённые (mtime/size) и удалённые
    (удалённые частичная загрузка не убирает — только предупреждаем)."""
    old = manifest.get('files') or {}
    cur = {}
    for f in src.rglob('*'):
        if f.is_file():
            try:
                st = f.stat()
            except OSError:
                continue
            cur[f.relative_to(src).as_posix()] = (st.st_mtime, st.st_size)
    changed = [p for p, v in cur.items() if p not in old or (old[p][0], old[p][1]) != v]
    return changed


def start(items: list, root: str, mode: str, files: list, update_db: bool, data_dir: str) -> dict:
    """items: [{guid, name, path, exe}] — файловые базы; root — каталог XML-исходников;
    mode: full|changed|files; files — rel-пути/имена объектов (для files)."""
    with _LOCK:
        if JOB.get('state') == 'running':
            return {'error': 'загрузка уже идёт — дождитесь окончания'}
    root = (root or '').strip().strip('"')
    if not root:
        return {'error': 'не указан каталог XML-исходников'}
    rootdir = Path(root)
    if not rootdir.is_dir():
        return {'error': 'каталога XML-исходников нет: %s' % rootdir}
    if mode not in ('full', 'changed', 'files'):
        return {'error': 'неизвестный режим: %s' % mode}
    if mode == 'files' and not files:
        return {'error': 'в режиме «заданные объекты» список пуст'}
    cfg = config.load()
    queue = []
    for it in items:
        info = dbtools.disk_base(it)
        if not info['ok']:
            continue
        ibcmd = dbtools.find_tool(it.get('exe') or '', 'ibcmd.exe')
        if not ibcmd:
            continue
        # каталог исходников базы: root/<имя> (как раскладывает наша выгрузка), иначе сам root
        sub = rootdir / _safe_name(it.get('name') or it['guid'])
        src = sub if sub.is_dir() else rootdir
        ent = config.get_base(cfg, it['guid']) or {}
        user = (ent.get('user_cfg') or ent.get('user') or '').strip()
        pwd = config.unseal(ent.get('pwd_cfg') or '') or config.unseal(ent.get('pwd') or '')
        man = read_manifest(it['guid'], data_dir)
        if mode == 'changed' and not man:
            continue                                # нет прошлой выгрузки — нечего сравнивать
        fl = []
        if mode == 'files':
            fl = expand_objects(src, files)
            if not fl:
                continue
        queue.append({'guid': it['guid'], 'name': it.get('name') or it['guid'],
                      'path': info['path'], 'ibcmd': ibcmd, 'user': user, 'pwd': pwd,
                      'src': str(src), 'mode': mode, 'files': fl, 'manifest': man,
                      'running': bool(it.get('running')),
                      'update_db': bool(update_db)})
    if not queue:
        return {'error': 'нет баз для загрузки: нужны файловые базы с ibcmd.exe; '
                         'для режима «изменённое» — ещё и манифест прошлой выгрузки '
                         '(сначала «Выгрузить конфигурации в XML…»)' if mode == 'changed'
                         else 'нет файловых баз с найденным ibcmd.exe'}
    with _LOCK:
        JOB.update(state='running', root=str(rootdir), mode=mode, total=len(queue), done=0,
                   ok=0, skip=0, fail=0,
                   started=datetime.datetime.now().isoformat(timespec='seconds'),
                   finished='', current='', results=[])
    threading.Thread(target=_work, args=(queue, data_dir), daemon=True, name='bl-loadxml').start()
    return {'ok': True, 'total': len(queue), 'root': str(rootdir), 'mode': mode}


def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    s = ''.join('_' if c in bad else c for c in (name or '').strip())[:80].rstrip('. ')
    return s or 'base'


def _run_ibcmd(cmd: list, timeout: int):
    pr = subprocess.run(cmd, input=b'', capture_output=True, timeout=timeout,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return pr


def _work(queue: list, data_dir: str):
    def res(guid, name, **kv):
        with _LOCK:
            JOB['results'].append({'guid': guid, 'name': name, **kv})
            JOB['done'] += 1
    try:
        for q in queue:
            with _LOCK:
                JOB['current'] = q['name']
            if q['running']:
                res(q['guid'], q['name'], state='skip',
                    error='база открыта (сеансы живы) — закройте и повторите')
                with _LOCK:
                    JOB['skip'] += 1
                continue
            try:
                fl = list(q['files'])
                note = ''
                if q['mode'] == 'changed':
                    fl = changed_files(Path(q['src']), q['manifest'])
                    if not fl:
                        res(q['guid'], q['name'], state='ok', files=0,
                            note='изменений с прошлой выгрузки нет')
                        with _LOCK:
                            JOB['ok'] += 1
                        continue
                cmd = [q['ibcmd'], 'infobase', 'config', 'import']
                if q['mode'] == 'full':
                    cmd.append(q['src'])
                else:
                    cmd += ['files'] + fl + ['--base-dir=%s' % q['src']]
                cmd.append('--db-path=%s' % q['path'])
                if q['user']:
                    cmd.append('--user=%s' % q['user'])
                if q['pwd']:
                    cmd.append('--password=%s' % q['pwd'])
                pr = _run_ibcmd(cmd, TIMEOUT_PER_BASE)
                text = (dbtools._decode(pr.stdout or b'') +
                        dbtools._decode(pr.stderr or b'')).strip()
                if pr.returncode != 0:
                    raise RuntimeError('ibcmd вернул код %s: %s'
                                       % (pr.returncode, text[-800:] or 'без вывода'))
                n = len(fl) if q['mode'] != 'full' else 1
                if q['mode'] == 'changed':
                    note = 'загружено файлов: %d' % n
                if q['update_db']:
                    acmd = [q['ibcmd'], 'infobase', 'config', 'apply',
                            '--db-path=%s' % q['path'], '--force']
                    if q['user']:
                        acmd.append('--user=%s' % q['user'])
                    if q['pwd']:
                        acmd.append('--password=%s' % q['pwd'])
                    ap = _run_ibcmd(acmd, TIMEOUT_PER_BASE)
                    atext = (dbtools._decode(ap.stdout or b'') +
                             dbtools._decode(ap.stderr or b'')).strip()
                    if ap.returncode != 0:
                        raise RuntimeError('загружено, но обновление БД не удалось (код %s): %s'
                                           % (ap.returncode, atext[-600:] or 'без вывода'))
                    note += ('; ' if note else '') + 'конфигурация БД обновлена'
                res(q['guid'], q['name'], state='ok', files=n or 1, note=note)
                with _LOCK:
                    JOB['ok'] += 1
            except subprocess.TimeoutExpired:
                res(q['guid'], q['name'], state='error',
                    error='ibcmd не завершился за %d мин — вероятно, ждёт ввода пользователя ИБ '
                          '(заполните пользователя/пароль Конфигуратора в свойствах базы)'
                          % (TIMEOUT_PER_BASE // 60))
                with _LOCK:
                    JOB['fail'] += 1
            except Exception as e:                          # noqa: BLE001
                res(q['guid'], q['name'], state='error', error=str(e)[:600])
                with _LOCK:
                    JOB['fail'] += 1
    finally:
        with _LOCK:
            JOB['state'] = 'done'
            JOB['current'] = ''
            JOB['finished'] = datetime.datetime.now().isoformat(timespec='seconds')
