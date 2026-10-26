# -*- coding: utf-8 -*-
"""Виртуальные рабочие столы Windows 11 (для dev-режима BaseLauncher).

Dev-инстанс открывает своё окно на отдельном виртуальном столе, чтобы не мешать
рабочему: фокус, Alt+Tab и запуски баз уходят мимо основного стола.

COM-интерфейсы недокументированы; GUID-ы и порядок vtable — Windows 11 24H2+
(проверено на сборке 26200, 25H2; источник раскладки — MScholtes/VirtualDesktop,
VirtualDesktop11-24H2.cs). На других сборках может потребоваться правка _IDX.

Только ctypes (ядро BaseLauncher — stdlib). Все вызовы — из одного потока
с CoInitializeEx(APARTMENTTHREADED).
"""
import ctypes
import ctypes.wintypes as wt
from ctypes import POINTER, WINFUNCTYPE, byref, c_void_p, c_ulong

ole32 = ctypes.OleDLL('ole32')
combase = ctypes.WinDLL('combase', use_last_error=True)

CLSCTX_LOCAL_SERVER = 0x4
COINIT_APARTMENTTHREADED = 0x2
S_OK = 0


class GUID(ctypes.Structure):
    _fields_ = [('Data1', c_ulong), ('Data2', ctypes.c_ushort),
                ('Data3', ctypes.c_ushort), ('Data4', ctypes.c_ubyte * 8)]

    def __str__(self):
        d = self.Data4
        return '{%08X-%04X-%04X-%02X%02X-%s}' % (
            self.Data1, self.Data2, self.Data3, d[0], d[1],
            ''.join('%02X' % b for b in d[2:]))

    @classmethod
    def parse(cls, s: str) -> 'GUID':
        g = GUID()
        rc = ole32.IIDFromString(s, byref(g))
        if rc != 0:
            raise OSError('IIDFromString(%s): 0x%08X' % (s, rc & 0xFFFFFFFF))
        return g


# --- GUID-ы (Win11 24H2+, сборки 26100/26200) -------------------------------
CLSID_IMMERSIVE_SHELL = GUID.parse('{C2F03A33-21F5-47FA-B4BB-156362A2F239}')
CLSID_VDM_INTERNAL = GUID.parse('{C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B}')  # service
IID_ISERVICE_PROVIDER = GUID.parse('{6D5140C1-7436-11CE-8034-00AA006009FA}')
IID_IVDM_INTERNAL = GUID.parse('{53F5CA0B-158F-4124-900C-057158060B27}')
IID_IVIRTUAL_DESKTOP = GUID.parse('{3F07F4BE-B107-441A-AF0F-39D82529072C}')
IID_IOBJECT_ARRAY = GUID.parse('{92CA9DCD-5622-4BBA-A805-5E9F541BD8C9}')
CLSID_VDM = GUID.parse('{AA509086-5CA9-4C25-8F95-589D3C07B48A}')
IID_IVDM = GUID.parse('{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}')
IID_IAPPVIEW_COLLECTION = GUID.parse('{1841C6D7-4F9D-42C0-AF41-8747538F10E5}')

# vtable-индексы IVirtualDesktopManagerInternal (24H2+; после IUnknown 0..2)
_IDX = {'GetCount': 3, 'GetCurrentDesktop': 6, 'GetDesktops': 7,
        'SwitchDesktop': 9, 'CreateDesktop': 11, 'FindDesktop': 14,
        'SetDesktopName': 16}
_IDX_VDM = {'IsWindowOnCurrentVirtualDesktop': 3, 'GetWindowDesktopId': 4,
            'MoveWindowToDesktop': 5}
_IDX_SP = {'QueryService': 3}
_IDX_IVD = {'GetId': 4}
_IDX_IOA = {'GetCount': 3, 'GetAt': 4}

import threading

_tls = threading.local()


def _init_com():
    """CoInitializeEx нужен в КАЖДОМ потоке, который зовёт COM (APARTMENTTHREADED)."""
    if not getattr(_tls, 'com', False):
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        _tls.com = True


def _call(iface: int, idx: int, restype, *argtypes):
    """Функция из vtable COM-интерфейса (iface — значение указателя)."""
    vt = ctypes.cast(c_void_p(iface), POINTER(c_void_p))[0]
    fn = ctypes.cast(c_void_p(vt), POINTER(c_void_p))[idx]
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)


CLSCTX_ALL = 0x7


def _create(clsid: GUID, iid: GUID, ctx: int = CLSCTX_LOCAL_SERVER) -> int:
    _init_com()
    ppv = c_void_p()
    rc = ole32.CoCreateInstance(byref(clsid), None, ctx,
                                byref(iid), byref(ppv))
    if rc != 0 or not ppv.value:
        raise OSError('CoCreateInstance(%s): 0x%08X' % (clsid, rc & 0xFFFFFFFF))
    return ppv.value


def _internal() -> int:
    """IVirtualDesktopManagerInternal через IServiceProvider оболочки."""
    _init_com()
    shell = _create(CLSID_IMMERSIVE_SHELL, IID_ISERVICE_PROVIDER)
    ppv = c_void_p()
    rc = _call(shell, _IDX_SP['QueryService'], ctypes.c_long,
               POINTER(GUID), POINTER(GUID), POINTER(c_void_p))(
        shell, byref(CLSID_VDM_INTERNAL), byref(IID_IVDM_INTERNAL), byref(ppv))
    if rc != S_OK or not ppv.value:
        raise OSError('QueryService(IVirtualDesktopManagerInternal): 0x%08X — '
                      'несовместимая сборка Windows?' % (rc & 0xFFFFFFFF))
    return ppv.value


def _desktop_guids() -> list[str]:
    """GUID-ы всех виртуальных столов (порядок = порядок в переключателе)."""
    internal = _internal()
    arr = c_void_p()
    rc = _call(internal, _IDX['GetDesktops'], ctypes.c_long,
               POINTER(c_void_p))(internal, byref(arr))
    if rc != S_OK or not arr.value:
        raise OSError('GetDesktops: 0x%08X' % (rc & 0xFFFFFFFF))
    _cnt = ctypes.c_int()
    _call(arr.value, _IDX_IOA['GetCount'], ctypes.c_long,
          POINTER(ctypes.c_int))(arr.value, byref(_cnt))
    cnt = _cnt.value
    out = []
    for i in range(cnt):
        dsk = c_void_p()
        rc = _call(arr.value, _IDX_IOA['GetAt'], ctypes.c_long,
                   ctypes.c_int, POINTER(GUID), POINTER(c_void_p))(
            arr.value, i, byref(IID_IVIRTUAL_DESKTOP), byref(dsk))
        if rc != S_OK or not dsk.value:
            continue
        g = GUID()
        rc = _call(dsk.value, _IDX_IVD['GetId'], ctypes.c_long,
                   POINTER(GUID))(dsk.value, byref(g))
        if rc == S_OK:
            out.append(str(g))
    return out


def create_desktop() -> str:
    """Создать новый виртуальный стол, вернуть его GUID."""
    internal = _internal()
    dsk = c_void_p()
    rc = _call(internal, _IDX['CreateDesktop'], ctypes.c_long,
               POINTER(c_void_p))(internal, byref(dsk))
    if rc != S_OK or not dsk.value:
        raise OSError('CreateDesktop: 0x%08X' % (rc & 0xFFFFFFFF))
    g = GUID()
    rc = _call(dsk.value, _IDX_IVD['GetId'], ctypes.c_long,
               POINTER(GUID))(dsk.value, byref(g))
    return str(g)


def set_desktop_name(guid: str, name: str) -> bool:
    """Имя стола в переключателе (Win+Ctrl+Tab). Не критично, если не выйдет."""
    try:
        internal = _internal()
        dsk = find_desktop(guid)
        if not dsk:
            return False
        hs = c_void_p()
        rc = combase.WindowsCreateString(name, len(name), byref(hs))
        if rc != S_OK:
            return False
        rc = _call(internal, _IDX['SetDesktopName'], ctypes.c_long,
                   c_void_p, c_void_p)(internal, dsk, hs)
        combase.WindowsDeleteString(hs)
        return rc == S_OK
    except OSError:
        return False


def find_desktop(guid: str) -> int:
    """Указатель IVirtualDesktop по GUID (0 — нет)."""
    internal = _internal()
    g = GUID.parse(guid)
    dsk = c_void_p()
    rc = _call(internal, _IDX['FindDesktop'], ctypes.c_long,
               POINTER(GUID), POINTER(c_void_p))(
        internal, byref(g), byref(dsk))
    return dsk.value if rc == S_OK else 0


def window_desktop(hwnd: int) -> str:
    """GUID стола, на котором сейчас окно."""
    vdm = _create(CLSID_VDM, IID_IVDM, CLSCTX_ALL)
    g = GUID()
    rc = _call(vdm, _IDX_VDM['GetWindowDesktopId'], ctypes.c_long,
               wt.HWND, POINTER(GUID))(vdm, hwnd, byref(g))
    return str(g) if rc == S_OK else ''


def current_desktop() -> str:
    internal = _internal()
    dsk = c_void_p()
    rc = _call(internal, _IDX['GetCurrentDesktop'], ctypes.c_long,
               POINTER(c_void_p))(internal, byref(dsk))
    if rc != S_OK or not dsk.value:
        return ''
    g = GUID()
    rc = _call(dsk.value, _IDX_IVD['GetId'], ctypes.c_long,
               POINTER(GUID))(dsk.value, byref(g))
    return str(g) if rc == S_OK else ''


def _appview(hwnd: int) -> int:
    """IApplicationView для любого чужого окна — через IApplicationViewCollection."""
    _init_com()
    shell = _create(CLSID_IMMERSIVE_SHELL, IID_ISERVICE_PROVIDER)
    ppv = c_void_p()
    rc = _call(shell, _IDX_SP['QueryService'], ctypes.c_long,
               POINTER(GUID), POINTER(GUID), POINTER(c_void_p))(
        shell, byref(IID_IAPPVIEW_COLLECTION), byref(IID_IAPPVIEW_COLLECTION),
        byref(ppv))
    if rc != S_OK or not ppv.value:
        raise OSError('QueryService(IApplicationViewCollection): 0x%08X'
                      % (rc & 0xFFFFFFFF))
    view = c_void_p()
    # vtable: GetViews=3, GetViewsByZOrder=4, GetViewsByAUMID=5, GetViewForHwnd=6
    rc = _call(ppv.value, 6, ctypes.c_long, wt.HWND,
               POINTER(c_void_p))(ppv.value, hwnd, byref(view))
    if rc != S_OK or not view.value:
        raise OSError('GetViewForHwnd: 0x%08X' % (rc & 0xFFFFFFFF))
    return view.value


def move_window(hwnd: int, guid: str) -> None:
    """Перенести ЛЮБОЕ окно на виртуальный стол с этим GUID.

    IVirtualDesktopManager::MoveWindowToDesktop работает только для окон
    собственного процесса (чужие — E_ACCESSDENIED), поэтому переносим через
    IVirtualDesktopManagerInternal::MoveViewToDesktop(IApplicationView, desktop).
    """
    internal = _internal()
    dsk = find_desktop(guid)
    if not dsk:
        raise OSError('стол %s не найден' % guid)
    view = _appview(hwnd)
    rc = _call(internal, 4, ctypes.c_long, c_void_p,
               c_void_p)(internal, view, dsk)      # 4 = MoveViewToDesktop
    if rc != S_OK:
        raise OSError('MoveViewToDesktop: 0x%08X' % (rc & 0xFFFFFFFF))


def remove_desktop(guid: str, fallback: str) -> bool:
    """Удалить виртуальный стол (окна переезжают на fallback)."""
    dsk = find_desktop(guid)
    fb = find_desktop(fallback)
    if not dsk or not fb:
        return False
    internal = _internal()
    rc = _call(internal, 13, ctypes.c_long, c_void_p, c_void_p)(internal, dsk, fb)
    return rc == S_OK


def switch_to(guid: str) -> bool:
    """Переключиться на стол (как Win+Ctrl+стрелка)."""
    dsk = find_desktop(guid)
    if not dsk:
        return False
    internal = _internal()
    rc = _call(internal, _IDX['SwitchDesktop'], ctypes.c_long,
               c_void_p, c_void_p)(internal, dsk)
    return rc == S_OK


DEV_DESKTOP_NAME = 'BaseLauncher DEV'


def ensure_dev_desktop() -> tuple[str, bool]:
    """Стол для dev-окна: находим по имени «BaseLauncher DEV» или создаём.

    Имя стола COM не отдаёт (в 24H2 его читают через IObjectArray
    ApplicationView — сложно); поэтому запоминаем GUID созданного стола
    в файл рядом с данными dev-инстанса и переиспользуем его.
    """
    from .paths import DATA_DIR
    import json
    mark = DATA_DIR / 'vdesk.json'
    known = ''
    if mark.is_file():
        try:
            known = (json.loads(mark.read_text(encoding='utf-8')) or {}).get('guid', '')
        except ValueError:
            pass
    guids = _desktop_guids()
    if known and known in guids:
        return known, False
    # прежний стол удалён — создаём новый
    guid = create_desktop()
    set_desktop_name(guid, DEV_DESKTOP_NAME)
    mark.write_text(json.dumps({'guid': guid, 'name': DEV_DESKTOP_NAME}),
                    encoding='utf-8')
    return guid, True


def move_window_to_dev_desktop(hwnd: int, switch: bool = False) -> dict:
    """Перенести окно dev-инстанса на его виртуальный стол."""
    guid, created = ensure_dev_desktop()
    move_window(hwnd, guid)
    res = {'ok': True, 'desktop': guid, 'created': created, 'hwnd': hwnd}
    if switch:
        res['switched'] = switch_to(guid)
    return res


def status() -> dict:
    """Диагностика: сколько столов, где окно. Для /api/vdesk в dev-режиме."""
    try:
        guids = _desktop_guids()
        return {'ok': True, 'desktops': len(guids), 'current': current_desktop(),
                'all': guids}
    except OSError as e:
        return {'ok': False, 'error': str(e)}
