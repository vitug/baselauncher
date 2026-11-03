@echo off
rem Полностью независимое окружение запуска BaseLauncher:
rem uv сам скачивает свой Python и собирает env\ с fastapi/uvicorn.
rem Нужен только uv:  winget install astral-sh.uv
cd /d "%~dp0"
uv run --python 3.12 tools\make_env.py %*
