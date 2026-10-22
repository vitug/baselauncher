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

import uvicorn

if __name__ == '__main__':
    # Порт из BL_PORT (8797 по умолчанию). 8790/8796 — сироты убитых сессий,
    # 8791 — вотчер моделей; биндинг поверх занятого порта даёт 10048/10013.
    # Останавливать сервер — POST /api/shutdown (иначе остаётся воркер-ребёнок).
    # reload по умолчанию ВЫКЛ (стабильнее); для разработки: BL_RELOAD=1.
    uvicorn.run('app.main:app', host='127.0.0.1',
                port=int(os.environ.get('BL_PORT', '8797')),
                reload=os.environ.get('BL_RELOAD', '0') == '1')
