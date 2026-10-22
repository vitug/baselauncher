# -*- coding: utf-8 -*-
"""Живые сеансы 1С на этом хосте: что открыто, кто в каком режиме, активация окна.

Только stdlib + ctypes (внешних пакетов в ядре нет):
  * список процессов — CreateToolhelp32Snapshot;
  * командная строка — NtQueryInformationProcess(ProcessCommandLineInformation=60)
    (WMI/PowerShell не используем: медленно и ломает кириллицу);
  * вывод окна наверх — EnumWindows + ShowWindow/SetForegroundWindow с AttachThreadInput
    (без этого Windows не даёт чужому процессу украсть фокус).
"""
import ctypes
import ctypes.wintypes as wt
import re

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')
u32 = ctypes.WinDLL('user32', use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
CLIENT_EXES = ('1cv8.exe', '1cv8c.exe', '1cv8s.exe')


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD),
                ('th32ProcessID', wt.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
                ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wt.DWORD), ('szExeFile', wt.WCHAR * 260)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', wt.USHORT), ('MaximumLength', wt.USHORT),
                ('Buffer', ctypes.c_void_p)]


def _processes() -> list[tuple[int, str]]:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    out = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out.append((int(pe.th32ProcessID), pe.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def cmdline(pid: int) -> str:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ''
    try:
        size = wt.ULONG(0)
        ntdll.NtQueryInformationProcess(wt.HANDLE(h), 60, None, 0, ctypes.byref(size))
        if not size.value:
            return ''
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(wt.HANDLE(h), 60, buf, size.value,
                                           ctypes.byref(size)) != 0:
            return ''
        us = UNICODE_STRING.from_buffer(buf)
        if not us.Buffer or not us.Length:
            return ''
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:
        return ''
    finally:
        k32.CloseHandle(h)


def _norm_path(p: str) -> str:
    return re.sub(r'/+', '/', (p or '').strip().strip('"').replace('\\', '/')).rstrip('/').lower()


def parse_session(cmd: str) -> dict:
    """Что за база и режим в командной строке сеанса.

    Учитываем два вида кавычек: обычные и CRT-экранированные (`File=""D:\\база""`),
    а также запуск через `/F"путь"` и `/IBName"имя из списка"`.
    """
    c = (cmd or '').replace('""', '"')
    mode = 'designer' if re.search(r'(?i)(^|\s)/?DESIGNER(\s|$)', c) else 'enterprise'
    out = {'mode': mode, 'path': '', 'server': '', 'ref': '', 'name': ''}
    m = re.search(r'(?i)File\s*=\s*"([^"]+)"', c)
    if m:
        out['path'] = m.group(1)
    else:
        m = re.search(r'(?i)(?:^|\s)/F\s*"([^"]+)"', c)
        if m:
            out['path'] = m.group(1)
    m = re.search(r'(?i)Srvr\s*=\s*"([^"]*)"', c)
    if m:
        out['server'] = m.group(1)
    m = re.search(r'(?i)Ref\s*=\s*"([^"]*)"', c)
    if m:
        out['ref'] = m.group(1)
    m = re.search(r'(?i)/IBName\s*"?([^"/]+)"?', c)
    if m:
        out['name'] = m.group(1).strip()
    return out


def running_sessions() -> list[dict]:
    """Список живых клиентских сеансов 1С."""
    res = []
    for pid, exe in _processes():
        if exe.lower() not in CLIENT_EXES:
            continue
        cmd = cmdline(pid)
        if not cmd:
            continue
        s = parse_session(cmd)
        s.update({'pid': pid, 'exe': exe})
        res.append(s)
    return res


def _top_windows(pid: int) -> list[int]:
    out = []
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:                      # только окна с заголовком (не служебные V8Window)
                out.append(hwnd)
        return True
    u32.EnumWindows(CB(cb), 0)
    return out


def focus_window(pid: int) -> dict:
    """Вывести главное окно сеанса наверх. Возвращает {ok, title} или {ok:False, error}."""
    hwnds = _top_windows(pid)
    if not hwnds:
        return {'ok': False, 'error': 'окно сеанса не найдено (может, он закрывается)'}
    hwnd = hwnds[0]
    title_len = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(title_len + 1)
    u32.GetWindowTextW(hwnd, buf, title_len + 1)
    try:
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, SW_RESTORE)
        fg = u32.GetForegroundWindow()
        tid_cur = k32.GetCurrentThreadId()
        tid_fg = u32.GetWindowThreadProcessId(fg, None)
        attached = False
        if tid_fg and tid_fg != tid_cur:
            attached = bool(u32.AttachThreadInput(tid_cur, tid_fg, True))
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.SetActiveWindow(hwnd)
        if attached:
            u32.AttachThreadInput(tid_cur, tid_fg, False)
    except Exception as e:
        return {'ok': False, 'error': f'не удалось активировать окно: {e}'}
    return {'ok': True, 'title': buf.value, 'pid': pid}
