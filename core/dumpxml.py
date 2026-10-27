# -*- coding: utf-8 -*-
"""Пакетная выгрузка конфигураций файловых баз в XML (ibcmd infobase config export).

Только stdlib. Одна фоновая задача на сервер: базы обрабатываются по очереди,
состояние опрашивается из REST. Открытые базы пропускаются (ibcmd не может взять
исключительную блокировку, пока жив сеанс — см. вики-заметку про airgap-dump).
"""
import datetime
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from core import config, dbtools, loadxml

JOB = {'state': 'idle'}                     # единственная задача выгрузки
_LOCK = threading.Lock()
TIMEOUT_PER_BASE = 15 * 60                 # сек: ibcmd без --user может ждать ввода с консоли вечно

# имя каталога для базы: безопасное для файловой системы
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str, used: set) -> str:
    s = _BAD.sub('_', (name or '').strip())[:80].rstrip('. ')
    if not s:
        s = 'base'
    out, i = s, 2
    while out.lower() in used:
        out = '%s (%d)' % (s, i)
        i += 1
    used.add(out.lower())
    return out


def state() -> dict:
    with _LOCK:
        return dict(JOB)


def start(items: list, root: str) -> dict:
    """items: [{guid, name, path, exe}] — только файловые базы. Возвращает {ok} или {error}."""
    with _LOCK:
        if JOB.get('state') == 'running':
            return {'error': 'выгрузка уже идёт — дождитесь окончания'}
    root = (root or '').strip().strip('"')
    if not root:
        return {'error': 'не указан каталог для выгрузки'}
    rootdir = Path(root)
    try:
        rootdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {'error': 'не создать каталог %s: %s' % (rootdir, e)}
    if not items:
        return {'error': 'не выбрано ни одной базы'}
    cfg = config.load()
    queue = []
    for it in items:
        guid = it.get('guid') or ''
        base = it
        info = dbtools.disk_base(base)
        if not info['ok']:
            continue                           # не файловая — в очередь не берём
        ibcmd = dbtools.find_tool(base.get('exe') or '', 'ibcmd.exe')
        if not ibcmd:
            continue
        ent = config.get_base(cfg, guid) or {}
        user = (ent.get('user_cfg') or ent.get('user') or '').strip()
        pwd = config.unseal(ent.get('pwd_cfg') or '') or config.unseal(ent.get('pwd') or '')
        queue.append({'guid': guid, 'name': base.get('name') or guid, 'path': info['path'],
                      'ibcmd': ibcmd, 'user': user, 'pwd': pwd,
                      'running': bool(base.get('running'))})
    if not queue:
        return {'error': 'среди выбранного нет файловых баз с найденным ibcmd.exe '
                         '(нужна платформа 8.3.17+ рядом с клиентом базы)'}
    used = {p.name.lower() for p in rootdir.iterdir() if p.is_dir()}
    for q in queue:
        q['out'] = str(rootdir / _safe(q['name'], used))
    with _LOCK:
        JOB.update(state='running', root=str(rootdir), total=len(queue), done=0,
                   ok=0, skip=0, fail=0,
                   started=datetime.datetime.now().isoformat(timespec='seconds'),
                   finished='', current='', results=[])
    threading.Thread(target=_work, args=(queue,), daemon=True, name='bl-dumpxml').start()
    return {'ok': True, 'total': len(queue), 'root': str(rootdir)}


def _work(queue: list):
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
            out = Path(q['out'])
            tmp = None
            try:
                if out.is_dir() and any(out.iterdir()):
                    # ibcmd отказывает в непустой каталог («Каталог не пуст») — выгружаем
                    # во временный подкаталог и кладём поверх, как конфигуратор
                    tmp = out.with_suffix(out.suffix + '.tmp-export')
                    shutil.rmtree(tmp, ignore_errors=True)
                    tmp.mkdir(parents=True)
                    target = tmp
                else:
                    out.mkdir(parents=True, exist_ok=True)
                    target = out
                cmd = [q['ibcmd'], 'infobase', 'config', 'export', str(target),
                       '--db-path=%s' % q['path'], '--force']
                if q['user']:
                    cmd.append('--user=%s' % q['user'])
                if q['pwd']:
                    cmd.append('--password=%s' % q['pwd'])
                pr = subprocess.run(cmd, input=b'', capture_output=True, timeout=TIMEOUT_PER_BASE,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                text = (dbtools._decode(pr.stdout or b'') +
                        dbtools._decode(pr.stderr or b'')).strip()
                if pr.returncode != 0:
                    raise RuntimeError('ibcmd вернул код %s: %s'
                                       % (pr.returncode, text[-800:] or 'без вывода'))
                files = sum(len(fs) for _, _, fs in os.walk(target))
                if not files:
                    raise RuntimeError('код 0, но файлов в выгрузке нет')
                if tmp and target == tmp:
                    for base, _dirs, fs in os.walk(target):
                        rel = os.path.relpath(base, target)
                        dst = out if rel == '.' else out / rel
                        dst.mkdir(parents=True, exist_ok=True)
                        for f in fs:
                            shutil.copy2(base + '\\' + f, str(dst) + '\\' + f)
                    shutil.rmtree(tmp, ignore_errors=True)
                try:                                   # манифест для режима загрузки «изменённое»
                    loadxml.write_manifest(q['guid'], str(config.DATA_DIR), out)
                except Exception:                       # noqa: BLE001
                    pass
                res(q['guid'], q['name'], state='ok', out=str(out), files=files)
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
