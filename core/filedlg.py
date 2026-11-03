# -*- coding: utf-8 -*-
"""Нативный диалог выбора файла (Windows, comdlg32/GetOpenFileNameW) — только stdlib.

Нужен для выбора внешней обработки в «разовых ключах» (`/Execute`). Сервер живёт на той же
машине, что и пользователь, поэтому диалог открывается прямо из процесса сервера.
Вызывать из обычного (не главного) потока — FastAPI для sync-эндпоинтов так и делает.
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

_co = ctypes.windll.ole32
_comdlg = ctypes.windll.comdlg32

OFN_HIDEREADONLY = 0x00000004
OFN_PATHMUSTEXIST = 0x00000800
OFN_FILEMUSTEXIST = 0x00001000
OFN_NOCHANGEDIR = 0x00000008
OFN_EXPLORER = 0x00080000


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ('lStructSize', wintypes.DWORD),
        ('hwndOwner', wintypes.HWND),
        ('hInstance', wintypes.HINSTANCE),
        ('lpstrFilter', wintypes.LPCWSTR),
        ('lpstrCustomFilter', wintypes.LPWSTR),
        ('nMaxCustFilter', wintypes.DWORD),
        ('nFilterIndex', wintypes.DWORD),
        ('lpstrFile', wintypes.LPWSTR),
        ('nMaxFile', wintypes.DWORD),
        ('lpstrFileTitle', wintypes.LPWSTR),
        ('nMaxFileTitle', wintypes.DWORD),
        ('lpstrInitialDir', wintypes.LPCWSTR),
        ('lpstrTitle', wintypes.LPCWSTR),
        ('Flags', wintypes.DWORD),
        ('nFileOffset', wintypes.WORD),
        ('nFileExtension', wintypes.WORD),
        ('lpstrDefExt', wintypes.LPCWSTR),
        ('lCustData', wintypes.LPARAM),
        ('lpfnHook', ctypes.c_void_p),
        ('lpTemplateName', wintypes.LPCWSTR),
        ('pvReserved', ctypes.c_void_p),
        ('dwReserved', wintypes.DWORD),
        ('FlagsEx', wintypes.DWORD),
    ]


def pick_file(title: str = 'Выбрать файл', filters: list[tuple[str, str]] | None = None,
              initial_dir: str = '') -> str:
    """Показать диалог «Открыть файл». Возвращает путь или '' (отмена/ошибка).

    filters: [('Внешние обработки 1С', '*.epf;*.erf'), ('Все файлы', '*.*')]
    """
    filters = filters or [('Внешние обработки 1С', '*.epf;*.erf'), ('Все файлы', '*.*')]
    # строка фильтра: "Название\0маска\0...\0\0"
    fstr = ''.join('%s\0%s\0' % (n, m) for n, m in filters) + '\0'
    buf = ctypes.create_unicode_buffer(32768)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = None
    ofn.lpstrFilter = fstr
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrInitialDir = initial_dir or None
    ofn.lpstrTitle = title
    ofn.Flags = (OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY
                 | OFN_NOCHANGEDIR | OFN_EXPLORER)
    ofn.lpstrDefExt = 'epf'
    try:
        _co.CoInitialize(None)
    except Exception:
        pass
    try:
        ok = _comdlg.GetOpenFileNameW(ctypes.byref(ofn))
        return buf.value if ok else ''
    finally:
        try:
            _co.CoUninitialize()
        except Exception:
            pass


def _raise_dialog_foreground(pid: int, stop: threading.Event) -> None:
    """Поднять COM-диалог нашего процесса на передний план.

    pythonw-сервер не является foreground-процессом, поэтому окно SHBrowse
    появляется ПОЗАДИ остальных. Монитор в отдельном потоке: находим первое
    окно #32770 нашего PID и поднимаем (SW_RESTORE + ALT-трюк для обхода
    foreground-lock + краткий TOPMOST).
    """
    user32 = ctypes.windll.user32
    user32.keybd_event.restype = None
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002

    def _enum_cb(h, _):
        try:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(h, cls, 64)
            if cls.value == '#32770':
                wpid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
                if wpid.value == pid:
                    user32.ShowWindow(h, 9)  # SW_RESTORE
                    # ALT-нажатие снимает foreground-lock для SetForegroundWindow
                    user32.keybd_event(0x12, 0, 0, 0)
                    user32.keybd_event(0x12, 0, 0x0002, 0)
                    user32.SetForegroundWindow(h)
                    user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOSIZE | SWP_NOMOVE)
                    user32.SetWindowPos(h, HWND_NOTOPMOST, 0, 0, 0, 0,
                                        SWP_NOSIZE | SWP_NOMOVE)
                    raise StopIteration
        except StopIteration:
            pass
        return True

    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    deadline = time.time() + 5.0
    while time.time() < deadline and not stop.is_set():
        try:
            user32.EnumWindows(CB(_enum_cb), 0)
        except Exception:
            pass
        time.sleep(0.1)


def pick_folder(title: str = 'Выбрать папку', initial: str = '') -> str:
    """Диалог выбора папки.

    Основной путь — pwsh 7 (WinForms FolderBrowserDialog, .NET 9+ = современный
    IFileDialog): живёт в ОТДЕЛЬНОМ процессе, поэтому не виснет из-за COM-контекста
    pythonw-сервера и нативно умеет SelectedPath. Фолбэк — SHBrowseForFolderW
    с CoInitializeEx(SA) и PIDL-BFFM_SETSELECTION (строчный вариант игнорируется).
    """
    try:
        return _pick_folder_pwsh(title, initial)
    except Exception:
        return _pick_folder_shbrowse(title, initial)


def _pick_folder_pwsh(title: str, initial: str) -> str:
    """Современный диалог через pwsh 7 (.NET FolderBrowserDialog)."""
    import subprocess
    import tempfile
    pwsh = r'C:\Program Files\PowerShell\7\pwsh.exe'
    if not os.path.isfile(pwsh):
        raise FileNotFoundError('pwsh 7 не найден')
    ps1_src = (
        'Add-Type -AssemblyName System.Windows.Forms' + '\n'
        '$d = New-Object System.Windows.Forms.FolderBrowserDialog' + '\n'
        "$d.Description = [Environment]::GetEnvironmentVariable('BL_DLG_TITLE')" + '\n'
        "$init = [Environment]::GetEnvironmentVariable('BL_DLG_INIT')" + '\n'
        'if ($init) { $d.SelectedPath = $init }' + '\n'
        'if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK -and $d.SelectedPath) {' + '\n'
        '  [Console]::Out.Write($d.SelectedPath)' + '\n'
        '}')
    with tempfile.NamedTemporaryFile('w', suffix='.ps1', delete=False,
                                     encoding='utf-8-sig') as f:
        f.write(ps1_src)
        ps1 = f.name
    env = dict(os.environ)
    env['BL_DLG_TITLE'] = title or 'Выбрать папку'
    env['BL_DLG_INIT'] = initial or ''
    _fg_stop = threading.Event()
    try:
        proc = subprocess.Popen(
            [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps1],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — без вспышки консоли
            env=env)
        # поднять диалог pwsh-процесса на передний план
        threading.Thread(target=_raise_dialog_foreground,
                         args=(proc.pid, _fg_stop), daemon=True).start()
        out, _ = proc.communicate()
        return (out or b'').decode('utf-8', 'replace').strip()
    finally:
        _fg_stop.set()
        try:
            os.unlink(ps1)
        except OSError:
            pass


def _pick_folder_shbrowse(title: str = 'Выбрать папку', initial: str = '') -> str:
    """Диалог выбора папки (SHBrowseForFolderW); initial — открыть сразу в ней.

    КРИТИЧНО: перед вызовом нужен CoInitializeEx(SA) в ЭТОМ потоке — без него
    SHBrowseForFolderW молча возвращает 0 без окна (кнопки «…» «не работали»).
    У pick_file COM-инициализация была, у папки — нет. Побочный эксперимент:
    Vista-вариант IFileOpenDialog (CLSID FileOpenDialog) на этой сборке Win11
    недоступен — IID_IFileOpenDialog/IModalWindow дают E_NOINTERFACE.
    """
    _ole32 = ctypes.windll.ole32
    _shell = ctypes.windll.shell32
    BIF_RETURNONLYFSDIRS = 0x1
    BIF_NEWDIALOGSTYLE = 0x40

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [('hwndOwner', wintypes.HWND), ('pidlRoot', ctypes.c_void_p),
                    ('pszDisplayName', wintypes.LPWSTR), ('lpszTitle', wintypes.LPCWSTR),
                    ('ulFlags', wintypes.UINT), ('lpfn', ctypes.c_void_p),
                    ('lParam', wintypes.LPARAM), ('iImage', ctypes.c_int)]

    buf = ctypes.create_unicode_buffer(260)
    disp = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.hwndOwner = None
    bi.pszDisplayName = ctypes.cast(disp, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    try:
        _ole32.CoInitializeEx(None, 0x2)  # STA — без этого диалог не показывается
    except Exception:
        pass
    # начальная папка: SHBrowse умеет только через колбэк (BFFM_SETSELECTION)
    _cb_ref = None
    _init_buf = None
    if initial:
        from pathlib import Path as _P
        q = _P(initial)
        if q.is_file():
            initial = str(q.parent)
        if initial and _P(initial).is_dir():
            BFFM_INITIALIZED = 1
            BFFM_SETSELECTION = 0x400 + 102  # WM_USER + 102
            _init_buf = ctypes.create_unicode_buffer(initial)
            _CB = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.UINT,
                                     wintypes.LPARAM, wintypes.LPARAM)

            _il_from_path = ctypes.windll.shell32.ILCreateFromPathW
            _il_from_path.restype = ctypes.c_void_p
            _il_from_path.argtypes = [wintypes.LPCWSTR]

            def _browse_cb(hwnd, msg, lp, data):
                if msg == BFFM_INITIALIZED and data:
                    path = ctypes.wstring_at(data)
                    # ВАЖНО: строчный BFFM_SETSELECTION (wParam=1, путь) на этой
                    # сборке игнорируется — работает только PIDL-вариант
                    # (ILCreateFromPathW → wParam=0, lParam=PIDL).
                    pidl = _il_from_path(path)
                    if pidl:
                        ctypes.windll.user32.SendMessageW(
                            hwnd, BFFM_SETSELECTION, 0, ctypes.c_longlong(pidl))
                return 0
            _cb_ref = _CB(_browse_cb)
            bi.lpfn = ctypes.cast(_cb_ref, ctypes.c_void_p)
            bi.lParam = ctypes.cast(_init_buf, ctypes.c_void_p).value or 0

    _fg_stop = threading.Event()
    _fg = threading.Thread(target=_raise_dialog_foreground, args=(os.getpid(), _fg_stop), daemon=True)
    _fg.start()
    try:
        pidl = _shell.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ''
        try:
            return buf.value if _shell.SHGetPathFromIDListW(pidl, buf) else ''
        finally:
            _ole32.CoTaskMemFree(pidl)
    finally:
        _fg_stop.set()
        try:
            _ole32.CoUninitialize()
        except Exception:
            pass
