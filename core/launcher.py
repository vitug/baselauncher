# -*- coding: utf-8 -*-
r"""Сборка команд запуска 1С и запуск процессов (с логом каждой команды).

Шаблон команд сверен с живым логом SM (smanager.log):
  1cv8.exe DESIGNER /IBConnectionString File="C:\...\base"
(значение = Connect из ibases.v8i, без хвостовой ';').
"""
import datetime
import json
import subprocess
from pathlib import Path

MODES = {'enterprise': 'ENTERPRISE', 'designer': 'DESIGNER'}
LOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'launches.log'


def build_argv(exe: str, mode: str, connect: str, extra_keys: list[str] | None = None) -> list[str]:
    if mode not in MODES:
        raise ValueError(f'неизвестный режим: {mode}')
    if not exe:
        raise ValueError('не задан exe платформы')
    conn = (connect or '').strip().rstrip(';')
    if not conn:
        raise ValueError('пустая строка подключения')
    argv = [exe, MODES[mode]]
    if extra_keys:
        argv += list(extra_keys)
    argv += ['/IBConnectionString', conn]
    return argv


def launch(argv: list[str]) -> int:
    """Отпочковать 1С от сервера: вернёт PID."""
    p = subprocess.Popen(
        argv,
        cwd=str(Path(argv[0]).parent),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return p.pid


def log_launch(base_name: str, mode: str, pid: int, argv: list[str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        'ts': datetime.datetime.now().isoformat(timespec='seconds'),
        'base': base_name, 'mode': mode, 'pid': pid,
        'cmd': subprocess.list2cmdline(argv),
    }
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def tail_log(n: int = 50) -> list[dict]:
    if not LOG_PATH.is_file():
        return []
    lines = LOG_PATH.read_text(encoding='utf-8').splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
