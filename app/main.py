# -*- coding: utf-8 -*-
"""REST-слой BaseLauncher (FastAPI). Только 127.0.0.1."""
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from core import hotkey, importer, launcher, platforms_share, procs, v8i_writer

app = FastAPI(title='BaseLauncher', version='0.9.1')
PORT = int(__import__('os').environ.get('BL_PORT') or 8797)

INDEX = Path(__file__).parent / 'static' / 'index.html'


@app.on_event('startup')
def _startup():
    """Глобальная клавиша Ctrl+Shift+D — поднять окно стартера."""
    hotkey.start(PORT)


@app.get('/')
def index():
    return FileResponse(INDEX)


@app.get('/api/state')
def state():
    """Полное состояние: базы (слитые с настройками SM), папки, платформы, слоты."""
    return importer.build_state()


def _base_or_404(guid: str):
    st = importer.build_state()
    base = importer.find_base(st, guid)
    return (st, base) if base else (None, None)


@app.get('/api/base/{guid}')
def base_detail(guid: str):
    st, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    return {'base': base, 'platforms': st['platforms']}


@app.put('/api/base/{guid}')
async def base_save(guid: str, req: Request):
    """Сохранить наши настройки базы в data/overrides.json.

    Поля: exe ('' = авто), thin (bool), storage {dir,user,password}, extra_keys [..].
    Отсутствующие поля не трогаются; {'clear': true} — сбросить всё.
    """
    _, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    body = await req.json()

    ov = importer.load_overrides()
    bases = ov.setdefault('bases', {})
    guid = guid.lower()
    if body.get('clear'):
        bases.pop(guid, None)
    else:
        cur = bases.setdefault(guid, {})
        if 'exe' in body:
            if body['exe']:
                cur['exe'] = body['exe']
            else:
                cur.pop('exe', None)
        if 'thin' in body:
            cur['thin'] = bool(body['thin'])
        if 'fav' in body:
            cur['fav'] = bool(body['fav'])
        if 'mode' in body:  # режим по умолчанию для клика по строке
            if body['mode'] in ('enterprise', 'designer'):
                cur['mode'] = body['mode']
            else:
                cur.pop('mode', None)
        for fld in ('name', 'descr', 'user', 'pwd'):
            if fld in body:
                if body[fld]:
                    cur[fld] = body[fld]
                else:
                    cur.pop(fld, None)
        if 'extra_keys' in body:
            cur['extra_keys'] = [k for k in body['extra_keys'] if k]
        if 'storage' in body:
            s = body['storage'] or {}
            if s.get('dir'):
                cur['storage'] = {'dir': s.get('dir', ''),
                                  'user': s.get('user', ''),
                                  'password': s.get('password', '')}
            else:
                cur.pop('storage', None)
        if not cur:
            bases.pop(guid, None)
    importer.OVERRIDES_PATH.write_text(
        json.dumps(ov, ensure_ascii=False, indent=1), encoding='utf-8')

    st = importer.build_state()
    return {'ok': True, 'base': importer.find_base(st, guid)}


def _build_cmd_from_body(body: dict, base: dict):
    exe = body.get('exe') or base['exe']
    mode = body.get('mode', 'enterprise')
    extra = body.get('extra_keys')
    storage = body.get('storage')
    if storage is None:
        storage = base.get('storage') or {}
    user = body.get('user') or base.get('user') or ''
    pwd = body.get('pwd') or base.get('pwd') or ''
    try:
        return launcher.build_cmd(exe, mode, base['connect'], extra, storage,
                                  user, pwd), mode
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400), mode


@app.post('/api/dryrun')
async def dryrun(req: Request):
    body = await req.json()
    _, base = _base_or_404(body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    cmd, _ = _build_cmd_from_body(body, base)
    if isinstance(cmd, JSONResponse):
        return cmd
    return {'cmd': cmd}


@app.post('/api/launch')
async def launch(req: Request):
    body = await req.json()
    _, base = _base_or_404(body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    cmd, mode = _build_cmd_from_body(body, base)
    if isinstance(cmd, JSONResponse):
        return cmd
    exe = body.get('exe') or base.get('exe')
    if not exe or not Path(exe).is_file():
        return JSONResponse(
            {'error': f'exe платформы не найден на диске: {exe or "(не определён)"}'},
            status_code=400)
    bpath = (base.get('path') or '').strip()
    if bpath and not Path(bpath).is_dir():
        return JSONResponse(
            {'error': f'папки базы нет на диске: {bpath}'}, status_code=400)
    # этот режим уже открыт? тогда не плодим второй сеанс без явного согласия
    if not body.get('force'):
        running = _running_pid(base, mode)
        if running:
            return JSONResponse(
                {'error': 'already_running', 'running': True, 'pid': running,
                 'mode': mode,
                 'message': ('база уже открыта в этом режиме (PID %d)' % running)},
                status_code=409)
    pid = launcher.launch(cmd)
    launcher.log_launch(base['name'], mode, pid, cmd, base['guid'])
    return {'ok': True, 'pid': pid, 'cmd': cmd}


def _running_pid(base: dict, mode: str) -> int | None:
    """PID живого сеанса этой базы в нужном режиме (или None)."""
    for s in procs.running_sessions():
        if s['mode'] != mode:
            continue
        if base.get('path') and s['path'] and procs._norm_path(s['path']) == procs._norm_path(base['path']):
            return s['pid']
        if base.get('server') and s['server'] and s['server'].lower() == base['server'].lower() \
                and (s['ref'] or '').lower() == (base.get('ref') or '').lower():
            return s['pid']
        if s['name'] and s['name'].strip().lower() == base['name'].strip().lower():
            return s['pid']
    return None


@app.get('/api/log')
def log(n: int = 50):
    return launcher.tail_log(n)


@app.post('/api/base/new')
async def base_new(req: Request):
    """Добавить базу в ibases.v8i (1С увидит её в своём лаунчере)."""
    body = await req.json()
    name = (body.get('name') or '').strip()
    folder = (body.get('folder') or '/').strip() or '/'
    path = (body.get('path') or '').strip()
    server = (body.get('server') or '').strip()
    ref = (body.get('ref') or '').strip()
    if not name:
        return JSONResponse({'error': 'укажите имя базы'}, status_code=400)
    if path:
        connect = 'File="%s"' % path.rstrip('\\/')
    elif server and ref:
        connect = 'Srvr="%s";Ref="%s"' % (server, ref)
    else:
        return JSONResponse(
            {'error': 'нужен путь к папке базы (файловая) или сервер + имя базы (серверная)'},
            status_code=400)
    try:
        guid = v8i_writer.add_base(name, connect, folder)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'guid': guid, 'base': importer.find_base(st, guid)}


@app.post('/api/folder/new')
async def folder_new(req: Request):
    """Добавить папку в дерево ibases.v8i."""
    body = await req.json()
    name = (body.get('name') or '').strip()
    parent = (body.get('parent') or '/').strip() or '/'
    if not name:
        return JSONResponse({'error': 'укажите имя папки'}, status_code=400)
    try:
        guid = v8i_writer.add_folder(name, parent)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'guid': guid, 'folders': len(st['folders'])}


@app.delete('/api/base/{guid}')
def base_delete(guid: str):
    """Удалить базу/пустую папку из ibases.v8i."""
    try:
        name = v8i_writer.delete(guid)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'deleted': name, 'bases': len(st['bases'])}


@app.post('/api/activate')
async def activate(req: Request):
    """Переключиться на уже открытый сеанс: вывести его окно наверх."""
    body = await req.json()
    try:
        pid = int(body.get('pid') or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return JSONResponse({'error': 'не передан pid сеанса'}, status_code=400)
    r = procs.focus_window(pid)
    if not r.get('ok'):
        return JSONResponse(r, status_code=400)
    return r


@app.post('/api/starter/focus')
def starter_focus():
    """Поднять окно стартера (то же, что глобальная Ctrl+Shift+D)."""
    r = hotkey.focus_starter(PORT)
    return JSONResponse(r, status_code=200 if r.get('ok') else 400)


@app.get('/api/hotkey')
def hotkey_status():
    return hotkey.status()


@app.post('/api/platform/probe')
async def platform_probe(req: Request):
    """Есть ли дистрибутив версии на шаре и не установлена ли она уже."""
    body = await req.json()
    v = (body.get('version') or '').strip()
    if not v:
        return JSONResponse({'error': 'не передана версия платформы'}, status_code=400)
    return platforms_share.probe(v)


@app.post('/api/platform/install')
async def platform_install(req: Request):
    """Поставить отсутствующую платформу с шары (UAC подтверждает пользователь)."""
    body = await req.json()
    v = (body.get('version') or '').strip()
    kind = (body.get('kind') or 'setup64full').strip()
    if not v:
        return JSONResponse({'error': 'не передана версия платформы'}, status_code=400)
    try:
        return platforms_share.install(v, kind)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)


@app.post('/api/platform/reveal')
async def platform_reveal(req: Request):
    """Открыть папку дистрибутива на шаре в проводнике."""
    body = await req.json()
    v = (body.get('version') or '').strip()
    return platforms_share.reveal(v)


@app.post('/api/shutdown')
def shutdown():
    """Штатная остановка сервера (освобождает порт без сирот-воркеров).

    Нужна потому, что kill обёртки background-сессии оставляет python-ребёнка
    с занятым портом: следующий запуск падает с WinError 10048/10013.
    """
    import os
    import threading

    def _bye():
        os._exit(0)

    threading.Timer(0.3, _bye).start()
    return {'ok': True, 'bye': 'shutting down'}
