# -*- coding: utf-8 -*-
"""Подключение к базе 1С по COM (как обновлятор): сведения и серверные операции.

Работает через `comcntr.dll` выбранной платформы (V83.COMConnector) — регистрируем её
per-user ключом `regsvr32 /s /n /i:<bin>` ровно как это делает обновлятор. Через соединение
доступны: имя/версия конфигурации, режим совместимости, произвольные запросы (пользователи,
счётчики), т.е. всё, что читается языком запросов без правки конфигурации.

Ограничения (проверено): в COMConnection нет `ВыполнитьКод`/`ПолучитьСеансы`, поэтому
произвольный код BSL не выполняется и список сеансов берём из процессов (core/procs.py).
"""
import subprocess
import threading
import re
from pathlib import Path

try:                                   # pywin32: нужен и для dispatch, и для IDispatch-вызовов
    import pythoncom
    import win32com.client.dynamic as _dyn
except Exception:                      # noqa: BLE001  (на машине без pywin32 модуль всё равно импортируется)
    pythoncom = None                   # type: ignore[assignment]
    _dyn = None                        # type: ignore[assignment]

_registered: dict[str, bool] = {}
_lock = threading.Lock()
PROGIDS = ('V83.COMConnector', 'V85.COMConnector')
ROOTS = (r'C:\Program Files\1cv8', r'C:\Program Files (x86)\1cv8',
         r'D:\storage1C\ПлатформаКвас')


def _bin_of(exe: str) -> str:
    return str(Path(exe).parent) if exe else ''


def _dispatching() -> str:
    """Какой ProgID уже отвечает в системе ('' — ни один)."""
    try:
        import win32com.client.dynamic as dyn
    except Exception:                              # noqa: BLE001
        return ''
    for prog in PROGIDS:
        try:
            dyn.Dispatch(prog)
            return prog
        except Exception:                          # noqa: BLE001
            continue
    return ''


def _version_of(path: str) -> str:
    for part in reversed(Path(path).parts):
        if part[:2].isdigit() and part.count('.'):
            return part
    return ''


def has_connector(exe: str) -> bool:
    return bool(exe) and (Path(_bin_of(exe)) / 'comcntr.dll').is_file()


def pick_connector_exe(exe: str) -> str:
    """Платформа с comcntr.dll: сама база, либо другая сборка того же семейства.

    У «тонкого клиента» (8.5.4.x, 8.5.5.x в Program Files (x86)) comcntr.dll нет вообще,
    поэтому для COM берём ближайшую подходящую платформу семейства.
    """
    if has_connector(exe):
        return exe
    fam = _version_of(exe)[:3] if exe else ''       # '8.5' / '8.3'
    cands = []
    for root in ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for d in base.iterdir():
            v = d.name
            if not (d / 'bin' / 'comcntr.dll').is_file():
                continue
            if fam and not v.startswith(fam):
                continue
            cands.append((v, str(d / 'bin' / '1cv8.exe')))
    if not cands:
        return ''
    cands.sort(key=lambda kv: [int(x) for x in kv[0].split('.') if x.isdigit()], reverse=True)
    return cands[0][1]


def ensure_connector(exe: str) -> str:
    """Гарантировать рабочий COM-коннектор. '' — ок, иначе текст ошибки."""
    if _dispatching():
        return ''
    bin_dir = _bin_of(pick_connector_exe(exe) or exe)
    if not bin_dir:
        return 'не задан exe платформы'
    dll = Path(bin_dir) / 'comcntr.dll'
    if not dll.is_file():
        return ('у этой платформы нет comcntr.dll (так у «тонкого клиента») — '
                'COM-подключение к базе невозможно')
    with _lock:
        if _registered.get(bin_dir.lower()):
            return ''
        r = subprocess.run(['regsvr32', '/s', '/n', '/i:%s' % bin_dir, str(dll)],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            return ('не удалось зарегистрировать comcntr.dll из %s (rc=%s). Нужны права '
                    'администратора: запустите установку коннектора вручную '
                    'regsvr32 /n /i:"%s" "%s"' % (bin_dir, r.returncode, bin_dir, dll))
        _registered[bin_dir.lower()] = True
    return '' if _dispatching() else 'comcntr.dll зарегистрирован, но V83.COMConnector не отвечает'


def _query(conn, text: str, aliases: list[str]) -> list[dict]:
    """Выполнить запрос; псевдонимы колонок передаём сами — у курсора свойства по ним."""
    q = conn.NewObject('Query')
    q.Text = text
    sel = q.Execute().Choose()
    rows = []
    while sel.Next():
        row = {}
        for a in aliases:
            try:
                v = getattr(sel, a)
            except Exception:                      # noqa: BLE001
                v = None
            if not isinstance(v, (int, float, bool, str)) and v is not None:
                v = str(v)                         # ссылки/даты → представление
            row[a] = v
        rows.append(row)
    return rows


def _conn_string(connect: str, user: str, pwd: str) -> str:
    c = (connect or '').strip().rstrip(';')
    if user:
        c += ';Usr="%s"' % user.replace('"', '""')
        if pwd:
            c += ';Pwd="%s"' % pwd.replace('"', '""')
    return c + ';'


_USERS_FALLBACK = ('Пользователи', 'ПользователиИБ', 'ПользователиБазы')


def _ole(obj):
    """Сырой IDispatch (для вызовов по именам — русские имена win32com.dynamic не умеет)."""
    return getattr(obj, '_oleobj_', obj)


def _disp(obj, name: str) -> int:
    return _ole(obj).GetIDsOfNames(name)


def _getprop(obj, name: str):
    o = _ole(obj)
    return o.Invoke(_disp(obj, name), 0, pythoncom.DISPATCH_PROPERTYGET, True)


def _callm(obj, name: str, *args):
    o = _ole(obj)
    return o.Invoke(_disp(obj, name), 0, pythoncom.DISPATCH_METHOD, True, *args)


def users_ib(conn) -> dict:
    """Пользователи информационной базы штатным механизмом платформы.

    ПользователиИнформационнойБазы.ПолучитьПользователей() — список зарегистрированных
    пользователей ИБ. Не зависит от конфигурации (в базе может не быть справочника
    Пользователи вовсе) и не требует выборки данных; нужны права администрирования ИБ.

    Вызовы идут через IDispatch по русским именам: win32com.client.dynamic генерирует
    питоновский код из имени члена и на кириллице падает (SyntaxError «def (self, *args)»).
    """
    try:
        import win32com.client.dynamic as dyn
    except Exception as e:                             # noqa: BLE001
        return {'ok': False, 'error': 'нет pywin32: %s' % e}
    try:
        coll = _getprop(conn, 'ПользователиИнформационнойБазы')
        raw = _callm(coll, 'ПолучитьПользователей')
    except Exception as e:                             # noqa: BLE001
        return {'ok': False, 'error': str(e).splitlines()[0][:200]}
    out = []
    try:
        arr = dyn.Dispatch(raw)
        try:
            items = list(arr) if int(arr.Count()) else []
        except Exception:                              # noqa: BLE001
            items = list(arr)
    except Exception as e:                             # noqa: BLE001
        return {'ok': False, 'error': 'список получен, но не читается: %s' % str(e)[:150]}
    for u in items:
        def val(fld, dflt=None):
            try:
                return _getprop(u, fld)
            except Exception:                          # noqa: BLE001
                return dflt
        nm = str(val('Имя', '') or '').strip()
        if not nm:
            continue
        out.append({'Имя': nm,
                    'Полное': str(val('ПолноеИмя', '') or ''),
                    # АутентификацияСтандартная — вход по логину/паролю 1С (не «без пароля»!)
                    'Аутентификация1С': bool(val('АутентификацияСтандартная', True)),
                    'Заблокирован': bool(val('Заблокирован', False)),
                    'Удалён': bool(val('ПометкаУдаления', False))})
    return {'ok': True, 'users': out}


def _users_tables(conn) -> tuple:
    """Имена справочников-пользователей: сначала найденные в метаданных, затем типовые.

    В разных конфигурациях (и в пустых базах) справочник может называться иначе или отсутствовать,
    поэтому смотрим Metadata.Справочники и пробуем всё, что похоже на пользователей.
    """
    found = []
    try:
        for c in conn.Metadata.Справочники:
            nm = str(getattr(c, 'Name', '') or '')
            if 'пользовател' in nm.lower() and nm not in found:
                found.append(nm)
    except Exception:                                  # noqa: BLE001
        pass
    for nm in _USERS_FALLBACK:
        if nm not in found:
            found.append(nm)
    return tuple(found)


def probe(connect: str, user: str = '', pwd: str = '', exe: str = '',
          users_limit: int = 200, want_users: bool = True) -> dict:
    """Подключиться и собрать сведения о базе. Возвращает dict (ok/error/...)."""
    try:
        import pythoncom
        pythoncom.CoInitialize()                   # COM в рабочем потоке (не главном) надо поднять
    except Exception:                              # noqa: BLE001
        pythoncom = None                           # type: ignore[assignment]
    use_exe = pick_connector_exe(exe) or exe
    err = ensure_connector(use_exe)
    if err:
        return {'ok': False, 'error': err, 'stage': 'register'}
    try:
        import win32com.client.dynamic as dyn
    except Exception as e:                         # noqa: BLE001
        return {'ok': False, 'error': 'нет pywin32 (win32com): %s' % e, 'stage': 'import'}
    out: dict = {'ok': False, 'platform': exe, 'com_platform': use_exe, 'connect_user': user}
    try:
        conn = dyn.Dispatch(_dispatching() or 'V83.COMConnector').Connect(
            _conn_string(connect, user, pwd))
    except Exception as e:                         # noqa: BLE001
        msg = str(e)
        hint = ''
        low = msg.lower()
        if 'идентификация' in low or 'пароль' in low or 'user' in low:
            hint = 'нужны логин/пароль пользователя ИБ (настройки базы)'
        elif 'различаются версии' in low or 'верси' in low:
            hint = 'версия платформы не совпадает с версией сервера'
        out.update({'error': msg[:600], 'hint': hint, 'stage': 'connect'})
        return out
    md = None
    try:
        md = conn.Metadata
        info = {'name': str(md.Name or ''), 'synonym': str(md.Synonym or ''),
                'version': str(md.Version or '')}
        try:
            info['compat'] = str(md.CompatibilityMode)
        except Exception:                          # noqa: BLE001
            info['compat'] = ''
    except Exception as e:                         # noqa: BLE001
        info = {'error': 'метаданные недоступны: %s' % str(e)[:200]}
    out['config'] = info
    out['ok'] = True

    if not want_users:
        return out
    # 1) пользователи ИБ штатным механизмом платформы (не зависит от конфигурации)
    out['users'] = []
    ib = users_ib(conn)
    if ib.get('ok'):
        rows_ib = ib['users']
        out['users'] = rows_ib[:users_limit]
        out['users_count'] = len(rows_ib)
        out['users_active'] = sum(1 for r in rows_ib if not r.get('Удалён'))
        out['users_source'] = 'ПользователиИнформационнойБазы'
        return out
    out['users_ib_error'] = ib.get('error') or ''
    # 2) справочник Пользователи (имя в разных конфигурациях может отличаться)
    last_err = ''
    out['users_tried'] = []
    for table in _users_tables(conn):
        out['users_tried'].append(table)
        try:
            rows = _query(conn, 'ВЫБРАТЬ Пользователи.Наименование КАК Имя, '
                                'Пользователи.ПометкаУдаления КАК Удалён, '
                                'Пользователи.НедействительныйПароль КАК БезПароля '
                                'ИЗ Справочник.%s КАК Пользователи '
                                'УПОРЯДОЧИТЬ ПО Имя' % table,
                          ['Имя', 'Удалён', 'БезПароля'])
            out['users'] = rows[:users_limit]
            out['users_count'] = len(rows)
            out['users_active'] = sum(1 for r in rows if not r.get('Удалён'))
            out['users_table'] = 'Справочник.' + table
            last_err = ''
            break
        except Exception as e:                     # noqa: BLE001
            last_err = str(e).splitlines()[0][:200]
            # нет такого справочника / нет полей (ПометкаУдаления, НедействительныйПароль) — пробуем проще
            try:
                rows = _query(conn, 'ВЫБРАТЬ Пользователи.Наименование КАК Имя '
                                    'ИЗ Справочник.%s КАК Пользователи УПОРЯДОЧИТЬ ПО Имя' % table,
                              ['Имя'])
                out['users'] = rows[:users_limit]
                out['users_count'] = len(rows)
                out['users_table'] = 'Справочник.' + table
                last_err = ''
                break
            except Exception as e2:                 # noqa: BLE001
                last_err = str(e2).splitlines()[0][:200]
    if last_err:
        out['users_error'] = last_err
    # общие счётчики (заодно проверка «живости» соединения)
    try:
        cnt = _query(conn, 'ВЫБРАТЬ КОЛИЧЕСТВО(*) КАК Всего ИЗ Справочник.Пользователи', ['Всего'])
        out['users_count'] = int(cnt[0].get('Всего') or 0) if cnt else out.get('users_count')
    except Exception:                              # noqa: BLE001
        pass
    try:
        conn = None                                # отпускаем COM-объекты
    except Exception:                              # noqa: BLE001
        pass
    return out


def detect_server_version(connect: str, user: str = '', pwd: str = '', exe: str = '',
                          timeout: float = 60.0) -> dict:
    """Узнать версию сервера без окна 1С: COM-подключение любым коннектором.

    Если версии не совпали, 1С отдаёт текст «Различаются версии клиента и сервера
    (клиент - сервер)» — этого достаточно, чтобы закрепить за базой нужную платформу.
    """
    res = probe_async(connect, user, pwd, exe, timeout, want_users=False)
    err = str(res.get('error') or '')
    out = {'ok': bool(res.get('ok')), 'client_version': _version_of(res.get('com_platform') or ''),
           'server_version': '', 'error': err, 'platform': res.get('com_platform') or ''}
    m = re.search(r'\(([\d.]+)\s*[-–]\s*([\d.]+)\)', err)
    if m:
        out['client_version'] = m.group(1)
        out['server_version'] = m.group(2)
    if res.get('ok'):
        out['server_version'] = out['server_version'] or out['client_version']
    return out


def probe_async(connect: str, user: str = '', pwd: str = '', exe: str = '',
                timeout: float = 90.0, want_users: bool = True) -> dict:
    """probe() с жёстким таймаутом: COM может висеть на недоступном сервере."""
    box: dict = {}

    def run():
        try:
            box.update(probe(connect, user, pwd, exe, want_users=want_users))
        except Exception as e:                     # noqa: BLE001
            box.update({'ok': False, 'error': str(e)[:400], 'stage': 'probe'})

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {'ok': False, 'error': 'таймаут подключения (%.0f с)' % timeout, 'stage': 'timeout'}
    return box or {'ok': False, 'error': 'нет ответа', 'stage': 'empty'}
