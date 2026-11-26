@echo off
rem === BaseLauncher: поднять прод-сервер, отделённо от родительской сессии ===
cd /d "%~dp0\.."
set LOG=%TEMP%\bl_prod.log
start "" /b env\Scripts\pythonw.exe run_server.py >> "%LOG%" 2>&1
