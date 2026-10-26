# -*- coding: utf-8 -*-
"""Глобальная горячая клавиша для окна стартера: Ctrl+Shift+S.

Регистрируется через Win32 RegisterHotKey на физическую клавишу (VK_S),
поэтому работает и в русской, и в английской раскладке.

Окно стартера — Edge в режиме `--app` (класс Chrome_WidgetWin_1), заголовок = <title> страницы.
Ищем его по заголовку и по командной строке процесса (--app=http://127.0.0.1:<port>).
"""
import ctypes
import ctypes.wintypes as wt
import json
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


def find_starter_window(port: int = 8797,
                        own_only: bool = False) -> list[tuple[int, str, int]]:
    """Окна стартера: (hwnd, title, pid), включая скрытые (трей), но БЕЗ вкладок браузера.

    Отличаем от обычных вкладок Edge по двум признакам: заголовок РОВНО «BaseLauncher»
    (у вкладки он вида «BaseLauncher — Личный: Microsoft Edge») либо `--app=` с нашим
    адресом в командной строке процесса. Заголовок «BaseLauncher DEV» (dev-инстанс,
    порт 8798) боевым окном НЕ считается.

    own_only=True (dev-инстанс): только `--app=` с нашим портом — по заголовку не
    ищем, иначе найдётся окно рабочего инстанса.
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
        tl = title.strip().lower()
        by_title = (tl.startswith(STARTER_TITLE.lower()) and 'edge' not in tl
                    and 'dev' not in tl)      # окно dev-инстанса не трогаем
        # ГЛАВНЫЙ признак при нескольких инстансах: метка порта в заголовке
        # («· UI/MCP :8797»). Edge держит все --app-окна в ОДНОМ процессе,
        # поэтому cmdline ('--app=...8797') по PID различает окна ненадёжно.
        import re as _re
        _mark = _re.search(r':(879\d|87\d\d)\b', tl)
        if _mark:
            hit = (_mark.group(1) == str(port))
        else:
            hit = own or (by_title and not own_only)
        if hit:
            out.append((hwnd, title, pid))
        return True
    u32.EnumWindows(CB(cb), 0)
    out.sort(key=lambda x: (0 if x[1].strip().lower().startswith(STARTER_TITLE.lower()) else 1,
                            0 if u32.IsWindowVisible(x[0]) else 1))
    return out


def _log(msg: str) -> None:
    """Диагностика горячей клавиши: пишем в data/focus.log (не в stdout — под pythonw его нет)."""
    try:
        from datetime import datetime
        from .paths import DATA_DIR
        p = DATA_DIR / 'focus.log'
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n')
    except Exception:
        pass


_known: dict[int, int] = {}          # port -> hwnd окна, которое мы спрятали в трей
# Пока окно только что открыто и мы навязываем ему сохранённый размер, сторож геометрии
# молчит: иначе он тут же записывал «дефолтный» размер нового окна поверх сохранённого.
_suppress = {'until': 0.0}


def _suppress_saving(seconds: float) -> None:
    import time as _t
    _suppress['until'] = max(_suppress['until'], _t.time() + seconds)


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [('length', wt.UINT), ('flags', wt.UINT), ('showCmd', wt.UINT),
                ('ptMinPosition', wt.POINT), ('ptMaxPosition', wt.POINT),
                ('rcNormalPosition', wt.RECT)]


u32.GetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
u32.SetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]


def _geom_file() -> Path:
    from .paths import DATA_DIR
    return DATA_DIR / 'window.json'


SPI_GETWORKAREA = 0x0030
u32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
u32.GetSystemMetrics.argtypes = [ctypes.c_int]


def _work_area() -> wt.RECT:
    """Рабочая область экрана (без панели задач)."""
    r = wt.RECT()
    if u32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return r
    return wt.RECT(0, 0, u32.GetSystemMetrics(0), u32.GetSystemMetrics(1))


def _fit_to_screen(rect: list[int]) -> list[int]:
    """Окно не должно вылезать за экран: сжать пропорционально и подвинуть внутрь."""
    l, tp, r, b = rect
    w, h = r - l, b - tp
    area = _work_area()
    aw, ah = area.right - area.left, area.bottom - area.top
    margin = 12
    maxw, maxh = aw - 2 * margin, ah - 2 * margin
    if w <= 0 or h <= 0:
        return rect
    k = 1.0
    if w > maxw:
        k = min(k, maxw / w)
    if h > maxh:
        k = min(k, maxh / h)
    if k < 1.0:                                   # «немного сжать», сохраняя пропорции
        w, h = int(w * k), int(h * k)
        l, tp = area.left + (aw - w) // 2, area.top + (ah - h) // 2
        _log(f'окно больше экрана — сжал до {w}x{h} (k={k:.3f})')
    if l < area.left:
        l = area.left
    if tp < area.top:
        tp = area.top
    if l + w > area.right:
        l = max(area.left, area.right - w)
    if tp + h > area.bottom:
        tp = max(area.top, area.bottom - h)
    return [l, tp, l + w, tp + h]


def _save_geom(hwnd: int, port: int) -> None:
    """Запомнить размер/положение (и «развёрнуто») — Edge --app сам это не гарантирует."""
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return False
    r = wp.rcNormalPosition
    if r.right - r.left < 200 or r.bottom - r.top < 150:
        return False
    try:
        _geom_file().write_text(json.dumps(
            {'port': port, 'showCmd': int(wp.showCmd),
             'rect': [r.left, r.top, r.right, r.bottom]}), encoding='utf-8')
        _log(f'геометрия сохранена {[r.left, r.top, r.right, r.bottom]} showCmd={wp.showCmd}')
        return True
    except OSError as e:
        _log(f'геометрия не сохранена: {e}')
        return False


def _geom_rect(port: int):
    """Сохранённый прямоугольник окна (уже подогнанный под экран) или None."""
    try:
        d = json.loads(_geom_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if d.get('port') not in (None, port):
        return None
    l, tp, r, b = d.get('rect') or (0, 0, 0, 0)
    if r - l < 200 or b - tp < 150:
        return None
    return _fit_to_screen([l, tp, r, b])


def _geom_matches(hwnd: int, want) -> bool:
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return False
    r = wp.rcNormalPosition
    return (abs(r.left - want[0]) <= 4 and abs(r.top - want[1]) <= 4
            and abs((r.right - r.left) - (want[2] - want[0])) <= 6
            and abs((r.bottom - r.top) - (want[3] - want[1])) <= 6)


def _restore_geom(hwnd: int, port: int) -> None:
    """Вернуть запомненный размер/положение при показе окна из трея."""
    _log(f'restore_geom start hwnd={hwnd} port={port}')
    try:
        _restore_geom_impl(hwnd, port)
    except Exception as e:                                     # noqa: BLE001
        _log(f'геометрия: ОШИБКА {type(e).__name__}: {e}')


def _restore_geom_impl(hwnd: int, port: int) -> None:
    try:
        d = json.loads(_geom_file().read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        _log(f'геометрия: файл не прочитан ({e})')
        return
    if d.get('port') not in (None, port):
        _log(f'геометрия: порт в файле {d.get("port")} != {port} — пропуск')
        return
    l, tp, r, b = d.get('rect') or (0, 0, 0, 0)
    _log(f'геометрия: в файле {d}')
    if r - l < 200 or b - tp < 150:
        _log(f'геометрия: слишком маленький rect {[l, tp, r, b]} — пропуск')
        return
    l, tp, r, b = _fit_to_screen([l, tp, r, b])
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    wp.showCmd = 3 if d.get('showCmd') == 3 else 1        # 3 — развёрнуто на весь экран
    wp.rcNormalPosition = wt.RECT(l, tp, r, b)
    u32.SetWindowPlacement(hwnd, ctypes.byref(wp))
    rect = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    if (abs(rect.left - l) > 6 or abs(rect.top - tp) > 6
            or abs((rect.right - rect.left) - (r - l)) > 8
            or abs((rect.bottom - rect.top) - (b - tp)) > 8):
        # Edge --app иногда игнорирует placement — ставим позицию напрямую
        u32.SetWindowPos(hwnd, 0, l, tp, r - l, b - tp, 0x0004 | 0x0010)  # NOZORDER|NOACTIVATE
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        _log(f'геометрия: placement не сработал, SetWindowPos → '
             f'{[rect.left, rect.top, rect.right, rect.bottom]}')
    else:
        _log(f'геометрия восстановлена {[l, tp, r, b]} showCmd={wp.showCmd}')


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


def _raise(hwnd: int, title: str, pid: int, port: int | None = None) -> dict:
    """Показать и поднять окно: SW_RESTORE для свёрнутых/SW_SHOW для скрытых + фокус."""
    before = {'visible': bool(u32.IsWindowVisible(hwnd)), 'iconic': bool(u32.IsIconic(hwnd))}
    if not before['visible']:
        u32.ShowWindow(hwnd, 5)                  # SW_SHOW — окно было спрятано в трей
        _restore_geom(hwnd, port) if port else None
    elif before['iconic']:
        u32.ShowWindow(hwnd, 9)                  # SW_RESTORE — окно было свёрнуто
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
    _save_geom(hwnd, port)                        # размер/положение — до SW_HIDE
    for h, _t, _p in vis:                         # прячем все окна приложения, не только первое
        u32.ShowWindow(h, 0)                       # SW_HIDE
    ok = not any(u32.IsWindowVisible(h) for h, _t, _p in vis)
    _log(f'hide hwnd={hwnd} pid={pid} окон={len(vis)} ok={ok}')
    _state['window'] = '' if ok else title
    return {'ok': ok, 'hidden': ok, 'title': title, 'pid': pid, 'hwnd': hwnd,
            'windows': len(vis)}


def _start_settle(port: int, delays=(0.6, 1.5, 3.0, 5.0, 7.5)) -> None:
    """Edge --app восстанавливает СВОЙ размер уже после открытия окна.

    Поэтому после открытия несколько раз проверяем и навязываем сохранённый размер,
    пока окно не совпадёт (или пока не кончатся попытки).
    """
    import time as _s

    def _settle():
        want = _geom_rect(port)
        if not want:
            return
        for d in delays:
            _s.sleep(d)
            wins = [w for w in find_starter_window(port) if u32.IsWindowVisible(w[0])]
            if not wins:
                continue
            hh = wins[0][0]
            if _geom_matches(hh, want):
                _log('окно нужного размера — навязывание остановлено')
                return
            _restore_geom(hh, port)
        _log('навязывание размера закончено')

    threading.Thread(target=_settle, daemon=True, name='bl-geom-settle').start()


def focus_starter(port: int = 8797) -> dict:
    """Поднять окно стартера на передний план. Если окна нет — открыть новое.

    Важно: раньше при отсутствии окна отдавали ошибку, а при показе использовали SW_SHOW —
    на скрытых окнах Edge это приводило к тому, что визуально «ничего не происходит» или
    окно пропадает. Теперь: только видимые окна, SW_RESTORE лишь для свёрнутых, а если
    окон нет — открываем заново.
    """
    found = _visible_starter(port)
    if found:
        return _raise(*found, port=port)

    # видимого окна нет: сначала пробуем вернуть то, что сами спрятали в трей
    hwnd = _known.get(port)
    if hwnd and _alive(hwnd):
        _log(f'возвращаю спрятанное окно hwnd={hwnd}')
        res = _raise(hwnd, _win_title(hwnd) or STARTER_TITLE, _pid_of(hwnd), port=port)
        res['restored'] = True
        return res

    # затем — любое существующее (скрытое) окно приложения: второе окно не создаём
    hidden = find_starter_window(port)
    if hidden:
        h, t, p = hidden[0]
        _log(f'показываю существующее скрытое окно hwnd={h}')
        res = _raise(h, t, p, port=port)
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
            hw, _tt, _pp = again
            _suppress_saving(14)                 # сторож не перезапишет сохранённый размер
            _restore_geom(hw, port)              # новое окно — сразу в прежний размер
            _start_settle(port)                  # и добить, если Edge передумает позже
            res = _raise(hw, _tt, _pp, port=port)
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


def close_starter(port: int = 8797, wait: float = 3.0) -> dict:
    """Закрыть окно приложения совсем (пункт «Выход» в меню трея).

    Сначала вежливо WM_CLOSE (Edge сам уберёт окно), затем, если окно осталось и его
    процесс запущен именно с нашим `--app=`, процесс добивается. Чужие окна браузера
    не трогаем никогда — проверка по командной строке процесса.
    """
    import time as _t
    wins = find_starter_window(port)
    if not wins:
        _known.pop(port, None)
        return {'ok': True, 'closed': 0, 'message': 'окно уже закрыто'}
    _save_geom(wins[0][0], port)                          # последний размер — в файл
    _suppress_saving(1.0)
    for h, _t2, p in wins:
        u32.PostMessageW(h, 0x0010, 0, 0)                 # WM_CLOSE
    deadline = _t.time() + wait
    while _t.time() < deadline:
        _t.sleep(0.3)
        if not find_starter_window(port):
            break
    left = find_starter_window(port)
    killed = 0
    for h, title, pid in left:
        cmd = procs.cmdline(pid) or ''
        if pid and '--app=' in cmd and ('127.0.0.1:%d' % port) in cmd:
            try:
                PROCESS_TERMINATE = 0x0001
                hp = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if hp:
                    k32.TerminateProcess(hp, 0)
                    k32.CloseHandle(hp)
                    killed += 1
            except Exception as e:                        # noqa: BLE001
                _log(f'не смог закрыть pid={pid}: {e}')
    _t.sleep(0.7)
    still = find_starter_window(port)
    _known.pop(port, None)
    _state['window'] = '' if not still else _win_title(still[0][0])
    _log(f'close_starter: окон было {len(wins)}, добито процессов {killed}, осталось {len(still)}')
    return {'ok': not still, 'closed': len(wins), 'killed': killed,
            'left': len(still)}


def _geom_watch(port: int, every: float = 2.0) -> None:
    """Фон: следить за размером/положением окна и хранить их в data/window.json.

    Сохранение только в момент «в трей» недостаточно: пользователь меняет размер и просто
    закрывает/перезапускает приложение — тогда запоминать нечего.
    """
    import time as _t
    last = None
    while True:
        _t.sleep(every)
        try:
            if _t.time() < _suppress['until']:
                continue
            found = _visible_starter(port)
            if not found:
                continue
            hwnd = found[0]
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(wp)
            if not u32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                continue
            r = wp.rcNormalPosition
            if r.right - r.left < 200 or r.bottom - r.top < 150:
                continue
            cur = (int(wp.showCmd), r.left, r.top, r.right, r.bottom)
            if cur == last:                      # ничего не изменилось — файл не трогаем
                continue
            if last is None:                     # первый замер: сравниваем с файлом
                try:
                    d = json.loads(_geom_file().read_text(encoding='utf-8'))
                    if (tuple([d.get('showCmd')] + list(d.get('rect') or [])) == cur):
                        last = cur
                        continue
                except (OSError, ValueError):
                    pass
            if _save_geom(hwnd, port):
                last = cur
        except Exception as e:                                  # noqa: BLE001
            _log(f'geom_watch: {e}')


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
    threading.Thread(target=_geom_watch, args=(port,), daemon=True, name='bl-geom').start()
    _state['thread'] = t
    t.start()
    t.join(0.4)   # даём зарегистрировать клавишу до ответа
    return status()


def status() -> dict:
    return {'registered': _state['registered'], 'error': _state['error'],
            'hits': _state['hits'], 'combo': 'Ctrl+Shift+S',
            'window': _state['window']}
