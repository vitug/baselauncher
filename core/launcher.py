# -*- coding: utf-8 -*-
"""Запуск баз: командная строка собирается СТРОКОЙ и передаётся в Popen как есть.

1С парсит свою командную строку не по правилам CRT: значение
/IBConnectionString должно идти литеральным текстом File="..."; — как это
делает SM. Если отдать список аргументов в subprocess, Python
заэкранирует кавычки (File=\\"...\\") и 1С покажет
LocalFileAccessError «Неправильный путь к файлу '1Cv8.cdn'.
Схема не зарегистрирована». Поэтому: build_cmd() -> строка -> Popen(строка).
"""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

MODES = {'enterprise': 'ENTERPRISE', 'designer': 'DESIGNER'}
from .paths import DATA_DIR as DATA
LOG = DATA / 'launches.log'


def _q(v) -> str:
    """Значение ключа 1С — всегда в литеральных кавычках (стиль SM)."""
    return '"' + str(v) + '"'


def build_cmd(exe: str, mode: str, connect: str,
              extra_keys: list[str] | None = None, storage: dict | None = None,
              user: str = '', pwd: str = '') -> str:
    """Собрать командную строку запуска (строкой, SM-стиль).

    extra_keys — разовые ключи момента (передаются телом запроса, не хранятся);
    user/pwd — пользователь и пароль ИБ (/N /P, настройки базы как в SM).
    """
    if mode not in MODES:
        raise ValueError(f'неизвестный режим: {mode}')
    if not exe:
        raise ValueError('не задан exe платформы')
    conn = (connect or '').strip().rstrip(';')
    if not conn:
        raise ValueError('пустая строка подключения')
    parts = [f'"{exe}"', MODES[mode]]
    if extra_keys:
        parts += [str(k) for k in extra_keys]
    if user:
        parts += ['/N', _q(user)]
    if pwd:
        parts += ['/P', _q(pwd)]
    if mode == 'designer' and storage and storage.get('dir'):
        # подключение к хранилищу конфигурации (ключи конфигуратора)
        parts += ['/ConfigurationRepositoryF', _q(storage['dir'])]
        if storage.get('user'):
            parts += ['/ConfigurationRepositoryN', _q(storage['user'])]
        if storage.get('password'):
            parts += ['/ConfigurationRepositoryP', _q(storage['password'])]
    # Веб-база (ws="https://…"). Проверено матрицей (bl_ws_matrix.py, bl_ws_ibstring2.py):
    # ПОЗИЦИОННЫЙ URL у 1С 8.3.27 обрезается — клиент ищет хост «d.1c.ru» вместо «sd.1c.ru»
    # (DNS 11001, «Этот хост неизвестен») и показывает «Не удается установить соединение».
    # Через /IBConnectionString "ws=""URL""" тот же клиент доходит до окна входа в базу,
    # поэтому ws идёт общим путём ниже (с удвоением кавычек), без особого случая.
    # Значение /IBConnectionString — в кавычках, внутренние кавычки удваиваются:
    #   /IBConnectionString "File=""D:\база с пробелом"";"
    # Иначе 1С обрывает значение на первом пробеле, не понимает строку подключения
    # и открывает стартовое окно «Запуск 1С:Предприятия» вместо самой базы.
    parts += ['/IBConnectionString', '"' + conn.replace('"', '""') + '"']
    # Автоподбор версии платформы: у серверных баз версия клиента должна совпадать с версией
    # сервера (8.3.27.2325 против 8.5.1.1434 → «Различаются версии клиента и сервера»). С этими
    # ключами клиент сам спрашивает сервер и перезапускает нужную установленную платформу
    # (так же делает SM: /AppAutoCheckMode /AppAutoCheckVersion). Если нужной версии
    # в системе нет — 1С покажет своё сообщение, его поймает сторож.
    joined = ' '.join(parts).lower()
    if '/appautocheckversion' not in joined:
        parts += ['/AppAutoCheckMode', '/AppAutoCheckVersion']
    return ' '.join(parts)


def launch(cmd: str) -> int:
    """Запустить команду-строку, вернуть PID. Создаётся отдельная группа,
    чтобы закрытие сервера не убивало запущенные базы."""
    p = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return p.pid


def log_launch(base: str, mode: str, pid: int, cmd: str, guid: str | None = None):
    # пароли в командной строке в лог не пишем
    safe = re.sub(r'(/(?:P|ConfigurationRepositoryP))\s+"[^"]*"', r'\1 "***"', cmd or '')
    rec = {'ts': datetime.now().isoformat(timespec='seconds'),
           'base': base, 'mode': mode, 'pid': pid, 'cmd': safe}
    if guid:
        rec['guid'] = guid
    with LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def history() -> list[dict]:
    """Сводка запусков по базам: последний запуск, сколько всего, режим.

    Ключ — guid (в новых записях), иначе имя базы (старые записи без guid).
    """
    if not LOG.is_file():
        return []
    agg: dict[str, dict] = {}
    for ln in LOG.read_text(encoding='utf-8').splitlines():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        ts = r.get('ts') or ''
        key = (r.get('guid') or '').lower() or ('#' + (r.get('base') or ''))
        cur = agg.get(key)
        if cur is None:
            agg[key] = {'guid': (r.get('guid') or '').lower(), 'name': r.get('base'),
                        'mode': r.get('mode'), 'last_ts': ts, 'count': 1}
        else:
            cur['count'] += 1
            if ts > (cur['last_ts'] or ''):
                cur['last_ts'] = ts
                cur['mode'] = r.get('mode')
                cur['name'] = r.get('base')
            if not cur['guid'] and r.get('guid'):
                cur['guid'] = r['guid'].lower()
    return sorted(agg.values(), key=lambda x: x['last_ts'] or '', reverse=True)


def tail_log(n: int = 50) -> list[dict]:
    if not LOG.is_file():
        return []
    lines = LOG.read_text(encoding='utf-8').splitlines()
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out
