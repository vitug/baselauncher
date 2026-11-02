@echo off
rem === BaseLauncher: ярлык в автозагрузку пользователя ===
setlocal
set "ROOT=%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BaseLauncher.lnk"
set "TARGET=%ROOT%BaseLauncher.cmd"

if not exist "%TARGET%" (
  echo Не найден BaseLauncher.cmd рядом с этим файлом.
  pause
  exit /b 2
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%ROOT%'; $s.IconLocation='%ROOT%assets\baselauncher.ico'; $s.WindowStyle=7; $s.Description='BaseLauncher'; $s.Save()"

if exist "%LNK%" (
  echo Ярлык создан:
  echo   %LNK%
  echo Убрать автозапуск — удалить этот ярлык.
) else (
  echo Не удалось создать ярлык. Создайте вручную: Win+R ^> shell:startup ^> ярлык на BaseLauncher.cmd
)
pause
