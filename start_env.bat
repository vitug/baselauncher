@echo off
setlocal EnableDelayedExpansion
rem Полностью независимое окружение запуска BaseLauncher:
rem uv сам скачивает свой Python и собирает env\ с fastapi/uvicorn.
rem Если uv нет — скачиваем его несколькими способами (какой сработает).

cd /d "%~dp0"
set "UVBIN="
set "DEST=%USERPROFILE%\.local\bin"
rem TEMP бывает сломан (POSIX-путь из чужих окружений) — проверяем, что каталог существует
set "TMPD=%TEMP%"
if not exist "%TMPD%\" set "TMPD=%USERPROFILE%\AppData\Local\Temp"
set "ZIP=%TMPD%\uv_dl.zip"
set "XDIR=%TMPD%\uv_x"
set "URL=https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
rem powershell.exe лежит не в System32, а в v1.0 — берём абсолютный путь
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"

rem --- 0) uv уже установлен? ---
where uv >nul 2>&1 && (set "UVBIN=uv" & goto :run)
if exist "%DEST%\uv.exe" (set "UVBIN=%DEST%\uv.exe" & goto :run)
echo [env] uv не найден — пробую скачать...

rem --- 1) curl напрямую ---
if exist "%ZIP%" del "%ZIP%"
curl -fsSL --connect-timeout 15 -o "%ZIP%" "%URL%" 2>nul
call :check_zip && goto :unzip

rem --- 2) curl через локальный прокси ---
if exist "%ZIP%" del "%ZIP%"
curl -fsSL --connect-timeout 15 -x http://127.0.0.1:3128 -o "%ZIP%" "%URL%" 2>nul
call :check_zip && goto :unzip

rem --- 3) PowerShell напрямую ---
if exist "%ZIP%" del "%ZIP%"
"%PS%" -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try{ Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%ZIP%' }catch{ exit 1 }" >nul 2>&1
call :check_zip && goto :unzip

rem --- 4) PowerShell через прокси ---
if exist "%ZIP%" del "%ZIP%"
"%PS%" -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try{ Invoke-WebRequest -UseBasicParsing -Proxy http://127.0.0.1:3128 -Uri '%URL%' -OutFile '%ZIP%' }catch{ exit 1 }" >nul 2>&1
call :check_zip && goto :unzip

rem --- 5) официальный установщик astral.sh (сам подбирает зеркало/сеть) ---
"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "try{ irm https://astral.sh/uv/install.ps1 | iex }catch{ exit 1 }" >nul 2>&1
if exist "%DEST%\uv.exe" (set "UVBIN=%DEST%\uv.exe" & goto :run)

echo [env] ОШИБКА: скачать uv не удалось ни одним способом.
echo Установи вручную: winget install astral-sh.uv
echo или скачай exe: https://github.com/astral-sh/uv/releases
exit /b 1

:unzip
if exist "%XDIR%" rmdir /s /q "%XDIR%"
"%PS%" -NoProfile -Command "try{ Expand-Archive -Force -Path '%ZIP%' -DestinationPath '%XDIR%' }catch{ exit 1 }" >nul 2>&1
if not exist "%XDIR%\uv.exe" (
  rem в некоторых релизах exe в подпапке — ищем
  for /r "%XDIR%" %%F in (uv.exe) do if not exist "%DEST%\uv.exe" (mkdir "%DEST%" 2>nul & copy /y "%%F" "%DEST%\uv.exe" >nul)
) else (
  mkdir "%DEST%" 2>nul
  copy /y "%XDIR%\uv.exe" "%DEST%\uv.exe" >nul
  if exist "%XDIR%\uvx.exe" copy /y "%XDIR%\uvx.exe" "%DEST%\uvx.exe" >nul
)
if exist "%ZIP%" del "%ZIP%"
if exist "%XDIR%" rmdir /s /q "%XDIR%"
if exist "%DEST%\uv.exe" (set "UVBIN=%DEST%\uv.exe" & goto :run)
echo [env] ОШИБКА: не удалось распаковать uv.
exit /b 1

:check_zip
if not exist "%ZIP%" exit /b 1
for %%A in ("%ZIP%") do set "SZ=%%~zA"
if !SZ! LSS 1000000 exit /b 1
exit /b 0

:run
echo [env] uv: %UVBIN%
"%UVBIN%" run --python 3.12 tools\make_env.py %*
