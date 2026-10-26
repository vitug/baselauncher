# -*- coding: utf-8 -*-
"""Dev-инстанс BaseLauncher: отдельная версия для разработки.

    python dev.py

Что делает:
  * поднимает второй сервер на 127.0.0.1:8798 (BL_PORT=8798);
  * данные — песочница data_dev/ (BL_DATA): свой config.json, логи, история,
    окно и т.д. — рабочий инстанс на :8797 и его data/ не затрагиваются;
  * дерево баз — СВЕЖАЯ КОПИЯ боевого ibases.v8i (BL_IBASES): выглядит как
    настоящее, но все записи/переименования из dev-версии уходят в копию
    и выбрасываются при следующем запуске dev.py;
  * глобальная клавиша Ctrl+Shift+S и трей НЕ занимаются (они у рабочего
    инстанса), а окно открывается на отдельном виртуальном столе Windows
    «BaseLauncher DEV» (core/vdesk.py; переключение столов Win+Ctrl+стрелки,
    обзор столов Win+Ctrl+Tab).

Остановка — Ctrl+C в этой консоли (или POST /api/shutdown на 8798).
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

DEV_PORT = 8798
DATA_DEV = os.path.join(ROOT, 'data_dev')
V8I_REAL = os.path.expandvars(r'%APPDATA%\1C\1CEStart\ibases.v8i')
V8I_COPY = os.path.join(DATA_DEV, 'ibases.v8i')


def _log(msg):
    print('[dev] ' + msg, flush=True)


def prepare():
    os.makedirs(DATA_DEV, exist_ok=True)
    if os.path.isfile(V8I_REAL):
        shutil.copyfile(V8I_REAL, V8I_COPY)   # свежая копия каждый запуск
        _log('дерево баз: свежая копия ibases.v8i (%d КБ)'
             % (os.path.getsize(V8I_COPY) // 1024))
    else:
        _log('боевой ibases.v8i не найден — dev увидит только своё')


def wait_up(timeout=30):
    for _ in range(timeout * 2):
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d/api/state' % DEV_PORT, timeout=1) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def main():
    prepare()
    env = dict(os.environ,
               BL_PORT=str(DEV_PORT),
               BL_DATA=DATA_DEV,
               BL_IBASES=V8I_COPY,
               BL_DEV='1',
               BL_RELOAD='1')      # автоперезагрузка кода при правках (разработка тут)
    _log('запуск сервера на :%d (песочница %s)' % (DEV_PORT, DATA_DEV))
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, 'run_server.py')],
                         cwd=ROOT, env=env)
    try:
        if wait_up():
            _log('готов: http://127.0.0.1:%d — окно откроется на виртуальном '
                 'столе «BaseLauncher DEV»' % DEV_PORT)
            _log('стоп: Ctrl+C')
        else:
            _log('сервер не поднялся за 30 с — смотри data_dev/server.log')
        p.wait()
    except KeyboardInterrupt:
        _log('останова... шлю /api/shutdown')
        try:
            req = urllib.request.Request(
                'http://127.0.0.1:%d/api/shutdown' % DEV_PORT, method='POST')
            urllib.request.urlopen(req, timeout=3)
        except OSError:
            p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
        _log('остановлен')


if __name__ == '__main__':
    main()
