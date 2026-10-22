# -*- coding: utf-8 -*-
"""Глобальная горячая клавиша для окна стартера: Ctrl+Shift+S.

Регистрируется через Win32 RegisterHotKey на физическую клавишу (VK_S),
поэтому работает и в русской, и в английской раскладке.

Окно стартера — Edge в режиме `--app` (класс Chrome_WidgetWin_1), заголовок = <title> страницы.
Ищем его по заголовку и по командной строке процесса (--app=http://127.0.0.1:<port>).
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import threading
from pathlib import Path

from . import procs

u32 = ctypes.WinDLL('user32', use_last_error=True)
k32 = ctypes.WinDLL('kernel32', use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_S = 0x53

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
    """Окна стартера: (hwnd, title, pid), включая скрытые (трей), но БЕЗ вкладок браузера.

    Отличаем от обычных вкладок Edge по двум признакам: заголовок РОВНО «BaseLauncher»
    (у вкладки он вида «BaseLauncher — Личный: Microsoft Edge») либо `--app=` с нашим
    адресом в командной строке процесса.
    """
    out: list[tuple[int, str, int]] = []
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    seen_cmd: dict[int, str] = {}

    def cb(hwnd, _):
        title = _win_title(hwnd)
        if not title:
            return True
        if _class_of(hwnd) != 'Chrome_WidgetWin_1':
            return True
        pid = _pid_of(hwnd)
        if pid not in seen_cmd:
            seen_cmd[pid] = procs.cmdline(pid) or ''
        cmd = seen_cmd[pid]
        own = '--app=' in cmd and ('127.0.0.1:%d' % port) in cmd
        if title.strip().lower() == STARTER_TITLE.lower() or own:
            out.append((hwnd, title, pid))
        return True
    u32.EnumWindows(CB(cb), 0)
    out.sort(key=lambda x: (0 if x[1].strip().lower() == STARTER_TITLE.lower() else 1,
                            0 if u32.IsWindowVisible(x[0]) else 1))
    return out


def _log(msg: str) -> None:
    """Диагностика горячей клавиши: пишем в data/focus.log (не в stdout — под pythonw его нет)."""
    try:
        from datetime import datetime
        p = Path(__file__).resolve().parent.parent / 'data' / 'focus.log'
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n')
    except Exception:
        pass


_known: dict[int, int] = {}          # port -> hwnd окна, которое мы спрятали в трей


def _alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(u32.IsWindow(hwnd))


def _visible_starter(port: int):
    """Первое ВИДИМОЕ окно стартера (скрытые окна Edge трогать нельзя)."""
    for hwnd, title, pid in find_starter_window(port):
        if u32.IsWindowVisible(hwnd):
            _known[port] = hwnd
            return hwnd, title, pid
    return None


def _edge_exe() -> str:
    """Полный путь к msedge.exe: у сервера под pythonw PATH короткий, 'msedge' не находится."""
    import shutil
    for cand in (shutil.which('msedge'),
                 r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                 r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'):
        if cand and Path(cand).is_file():
            return cand
    raise FileNotFoundError('msedge.exe не найден (Edge не установлен?)')


def _open_starter(port: int) -> None:
    """Окна нет — открыть его так же, как start_bl.bat (Edge в режиме --app)."""
    subprocess.Popen([_edge_exe(), '--app=http://127.0.0.1:%d' % port],
                     close_fds=True, creationflags=0x00000008)      # DETACHED_PROCESS


def _raise(hwnd: int, title: str, pid: int) -> dict:
    """Показать и поднять окно: SW_RESTORE для свёрнутых/SW_SHOW для скрытых + фокус."""
    before = {'visible': bool(u32.IsWindowVisible(hwnd)), 'iconic': bool(u32.IsIconic(hwnd))}
    if not before['visible'] or before['iconic']:
        u32.ShowWindow(hwnd, 9 if before['iconic'] else 5)   # SW_RESTORE / SW_SHOW
    fg_tid = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    cur_tid = k32.GetCurrentThreadId()
    if fg_tid and fg_tid != cur_tid:
        u32.AttachThreadInput(cur_tid, fg_tid, True)
    u32.SetForegroundWindow(hwnd)
    u32.BringWindowToTop(hwnd)
    if fg_tid and fg_tid != cur_tid:
        u32.AttachThreadInput(cur_tid, fg_tid, False)
    ok = u32.GetForegroundWindow() == hwnd
    after = {'visible': bool(u32.IsWindowVisible(hwnd)), 'iconic': bool(u32.IsIconic(hwnd))}
    _log(f'raise hwnd={hwnd} pid={pid} before={before} after={after} foreground={ok}')
    _state['window'] = title
    res = {'ok': True, 'foreground': ok, 'title': title, 'pid': pid, 'hwnd': hwnd}
    if not after['visible']:
        res['warning'] = 'окно стало невидимым после активации — Edge его скрыл'
    return res


def hide_starter(port: int = 8797) -> dict:
    """Свернуть окно стартера в трей: SW_HIDE (из панели задач окно тоже пропадает)."""
    vis = [(h, t, p) for h, t, p in find_starter_window(port) if u32.IsWindowVisible(h)]
    if not vis:
        return {'ok': False, 'error': 'видимого окна стартера нет'}
    hwnd, title, pid = vis[0]
    _known[port] = hwnd                           # запоминаем: вернуть надо ЭТО окно
    for h, _t, _p in vis:                         # прячем все окна приложения, не только первое
        u32.ShowWindow(h, 0)                       # SW_HIDE
    ok = not any(u32.IsWindowVisible(h) for h, _t, _p in vis)
    _log(f'hide hwnd={hwnd} pid={pid} окон={len(vis)} ok={ok}')
    _state['window'] = '' if ok else title
    return {'ok': ok, 'hidden': ok, 'title': title, 'pid': pid, 'hwnd': hwnd,
            'windows': len(vis)}


def focus_starter(port: int = 8797) -> dict:
    """Поднять окно стартера на передний план. Если окна нет — открыть новое.

    Важно: раньше при отсутствии окна отдавали ошибку, а при показе использовали SW_SHOW —
    на скрытых окнах Edge это приводило к тому, что визуально «ничего не происходит» или
    окно пропадает. Теперь: только видимые окна, SW_RESTORE лишь для свёрнутых, а если
    окон нет — открываем заново.
    """
    found = _visible_starter(port)
    if found:
        return _raise(*found)

    # видимого окна нет: сначала пробуем вернуть то, что сами спрятали в трей
    hwnd = _known.get(port)
    if hwnd and _alive(hwnd):
        _log(f'возвращаю спрятанное окно hwnd={hwnd}')
        res = _raise(hwnd, _win_title(hwnd) or STARTER_TITLE, _pid_of(hwnd))
        res['restored'] = True
        return res

    # затем — любое существующее (скрытое) окно приложения: второе окно не создаём
    hidden = find_starter_window(port)
    if hidden:
        h, t, p = hidden[0]
        _log(f'показываю существующее скрытое окно hwnd={h}')
        res = _raise(h, t, p)
        res['restored'] = True
        return res

    # окна действительно нет — открываем новое и ждём, пока оно появится
    _log('видимого окна нет — открываю новое')
    try:
        _open_starter(port)
    except Exception as e:                                        # noqa: BLE001
        _log(f'не удалось открыть окно: {e}')
        return {'ok': False, 'error': f'окно стартера закрыто, открыть не удалось: {e}'}
    import time as _t
    for _ in range(30):                       # до 15 с
        _t.sleep(0.5)
        again = _visible_starter(port)
        if again:
            res = _raise(*again)
            res['reopened'] = True
            return res
    _state['window'] = ''
    return {'ok': True, 'reopened': True, 'foreground': False,
            'message': 'окно стартера было закрыто — открыл заново'}


def toggle_starter(port: int = 8797) -> dict:
    """Ctrl+Shift+S и левый клик по трею: показать, если скрыто/свёрнуто, иначе убрать в трей."""
    found = _visible_starter(port)
    if found:
        hwnd, title, _pid = found
        if u32.GetForegroundWindow() == hwnd and not u32.IsIconic(hwnd):
            r = hide_starter(port)
            r['action'] = 'hidden'
            return r
    r = focus_starter(port)
    r['action'] = 'reopened' if r.get('reopened') else 'shown'
    return r


def _loop(port: int):
    msg = wt.MSG()
    k32.GetCurrentThreadId()  # поток, владеющий регистрацией
    ok = u32.RegisterHotKey(None, HOTKEY_ID,
                            MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_S)
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
                toggle_starter(port)     # показать / убрать в трей
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
            'hits': _state['hits'], 'combo': 'Ctrl+Shift+S',
            'window': _state['window']}
