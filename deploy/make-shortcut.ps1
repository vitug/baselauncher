# Ярлык BaseLauncher в меню «Пуск» (ASCII-only, чтобы не зависеть от кодировки консоли).
# Запуск:  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\make-shortcut.ps1
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell

$lnkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\BaseLauncher.lnk'
$vbs     = 'D:\git_repos\baselauncher\deploy\BaseLauncher-autostart.vbs'
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$icon    = 'D:\git_repos\baselauncher\assets\baselauncher.ico'
if (-not (Test-Path $icon)) { $icon = $wscript }

$s = $ws.CreateShortcut($lnkPath)
$s.TargetPath       = $wscript
$s.Arguments        = '"' + $vbs + '"'
$s.WorkingDirectory = 'D:\git_repos\baselauncher'
$s.IconLocation     = $icon + ',0'
$s.Description      = 'BaseLauncher - 1C starter (Ctrl+Shift+S)'
$s.WindowStyle      = 7
$s.Save()

$r = $ws.CreateShortcut($lnkPath)
"lnk     = $lnkPath"
"exists  = $(Test-Path $lnkPath)"
"target  = $($r.TargetPath) $($r.Arguments)"
"icon    = $($r.IconLocation)"
