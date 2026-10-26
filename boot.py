# -*- coding: utf-8 -*-
"""Запуск и перезапуск BaseLauncher .





Режимы:
  pythonw boot.py               — поднять сервер (если не запущен) и показать окно
  pythonw boot.py --wait-down   — дождаться, пока старый сервер освободит порт,
                                  затем поднять и показать окно (используется при перезапуске)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('BL_PORT') or 8797)
BASE = 'http://127.0.0.1:%d' % PORT
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PYTHONW = Path(sys.executable).with_name('pythonw.exe')


def state(timeout=1.5):
    try:
        return json.loads(OP.open(BASE + '/api/state', timeout=timeout).read().decode())
    except Exception:                                          # noqa: BLE001
        return None


def post(path, timeout=45):
    req = urllib.request.Request(BASE + path, data=b'{}',
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        return json.loads(OP.open(req, timeout=timeout).read().decode())
    except Exception as e:                                     # noqa: BLE001
        return {'error': f'{type(e).__name__}: {str(e)[:80]}'}


def log(msg):
    try:
        from core.paths import DATA_DIR
        with open(DATA_DIR / 'boot.log', 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    except OSError:
        pass


def main():
    wait_down = '--wait-down' in sys.argv
    if wait_down:
        for _ in range(40):                     # до 20 с: старый процесс выходит
            if state(timeout=1.0) is None:
                break
            time.sleep(0.5)

    if state() is None:
        exe = PYTHONW if PYTHONW.is_file() else Path(sys.executable)
        log(f'старт сервера: {exe}')
        subprocess.Popen([str(exe), 'run_server.py'], cwd=str(ROOT), close_fds=True,
                         creationflags=0x00000008)                  # DETACHED_PROCESS
        for _ in range(80):                     # до 40 с
            time.sleep(0.5)
            if state():
                break
        log('сервер поднялся' if state() else 'сервер не ответил')
    else:
        log('сервер уже работает')

    r = post('/api/starter/focus')
    log(f'focus → {r}')


if __name__ == '__main__':
    main()
