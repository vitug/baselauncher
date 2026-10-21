# -*- coding: utf-8 -*-
"""REST-слой BaseLauncher (FastAPI). Только 127.0.0.1."""
import subprocess

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from core import importer, launcher

app = FastAPI(title='BaseLauncher', version='0.1.0')

STATIC = FileResponse(__import__('pathlib').Path(__file__).parent / 'static' / 'index.html')


@app.get('/')
def index():
    return STATIC


@app.get('/api/state')
def state():
    """Полное состояние: базы (слитые с настройками SM), платформы, слоты."""
    return importer.build_state()


@app.post('/api/dryrun')
async def dryrun(req: Request):
    body = await req.json()
    st = importer.build_state()
    base = importer.find_base(st, body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    exe = body.get('exe') or base['exe']
    try:
        argv = launcher.build_argv(exe, body.get('mode', 'enterprise'), base['connect'],
                                   body.get('extra_keys'))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return {'cmd': subprocess.list2cmdline(argv), 'argv': argv}


@app.post('/api/launch')
async def launch(req: Request):
    body = await req.json()
    st = importer.build_state()
    base = importer.find_base(st, body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    exe = body.get('exe') or base['exe']
    try:
        argv = launcher.build_argv(exe, body.get('mode', 'enterprise'), base['connect'],
                                   body.get('extra_keys'))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    pid = launcher.launch(argv)
    launcher.log_launch(base['name'], body.get('mode', 'enterprise'), pid, argv)
    return {'ok': True, 'pid': pid, 'cmd': subprocess.list2cmdline(argv)}


@app.get('/api/log')
def log(n: int = 50):
    return launcher.tail_log(n)
