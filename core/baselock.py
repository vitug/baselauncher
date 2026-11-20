# -*- coding: utf-8 -*-
"""Блокировка базы на обслуживание: запрет запуска из стартера + завершение сеансов.

Режим обслуживания — для операций, требующих монопольного доступа (загрузка .dt,
загрузка XML-конфигурации, сверка/починка базы): во время окна стартер не даёт
запустить базу, а живые клиентские сеансы можно завершить принудительно.

Ключи в config.json базы: maintenance_until (epoch, сек), maintenance_note.
Блокировка самоснимается по истечении TTL (по умолчанию 12 часов).

Сеансы 1С завершаются ТОЛЬКО по совпадению базы в командной строке процесса —
никогда «по имени образа» (можно убить чужой сеанс другой базы).
"""
import subprocess
import time

from core import config
from core import procs

DEFAULT_TTL_MIN = 720          # 12 часов — авто-снятие блокировки


def _norm(p: str) -> str:
    try:
        return procs._norm_path(p)
    except Exception:                            # noqa: BLE001
        return (p or '').strip().lower()


def _base_path(connect: str) -> str:
    return _norm(procs.parse_session(connect or '').get('path') or '')


def sessions_of(base: dict) -> list[dict]:
    """Живые клиентские сеансы, относящиеся к этой базе (файловый путь или Srvr+Ref)."""
    connect = base.get('connect') or ''
    path = _base_path(connect)
    srv = ref = ''
    if not path:
        s = procs.parse_session(connect)
        srv, ref = _norm(s.get('server') or ''), _norm(s.get('ref') or '')
    out = []
    for s in procs.running_sessions():
        if path and _norm(s.get('path') or '') == path:
            out.append(s)
        elif srv and ref and _norm(s.get('server') or '') == srv \
                and _norm(s.get('ref') or '') == ref:
            out.append(s)
    return out


def terminate_sessions(base: dict, timeout: float = 20.0) -> dict:
    """Принудительно завершить сеансы этой базы (taskkill /F по pid)."""
    victims = sessions_of(base)
    killed, failed = [], []
    for s in victims:
        pid = s.get('pid')
        if not pid:
            continue
        try:
            r = subprocess.run(['taskkill', '/PID', str(pid), '/F', '/T'],
                               capture_output=True, timeout=timeout,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (killed if r.returncode == 0 else failed).append(pid)
        except Exception as e:                    # noqa: BLE001
            failed.append(pid)
    return {'ok': not failed, 'killed': killed, 'failed': failed,
            'count': len(victims)}


def status(base: dict) -> dict:
    """Состояние блокировки: активна ли, до какого времени, сколько осталось."""
    until = float(base.get('maintenance_until') or 0)
    now = time.time()
    active = until > now
    return {'active': active, 'until': until or None,
            'remaining_min': round((until - now) / 60, 1) if active else 0,
            'note': base.get('maintenance_note') or ''}


def check_blocked(base: dict) -> str:
    """'' — базу можно запускать, иначе причина отказа (с автоснятием истёкшей)."""
    st = status(base)
    if not st['active']:
        if base.get('maintenance_until'):
            try:                                   # истёкшая — подчистить
                config.set_base(base.get('guid') or '',
                                {'maintenance_until': 0, 'maintenance_note': ''})
            except Exception:                      # noqa: BLE001
                pass
        return ''
    return ('база закрыта на обслуживание (ещё %.0f мин%s) — запустите позже '
            'или снимите блокировку' % (
                st['remaining_min'],
                '; %s' % st['note'] if st['note'] else ''))


def lock(guid: str, minutes: float = DEFAULT_TTL_MIN, note: str = '',
         base: dict | None = None, kick_sessions: bool = False) -> dict:
    """Поставить базу на обслуживание. Опционально завершить живые сеансы."""
    until = time.time() + max(1.0, float(minutes)) * 60
    try:
        config.set_base(guid, {'maintenance_until': until, 'maintenance_note': note or ''})
    except Exception as e:                         # noqa: BLE001
        return {'ok': False, 'error': str(e)}
    out = {'ok': True, 'until': until, 'minutes': minutes, 'note': note or ''}
    if kick_sessions and base is not None:
        out['sessions'] = terminate_sessions(base)
    return out


def unlock(guid: str) -> dict:
    """Снять блокировку обслуживания."""
    try:
        config.set_base(guid, {'maintenance_until': 0, 'maintenance_note': ''})
        return {'ok': True}
    except Exception as e:                         # noqa: BLE001
        return {'ok': False, 'error': str(e)}
