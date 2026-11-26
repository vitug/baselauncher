# -*- coding: utf-8 -*-
"""Проверка на утечку личной/внутренней информации ПЕРЕД коммитом и пушем.

Что ловит (PATTERNS ниже): имена людей, внутренние хосты/IP, локальные пути
дисков, корпоративные/личные email, токены. Запускается автоматически хуками
git (pre-commit / pre-push, см. install_hooks), можно звать руками:

  python tools/check_privacy.py                 # всё рабочее дерево (tracked)
  python tools/check_privacy.py --staged        # индекс (pre-commit)
  python tools/check_priv.py --range A..B       # коммиты диапазона (pre-push)

Выход: 0 = чисто, 1 = найдены совпадения (в stderr — файл:строка:что).
Дополнения в PATTERNS вносить сюда же; ложные срабатывания — в ALLOW
(подстрока, например имя общего модуля, совпавшее с паттерном).
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (имя, regex) — чувствительно к регистру там, где важно, иначе (?i)
PATTERNS = [
    ('имя',            r'Тугаринов|Tugarinov|Суслов|Suslov|Широков'),
    ('внутр.хост',     r'(?i)acrisia|dept07|pyramid'),
    ('внутр.IP',       r'\b172\.30\.\d+\.\d+\b|\b192\.168\.\d+\.\d+\b|\b10\.\d+\.\d+\.\d+\b'),
    ('локал.путь',     r'(?i)[CD]:[\\/](Users[\\/][A-Za-z0-9_.-]{3,}|bases1C[\\/][А-Яа-яA-Za-z0-9_ .]+\.(cf|dt|1cd))'),
    ('email',          r'(?i)[a-z0-9._%+-]+@(1c\.ru|gmail\.com|mail\.ru|yandex\.ru)'),
    ('токен',          r'github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]{20,}|xox[baprs]-'),
    ('пароль-маркер',  r'(?i)(password|passwd|pwd)\s*[=:]\s*[\'"][^\'"{}\s]{8,}'),
]

# Ложные срабатывания (подстрока строки-совпадения)
ALLOW = (
    'example.com',            # примеры в доке
    'C:/Program Files',      # безопасный системный путь
    'users.noreply.github.com',
    'bl1:',                   # формат обфускации паролей, не сам пароль
)

SKIP_FILES = ('check_privacy.py',)   # сам скрипт содержит паттерны


def _git(*args):
    r = subprocess.run(['git', *args], capture_output=True, cwd=str(ROOT),
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return r.stdout.decode('utf-8', 'replace')


def _check_line(path: str, lineno: int, line: str) -> list[str]:
    out = []
    if Path(path).name in SKIP_FILES:
        return out
    for name, rx in PATTERNS:
        for m in re.finditer(rx, line):
            frag = m.group(0)
            if any(a.lower() in frag.lower() for a in ALLOW):
                continue
            out.append(f'{path}:{lineno}: [{name}] {frag}')
    return out


def scan_files() -> list[str]:
    files = [f for f in _git('ls-files').splitlines() if f]
    findings = []
    for f in files:
        p = ROOT / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            findings += _check_line(f, i, line)
    return findings


def scan_staged() -> list[str]:
    """Дифф индекса: только добавленные/изменённые строки."""
    diff = _git('diff', '--cached', '--unified=0',
                '--diff-filter=ACMRTUXB')
    findings = []
    path, lineno = '', 0
    for ln in diff.splitlines():
        if ln.startswith('+++ b/'):
            path = ln[6:]
        elif ln.startswith('@@'):
            m = re.search(r'\+(\d+)', ln)
            lineno = int(m.group(1)) if m else 0
        elif ln.startswith('+') and not ln.startswith('+++'):
            findings += _check_line(path, lineno, ln[1:])
            lineno += 1
    return findings


def scan_range(rev_range: str) -> list[str]:
    """Новые строки всех коммитов диапазона (pre-push: remote..HEAD)."""
    diff = _git('diff', rev_range, '--unified=0')
    findings = []
    path, lineno = '', 0
    for ln in diff.splitlines():
        if ln.startswith('+++ b/'):
            path = ln[6:]
        elif ln.startswith('@@'):
            m = re.search(r'\+(\d+)', ln)
            lineno = int(m.group(1)) if m else 0
        elif ln.startswith('+') and not ln.startswith('+++'):
            findings += _check_line(path, lineno, ln[1:])
            lineno += 1
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description='проверка на утечку личных данных')
    ap.add_argument('--staged', action='store_true', help='только индекс (pre-commit)')
    ap.add_argument('--range', dest='rev_range', help='диапазон ревизий (pre-push)')
    a = ap.parse_args()
    findings = scan_staged() if a.staged else (scan_range(a.rev_range) if a.rev_range else scan_files())
    if findings:
        print('ПРОВАЛ: похоже на личную/внутреннюю информацию:', file=sys.stderr)
        for f in findings:
            print('  ' + f, file=sys.stderr)
        print('Исправь или добавь ложное срабатывание в ALLOW (tools/check_privacy.py).',
              file=sys.stderr)
        print('Обход (осознанно!): git commit --no-verify / push --no-verify', file=sys.stderr)
        return 1
    print('privacy-check: чисто')
    return 0


if __name__ == '__main__':
    sys.exit(main())
