# -*- coding: utf-8 -*-
"""Нативный диалог выбора файла (Windows, comdlg32/GetOpenFileNameW) — только stdlib.

Нужен для выбора внешней обработки в «разовых ключах» (`/Execute`). Сервер живёт на той же
машине, что и пользователь, поэтому диалог открывается прямо из процесса сервера.
Вызывать из обычного (не главного) потока — FastAPI для sync-эндпоинтов так и делает.
"""
import ctypes
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


def pick_folder(title: str = 'Выбрать папку', initial: str = '') -> str:
    """Диалог выбора папки (SHBrowseForFolderW).

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
    try:
        pidl = _shell.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ''
        try:
            return buf.value if _shell.SHGetPathFromIDListW(pidl, buf) else ''
        finally:
            _ole32.CoTaskMemFree(pidl)
    finally:
        try:
            _ole32.CoUninitialize()
        except Exception:
            pass
