# Ярлык BaseLauncher (ASCII-only, чтобы не зависеть от кодировки консоли).
#   ... -File deploy\make-shortcut.ps1           -> меню "Пуск"
#   ... -File deploy\make-shortcut.ps1 -Startup  -> автозапуск (папка Startup)
# Запуск идёт через pythonw.exe boot.py (без окна консоли).
param([switch]$Startup)
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell

$root = 'D:\git_repos\baselauncher'
$py   = 'D:\python\WPy64-31241\python-3.12.4.amd64\pythonw.exe'
$icon = Join-Path $root 'assets\baselauncher.ico'

if ($Startup) {
    $lnkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\BaseLauncher.lnk'
    $desc = 'BaseLauncher autostart'
} else {
    $lnkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\BaseLauncher.lnk'
    $desc = 'BaseLauncher - 1C starter (Ctrl+Shift+S)'
}
if (-not (Test-Path $icon)) { $icon = $py }

$s = $ws.CreateShortcut($lnkPath)
$s.TargetPath       = $py
$s.Arguments        = '"' + $root + '\boot.py"'
$s.WorkingDirectory = $root
$s.IconLocation     = $icon + ',0'
$s.Description      = $desc
$s.WindowStyle      = 7
$s.Save()

$r = $ws.CreateShortcut($lnkPath)
"lnk     = $lnkPath"
"exists  = $(Test-Path $lnkPath)"
"target  = $($r.TargetPath) $($r.Arguments)"
"icon    = $($r.IconLocation)"
