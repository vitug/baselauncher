# -*- coding: utf-8 -*-
"""Сканер установленных платформ 1С (аналог FUN_0089d058 из SM, но без слотов 8.1-8.4).

Ищет <корень>/<версия>/bin/1cv8.exe в стандартных каталогах и в хранилище пользователя.
"""
import struct
from pathlib import Path

from .model import Platform

# Порядок = приоритет при автоподборе (первый корень предпочтительнее)
SCAN_ROOTS = [
    r'C:\Program Files\1cv8',
    r'C:\Program Files (x86)\1cv8',
    r'D:\storage1C\ПлатформаКвас',   # личное хранилище пользователя (разведано при импорте из SM)
]


def pe_bitness(exe: str) -> str:
    """Разрядность PE-файла по заголовку ('x64' | 'x86' | '?')."""
    try:
        with open(exe, 'rb') as f:
            if f.read(2) != b'MZ':
                return '?'
            f.seek(0x3C)
            e_lfanew = struct.unpack('<I', f.read(4))[0]
            f.seek(e_lfanew + 4)
            machine = struct.unpack('<H', f.read(2))[0]
        return 'x64' if machine == 0x8664 else 'x86'
    except OSError:
        return '?'


def _ver_tuple(key: str):
    try:
        return tuple(int(x) for x in key.split('.'))
    except ValueError:
        return (0,)


def scan_platforms(roots: list[str] | None = None) -> list[Platform]:
    out = []
    for root_s in (roots or SCAN_ROOTS):
        root = Path(root_s)
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if not d.is_dir():
                continue
            exe = d / 'bin' / '1cv8.exe'
            if not exe.is_file():
                continue
            thin = d / 'bin' / '1cv8c.exe'
            out.append(Platform(
                key=d.name,
                exe=str(exe),
                exe_thin=str(thin) if thin.is_file() else '',
                bitness=pe_bitness(str(exe)),
                source=root_s,
                family='.'.join(d.name.split('.')[:2]),
            ))
    out.sort(key=lambda p: _ver_tuple(p.key), reverse=True)
    return out
