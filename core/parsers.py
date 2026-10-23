# -*- coding: utf-8 -*-
"""Парсеры источников: ibases.v8i, v8config.smc, settings.cnf (SM 1.4).

Форматы разведаны реверсом launcher.exe (см. ~/wiki/tech/entities/startmanager14-copy.md):
  ibases.v8i    — INI (utf-8-sig): секция = имя базы, ключи Connect/ID/Folder/OrderInTree/App/WA.
  v8config.smc  — INI: секция [GUID базы]: V8AppFile/V8UseAppFile (явный exe), Description,
                  учётки (пароли зашифрованы — НЕ импортируем), LaunchDate, StorageDir.
  settings.cnf  — XML SM: <Launch><V8xAppFile> — exe по слотам платформ 8.1..8.4.
"""
import configparser
import re
import xml.etree.ElementTree as ET
from pathlib import Path

V8I_PATH = Path(__import__('os').path.expandvars(r'%APPDATA%\1C\1CEStart\ibases.v8i'))
SM_DIR = Path(__import__('os').path.expandvars(r'%APPDATA%\SM14\SMSettings'))
V8CONFIG_PATH = SM_DIR / 'v8config.smc'
SETTINGS_PATH = SM_DIR / 'settings.cnf'


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


def parse_v8config(path: Path = V8CONFIG_PATH) -> dict[str, dict]:
    """{guid_lowercase: {ключ: значение}} из v8config.smc SM."""
    if not path.is_file():
        return {}
    cp = _ini(_read_text(path))
    out = {}
    for sec in cp.sections():
        s = sec.strip().strip('[]').lower()
        if re.fullmatch(r'[0-9a-f-]{36}', s):
            out[s] = dict(cp._sections[sec])
    return out


def parse_settings_slots(path: Path = SETTINGS_PATH) -> dict[str, str]:
    """Явные exe платформ из settings.cnf SM: {'8.3': '...', '8.5': '...'}.

    Слоты V81..V84; слот 8.4 на этом хосте занят под 8.5 (2026-10-22), поэтому
    '8.4' и '8.5' берут значение V84AppFile.
    """
    slots = {}
    if not path.is_file():
        return slots
    try:
        root = ET.parse(str(path)).getroot()
    except ET.ParseError:
        return slots
    launch = root.find('Launch')
    if launch is None:
        return slots
    for tag, fams in [('V81AppFile', ('8.1',)), ('V82AppFile', ('8.2',)),
                      ('V83AppFile', ('8.3',)), ('V84AppFile', ('8.4', '8.5'))]:
        el = launch.find(tag)
        if el is not None and (el.text or '').strip():
            for fam in fams:
                slots[fam] = el.text.strip()
    return slots


# Управляющие символы вместо букв — след «лишнего» раскодирования escape-строк:
# путь D:\bases1C после одного лишнего unicode_escape превращается в D:<BS>ases1C,
# и COM отвечает «Неправильный путь к файлу … 123(0x7B): Синтаксическая ошибка в имени».
# «\b» в пути — это обратный слэш + буква, съеденные раскодированием: вернуть оба знака.
_CTRL_LETTER = {'\x07': '\\a', '\x08': '\\b', '\x09': '\\t', '\x0a': '\\n',
                '\x0b': '\\v', '\x0c': '\\f', '\x0d': '\\r'}


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
    parts = dict(re.findall(r'(\w+)="([^"]*)"', connect or ''))
    return sanitize_path(parts.get('File', '')), parts.get('Srvr', ''), \
        parts.get('Ref', ''), parts.get('ws', '')
