# -*- coding: utf-8 -*-
"""Пути данных BaseLauncher — единая точка, управляемая переменными окружения.

Переменные:
  BL_DATA    — каталог данных (по умолчанию <репо>/data).
               Dev-инстанс указывает сюда свою песочницу (data_dev), чтобы
               не трогать боевой config.json/логи.
  BL_IBASES  — путь к ibases.v8i (по умолчанию %APPDATA%/1C/1CEStart/ibases.v8i).
               Dev-инстанс подставляет КОПИЮ боевого файла: дерево реальное,
               но записи тестов не попадают в список баз 1С.
  BL_DEV     — '1' = dev-режим: без глобальной клавиши и трея (они заняты
               рабочим инстансом), окно уезжает на отдельный виртуальный стол.

Импортируется из core/ и корневых скриптов; менять пути в модулях напрямую нельзя.
"""
import os
import sys
from pathlib import Path

# Frozen (PyInstaller onefile): всё статическое распаковано в _MEIPASS,
# данные и логи живут рядом с exe; из репозитория — как раньше.
if getattr(sys, 'frozen', False):
    REPO = Path(sys.executable).resolve().parent
else:
    REPO = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('BL_DATA') or (REPO / 'data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)

V8I_PATH = Path(os.environ.get('BL_IBASES') or os.path.expandvars(
    r'%APPDATA%\1C\1CEStart\ibases.v8i'))

DEV = (os.environ.get('BL_DEV') or '').strip() not in ('', '0', 'false', 'False')
