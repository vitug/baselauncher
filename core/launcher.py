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
import subprocess
from datetime import datetime
from pathlib import Path

MODES = {'enterprise': 'ENTERPRISE', 'designer': 'DESIGNER'}
DATA = Path(__file__).parent.parent / 'data'
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
    parts += ['/IBConnectionString', conn]
    return ' '.join(parts)


def launch(cmd: str) -> int:
    """Запустить команду-строку, вернуть PID. Создаётся отдельная группа,
    чтобы закрытие сервера не убивало запущенные базы."""
    p = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return p.pid


def log_launch(base: str, mode: str, pid: int, cmd: str):
    rec = {'ts': datetime.now().isoformat(timespec='seconds'),
           'base': base, 'mode': mode, 'pid': pid, 'cmd': cmd}
    with LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


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
