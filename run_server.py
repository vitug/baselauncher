# -*- coding: utf-8 -*-
"""Точка входа BaseLauncher: uvicorn на 127.0.0.1:8790, reload=True для отладки."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import uvicorn

if __name__ == '__main__':
    uvicorn.run('app.main:app', host='127.0.0.1', port=8790, reload=True)
