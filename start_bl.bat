@echo off
rem === BaseLauncher: start server if needed, then open app in a separate window ===
curl -s --noproxy "*" -o nul http://127.0.0.1:8797/api/state
if errorlevel 1 (
  echo Starting BaseLauncher server...
  start "BaseLauncher server" /min /d "D:\git_repos\baselauncher" "D:\python\WPy64-31241\python-3.12.4.amd64\python.exe" run_server.py
  timeout /t 5 /nobreak >nul
)
rem Окно просим показать сервер (он вернёт уже открытое, а не создаст второе).
rem Только если сервер не ответил — запускаем Edge сами.
curl -s --noproxy "*" -X POST http://127.0.0.1:8797/api/starter/focus -H "Content-Type: application/json" -d "{}" -o nul
if errorlevel 1 start "" msedge --app=http://127.0.0.1:8797
