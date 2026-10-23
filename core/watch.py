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


def _drop(ev_id: str) -> bool:
    """Убрать событие (например, окно базы всё-таки появилось — ложная тревога)."""
    with _LOCK:
        for i, e in enumerate(_EVENTS):
            if e.get('id') == ev_id:
                del _EVENTS[i]
                return True
    return False


def _alive(pid: int) -> bool:
    """Процесс жив? (tasklist через subprocess — без psutil)."""
    return _pid_alive(pid)


def _unlink(p: Path) -> None:
    """Тихо удалить служебный снимок-пробу."""
    try:
        Path(p).unlink(missing_ok=True)
    except Exception:                   # noqa: BLE001
        pass


def _pid_alive(pid: int) -> bool:
    try:
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return str(pid) in (r.stdout or b'').decode('cp866', 'replace')
    except Exception:                       # noqa: BLE001
        return False


def _windows_of(pid: int) -> list[tuple[int, str, str]]:
    """(hwnd, класс, заголовок) для видимых окон процесса."""
    out = []

    def cb(hwnd, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            cb2 = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cb2, 256)
            out.append((hwnd, cb2.value, buf.value))
        return True
    u32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return out


def _ready(pid: int) -> tuple[str, int]:
    """(заголовок окна, если он уже не сплэш | '', hwnd видимого диалога-сообщения 1С | 0).

    Внимание: у открывшейся базы заголовок главного окна 1С часто ОСТАЁТСЯ
    «Загрузка конфигурационной информации…» (проверено: 75 с и больше), поэтому по одному
    заголовку готовность определять нельзя — только как ранний признак «уже открылось».
    """
    ready = ''
    dialog = 0
    for hwnd, cls, title in _windows_of(pid):
        tt = (title or '').strip()
        if cls.startswith('V8TopLevelFrame'):
            if tt and not any(tt.startswith(s) for s in SPLASH):
                ready = tt
            continue
        if cls.startswith('V8ConfirmationWindow') or cls.startswith('V8MessageBox'):
            dialog = dialog or hwnd
    return ready, dialog


def _ink_ratio(path: str) -> float:
    """Доля «не фоновых» пикселей в BMP (0 — пустое окно, 0.01+ — текст/иконки)."""
    try:
        data = Path(path).read_bytes()
        off = int.from_bytes(data[10:14], 'little')
        w = int.from_bytes(data[18:22], 'little', signed=True)
        h = int.from_bytes(data[22:26], 'little', signed=True)
        bpp = int.from_bytes(data[28:30], 'little')
        if bpp != 24 or not w or not h:
            return 0.0
        row = ((w * 3 + 3) // 4) * 4
        px = data[off:]
        colors: dict[tuple, int] = {}
        total = 0
        for y in range(abs(h)):
            base = y * row
            for x in range(0, w * 3, 3):
                i = base + x
                if i + 2 >= len(px):
                    break
                c = (px[i], px[i + 1], px[i + 2])
                colors[c] = colors.get(c, 0) + 1
                total += 1
        if not total:
            return 0.0
        return 1.0 - max(colors.values()) / total
    except Exception:                   # noqa: BLE001
        return 0.0


def _message_window(pid: int) -> tuple[int, str]:
    """(hwnd, снимок) окна-сообщения 1С, иначе (0, '').

    Класс/размер не помогают: служебное `V8TopLevelFrameSDIsec` 413x174 при обычном запуске
    выглядит так же, как окно «Не удается установить соединение» 414x163. Различаем по
    содержимому: у сообщения есть текст/иконка, служебное окно пустое (см. _ink_ratio).
    """
    GWL_STYLE = -16
    WS_CHILD = 0x40000000
    probe = _dir() / ('_probe_%d.bmp' % pid)      # один файл на процесс: не мусорим
    for hwnd, cls, _title in _windows_of(pid):
        if not cls.startswith('V8'):
            continue
        if cls.startswith('V8ToolTip'):
            continue
        # главное окно (заставка/база) — не сообщение; окно-сообщение живёт в ...SDIsec
        if cls.startswith('V8TopLevelFrame') and not cls.startswith('V8TopLevelFrameSDIsec'):
            continue
        if (_title or '').strip():
            continue                            # у сообщений заголовок пустой
        style = u32.GetWindowLongW(hwnd, GWL_STYLE) or 0
        if style & WS_CHILD:
            continue
        r = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if not (200 <= w <= 1400 and 90 <= h <= 900):
            continue
        if not _shot(hwnd, pid, target=probe):
            continue
        if _ink_ratio(str(probe)) >= 0.008:         # есть содержимое → это сообщение
            keep = _dir() / ('%s_%s_%d.bmp' % (datetime.datetime.now().strftime('%Y%m%d-%H%M%S'),
                                               pid, hwnd))
            try:
                keep.write_bytes(probe.read_bytes())
            except OSError:
                keep = probe
            return hwnd, str(keep)
    return 0, ''


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG),
                ('biPlanes', wt.WORD), ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD), ('biClrImportant', wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


def _shot(hwnd: int, pid: int, target: Path | None = None) -> str:
    """BMP содержимого одного окна (через DC окна — работает и на отключённом RDP-экране)."""
    rect = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 80 or h < 50:
        return ''
    src = u32.GetWindowDC(hwnd)
    if not src:
        return ''
    mdc = g32.CreateCompatibleDC(src)
    bmp = g32.CreateCompatibleBitmap(src, w, h)
    g32.SelectObject(mdc, bmp)
    ok = g32.BitBlt(mdc, 0, 0, w, h, src, 0, 0, 0x00CC0020 | 0x40000000)
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
    u32.ReleaseDC(hwnd, src)
    if not ok:
        return ''
    if target is None:
        fname = '%s_%s_%d.bmp' % (datetime.datetime.now().strftime('%Y%m%d-%H%M%S'), pid, hwnd)
        path = _dir() / fname
    else:
        path = target
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


def capture(pid: int, name: str) -> list[str]:
    """Снимки видимых окон процесса (сначала самые мелкие — это и есть сообщения 1С)."""
    items = []

    def cb(h, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != pid or not u32.IsWindowVisible(h):
            return True
        r = wt.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w >= 80 and hh >= 50:
            items.append((w * hh, h))
        return True
    u32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    items.sort()                       # мелкие окна — первыми: диалог ошибки меньше главного
    out = []
    for _area, hwnd in items[:3]:
        p = _shot(hwnd, pid)
        if p:
            out.append(p)
    return out


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
    if kind == 'dialog':
        parts.append('1С показала сообщение при подключении — смотри снимок окна ниже.')
    elif kind == 'stuck':
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
          family_versions: list[str] | None = None, watch_timeout: float = 120.0) -> None:
    """Фоновый сторож одного запуска (вызывать в отдельном потоке).

    Тревогу поднимаем только по фактам: 1С показала диалог-сообщение или процесс умер,
    не открыв базу. Таймаут сам по себе тревогой НЕ считается: заголовок окна у нормально
    открытой базы часто остаётся сплэшным, и по времени «медленный старт» не отличить
    от ошибки — лучше промолчать, чем пугать ложной тревогой (предпроверка соединения
    ловит недоступный сервер и битый путь заранее).
    """
    started = time.time()
    ready = ''
    dialog = 0
    dialog_shot = ''
    probe = _dir() / ('_probe_%d.bmp' % pid)
    while time.time() - started < watch_timeout:
        time.sleep(1.0)
        if not _alive(pid):
            break
        ready, dialog = _ready(pid)
        if ready:
            _unlink(probe)
            return                      # нормальный запуск: окно базы появилось
        if dialog:
            break                       # 1С показала сообщение — сразу снимаем его
        if not _alive(pid):
            break
        d, shot_now = _message_window(pid)
        if d:
            dialog, dialog_shot = d, shot_now
            break
    if ready:
        _unlink(probe)
        return
    if _alive(pid) and not dialog:
        _unlink(probe)
        # процесс жив и молчит: считаем, что база открылась медленно
        return
    if dialog and _alive(pid):
        kind = 'dialog'
        shots = [dialog_shot] if dialog_shot else ([_shot(dialog, pid)] or capture(pid, name))
    else:
        kind = 'exited'
        shots = []
    shot = shots[0] if shots else ''
    ev = {'id': '%d-%d' % (int(time.time() * 1000), pid), 'ts': datetime.datetime.now().isoformat(timespec='seconds'),
          'kind': kind, 'pid': pid, 'guid': guid, 'name': name, 'exe': exe,
          'client_version': _ver(exe), 'conn': conn, 'shot': shot, 'shots': shots,
          'title': ready, 'hint': hint(kind, exe, conn, family_versions or [])}
    _add(ev)
    try:
        log = _dir().parent / 'launch-errors.json'
        old = json.loads(log.read_text(encoding='utf-8')) if log.is_file() else []
        old.insert(0, ev)
        log.write_text(json.dumps(old[:40], ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception:                   # noqa: BLE001
        pass
    _unlink(probe)


def start(pid: int, guid: str, name: str, exe: str, conn: str,
          family_versions: list[str] | None = None) -> None:
    threading.Thread(target=watch, name='bl-watch-%d' % pid, daemon=True,
                     kwargs=dict(pid=pid, guid=guid, name=name, exe=exe, conn=conn,
                                 family_versions=family_versions)).start()
