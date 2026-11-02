# -*- coding: utf-8 -*-
"""Парсеры источников: ibases.v8i — общий список баз 1С (формат 1С, читает и штатный стартер).

  ibases.v8i  — INI-подобный формат (BOM UTF-8): секции-базы [GUID] с Connect=/ID=,
                вложенные папки через Folder=/OrderInTree=.
"""
import configparser
import re
from pathlib import Path

from .paths import DATA_DIR, V8I_PATH


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        return path.read_text(encoding='cp1251', errors='replace')


def _ini(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('=',))
    cp.optionxform = str  # сохраняем регистр ключей
    cp.read_string(text)
    return cp


def parse_v8i(path: Path = V8I_PATH) -> list[dict]:
    """Секции ibases.v8i в порядке файла; '__name__' = имя секции (имя базы)."""
    cp = _ini(_read_text(path))
    out = []
    for sec in cp.sections():
        d = dict(cp._sections[sec])
        d['__name__'] = sec
        out.append(d)
    return out




def sanitize_path(p: str) -> str:
    """Починить путь из битого Connect: вернуть буквы вместо управляющих символов,
    схлопнуть удвоенные обратные слэши (кроме UNC-префикса \\\\server)."""
    s = (p or '').strip().strip('"')
    if not s:
        return s
    s = ''.join(_CTRL_LETTER.get(ch, ch) if ch < ' ' else ch for ch in s)
    unc = s.startswith('\\\\')
    body = s[2:] if unc else s
    while '\\\\' in body:
        body = body.replace('\\\\', '\\')
    return ('\\\\' if unc else '') + body


def parse_connect(connect: str) -> tuple[str, str, str, str]:
    """'File="C:\\x";' -> (path, '', '', '');  'Srvr="s:port";Ref="db";' -> ('', srvr, ref, '');
    'ws="https://…"' (веб-база) -> ('', '', '', url)."""
    parts = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', connect or '')}
    return (sanitize_path(parts.get('file', '')), parts.get('srvr', ''),
            parts.get('ref', ''), parts.get('ws', ''))