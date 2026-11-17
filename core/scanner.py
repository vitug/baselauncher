# -*- coding: utf-8 -*-
"""Сканер установленных платформ 1С .

Ищет <корень>/<версия>/bin/1cv8.exe в стандартных каталогах и в хранилище пользователя.
"""
import struct
from pathlib import Path

from .model import Platform

# Порядок = приоритет при автоподборе (первый корень предпочтительнее)
SCAN_ROOTS = [
    r'C:\Program Files\1cv8',
    r'C:\Program Files (x86)\1cv8',
    r'C:\Program Files\1cv82',       # старые 8.2 (там же лежит 1cv8s.exe — серверный)
    r'C:\Program Files (x86)\1cv82',
    r'C:\Program Files\1cv81',
    r'C:\Program Files (x86)\1cv81',  # 8.1.15.14 — лежала bin-без-версии, переложена в <ver>/bin
]


def _extra_roots() -> list[str]:
    """Локальное хранилище готовых платформ из config/env (внутренние пути в коде не упоминаем)."""
    try:
        from core import platforms_share
        _, local = platforms_share._special()
        return [local] if local else []
    except Exception:                                    # noqa: BLE001
        return []


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
    for root_s in (roots or (SCAN_ROOTS + _extra_roots())):
        root = Path(root_s)
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if not d.is_dir():
                continue
            exe = d / 'bin' / '1cv8.exe'
            thin = d / 'bin' / '1cv8c.exe'
            if not exe.is_file() and not thin.is_file():
                # «плоская» версия (готовые каталоги с сетевых хранилищ): exe прямо в d
                exe = d / '1cv8.exe'
                thin = d / '1cv8c.exe'
            if not exe.is_file() and not thin.is_file():
                continue
            out.append(Platform(
                key=d.name,
                exe=str(exe) if exe.is_file() else '',
                exe_thin=str(thin) if thin.is_file() else '',
                bitness=pe_bitness(str(exe if exe.is_file() else thin)),
                source=root_s,
                family='.'.join(d.name.split('.')[:2]),
            ))
    out.sort(key=lambda p: _ver_tuple(p.key), reverse=True)
    return out
