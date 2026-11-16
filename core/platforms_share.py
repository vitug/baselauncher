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


def _share_root() -> str:
    """Корень шары дистрибутивов: env BL_SHARE > config.json share_root > ''."""
    root = os.environ.get('BL_SHARE', '')
    if not root:
        try:
            from core import config
            root = str((config.load() or {}).get('share_root') or '')
        except Exception:                                # noqa: BLE001
            root = ''
    return root


SHARE = _share_root()          # вычислено на импорте; в probe/reveal корень читается заново
NO_SHARE_HINT = 'корень шары не задан: поле share_root в data/config.json или env BL_SHARE'
DATA = Path(__file__).resolve().parent.parent / 'data' / 'install'


def _special() -> tuple[str, str]:
    """(шара, локальная папка) «специальных» платформ (готовые каталоги, не установщики):
    версия с шары копируется каталогом в локальную папку. env BL_SPECIAL_SHARE/BL_SPECIAL_DIR
    > поля special_share/special_dir в config.json > ''."""
    share = os.environ.get('BL_SPECIAL_SHARE', '')
    local = os.environ.get('BL_SPECIAL_DIR', '')
    try:
        from core import config
        cfg = config.load() or {}
        share = share or str(cfg.get('special_share') or '')
        local = local or str(cfg.get('special_dir') or '')
    except Exception:                                    # noqa: BLE001
        pass
    return share, local


def _special_local_default() -> Path:
    """Локальное хранилище готовых платформ (уже сканируется scanner.py)."""
    return Path(r'D:\storage1C\platforms')


def probe_special(version: str) -> dict:
    """Есть ли готовый каталог версии на спец-шаре и скопирован ли он локально."""
    version = (version or '').strip()
    share, local = _special()
    if not local:
        local = str(_special_local_default())
    sd, ld = Path(share) / version, Path(local) / version
    res = {'version': version, 'share': share, 'local': local,
           'share_dir': str(sd), 'local_dir': str(ld),
           'configured': bool(share), 'exists': sd.is_dir(),
           'local_ready': any((ld / 'bin' / e).is_file() or (ld / e).is_file()
                              for e in ('1cv8.exe', '1cv8c.exe')),
           'size_gb': 0.0}
    if res['exists']:
        try:                       # оценка размера каталога версии, без глубокой рекурсии
            total = 0
            for _r, _d, files in os.walk(sd):
                total += sum(os.path.getsize(os.path.join(_r, f)) for f in files)
                if total > 50 * 1024 ** 3:
                    break
            res['size_gb'] = round(total / 1024 ** 3, 1)
        except OSError:
            pass
    return res


def _copy_special_thread(version: str, log: Path):
    import threading
    share, local = _special()
    if not local:
        local = str(_special_local_default())
    sd, ld = Path(share) / version, Path(local) / version
    def run():
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, 'a', encoding='utf-8') as f:
                f.write('START %s -> %s\n' % (sd, ld))
            # robocopy: права на D:\\storage1C админа не требуют
            rc = subprocess.run(['robocopy', str(sd), str(ld), '/E', '/NFL', '/NDL',
                                 '/NJH', '/NJS', '/NP', '/R:2', '/W:5'],
                                capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with open(log, 'a', encoding='utf-8') as f:
                f.write('ROBOCOPY_EXIT=%d\n' % rc.returncode)   # 0..7 = успех, 8+ = ошибка
        except Exception as e:                                    # noqa: BLE001
            with open(log, 'a', encoding='utf-8') as f:
                f.write('ERROR %r\n' % (e,))
    threading.Thread(target=run, daemon=True).start()


def copy_special(version: str) -> dict:
    """Скопировать готовый каталог версии со спец-шары в локальную папку (фоном)."""
    p = probe_special(version)
    if not p['configured'] or not p['exists']:
        raise ValueError('на специальной шаре нет каталога %s' % p['share_dir'])
    if p['local_ready']:
        return {'ok': True, 'started': False, 'already': True, 'local_dir': p['local_dir']}
    log = DATA / ('copy-%s.log' % version)
    _copy_special_thread(version, log)
    return {'ok': True, 'started': True, 'log': str(log), 'local_dir': p['local_dir'],
            'hint': 'копирование идёт фоном; готовность — bin\\\\1cv8c.exe в локальной папке'}


KINDS = {
    'setup64full': 'полный 64 бита (клиент + сервер + автономный сервер)',
    'setup64': 'клиент 64 бита',
    'setup': 'клиент 32 бита',
    'setuptc64': 'только тонкий клиент 64 бита',
    'setuptc': 'только тонкий клиент 32 бита',
    'setupfv': 'файловый вариант (только сервер)',
}


def _bin_dir(version: str, kind: str) -> Path:
    """Куда лягет bin этой сборки (x86-виды → Program Files (x86))."""
    root = Path(r'C:\Program Files (x86)\1cv8') if kind in ('setup', 'setuptc', 'setupfv') \
        else Path(r'C:\Program Files\1cv8')
    return root / version / 'bin'


def _line(version: str) -> str:
    """8.3.27.1775 → 8.3.27"""
    parts = (version or '').strip().split('.')
    return '.'.join(parts[:3]) if len(parts) >= 3 else version


def probe(version: str) -> dict:
    """Что есть на шаре для этой версии и не установлена ли она уже."""
    version = (version or '').strip()
    line = _line(version)
    share = _share_root()          # динамически: поле могли задать после старта сервера
    d = Path(share) / line / version
    res = {'version': version, 'line': line, 'share': share, 'dir': str(d),
           'share_ok': Path(share).exists() if share else False, 'exists': d.is_dir(), 'kinds': {}}
    if not share:
        res['hint'] = NO_SHARE_HINT
    if res['exists']:
        for k in KINDS:
            exe = d / k / 'setup.exe'
            if exe.is_file():
                res['kinds'][k] = {'path': str(exe), 'descr': KINDS[k]}
    res['installed'] = find_installed(version)
    try:                          # спец-источник: готовый каталог версии (копируется, не ставится)
        res['special'] = probe_special(version)
    except Exception:                                    # noqa: BLE001
        pass
    return res


def find_installed(version: str) -> list[str]:
    """Установленные exe этой версии (Program Files и локальное хранилище платформ)."""
    version = (version or '').strip()
    roots = [Path(r'C:\Program Files\1cv8'), Path(r'C:\Program Files (x86)\1cv8'),
             Path(r'D:\storage1C\platforms')]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for exe in ('1cv8.exe', '1cv8c.exe'):
            p = root / version / 'bin' / exe
            if p.is_file():
                found.append(str(p))
            p = root / version / exe      # «плоские» версии: exe прямо в каталоге версии
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
    # пост-фикс для тонких пакетов (грабля 8.5.4.2030): mngcln.dll импортирует ssh.dll,
    # но в setuptc/setuptc64 она не кладётся (проверено по cab) — вытаскиваем из полного
    # setup\Data1.cab, иначе клиент падает «Ошибка загрузки компоненты mngcln. 126».
    if kind in ('setuptc', 'setuptc64'):
        binp = _bin_dir(version, kind)
        full_cab = Path(k['path']).parent.parent / 'setup' / 'Data1.cab'
        if full_cab.is_file():
            fix = [
                'if exist "{m}" if not exist "{s}" (echo fix: extracting ssh.dll from full setup >> "%LOG%"'
                ' & expand.exe "{c}" -F:ssh.dll "{b}" >> "%LOG%" 2>&1)'.format(
                    m=binp / 'mngcln.dll', s=binp / 'ssh.dll', c=full_cab, b=binp),
            ]
            data = cmd_file.read_bytes().decode('cp866', 'replace')
            cmd_file.write_bytes((data + '\r\n'.join(fix) + '\r\n').encode('cp866', 'replace'))
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
    """Открыть папку дистрибутива на шаре в проводнике."""
    p = probe(version)
    target = p['dir'] if p['exists'] else str(Path(_share_root()) / p['line'])
    if not Path(target).exists():
        target = _share_root()
    subprocess.Popen(['explorer', target])
    return {'ok': True, 'opened': target}


def installed_versions() -> list[str]:
    """Версии, установленные локально (для подсказки «заменить на …»)."""
    seen = []
    for root in (Path(r'C:\Program Files\1cv8'), Path(r'C:\Program Files (x86)\1cv8'),
                 Path(r'D:\storage1C\platforms')):
        if not root.is_dir():
            continue
        for sub in root.iterdir():
            if sub.is_dir() and (sub / 'bin' / '1cv8.exe').is_file():
                seen.append(sub.name)
    return sorted(set(seen), reverse=True)
