# -*- coding: utf-8 -*-
"""Точка входа BaseLauncher: uvicorn на 127.0.0.1, порт из BL_PORT (умолчание 8797).

8797, а не 8790/8796: на 8790 и 8796 остались python-воркеры убитых сессий
(kill бьёт bash-обёртку, uvicorn-ребёнок переживает и держит порт), 8791 занят
вотчером моделей. Останавливать сервер — через POST /api/shutdown, а не kill:
тогда ребёнок не остаётся.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Автозапуск идёт через pythonw.exe (без окна консоли): там sys.stdout/sys.stderr == None,
# и uvicorn с дефолтным логгером падает на попытке писать в них. Подменяем на файл.
if sys.stdout is None or sys.stderr is None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from core.paths import DATA_DIR as _logdir
    os.makedirs(_logdir, exist_ok=True)
    _f = open(os.path.join(_logdir, 'server.log'), 'a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stdout or _f
    sys.stderr = sys.stderr or _f

import uvicorn

if __name__ == '__main__':
    # Порт из BL_PORT (8797 по умолчанию). 8790/8796 — сироты убитых сессий,
    # 8791 — вотчер моделей; биндинг поверх занятого порта даёт 10048/10013.
    # Останавливать сервер — POST /api/shutdown (иначе остаётся воркер-ребёнок).
    # reload по умолчанию ВЫКЛ (стабильнее); для разработки: BL_RELOAD=1.
    _port = int(os.environ.get('BL_PORT', '8797'))
    if '--wait-down' in sys.argv:                      # exe-сборка: рестарт самим собой,
        import socket, time                             # ждём освобождения порта старым процессом
        for _ in range(600):
            s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', _port)); s.close(); break
            except OSError:
                s.close(); time.sleep(0.5)
    uvicorn.run('app.main:app', host='127.0.0.1',
                port=_port,
                reload=os.environ.get('BL_RELOAD', '0') == '1')
