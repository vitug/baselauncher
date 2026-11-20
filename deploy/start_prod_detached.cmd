@echo off
rem === BaseLauncher: поднять прод-сервер, отделённо от родительской сессии ===
cd /d D:\git_repos\baselauncher
set LOG=%LOCALAPPDATA%\hermes\cache\scratch\bl_prod.log
start "" /b env\Scripts\pythonw.exe run_server.py >> "%LOG%" 2>&1
