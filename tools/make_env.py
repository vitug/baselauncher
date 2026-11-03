# -*- coding: utf-8 -*-
"""Создать полностью независимое окружение запуска BaseLauncher.

Свой Python (uv сам скачивает standalone-CPython — не привязан к питонам хоста)
+ venv в ./env/ + пакеты из requirements.txt. После этого сервер запускается
из этого окружения и не зависит ни от чьих venv/WinPython:

    env\\Scripts\\pythonw.exe run_server.py          (прод, без окна)
    env\\Scripts\\python.exe  run_server.py          (с консолью)
    env\\Scripts\\python.exe  dev.py                 (dev :8798)

Требования: установленный uv (https://docs.astral.sh/uv/). Прокси берётся из
окружения (HTTP_PROXY/HTTPS_PROXY) или передаётся --proxy.
Повторный запуск обновляет пакеты, Python не перекачивает.

Запуск из любого питона, даже без него — есть start_env.bat.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / 'env'
PY_VER = '3.12'          # standalone-CPython, который скачает uv
REQS = ROOT / 'requirements.txt'


def find_uv() -> str:
    uv = shutil.which('uv') or shutil.which('uv.exe')
    if uv:
        return uv
    # типичные места установки uv на Windows
    for p in (Path.home() / '.local' / 'bin' / 'uv.exe',
              Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'uv' / 'uv.exe',
              Path(os.environ.get('USERPROFILE', '')) / '.cargo' / 'bin' / 'uv.exe'):
        if p.is_file():
            return str(p)
    sys.exit('uv не найден. Установи: winget install astral-sh.uv  (или pip install uv)')


def run(cmd, **kw) -> None:
    cmd = [str(c) for c in cmd]
    print('>>', ' '.join(cmd), flush=True)
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        sys.exit('команда упала (код %d): %s' % (r.returncode, ' '.join(cmd)))


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    ap.add_argument('--proxy', default=os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY'),
                    help='прокси для скачивания Python и пакетов (по умолчанию — из окружения)')
    ap.add_argument('--python', default=PY_VER, help='версия Python (умолчание %s)' % PY_VER)
    ap.add_argument('--fresh', action='store_true', help='пересоздать env с нуля')
    a = ap.parse_args()

    uv = find_uv()
    env = dict(os.environ)
    if a.proxy:
        env['HTTP_PROXY'] = env['HTTPS_PROXY'] = a.proxy

    if a.fresh and ENV.is_dir():
        shutil.rmtree(ENV)

    # 1) venv со СВОИМ Python: uv скачает standalone-CPython в свой кэш
    #    --clear: повторный запуск пересоздаёт env на месте (идемпотентно)
    try:
        run([uv, 'venv', '--python', a.python, '--clear', ENV])
    except SystemExit:
        if ENV.is_dir():
            print()
            print('env/ занят — похоже, из него запущен сервер BaseLauncher.')
            print('Останови его (иконка в трее → выход) и повтори.')
        raise

    # 2) пакеты
    run([uv, 'pip', 'install', '--python', ENV / 'Scripts' / 'python.exe', '-r', REQS])

    # 3) контрольный запуск: импорт fastapi/uvicorn из нового окружения
    chk = subprocess.run([str(ENV / 'Scripts' / 'python.exe'), '-c',
                          'import fastapi, uvicorn, win32api; import sys; '
                          'print("env ok:", sys.version.split()[0])'],
                         capture_output=True, text=True)
    print(chk.stdout.strip() or chk.stderr.strip())
    if chk.returncode != 0:
        sys.exit('проверка импорта не прошла')

    print()
    print('Окружение готово:', ENV)
    print('Запуск прод:   %s\\Scripts\\pythonw.exe run_server.py' % ENV)
    print('Запуск dev:    %s\\Scripts\\python.exe dev.py' % ENV)


if __name__ == '__main__':
    main()
