# -*- coding: utf-8 -*-
"""Иконка BaseLauncher в системном трее (Win32, только stdlib).

Зачем: окно приложения — Edge в режиме `--app`, у него нет своего «свернуть в трей».
Сервер живёт в отдельном процессе, поэтому трей делает именно он: скрытое окно-приёмник
сообщений + `Shell_NotifyIcon`. Левый клик по иконке — показать/скрыть окно стартера
(то же, что Ctrl+Shift+S), правый — меню: Показать/Скрыть, Открыть в браузере, Выход.

Трей-поток держит свой `GetMessage`-цикл; uvicorn его не касается.
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import threading

from . import hotkey

u32 = ctypes.WinDLL('user32', use_last_error=True)
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
CW_USEDEFAULT = -2147483648

CLASS_NAME = 'BaseLauncherTrayWnd'
ID_SHOW, ID_HIDE, ID_WEB, ID_QUIT = 101, 102, 103, 104
_state = {'thread': None, 'ready': False, 'error': '', 'hwnd': 0, 'last': ''}


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('hWnd', wt.HWND), ('uID', wt.UINT),
                ('uFlags', wt.UINT), ('uCallbackMessage', wt.UINT),
                ('hIcon', wt.HICON), ('szTip', wt.WCHAR * 128),
                ('dwState', wt.DWORD), ('dwStateMask', wt.DWORD),
                ('szInfo', wt.WCHAR * 256), ('uVersion', wt.UINT),
                ('szInfoTitle', wt.WCHAR * 64), ('dwInfoFlags', wt.DWORD),
                ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', wt.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wt.UINT), ('lpfnWndProc', ctypes.c_void_p),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wt.HINSTANCE), ('hIcon', wt.HICON),
                ('hCursor', wt.HANDLE), ('hbrBackground', wt.HBRUSH),
                ('lpszMenuName', wt.LPCWSTR), ('lpszClassName', wt.LPCWSTR)]


def _icon_path() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent / 'assets' / 'baselauncher.ico')


def _notify(hwnd, add=False, modify=False, tip='BaseLauncher — баз 1С (Ctrl+Shift+S)'):
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    hicon = u32.LoadImageW(None, _icon_path(), IMAGE_ICON, 0, 0,
                           LR_LOADFROMFILE | LR_DEFAULTSIZE)
    nid.hIcon = hicon or u32.LoadIconW(None, ctypes.cast(32512, wt.LPCWSTR))   # IDI_APPLICATION
    nid.szTip = tip[:127]
    ok = shell32.Shell_NotifyIconW(NIM_ADD if add else NIM_MODIFY, ctypes.byref(nid))
    return bool(ok), nid.hIcon


def _run(port: int):
    hinst = k32.GetModuleHandleW(None)
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    def on_menu(cmd: int):
        if cmd == ID_SHOW:
            hotkey.show_starter(port)
        elif cmd == ID_HIDE:
            hotkey.hide_starter(port)
        elif cmd == ID_WEB:
            try:
                hotkey._open_starter(port)          # полный путь к msedge.exe
            except Exception as e:                  # noqa: BLE001
                _state['error'] = f'открыть в браузере: {e}'
        elif cmd == ID_QUIT:
            try:
                import urllib.request
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                op.open(urllib.request.Request('http://127.0.0.1:%d/api/shutdown' % port,
                                               data=b'{}', method='POST'), timeout=10)
            except Exception:
                pass

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    r = hotkey.toggle_starter(port)
                    _state['last'] = str(r)[:300]
                    return 0
                if ev == WM_RBUTTONUP:
                    menu = u32.CreatePopupMenu()
                    u32.AppendMenuW(menu, MF_STRING, ID_SHOW, 'Показать окно')
                    u32.AppendMenuW(menu, MF_STRING, ID_HIDE, 'Скрыть в трей')
                    u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    u32.AppendMenuW(menu, MF_STRING, ID_WEB, 'Открыть в браузере')
                    u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    u32.AppendMenuW(menu, MF_STRING, ID_QUIT, 'Выход (остановить сервер)')
                    pt = wt.POINT()
                    u32.GetCursorPos(ctypes.byref(pt))
                    u32.SetForegroundWindow(hwnd)      # иначе меню не закрывается по клику вне
                    cmd = u32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                                             pt.x, pt.y, 0, hwnd, None)
                    u32.DestroyMenu(menu)
                    if cmd:
                        on_menu(int(cmd))
                    return 0
            if msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u32.PostQuitMessage(0)
                return 0
        except Exception as e:                                    # noqa: BLE001
            _state['error'] = f'wndproc: {type(e).__name__}: {e}'
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = CLASS_NAME
    if not u32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        if err not in (1410,):            # 1410 = класс уже зарегистрирован
            _state['error'] = f'RegisterClassW: err {err}'
            return
    hwnd = u32.CreateWindowExW(0, CLASS_NAME, 'BaseLauncher tray', 0,
                               0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        _state['error'] = f'CreateWindowExW: err {ctypes.get_last_error()}'
        return
    _state['hwnd'] = hwnd
    ok, _hicon = _notify(hwnd, add=True)
    if not ok:
        _state['error'] = f'Shell_NotifyIcon(NIM_ADD): err {ctypes.get_last_error()}'
        u32.DestroyWindow(hwnd)
        return
    _state['ready'] = True
    msg = wt.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_arr(hwnd)))
    _state['ready'] = False


def _arr(hwnd):
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    return nid


def start(port: int = 8797) -> dict:
    """Поднять трей-иконку в фоновом потоке (повторный вызов — no-op)."""
    if _state['thread'] and _state['thread'].is_alive():
        return status()
    t = threading.Thread(target=_run, args=(port,), daemon=True, name='bl-tray')
    _state['thread'] = t
    t.start()
    t.join(0.6)
    return status()


def status() -> dict:
    return {'ready': _state['ready'], 'error': _state['error'],
            'hwnd': _state['hwnd'], 'last': _state['last']}
