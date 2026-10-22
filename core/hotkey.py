# -*- coding: utf-8 -*-
"""Глобальная горячая клавиша для окна стартера: Ctrl+Shift+D.

Регистрируется через Win32 RegisterHotKey на физическую клавишу (VK_D),
поэтому работает и в русской, и в английской раскладке.

Окно стартера — Edge в режиме `--app` (класс Chrome_WidgetWin_1), заголовок = <title> страницы.
Ищем его по заголовку и по командной строке процесса (--app=http://127.0.0.1:<port>).
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import threading

from . import procs

u32 = ctypes.WinDLL('user32', use_last_error=True)
k32 = ctypes.WinDLL('kernel32', use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_D = 0x44

HOTKEY_ID = 0xB1A5
STARTER_TITLE = 'BaseLauncher'
_state = {'registered': False, 'error': '', 'hits': 0, 'thread': None, 'window': ''}


def _win_title(hwnd: int) -> str:
    n = u32.GetWindowTextLengthW(hwnd)
    if not n:
        return ''
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid_of(hwnd: int) -> int:
    p = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return p.value


def find_starter_window(port: int = 8797) -> list[tuple[int, str, int]]:
    """Все видимые окна, похожие на окно стартера: (hwnd, title, pid)."""
    out: list[tuple[int, str, int]] = []
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        title = _win_title(hwnd)
        if not title:
            return True
        cls = _class_of(hwnd)
        if cls != 'Chrome_WidgetWin_1':
            return True
        pid = _pid_of(hwnd)
        # сначала точное совпадение заголовка, иначе — по --app в командной строке
        if STARTER_TITLE.lower() in title.lower():
            out.append((hwnd, title, pid))
            return True
        cmd = procs.cmdline(pid) or ''
        if '--app=' in cmd and ('127.0.0.1:%d' % port) in cmd:
            out.append((hwnd, title, pid))
        return True
    u32.EnumWindows(CB(cb), 0)
    out.sort(key=lambda x: 0 if STARTER_TITLE.lower() in x[1].lower() else 1)
    return out


def focus_starter(port: int = 8797) -> dict:
    """Поднять окно стартера на передний план."""
    wins = find_starter_window(port)
    if not wins:
        return {'ok': False, 'error': 'окно стартера не найдено — откройте его через start_bl.bat'}
    hwnd, title, pid = wins[0]
    fg_tid = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    cur_tid = k32.GetCurrentThreadId()
    u32.AttachThreadInput(cur_tid, fg_tid, True)
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE
    else:
        u32.ShowWindow(hwnd, 5)          # SW_SHOW
    u32.SetForegroundWindow(hwnd)
    u32.BringWindowToTop(hwnd)
    u32.AttachThreadInput(cur_tid, fg_tid, False)
    ok = u32.GetForegroundWindow() == hwnd
    _state['window'] = title
    return {'ok': True, 'foreground': ok, 'title': title, 'pid': pid, 'hwnd': hwnd}


def _loop(port: int):
    msg = wt.MSG()
    k32.GetCurrentThreadId()  # поток, владеющий регистрацией
    ok = u32.RegisterHotKey(None, HOTKEY_ID,
                            MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_D)
    if not ok:
        _state['error'] = 'RegisterHotKey не удался (err %d): клавиша занята другим приложением' \
                          % ctypes.get_last_error()
        _state['registered'] = False
        return
    _state['registered'] = True
    while True:
        r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r in (0, -1):
            break
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            try:
                focus_starter(port)
                _state['hits'] += 1
            except Exception:
                pass
    u32.UnregisterHotKey(None, HOTKEY_ID)
    _state['registered'] = False


def start(port: int = 8797) -> dict:
    """Запустить фоновый поток с горячей клавишей (повторный вызов — no-op)."""
    if _state['thread'] and _state['thread'].is_alive():
        return status()
    t = threading.Thread(target=_loop, args=(port,), daemon=True, name='bl-hotkey')
    _state['thread'] = t
    t.start()
    t.join(0.4)   # даём зарегистрировать клавишу до ответа
    return status()


def status() -> dict:
    return {'registered': _state['registered'], 'error': _state['error'],
            'hits': _state['hits'], 'combo': 'Ctrl+Shift+D',
            'window': _state['window']}
