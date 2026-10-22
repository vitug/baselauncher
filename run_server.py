# -*- coding: utf-8 -*-
"""Точка входа BaseLauncher: uvicorn на 127.0.0.1, порт из BL_PORT (умолчание 8796).

8796, а не 8790: на 8790 остался воркер-сирота старого сервера (пережил
убийство родителя), 8791 занят вотчером моделей, 8792 зарезервирован Windows.
Когда сироту закроют — можно вернуть 8790: BL_PORT=8790.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import uvicorn

if __name__ == '__main__':
    # Порт из BL_PORT (8796 по умолчанию). 8790 держит воркер-сирота старого
    # сервера, 8791 — вотчер моделей: биндинг поверх занятого порта даёт
    # WinError 10013, а не 10048.
    # reload по умолчанию ВЫКЛ (стабильнее); для разработки: BL_RELOAD=1.
    uvicorn.run('app.main:app', host='127.0.0.1',
                port=int(os.environ.get('BL_PORT', '8796')),
                reload=os.environ.get('BL_RELOAD', '0') == '1')
