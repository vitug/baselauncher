# -*- coding: utf-8 -*-
"""Платформы 1С: поиск дистрибутива на шаре и установка отсутствующей версии.

Корень шары задаётся переменной окружения `BL_SHARE` или полем `share_root` в
`data/config.json` (в git не попадает). Структура: шара / линия / полная версия.
`setup` (32 клиент), `setup64` (64 клиент), `setup64full` (полный 64: клиент+сервер+авт.сервер),
`setuptc64` (только тонкий клиент). Ставим .cmd-обёрткой с UAC, лог — в data/install/.
"""
import os
import subprocess
from pathlib import Path

SHARE = r'\\Chicago\v81mcint'
DATA = Path(__file__).resolve().parent.parent / 'data' / 'install'
KINDS = {
    'setup64full': 'полный 64 бита (клиент + сервер + автономный сервер)',
    'setup64': 'клиент 64 бита',
    'setup': 'клиент 32 бита',
    'setuptc64': 'только тонкий клиент 64 бита',
    'setupfv': 'файловый вариант (только сервер)',
}


def _line(version: str) -> str:
    """8.3.27.1775 → 8.3.27"""
    parts = (version or '').strip().split('.')
    return '.'.join(parts[:3]) if len(parts) >= 3 else version


def probe(version: str) -> dict:
    """Что есть на шаре для этой версии и не установлена ли она уже."""
    version = (version or '').strip()
    line = _line(version)
    d = Path(SHARE) / line / version
    res = {'version': version, 'line': line, 'share': SHARE, 'dir': str(d),
           'share_ok': Path(SHARE).exists(), 'exists': d.is_dir(), 'kinds': {}}
    if res['exists']:
        for k in KINDS:
            exe = d / k / 'setup.exe'
            if exe.is_file():
                res['kinds'][k] = {'path': str(exe), 'descr': KINDS[k]}
    res['installed'] = find_installed(version)
    return res


def find_installed(version: str) -> list[str]:
    """Установленные exe этой версии (Program Files и локальное хранилище платформ)."""
    version = (version or '').strip()
    roots = [Path(r'C:\Program Files\1cv8'), Path(r'C:\Program Files (x86)\1cv8'),
             Path(r'D:\storage1C\ПлатформаКвас')]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for exe in ('1cv8.exe', '1cv8c.exe'):
            p = root / version / 'bin' / exe
            if p.is_file():
                found.append(str(p))
    if not found:                       # версия могла лечь в подпапку с суффиксом
        for root in roots:
            if not root.is_dir():
                continue
            for sub in root.glob(version + '*'):
                p = sub / 'bin' / '1cv8.exe'
                if p.is_file():
                    found.append(str(p))
    return found


def build_cmd(version: str, kind: str = 'setup64full') -> Path:
    """Собрать .cmd-обёртку установщика (CRLF: с LF установщик молча не стартует)."""
    p = probe(version)
    if not p['exists']:
        raise ValueError('на шаре нет каталога %s — версию %s не поставить' % (p['dir'], version))
    k = p['kinds'].get(kind)
    if not k:
        raise ValueError('в дистрибутиве %s нет %s (есть: %s)'
                         % (version, kind, ', '.join(p['kinds']) or 'ничего'))
    DATA.mkdir(parents=True, exist_ok=True)
    cmd_file = DATA / ('inst-%s.cmd' % version)
    log = DATA / ('inst-%s.log' % version)
    setup = k['path']
    # проценты cmd-переменных здесь литеральные: строки собираются f-string'ами, без %-форматирования
    lines = [
        '@echo off',
        'rem BaseLauncher: установка платформы 1С {} ({})'.format(version, kind),
        'set LOG={}'.format(log),
        'echo START %DATE% %TIME% > "%LOG%"',
        '"{}" /S /L1049 /v"/qn INSTALLSRVRASSRVC=0" >> "%LOG%" 2>&1'.format(setup),
        'echo EXIT=%ERRORLEVEL% >> "%LOG%"',
    ]
    cmd_file.write_bytes(('\r\n'.join(lines) + '\r\n').encode('cp866', 'replace'))
    return cmd_file


def install(version: str, kind: str = 'setup64full') -> dict:
    """Запустить установку с повышением прав (UAC подтверждает пользователь)."""
    cmd_file = build_cmd(version, kind)
    log = DATA / ('inst-%s.log' % version)
    ps = ('Start-Process -FilePath "%s" -Verb RunAs' % cmd_file)
    subprocess.Popen(['powershell', '-NoProfile', '-WindowStyle', 'Hidden',
                      '-Command', ps],
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return {'ok': True, 'started': True, 'cmd_file': str(cmd_file),
            'log': str(log),
            'hint': 'подтвердите UAC; установка идёт ~2 минуты, результат — в логе '
                    '(EXIT=0 и появился bin\\1cv8.exe)'}


def reveal(version: str) -> dict:
    """Открыть папку дистрибутива в проводнике."""
    p = probe(version)
    target = p['dir'] if p['exists'] else str(Path(SHARE) / p['line'])
    if not Path(target).exists():
        target = SHARE
    subprocess.Popen(['explorer', target])
    return {'ok': True, 'opened': target}


def installed_versions() -> list[str]:
    """Версии, установленные локально (для подсказки «заменить на …»)."""
    seen = []
    for root in (Path(r'C:\Program Files\1cv8'), Path(r'C:\Program Files (x86)\1cv8'),
                 Path(r'D:\storage1C\ПлатформаКвас')):
        if not root.is_dir():
            continue
        for sub in root.iterdir():
            if sub.is_dir() and (sub / 'bin' / '1cv8.exe').is_file():
                seen.append(sub.name)
    return sorted(set(seen), reverse=True)
