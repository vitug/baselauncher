@echo off
rem === BaseLauncher: установка зависимостей в .venv рядом с программой ===
setlocal
cd /d "%~dp0"

set "BASEPY="
where py >nul 2>nul && set "BASEPY=py -3"
if not defined BASEPY where python >nul 2>nul && set "BASEPY=python"
if not defined BASEPY (
  echo Python не найден. Установите Python 3.11/3.12 x64 и отметьте "Add python.exe to PATH".
  pause
  exit /b 2
)

echo [1/3] создаю .venv ...
if exist ".venv\Scripts\python.exe" (
  echo     .venv уже есть — использую его
) else (
  %BASEPY% -m venv .venv || (echo Не удалось создать .venv & pause & exit /b 3)
)

echo [2/3] обновляю pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo [3/3] ставлю зависимости (fastapi, uvicorn, pywin32) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Не удалось поставить зависимости. Если у вас прокси — задайте HTTPS_PROXY и повторите.
  pause
  exit /b 4
)

echo.
echo Готово. Проверка:
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, win32com; print('  fastapi', fastapi.__version__, '| uvicorn', uvicorn.__version__, '| pywin32 ok')"
echo.
echo Дальше: запустить BaseLauncher.cmd (или install-autostart.cmd, чтобы автозапуск).
pause
