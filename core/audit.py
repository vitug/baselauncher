# -*- coding: utf-8 -*-
"""Аудит действий пользователя: data/audit.log (JSONL, ротация).

Пишется middleware сервера (app/main.py): каждое не-GET обращение к API —
запуск, сохранение настроек, удаление, чистка кэша, перевод платформ и т.п.
Пароли и паролеподобные поля маскируются '***' до записи на диск.
Читается через GET /api/audit?tail=N (последние N записей).

Формат строки: {"ts": "...", "action": "POST /api/launch", "detail": {...}}
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .paths import DATA_DIR

AUDIT_LOG = DATA_DIR / 'audit.log'
ROTATE_BYTES = 2 * 1024 * 1024          # при превышении — audit.log.1
KEEP = 1                                # хранить один предыдущий

# поля, которые никогда не пишем как есть
_SECRET_HINTS = ('pwd', 'password', 'pass', 'secret')


def _mask(obj):
    """Рекурсивно замаскировать паролеподобные значения."""
    if isinstance(obj, dict):
        return {k: ('***' if any(h in str(k).lower() for h in _SECRET_HINTS)
                    and v not in (None, '', False)
                    else _mask(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:500] + '…'
    return obj


def _rotate_if_needed():
    try:
        if AUDIT_LOG.is_file() and AUDIT_LOG.stat().st_size > ROTATE_BYTES:
            old = AUDIT_LOG.with_suffix('.log.1')
            if old.exists():
                old.unlink()
            os.replace(AUDIT_LOG, old)
    except OSError:
        pass


def log(action: str, detail=None):
    """Записать действие. detail — словарь/строка (маскируется)."""
    rec = {'ts': datetime.now().isoformat(timespec='seconds'),
           'action': action,
           'detail': _mask(detail if detail is not None else {})}
    try:
        _rotate_if_needed()
        with AUDIT_LOG.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass                                     # аудит не должен ронять действие


def tail(n: int = 50) -> list[dict]:
    """Последние n записей (новые в конце)."""
    if not AUDIT_LOG.is_file():
        return []
    try:
        lines = AUDIT_LOG.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-max(1, min(n, 500)):]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out
