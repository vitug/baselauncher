@echo off
rem === BaseLauncher: поднять сервер (если не запущен) и показать окно ===

set PY=D:\python\WPy64-31241\python-3.12.4.amd64
set ROOT=D:\git_repos\baselauncher
curl -s --noproxy "*" -o nul http://127.0.0.1:8797/api/state
if errorlevel 1 (
  echo Starting BaseLauncher server...
  start "" /d "%ROOT%" "%PY%\pythonw.exe" boot.py
  timeout /t 6 /nobreak >nul
)
curl -s --noproxy "*" -X POST http://127.0.0.1:8797/api/starter/focus -H "Content-Type: application/json" -d "{}" -o nul
