# -*- coding: utf-8 -*-
"""Запись в ibases.v8i (список баз 1С:Предприятие): добавление баз и папок.

Файл живой — его читает 1С:Предприятие. Правила:
  * существующие байты НЕ переформатируются: новый блок дописывается в конец,
    удаление вырезает ровно диапазон секции;
  * шаблон тела секции берётся из самого файла (у 1С там смешанные окончания
    строк `\\r\\r\\n`, свои ключи App/WA/... — копия гарантирует совместимость);
  * кодировка UTF-8 с BOM (как пишет 1С), бэкап `ibases.v8i.bak-YYYYMMDD`
    один раз в день, запись — через временный файл + os.replace.
"""
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from .paths import V8I_PATH as V8I
_HEADER = re.compile(r'(?m)^\[(.+)\]\r?\n')


def _read() -> tuple[bytes, str]:
    raw = V8I.read_bytes()
    return raw, raw.decode('utf-8-sig')


def _sections(text: str) -> list[dict]:
    """[{name, start, end, body_start, body}] — границы секций в исходном тексте."""
    marks = [(m.start(), m.end(), m.group(1)) for m in _HEADER.finditer(text)]
    out = []
    for i, (start, body_start, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append({'name': name, 'start': start, 'end': end,
                    'body_start': body_start, 'body': text[body_start:end]})
    return out


def _backup(raw: bytes):
    bak = V8I.with_name('ibases.v8i.bak-' + datetime.now().strftime('%Y%m%d'))
    if not bak.exists():
        bak.write_bytes(raw)


def _write(text: str):
    raw, _ = _read()
    bom = raw.startswith(b'\xef\xbb\xbf')
    _backup(raw)
    tmp = V8I.with_name('ibases.v8i.tmp-bl')
    tmp.write_bytes((('\ufeff' if bom else '') + text).encode('utf-8'))
    os.replace(tmp, V8I)


def _set(body: str, key: str, value: str) -> str:
    pat = re.compile(r'(?m)^' + key + r'=[^\r\n]*')
    return pat.sub(lambda m: f'{key}={value}', body, count=1)


def _template(text: str, with_connect: bool) -> str:
    for s in _sections(text):
        if with_connect == ('Connect=' in s['body']):
            return s['body']
    raise RuntimeError('в ibases.v8i не нашлось секции-шаблона')


def _max_order(secs: list[dict], key: str, folder: str | None = None) -> float:
    vals = []
    for s in secs:
        if folder is not None:
            m = re.search(r'(?m)^Folder=([^\r\n]*)', s['body'])
            if not m or m.group(1).strip() != folder:
                continue
        m = re.search(r'(?m)^' + key + r'=([\d.\-]+)', s['body'])
        if m:
            try:
                vals.append(float(m.group(1)))
            except ValueError:
                pass
    return max(vals) if vals else 0.0


def _check_name(text: str, name: str):
    for s in _sections(text):
        if s['name'].strip().lower() == name.strip().lower():
            raise ValueError(f'секция «{name}» уже есть в ibases.v8i')


def add_base(name: str, connect: str, folder: str = '/', app: str = 'Auto') -> str:
    """Дописать базу. connect — без завершающей «;» (File="…" / Srvr="…";Ref="…")."""
    name = name.strip()
    folder = (folder or '/').strip() or '/'
    if not name:
        raise ValueError('пустое имя базы')
    # Путь мог прийти с «съеденными» слэшами (\b -> 0x08): писать в файл 1С только чистый.
    from core.parsers import sanitize_path
    _m = re.match(r'(?is)^\s*File\s*=\s*"(.*)"\s*$', connect or '')
    if _m:
        connect = 'File="%s"' % sanitize_path(_m.group(1))
    _, text = _read()
    secs = _sections(text)
    _check_name(text, name)
    guid = str(uuid.uuid4())
    body = _template(text, with_connect=True)
    body = _set(body, 'Connect', connect.rstrip(';') + ';')
    body = _set(body, 'ID', guid)
    body = _set(body, 'Folder', folder)
    body = _set(body, 'OrderInList', repr(_max_order(secs, 'OrderInList') + 1.0))
    body = _set(body, 'OrderInTree', str(int(_max_order(secs, 'OrderInTree', folder)) + 1024))
    if re.search(r'(?m)^App=', body):
        body = _set(body, 'App', app or 'Auto')
    if not text.endswith('\n'):
        text += '\r\n'
    _write(text + f'[{name}]\r\n' + body)
    return guid


def add_folder(name: str, parent: str = '/') -> str:
    name = name.strip()
    parent = (parent or '/').strip() or '/'
    if not name:
        raise ValueError('пустое имя папки')
    _, text = _read()
    secs = _sections(text)
    path = ('' if parent == '/' else parent) + '/' + name
    for s in secs:  # полный путь тоже уникален
        m = re.search(r'(?m)^Folder=([^\r\n]*)', s['body'])
        if (m and m.group(1).strip() == parent and s['name'].strip().lower() == name.lower()):
            raise ValueError(f'папка «{path}» уже есть')
    guid = str(uuid.uuid4())
    body = _template(text, with_connect=False)
    body = _set(body, 'ID', guid)
    body = _set(body, 'OrderInList', '-1')
    body = _set(body, 'Folder', parent)
    body = _set(body, 'OrderInTree', str(int(_max_order(secs, 'OrderInTree', parent)) + 1024))
    if not text.endswith('\n'):
        text += '\r\n'
    _write(text + f'[{name}]\r\n' + body)
    return guid


def move(guid: str, folder: str = '/') -> dict:
    """Перенести базу (секцию с Connect=) в другую папку: правится только Folder= и OrderInTree.

    Остальные байты секции не трогаются — 1С читает файл целиком, переписывать его не нужно.
    Возвращает {'name', 'from', 'to'}.
    """
    guid = guid.strip().lower()
    folder = (folder or '/').strip() or '/'
    _, text = _read()
    secs = _sections(text)
    if folder != '/':
        # папка должна существовать: ищем секцию-папку с таким полным путём
        found = False
        for s in secs:
            if 'Connect=' in s['body']:
                continue
            m = re.search(r'(?m)^Folder=([^\r\n]*)', s['body'])
            parent = m.group(1).strip() if m else '/'
            own = ('' if parent == '/' else parent) + '/' + s['name'].strip()
            if own == folder:
                found = True
                break
        if not found:
            raise ValueError(f'папка «{folder}» не найдена в ibases.v8i')
    for s in secs:
        m = re.search(r'(?m)^ID=([^\r\n]*)', s['body'])
        if not m or m.group(1).strip().lower() != guid:
            continue
        if 'Connect=' not in s['body']:
            raise ValueError('это папка, а не база')
        fm = re.search(r'(?m)^Folder=([^\r\n]*)', s['body'])
        old = fm.group(1).strip() if fm else '/'
        if old == folder:
            return {'name': s['name'], 'from': old, 'to': folder}
        body = _set(s['body'], 'Folder', folder)
        body = _set(body, 'OrderInTree',
                    str(int(_max_order(secs, 'OrderInTree', folder)) + 1024))
        _write(text[:s['body_start']] + body + text[s['end']:])
        return {'name': s['name'], 'from': old, 'to': folder}
    raise ValueError('секция с таким ID не найдена')


def delete(guid: str) -> str:
    """Вырезать секцию по ID (база или пустая папка). Возвращает имя секции."""
    guid = guid.strip().lower()
    _, text = _read()
    secs = _sections(text)
    for i, s in enumerate(secs):
        m = re.search(r'(?m)^ID=([^\r\n]*)', s['body'])
        if not m or m.group(1).strip().lower() != guid:
            continue
        if 'Connect=' not in s['body']:
            # папка: не удаляем, если внутри что-то есть
            fol = re.search(r'(?m)^Folder=([^\r\n]*)', s['body'])
            parent = fol.group(1).strip() if fol else '/'
            own = ('' if parent == '/' else parent) + '/' + s['name'].strip()
            for o in secs:
                of = re.search(r'(?m)^Folder=([^\r\n]*)', o['body'])
                if of and (of.group(1).strip() == own or of.group(1).strip().startswith(own + '/')):
                    raise ValueError(f'папка «{s["name"]}» не пуста — сначала уберите её содержимое')
        _backup(_read()[0])
        _write(text[:s['start']] + text[s['end']:])
        return s['name']
    raise ValueError('секция с таким ID не найдена')
