"""Сторож запуска 1С: замечает, что клиент не открылся (в т.ч. «Несоответствие версий клиента и сервера»).

1С рисует сообщения сама (кастомные окна), поэтому текст из окна через Win32 не читается.
Что делаем вместо этого:
  * следим за процессом и его окнами: если через N секунд вместо окна базы всё ещё висит
    сплэш «Загрузка конфигурационной информации…» (или процесс вообще умер) — это провал запуска;
  * сохраняем снимок окна 1С в data/errors/*.bmp, чтобы человек увидел точный текст ошибки
    прямо в стартере, не переключаясь в 1С;
  * в событие пишем версию клиента, строку подключения и подсказку (в т.ч. про несоответствие
    версий клиента и сервера и список установленных сборок того же семейства).
"""
from __future__ import annotations

import ctypes
import datetime
import json
import subprocess
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

from . import procs

u32 = ctypes.WinDLL('user32', use_last_error=True)
g32 = ctypes.WinDLL('gdi32', use_last_error=True)

SPLASH = ('Загрузка конфигурационной информации', 'Запуск 1С:Предприятия',
          'Запуск 1С:Предприятие', 'Подключение')

_EVENTS: list[dict] = []          # newest first
_LOCK = threading.Lock()
_DIR: Path | None = None


def _dir() -> Path:
    global _DIR
    if _DIR is None:
        _DIR = Path(__file__).resolve().parent.parent / 'data' / 'errors'
        _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR


def events() -> list[dict]:
    with _LOCK:
        return list(_EVENTS)


def clear() -> None:
    with _LOCK:
        _EVENTS.clear()


def _add(ev: dict) -> None:
    with _LOCK:
        _EVENTS.insert(0, ev)
        del _EVENTS[40:]


def _alive(pid: int) -> bool:
    """Процесс жив? (tasklist через subprocess — без psutil)."""
    try:
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return str(pid) in (r.stdout or b'').decode('cp866', 'replace')
    except Exception:                       # noqa: BLE001
        return False


def _visible_titles(pid: int) -> list[str]:
    """Заголовки видимых окон процесса."""
    out = []

    def cb(hwnd, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                out.append(buf.value)
        return True
    u32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return out


def _ready(pid: int) -> str:
    """Есть ли у процесса нормальное окно базы: возвращает его заголовок или ''."""
    for t in _visible_titles(pid):
        tt = t.strip()
        if not tt:
            continue
        if any(tt.startswith(s) for s in SPLASH):
            continue
        return tt
    return ''


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
                ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD), ('biClrImportant', wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


def capture(pid: int, name: str) -> str:
    """Снимок области окна 1С — снимаем ЭКРАН, а не содержимое окна.
    Сообщения 1С — отдельные окна, нарисованные поверх заставки, поэтому BitBlt с DC окна
    даёт только заставку без текста ошибки. Снимок с экрана показывает то, что видит человек.
    """
    hwnd = 0
    best = 0

    def cb(h, _):
        nonlocal hwnd, best
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(h) and u32.GetWindowTextLengthW(h):
            r = wt.RECT()
            u32.GetWindowRect(h, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best:
                best, hwnd = area, h
        return True
    u32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    if not hwnd:
        return ''
    rect = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 50 or h < 30:
        return ''
    src = u32.GetDC(0)                    # весь экран: захватим и диалог поверх окна
    if not src:
        return ''
    mdc = g32.CreateCompatibleDC(src)
    bmp = g32.CreateCompatibleBitmap(src, w, h)
    g32.SelectObject(mdc, bmp)
    ok = g32.BitBlt(mdc, 0, 0, w, h, src, rect.left, rect.top, 0x00CC0020 | 0x40000000)
    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = h          # снизу вверх
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 24
    info.bmiHeader.biCompression = 0
    stride = ((w * 3 + 3) // 4) * 4
    buf = ctypes.create_string_buffer(stride * h)
    g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(info), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(0, src)
    if not ok:
        return ''
    fname = '%s_%s.bmp' % (datetime.datetime.now().strftime('%Y%m%d-%H%M%S'), pid)
    path = _dir() / fname
    hdr = (b'BM' + (14 + 40 + len(buf.raw)).to_bytes(4, 'little') + b'\0\0\0\0'
           + (14 + 40).to_bytes(4, 'little') + (40).to_bytes(4, 'little')
           + w.to_bytes(4, 'little', signed=True) + h.to_bytes(4, 'little', signed=True)
           + (1).to_bytes(2, 'little') + (24).to_bytes(2, 'little')
           + (0).to_bytes(4, 'little') + len(buf.raw).to_bytes(4, 'little')
           + (2835).to_bytes(4, 'little', signed=True) * 2 + (0).to_bytes(4, 'little') * 2)
    try:
        path.write_bytes(hdr + buf.raw)
    except OSError:
        return ''
    return str(path)


def _ver(exe: str) -> str:
    for part in re_split_path(exe):
        if part and part[0].isdigit() and part.count('.') >= 3:
            return part
    return ''


def re_split_path(p: str) -> list[str]:
    return [x for x in (p or '').replace('/', '\\').split('\\') if x]


def hint(kind: str, exe: str, conn: str, family_versions: list[str]) -> str:
    ver = _ver(exe)
    parts = []
    if kind == 'stuck':
        parts.append('1С осталась на «Загрузка конфигурационной информации…» — подключиться не удалось.')
    else:
        parts.append('Процесс 1С завершился, не открыв окно базы.')
    parts.append('Частые причины: несоответствие версий клиента и сервера, нет доступа к серверу '
                 '(VPN/сеть), занята лицензия, неверный пользователь/пароль, '
                 'база снята с публикации.')
    if ver:
        fam = '.'.join(ver.split('.')[:3])
        same = [v for v in family_versions if v.startswith(fam) and v != ver]
        parts.append('Клиент: %s.' % ver)
        if same:
            parts.append('Другие установленные сборки %s: %s — если сервер обновлён, '
                         'переключи платформу базы на его версию.' % (fam, ', '.join(sorted(same))))
    if 'Srvr=' in (conn or ''):
        parts.append('Подключение: %s' % conn)
    return ' '.join(parts)


def watch(pid: int, guid: str, name: str, exe: str, conn: str,
          family_versions: list[str] | None = None, splash_timeout: float = 18.0) -> None:
    """Фоновый сторож одного запуска (вызывать в отдельном потоке)."""
    started = time.time()
    ready = ''
    while time.time() - started < splash_timeout:
        time.sleep(1.0)
        if not _alive(pid):
            break
        ready = _ready(pid)
        if ready:
            return                      # нормальный запуск: окно базы появилось
    if ready:
        return
    if _alive(pid):
        kind = 'stuck'
        shot = capture(pid, name)
    else:
        kind = 'exited'
        shot = ''
    ev = {'id': '%d-%d' % (int(time.time() * 1000), pid), 'ts': datetime.datetime.now().isoformat(timespec='seconds'),
          'kind': kind, 'pid': pid, 'guid': guid, 'name': name, 'exe': exe,
          'client_version': _ver(exe), 'conn': conn, 'shot': shot,
          'title': ready, 'hint': hint(kind, exe, conn, family_versions or [])}
    _add(ev)
    try:
        log = _dir().parent / 'launch-errors.json'
        old = json.loads(log.read_text(encoding='utf-8')) if log.is_file() else []
        old.insert(0, ev)
        log.write_text(json.dumps(old[:40], ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception:                   # noqa: BLE001
        pass


def start(pid: int, guid: str, name: str, exe: str, conn: str,
          family_versions: list[str] | None = None) -> None:
    threading.Thread(target=watch, name='bl-watch-%d' % pid, daemon=True,
                     kwargs=dict(pid=pid, guid=guid, name=name, exe=exe, conn=conn,
                                 family_versions=family_versions)).start()
