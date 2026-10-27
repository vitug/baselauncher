# -*- coding: utf-8 -*-
"""Сборка поставки BaseLauncher: dist/Baselauncher-<версия>/ + zip + контрольная сумма.

Запуск:  python tools/make_release.py [--no-zip]

Что делает:
  1) берёт версию из app/main.py (`version = "..."`);
  2) копирует код (app, core, assets, boot.py, run_server.py, mcp_server.py, README, requirements);
  3) добавляет файлы поставки из deploy/ (INSTALL.md, BaseLauncher.cmd, install.cmd,
     install-autostart.cmd) и VERSION;
  4) создаёт пустую data/ с памяткой — НИКАКИХ config.json, логов и бэкапов (там пароли);
  5) пакует всё в dist/Baselauncher-<версия>.zip и пишет SHA256SUMS.txt.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
DEPLOY = ROOT / 'deploy'

# что кладём в поставку
DIRS = ['app', 'core', 'assets', 'docs']
FILES = ['boot.py', 'run_server.py', 'mcp_server.py', 'dev.py', 'README.md', 'requirements.txt']
# файлы поставки: (источник в deploy/, имя в пакете)
DEPLOY_FILES = [('INSTALL.md', 'INSTALL.md'),
                ('BaseLauncher.cmd', 'BaseLauncher.cmd'),
                ('install.cmd', 'install.cmd'),
                ('install-autostart.cmd', 'install-autostart.cmd')]

# чего в поставке быть не должно ни при каких условиях
FORBIDDEN_NAMES = ('config.json', 'launches.log', 'server.log', 'boot.log', 'focus.log',
                   'launch-errors.json', 'overrides.json', 'window.json')
IGNORE_DIRS = {'__pycache__', '.git', '.venv', 'dist', 'data', 'node_modules'}
IGNORE_SUFFIX = ('.pyc', '.pyo', '.log', '.bmp')


def version() -> str:
    text = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
    m = re.search(r"version\s*=\s*['\"]([0-9][0-9.\w-]*)['\"]", text)
    if not m:
        raise SystemExit('не нашёл версию в app/main.py')
    return m.group(1)


def ignore(_dir, names):
    out = set()
    for n in names:
        if n in IGNORE_DIRS or n.lower().endswith(IGNORE_SUFFIX) or '.bak-' in n:
            out.add(n)
    return out


def copy_tree(src: Path, dst: Path):
    shutil.copytree(src, dst, ignore=ignore)


def build(no_zip: bool = False) -> Path:
    ver = version()
    pkg = DIST / ('Baselauncher-%s' % ver)
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)

    for d in DIRS:
        copy_tree(ROOT / d, pkg / d)
    for f in FILES:
        shutil.copy2(ROOT / f, pkg / f)
    for src_name, dst_name in DEPLOY_FILES:
        src = DEPLOY / src_name
        if not src.is_file():
            raise SystemExit('нет файла поставки: %s' % src)
        text = src.read_text(encoding='utf-8')
        (pkg / dst_name).write_text(text.replace('{VERSION}', ver), encoding='utf-8')

    (pkg / 'VERSION').write_text('BaseLauncher %s\nсобрано %s\n'
                                 % (ver, datetime.now().strftime('%Y-%m-%d %H:%M')), encoding='utf-8')

    data = pkg / 'data'
    data.mkdir()
    (data / 'README.txt').write_text(
        'Здесь BaseLauncher держит свои данные: config.json (настройки баз, пароли в обфускации),\n'
        'журналы запусков и снимки ошибок. Папка создаётся при первом запуске.\n'
        'В поставке намеренно пусто: чужие настройки и пароли не переносятся.\n', encoding='utf-8')

    # ── проверки ─────────────────────────────────────────────────────────────
    bad = [p for p in pkg.rglob('*') if p.is_file() and p.name in FORBIDDEN_NAMES]
    bad += [p for p in pkg.rglob('*.bak-*')]
    if bad:
        raise SystemExit('в поставку попали личные данные: %s' % ', '.join(str(p) for p in bad))
    for p in pkg.rglob('*.py'):
        if p.stat().st_size == 0 and p.name != '__init__.py':
            raise SystemExit('пустой файл в поставке: %s' % p)

    files = sorted(p for p in pkg.rglob('*') if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print('версия : %s' % ver)
    print('папка  : %s' % pkg)
    print('файлов : %d, суммарно %.1f КБ' % (len(files), total / 1024))
    for p in files:
        print('   %7.1f КБ  %s' % (p.stat().st_size / 1024, p.relative_to(pkg)))

    if no_zip:
        return pkg

    DIST.mkdir(exist_ok=True)
    zip_path = DIST / ('Baselauncher-%s.zip' % ver)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=str(Path(pkg.name) / p.relative_to(pkg)))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sums = DIST / 'SHA256SUMS.txt'
    old = []
    if sums.is_file():
        old = [l for l in sums.read_text(encoding='utf-8').splitlines()
               if l.strip() and not l.strip().endswith(zip_path.name)]
    old.append('%s  %s' % (digest, zip_path.name))
    sums.write_text('\n'.join(old) + '\n', encoding='utf-8')
    print('\nархив : %s (%.1f КБ)' % (zip_path, zip_path.stat().st_size / 1024))
    print('sha256: %s' % digest)
    return zip_path


if __name__ == '__main__':
    build('--no-zip' in sys.argv)
