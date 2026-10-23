@echo off
rem === BaseLauncher: запуск (сервер поднимется сам, если ещё не запущен) ===
setlocal
set "ROOT=%~dp0"
if "%BL_PORT%"=="" set "BL_PORT=8797"

rem Порядок поиска интерпретатора: .venv рядом -> runtime\python -> python из PATH
set "PYW="
if exist "%ROOT%.venv\Scripts\pythonw.exe" set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
if not defined PYW if exist "%ROOT%runtime\python\pythonw.exe" set "PYW=%ROOT%runtime\python\pythonw.exe"
if not defined PYW if exist "%ROOT%.venv\Scripts\python.exe"  set "PYW=%ROOT%.venv\Scripts\python.exe"
if not defined PYW if exist "%ROOT%runtime\python\python.exe" set "PYW=%ROOT%runtime\python\python.exe"
if not defined PYW where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
if not defined PYW where python.exe  >nul 2>nul && set "PYW=python.exe"
if not defined PYW (
  echo Python не найден.
  echo Поставьте Python 3.11/3.12 x64 или запустите install.cmd, чтобы создать .venv.
  pause
  exit /b 2
)

start "" /d "%ROOT%" "%PYW%" boot.py
exit /b 0
