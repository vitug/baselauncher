# -*- coding: utf-8 -*-
"""REST-слой BaseLauncher (FastAPI). Только 127.0.0.1."""
import json
import re
import subprocess
import time
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core import (cache, com, config, hotkey, importer, launcher, netcheck, platforms_share,
                  procs, tray, v8i_writer, watch)

app = FastAPI(title='BaseLauncher', version='0.29.1')
PORT = int(__import__('os').environ.get('BL_PORT') or 8797)

INDEX = Path(__file__).parent / 'static' / 'index.html'
ICON = Path(__file__).parent.parent / 'assets' / 'baselauncher.ico'
ROOT = Path(__file__).parent.parent


@app.on_event('startup')
def _startup():
    """Глобальная клавиша Ctrl+Shift+S (показать / убрать в трей) и иконка в трее."""
    hotkey.start(PORT)          # клавиша Ctrl+Shift+S + сторож геометрии окна

    def _ensure_window():
        import time
        time.sleep(0.8)
        try:
            if not hotkey.find_starter_window(PORT):
                hotkey.focus_starter(PORT)      # окна нет — открыть (и одно!)
        except Exception:                       # noqa: BLE001
            pass

    threading.Thread(target=_ensure_window, daemon=True, name='bl-window').start()
    tray.start(PORT)


@app.get('/')
def index():
    # no-store: иначе Edge держит старую копию UI в кеше, и после обновления приложения
    # в окне продолжает работать прежний JS (лечится только F5, о котором никто не знает)
    html = INDEX.read_text(encoding='utf-8').replace('__BL_VER__', app.version)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store, must-revalidate',
                                       'Pragma': 'no-cache', 'Expires': '0'})


@app.get('/favicon.ico')
def favicon():
    """Иконка приложения: та же, что у ярлыка в меню «Пуск» (assets/baselauncher.ico)."""
    if ICON.is_file():
        return FileResponse(ICON, media_type='image/x-icon')
    return JSONResponse({'error': 'assets/baselauncher.ico отсутствует'}, status_code=404)


# ---------------------------------------------------------------- MCP (часть приложения)
@app.post('/mcp')
async def mcp_endpoint(req: Request):
    """MCP-транспорт (streamable HTTP, JSON-ответы). Тело — JSON-RPC 2.0 (одно или батч)."""
    from fastapi import Response
    from app.mcp import handle_http
    try:
        body = await req.json()
    except Exception:                                        # noqa: BLE001
        return JSONResponse({'jsonrpc': '2.0', 'id': None,
                             'error': {'code': -32700, 'message': 'Parse error'}},
                            status_code=400)
    out = handle_http(body)
    if out is None:                     # уведомление: ответа нет
        return Response(status_code=202)
    return JSONResponse(out)


@app.get('/mcp')
def mcp_info():
    """GET на тот же адрес: человеку — что это и какие инструменты есть."""
    from app.mcp import SERVER_NAME, SERVER_VERSION, tools_list
    return {'server': SERVER_NAME, 'version': SERVER_VERSION,
            'transport': 'streamable-http (POST JSON-RPC 2.0)', 'path': '/mcp',
            'tools': [t['name'] for t in tools_list()]}


@app.get('/api/state')
def state():
    """Полное состояние: базы (со своим конфигом), папки, платформы, семейства."""
    return {**importer.build_state(), 'version': app.version}


@app.get('/api/sessions')
def sessions():
    """Лёгкий ответ для частого опроса UI (3 с): какие базы открыты и в каком режиме."""
    if not importer._BASES_CACHE:
        importer.build_state()
    # version — чтобы окно, загруженное старой версией, само перечиталось после обновления
    data = importer.sessions_light()
    _learn_from_sessions(data)
    return {**data, 'version': app.version}


def _learn_from_sessions(data: dict) -> None:
    """Запомнить платформу, которой базу реально открыли (её и просит сервер).

    Версию сервера 1С заранее не сообщает (текст сообщений не читается, API нет), зато
    живой сеанс — готовый ответ: если базу открыли 8.5.x, значит сервер такой и требует.
    Учимся только когда пользователь платформу не выбирал (нет bases[guid].exe).
    """
    try:
        cfg = config.load()
    except Exception:                       # noqa: BLE001
        return
    changed = False
    for guid, run in (data.get('running') or {}).items():
        our = config.get_base(cfg, guid)
        if our.get('exe'):
            continue                        # выбран вручную — не трогаем
        pid = next((p for p in (run.get('pids') or {}).values() if p), 0)
        exe = _exe_of(pid) if pid else ''
        if not exe or not re.search(r'[\\/]1cv8[cs]?\.exe$', exe, re.I):
            continue
        if (our.get('learnt') or '').lower() != exe.lower():
            try:
                config.set_base(guid, {'learnt': exe})
                changed = True
            except Exception:               # noqa: BLE001
                pass
    if changed:
        importer.build_state()


def detect_platform(base: dict, pin: bool = True) -> dict:
    """Узнать версию сервера 1С через COM и (по умолчанию) закрепить её платформу за базой.

    Работает без окна: даже неудачное COM-подключение отдаёт «Различаются версии клиента и
    сервера (клиент - сервер)», откуда берём версию сервера. Для файловых баз версия сервера =
    версия коннектора, поэтому там просто проверяем подключение.
    """
    guid = base.get('guid') or ''
    r = com.detect_server_version(base.get('connect') or '', base.get('user') or '',
                                  base.get('pwd') or '', base.get('exe') or '', 90)
    ver = (r.get('server_version') or '').strip()
    out = {'ok': bool(r.get('ok')), 'server_version': ver,
           'client_version': r.get('client_version') or '', 'exe': '', 'pinned': False,
           'error': r.get('error') or '', 'base': base.get('name') or ''}
    if not ver:
        out['hint'] = ('если база требует логин — задайте пользователя/пароль базы '
                       '(set_credentials) и повторите')
        return out
    plats = importer.build_state().get('platforms', [])
    exact = [p for p in plats if (p.get('key') or '') == ver]
    if not exact:                       # такой сборки нет — берём ближайшую того же семейства
        fam = ver[:3]
        same = [p for p in plats if (p.get('key') or '').startswith(fam) and (p.get('exe') or p.get('exe_thin'))]
        exact = same[:1]
        out['note'] = 'точная сборка %s не установлена — взята ближайшая' % ver if same else \
            'ни одной платформы %s не установлено' % fam
    if exact:
        exe = exact[0].get('exe') or exact[0].get('exe_thin') or ''
        out['exe'] = exe
        out['version'] = exact[0].get('key')
        if pin and exe and not base.get('ws'):
            config.set_base(guid, {'exe': exe, 'family': '', 'learnt': ''})
            importer.build_state()
            out['pinned'] = True
    return out


def _base_or_404(guid: str):
    st = importer.build_state()
    base = importer.find_base(st, guid)
    return (st, base) if base else (None, None)


@app.get('/api/base/{guid}')
def base_detail(guid: str):
    st, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    return {'base': base, 'platforms': st['platforms']}


@app.put('/api/base/{guid}')
async def base_save(guid: str, req: Request):
    """Сохранить настройки базы в свой конфиг (data/config.json).

    Поля: exe ('' = авто), thin (bool), mode, fav, name/descr/user,
    pwd / pwd_cfg (пароли ИБ для Предприятия/Конфигуратора — кладутся зашифрованными),
    storage {dir,user,password}, extra_keys [..].
    Отсутствующие поля не трогаются; {'clear': true} — сбросить всё.
    """
    _, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    body = await req.json()
    # имя/описание, совпадающие с именем базы в ibases.v8i, — не переопределение: пишем пусто,
    # иначе в конфиге копятся дубли имени и поле «Описание» показывает имя базы
    ibname = (base.get('ibname') or '').strip()
    if ibname:
        for f in ('name', 'descr'):
            if str(body.get(f) or '').strip() == ibname:
                body[f] = ''
    # выбрали «авто» по платформе → снимаем и закреплённое семейство (8.3/8.5)
    if 'exe' in body and not (body.get('exe') or '').strip():
        body['family'] = ''
        body['learnt'] = ''                 # и «выученную» платформу: пусть определится заново
    config.set_base(guid, body)

    st = importer.build_state()
    return {'ok': True, 'base': importer.find_base(st, guid)}


def _build_cmd_from_body(body: dict, base: dict):
    exe = body.get('exe') or base['exe']
    mode = body.get('mode', 'enterprise')
    extra = body.get('extra_keys')
    err = _check_execute(extra)
    if err:
        return JSONResponse({'error': err}, status_code=400), mode
    storage = body.get('storage')
    if storage is None:
        storage = base.get('storage') or {}
    # пользователь: у Конфигуратора может быть свой логин; пусто — берём общий
    if mode == 'designer':
        user = (body.get('user_cfg') or base.get('user_cfg')     # свой для Конфигуратора
                or body.get('user') or base.get('user') or '')
    else:
        user = body.get('user') or base.get('user') or ''
    # пароль: из запроса -> свой для режима -> свой для другого режима (config.json, sealed)
    pwd = (body.get('pwd') or '').strip()
    if not pwd:
        if mode == 'designer':
            pwd = base.get('pwd_cfg') or base.get('pwd') or ''
        else:
            pwd = base.get('pwd') or base.get('pwd_cfg') or ''
    # конфигуратора может не быть: веб-база (ws) или платформа «только тонкий клиент»
    if mode == 'designer' and base.get('has_designer') is False:
        if base.get('ws'):
            return JSONResponse({'error': 'веб-база: конфигуратора у сайта нет — '
                                          'запускайте Предприятие или откройте в браузере (🌐)'},
                                status_code=400), mode
        return JSONResponse({'error': 'у платформы только тонкий клиент (1cv8c.exe) — '
                                      'конфигуратора нет, выберите полную платформу'},
                            status_code=400), mode
    # веб-база (ws=): Предприятие — тонкий клиент (1cv8c.exe),
    # Конфигуратор — только толстый (у тонкого конфигуратора нет)
    if base.get('ws') and not body.get('exe'):
        if mode == 'enterprise' and exe and not exe.lower().endswith('1cv8c.exe'):
            alt = Path(exe).with_name('1cv8c.exe')
            if alt.is_file():
                exe = str(alt)
        elif mode == 'designer' and exe and exe.lower().endswith('1cv8c.exe'):
            alt = Path(exe).with_name('1cv8.exe')
            if alt.is_file():
                exe = str(alt)
        # клиент новее публикации к ней не подключается («Не удается установить соединение»,
        # 1С при этом ещё и перезапускается автообновлением). Берём платформу версии публикации.
        try:
            pub = netcheck.web_version(base['ws'])
            plats = importer.build_state().get('platforms', [])
            low = (exe or '').lower()
            cur = ''
            if low:
                for p in plats:
                    if low in ((p.get('exe') or '').lower(), (p.get('exe_thin') or '').lower()):
                        cur = p.get('key') or ''
                        break
            if pub and cur and pub != cur:
                for p in plats:
                    if p.get('key') == pub and (p.get('exe_thin') or p.get('exe')):
                        exe = p.get('exe_thin') or p.get('exe')
                        break
        except Exception:                       # noqa: BLE001
            pass
    try:
        return launcher.build_cmd(exe, mode, base['connect'], extra, storage,
                                  user, pwd), mode
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400), mode


def _check_execute(extra: list | None) -> str | None:
    """Разовый ключ /Execute — файл обработки должен существовать (иначе 1С молча падает)."""
    for k in extra or []:
        s = str(k).strip()
        if not s.lower().startswith('/execute'):
            continue
        p = s[len('/execute'):].strip().strip('"')
        if not p:
            return 'пустой путь в /Execute'
        if not Path(p).is_file():
            return f'файл обработки не найден: {p}'
        if Path(p).suffix.lower() not in ('.epf', '.erf'):
            return f'/Execute ждёт .epf или .erf, а не {Path(p).suffix or "(без расширения)"}'
    return None


@app.post('/api/pickfile')
def pickfile(req: dict):
    """Нативный диалог выбора файла обработки (сервер на этой же машине)."""
    try:
        from core import filedlg
        p = filedlg.pick_file(title=req.get('title') or 'Выбрать файл',
                              initial_dir=_initial_dir(req.get('initial')))
        return {'path': p, 'cancelled': p == ''}
    except Exception as e:                     # noqa: BLE001
        return JSONResponse({'error': f'{type(e).__name__}: {e}'}, status_code=500)


def _initial_dir(p: str | None) -> str:
    if not p:
        return ''
    q = Path(str(p))
    if q.is_file():
        return str(q.parent)
    return str(q) if q.is_dir() else ''


@app.post('/api/dryrun')
async def dryrun(req: Request):
    body = await req.json()
    _, base = _base_or_404(body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    cmd, _ = _build_cmd_from_body(body, base)
    if isinstance(cmd, JSONResponse):
        return cmd
    return {'cmd': cmd}


@app.post('/api/launch')
async def launch(req: Request):
    body = await req.json()
    _, base = _base_or_404(body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    cmd, mode = _build_cmd_from_body(body, base)
    if isinstance(cmd, JSONResponse):
        return cmd
    exe = body.get('exe') or base.get('exe')
    # для веб-баз платформа могла быть заменена на версию публикации внутри build_cmd —
    # сторож должен знать настоящий exe (в командной строке он первым в кавычках)
    try:
        import re as _re
        m_exe = _re.match(r'"([^"]+)"', cmd if isinstance(cmd, str) else '')
        if m_exe and Path(m_exe.group(1)).is_file():
            exe = m_exe.group(1)
    except Exception:                       # noqa: BLE001
        pass
    if not exe or not Path(exe).is_file():
        return JSONResponse(
            {'error': f'exe платформы не найден на диске: {exe or "(не определён)"}'},
            status_code=400)
    bpath = (base.get('path') or '').strip()
    if bpath and not Path(bpath).is_dir():
        return JSONResponse(
            {'error': f'папки базы нет на диске: {bpath}'}, status_code=400)
    # этот режим уже открыт? тогда не плодим второй сеанс без явного согласия
    if not body.get('force'):
        running = _running_pid(base, mode)
        if running:
            return JSONResponse(
                {'error': 'already_running', 'running': True, 'pid': running,
                 'mode': mode,
                 'message': ('база уже открыта в этом режиме (PID %d)' % running)},
                status_code=409)
    # проверяем соединение ДО запуска: так «Сервер 1С:Предприятия не обнаружен» и битый путь
    # видны сразу и понятным текстом (окна 1С текст не отдают)
    conn = ('Srvr="%s";Ref="%s";' % (base['server'], base.get('ref') or '')) if base.get('server') \
        else (base['ws'] if base.get('ws') else 'File="%s";' % (base.get('path') or ''))
    chk = netcheck.check(conn)
    if not chk['ok'] and not body.get('skip_check'):
        return JSONResponse({'error': 'conn_check', 'check': chk,
                             'message': chk.get('error') or 'нет связи с базой',
                             'hint': chk.get('hint') or ''}, status_code=409)
    pid = launcher.launch(cmd)
    launcher.log_launch(base['name'], mode, pid, cmd, base['guid'])
    # сторож: если 1С не дойдёт до окна базы (например «Несоответствие версий клиента и сервера»),
    # пришлём событие с подсказкой и снимком окна 1С
    try:
        fam = [p.get('key', '') for p in importer.build_state().get('platforms', [])]
        watch.start(pid, base['guid'], base['name'], exe, conn, fam)
    except Exception:                       # noqa: BLE001
        pass
    # стартер не должен остаться на переднем плане: как только у базы появится настоящее
    # окно (не сплэш «Загрузка конфигурационной информации…»), выносим его наверх
    import threading
    threading.Thread(target=procs.wait_and_focus,
                     args=(lambda s: _session_matches(s, base), mode),
                     kwargs={'timeout': 120, 'settle': 3}, daemon=True).start()
    # версия сервера может не совпасть с выбранной платформой (8.3 против 8.5) — тогда 1С
    # покажет «Различаются версии клиента и сервера». Пробуем другое семейство и запоминаем.
    if not body.get('exe') and not base.get('ws'):
        threading.Thread(target=_auto_family_retry, args=(pid, base, mode),
                         daemon=True).start()
    return {'ok': True, 'pid': pid, 'cmd': cmd, 'focus': 'queued'}


def _wait_event(pid: int, seconds: int = 45):
    """Ждать событие сторожа по процессу. None — 1С не пожаловалась (значит открылась/грузится)."""
    for _ in range(seconds):
        time.sleep(1)
        ev = next((e for e in watch.events() if e.get('pid') == pid), None)
        if ev:
            return ev
    return None


def _exe_of(pid: int) -> str:
    """Путь к exe по командной строке процесса (первый аргумент в кавычках)."""
    line = procs.cmdline(pid) or ''
    m = re.match(r'\s*"([^"]+)"', line) or re.match(r'\s*(\S+)', line)
    return m.group(1) if m else ''


def _switched_client(base: dict, pid: int) -> tuple[int, str]:
    """Клиент, в которого 1С перезапустилась сама (/AppAutoCheckVersion). (pid, exe) или (0, '')."""
    guid = base.get('guid') or ''
    try:
        run = (importer.sessions_light().get('running') or {}).get(guid) or {}
        for m, p in (run.get('pids') or {}).items():
            if p and p != pid:
                return p, _exe_of(p)
    except Exception:                       # noqa: BLE001
        pass
    # сеанса ещё нет (клиент грузится) — ищем процесс 1С, который держит эту же базу
    ref = (base.get('ref') or '').lower()
    name = (base.get('name') or '').lower()
    for p, line in procs._processes():
        low = line.lower()
        if '1cv8' not in low or p == pid:
            continue
        if (ref and 'ref="%s"' % ref in low) or (name and 'ibname"%s"' % name in low):
            return p, _exe_of(p)
    return 0, ''


def _pin_exe(guid: str, exe: str) -> None:
    """Запомнить платформу за базой (bases[guid].exe), чтобы дальше запускать сразу ею."""
    try:
        config.set_base(guid, {'exe': exe})
        importer.build_state()
    except Exception:                       # noqa: BLE001
        pass


def _other_family(base: dict, mode: str = 'enterprise') -> list[dict]:
    """Самые новые платформы ДРУГОГО семейства (8.3 ↔ 8.5) — кандидаты на повторный запуск."""
    cur = (base.get('family') or '8.3')
    alt = '8.5' if cur.startswith('8.3') else '8.3'
    designer = (mode or '').lower().startswith('config')
    out = []
    for p in importer.build_state().get('platforms', []):
        if p.get('family') != alt:
            continue
        exe = p.get('exe') or ('' if designer else p.get('exe_thin')) or ''
        if exe:
            out.append({'exe': exe, 'version': p.get('key') or '', 'family': alt})
    return out


def _auto_family_retry(pid: int, base: dict, mode: str) -> None:
    """1С не открылась → пробуем платформу другого семейства (8.3 ↔ 8.5) и запоминаем рабочую.

    Версия клиента должна совпадать с сервером (8.5.x против 8.3.x и наоборот), а узнать её
    заранее неоткуда: нечитаемый текст сообщения 1С и никакого API. Поэтому после неудачи
    сами пробуем 2–3 самые новые платформы другого семейства; та, что открыла базу,
    закрепляется за базой в config.json — дальше запуск идёт сразу ею, без «пристрелки».
    """
    guid = base.get('guid') or ''
    ev = _wait_event(pid)
    if not ev or ev.get('kind') not in ('dialog', 'exited'):
        return
    # 1) 1С могла сама перезапуститься нужной версией (/AppAutoCheckVersion)
    for _ in range(5):
        npid, exe2 = _switched_client(base, pid)
        if npid and exe2:
            _pin_exe(guid, exe2)
            ev['switched'] = {'pid': npid, 'exe': exe2}
            watch._drop(ev.get('id') or '')
            return
        time.sleep(1.5)
    # 2) спрашиваем версию сервера через COM (без окон) и берём ровно её платформу
    cands: list[dict] = []
    det = detect_platform(base, pin=False)
    alt_exe = det.get('exe') or ''
    if alt_exe and alt_exe.lower() != (base.get('exe') or '').lower():
        cands.append({'exe': alt_exe, 'version': det.get('version') or '',
                      'family': (det.get('version') or '')[:3]})
    # 3) сервер не ответил — перебираем другое семейство
    for c in _other_family(base, mode)[:3]:
        if not any(o['exe'].lower() == c['exe'].lower() for o in cands):
            cands.append(c)
    cands = cands[:3]
    if cands:
        ev['retrying'] = {'family': cands[0]['family'],
                          'versions': [c['version'] for c in cands]}
    for cand in cands:
        try:
            if any(p == pid for p, _ in procs._processes()):
                try:
                    subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                                   capture_output=True, timeout=20)
                except Exception:           # noqa: BLE001
                    pass
                time.sleep(1)
            cmd2 = launcher.build_cmd(cand['exe'], mode, base.get('connect') or '', [],
                                      base.get('storage') or {},
                                      base.get('user') or '', base.get('pwd') or '')
            pid2 = launcher.launch(cmd2)
            launcher.log_launch(base.get('name') or guid, mode, pid2, cmd2, guid)
            watch.start(pid2, guid, base.get('name') or '', cand['exe'],
                        base.get('connect') or '', [])
            if _wait_event(pid2, 45) is None:       # открылась — закрепляем
                _pin_exe(guid, cand['exe'])
                ev['switched'] = {'pid': pid2, 'exe': cand['exe'],
                                  'version': cand['version'], 'family': cand['family']}
                watch._drop(ev.get('id') or '')
                return
            try:
                subprocess.run(['taskkill', '/PID', str(pid2), '/F'],
                               capture_output=True, timeout=20)
            except Exception:           # noqa: BLE001
                pass
        except Exception as e:          # noqa: BLE001
            ev['retry_error'] = str(e)
            return


@app.post('/api/openweb')
async def openweb(req: Request):
    """Веб-база (ws=): открыть адрес в браузере по умолчанию (веб-клиент)."""
    body = await req.json()
    _, base = _base_or_404(body.get('guid', ''))
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    url = base.get('ws')
    if not url:
        return JSONResponse({'error': 'это не веб-база (нет ws-адреса)'}, status_code=400)
    import os
    os.startfile(url)      # браузер по умолчанию
    return {'ok': True, 'url': url}


def _session_matches(s: dict, base: dict) -> bool:
    """Сеанс 1С относится к этой базе (путь / сервер+ref / ws-URL / имя из /IBName)."""
    if base.get('path') and s.get('path') and procs._norm_path(s['path']) == procs._norm_path(base['path']):
        return True
    if base.get('server') and s.get('server') and s['server'].lower() == base['server'].lower() \
            and (s.get('ref') or '').lower() == (base.get('ref') or '').lower():
        return True
    if base.get('ws') and s.get('ws') \
            and s['ws'].rstrip('/').lower() == base['ws'].rstrip('/').lower():
        return True
    if s.get('name') and s['name'].strip().lower() == (base.get('name') or '').strip().lower():
        return True
    return False


def _running_pid(base: dict, mode: str) -> int | None:
    """PID живого сеанса этой базы в нужном режиме (или None)."""
    for s in procs.running_sessions():
        if s['mode'] == mode and _session_matches(s, base):
            return s['pid']
    return None


@app.get('/api/log')
def log(n: int = 50):
    return launcher.tail_log(n)


@app.post('/api/base/new')
async def base_new(req: Request):
    """Добавить базу в ibases.v8i (1С увидит её в своём лаунчере)."""
    body = await req.json()
    name = (body.get('name') or '').strip()
    folder = (body.get('folder') or '/').strip() or '/'
    path = (body.get('path') or '').strip()
    server = (body.get('server') or '').strip()
    ref = (body.get('ref') or '').strip()
    if not name:
        return JSONResponse({'error': 'укажите имя базы'}, status_code=400)
    if path:
        connect = 'File="%s"' % path.rstrip('\\/')
    elif server and ref:
        connect = 'Srvr="%s";Ref="%s"' % (server, ref)
    else:
        return JSONResponse(
            {'error': 'нужен путь к папке базы (файловая) или сервер + имя базы (серверная)'},
            status_code=400)
    try:
        guid = v8i_writer.add_base(name, connect, folder)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'guid': guid, 'base': importer.find_base(st, guid)}


@app.post('/api/folder/new')
async def folder_new(req: Request):
    """Добавить папку в дерево ibases.v8i."""
    body = await req.json()
    name = (body.get('name') or '').strip()
    parent = (body.get('parent') or '/').strip() or '/'
    if not name:
        return JSONResponse({'error': 'укажите имя папки'}, status_code=400)
    try:
        guid = v8i_writer.add_folder(name, parent)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'guid': guid, 'folders': len(st['folders'])}


@app.post('/api/base/{guid}/move')
async def base_move(guid: str, req: Request):
    """Перенести базу в другую папку дерева (пишется в ibases.v8i, Folder=).

    body: {"folder": "/Разработка"} — '/', чтобы убрать из папок («Без папки»).
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    folder = str(body.get('folder') or '/').strip() or '/'
    try:
        r = v8i_writer.move(guid, folder)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:                       # файл занят/нет прав
        return JSONResponse({'error': 'не удалось записать ibases.v8i: %s' % e}, status_code=500)
    importer._BASES_CACHE.clear()
    st = importer.build_state()
    return {'ok': True, 'moved': r, 'bases': len(st['bases'])}


@app.delete('/api/base/{guid}')
def base_delete(guid: str):
    """Удалить базу/пустую папку из ibases.v8i."""
    try:
        name = v8i_writer.delete(guid)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'deleted': name, 'bases': len(st['bases'])}


@app.delete('/api/folder/{guid}')
def folder_delete(guid: str):
    """Удалить папку (или базу) из ibases.v8i по guid раздела."""
    try:
        name = v8i_writer.delete(guid)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'deleted': name, 'folders': len(st['folders'])}


@app.post('/api/base/{guid}/detect')
async def base_detect(guid: str, req: Request):
    """Версия сервера базы через COM (без окна 1С); по умолчанию сразу закрепляет платформу."""
    _, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    body = {}
    try:
        body = await req.json()
    except Exception:                       # noqa: BLE001
        pass
    res = detect_platform(base, pin=bool(body.get('pin', True)))
    return {**res, 'base': importer.find_base(importer.build_state(), base['guid']) or base}


@app.post('/api/base/{guid}/users')
async def base_users(guid: str, req: Request):
    """Список пользователей ИБ через COM (как обновлятор): для выбора логина в настройках.

    Тело: {user, pwd} — чем подключаться (по умолчанию из настроек базы).
    Возвращает {ok, users:[{Имя, Удалён, БезПароля}], users_count, error, hint}.
    """
    _, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    body = {}
    try:
        body = await req.json()
    except Exception:                       # noqa: BLE001
        pass
    user = (body.get('user') or base.get('user') or '').strip()
    pwd = body.get('pwd')
    if pwd is None:
        pwd = base.get('pwd') or ''
    res = com.probe_async(base.get('connect') or '', user, pwd, base.get('exe') or '', 90, want_users=True)
    names = [str(r.get('Имя') or '').strip() for r in (res.get('users') or [])]
    return {'ok': bool(res.get('ok')), 'users': [n for n in names if n],
            'users_count': res.get('users_count'), 'platform': res.get('com_platform') or '',
            'users_table': res.get('users_table') or '', 'users_tried': res.get('users_tried') or [],
            'users_source': res.get('users_source') or '', 'users_ib_error': res.get('users_ib_error') or '',
            'note': ('' if (res.get('users') or []) else
                     'в базе нет зарегистрированных пользователей ИБ (это не ошибка подключения)'),
            'error': res.get('error') or '', 'hint': res.get('hint') or '',
            'users_error': res.get('users_error') or ''}


@app.post('/api/base/{guid}/cominfo')
async def base_cominfo(guid: str, req: Request):
    """Сведения из базы через COM-подключение: конфигурация, пользователи (как обновлятор)."""
    _, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    body = {}
    try:
        body = await req.json()
    except Exception:                       # noqa: BLE001
        pass
    user = (body.get('user') or base.get('user') or '').strip()
    pwd = body.get('pwd')
    if pwd is None:
        pwd = base.get('pwd') or ''
    res = com.probe_async(base.get('connect') or '', user, pwd, base.get('exe') or '', 90)
    if body.get('save') and (body.get('user') is not None or body.get('pwd') is not None):
        patch = {}
        if body.get('user') is not None:
            patch['user'] = user
        if body.get('pwd') is not None:
            patch['pwd'] = pwd
        try:
            config.set_base(base['guid'], patch)
            importer.build_state()
            res['saved'] = True
        except Exception as e:              # noqa: BLE001
            res['saved'] = False
            res['save_error'] = str(e)
    res['base'] = base.get('name')
    return res


@app.get('/api/precheck/{guid}')
def precheck(guid: str):
    """Проверка соединения с базой без запуска 1С (сервер/веб/папка)."""
    _, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    conn = ('Srvr="%s";Ref="%s";' % (base['server'], base.get('ref') or '')) if base.get('server') \
        else (base['ws'] if base.get('ws') else 'File="%s";' % (base.get('path') or ''))
    out = {'ok': True, 'conn': conn, 'check': netcheck.check(conn)}
    if base.get('ws'):
        out['web_version'] = netcheck.web_version(base['ws'])
        out['exe_version'] = next((p.get('key') for p in importer.build_state().get('platforms', [])
                                   if (base.get('exe') or '').lower()
                                   in ((p.get('exe') or '').lower(), (p.get('exe_thin') or '').lower())), '')
    return out


@app.get('/api/launch/events')
def launch_events():
    """События сторожа запуска: 1С не дошла до окна базы (ошибка подключения и т.п.)."""
    return {'events': watch.events()}


@app.post('/api/launch/events/clear')
def launch_events_clear():
    watch.clear()
    return {'ok': True}


@app.get('/api/launch/shot/{name}')
def launch_shot(name: str):
    """Снимок окна 1С, сохранённый сторожем (BMP)."""
    safe = Path(name).name
    p = ROOT / 'data' / 'errors' / safe
    if not p.is_file():
        return JSONResponse({'error': 'нет такого снимка'}, status_code=404)
    return FileResponse(p, media_type='image/bmp',
                        headers={'Cache-Control': 'no-store'})


@app.get('/api/cache')
def cache_info():
    """Размеры кэшей: клиентский кэш 1С у пользователя и служебные файлы стартера."""
    return cache.info(ROOT)


@app.post('/api/cache/clear')
async def cache_clear(req: Request):
    """Очистить кэш: what=one_c (все базы), one_c_base (с guid), app (стартер)."""
    body = await req.json()
    what = (body.get('what') or '').strip()
    guid = (body.get('guid') or '').strip() or None
    try:
        if what == 'one_c':
            res = cache.clear_one_c(None)
        elif what == 'one_c_base':
            if not guid:
                return JSONResponse({'error': 'не передан guid базы'}, status_code=400)
            res = cache.clear_one_c(guid)
        elif what == 'app':
            res = cache.clear_app(ROOT)
            importer._BASES_CACHE.clear()       # состояние перечитается из ibases.v8i
        else:
            return JSONResponse({'error': "what: one_c | one_c_base | app"}, status_code=400)
    except Exception as e:                      # noqa: BLE001
        return JSONResponse({'error': str(e)}, status_code=500)
    running = len(procs.running_sessions())
    res['sessions_running'] = running
    return res


@app.get('/api/base/{guid}/cache')
def base_cache(guid: str):
    """Размеры кэша базы: user (текущий пользователь Windows) и program (все пользователи ПК)."""
    st = importer.build_state()
    if not importer.find_base(st, guid):
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    try:
        return {'ok': True, 'guid': guid, **cache.base_info(guid)}
    except Exception as e:                       # noqa: BLE001
        return JSONResponse({'error': str(e)}, status_code=500)


@app.post('/api/base/{guid}/cache/clear')
async def base_cache_clear(guid: str, req: Request):
    """Очистить кэш базы: what=user | program | both.

    Кэш самой программы-стартера здесь не чистится — только клиентский кэш 1С этой базы.
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    what = str(body.get('what') or 'user').strip().lower()
    st = importer.build_state()
    base = importer.find_base(st, guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    try:
        res = cache.clear_base(guid, what)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:                       # noqa: BLE001
        return JSONResponse({'error': str(e)}, status_code=500)
    res['base'] = base.get('name')
    res['running'] = bool(base.get('running'))
    res['sessions_running'] = len(procs.running_sessions())
    return res


@app.post('/api/activate')
async def activate(req: Request):
    """Переключиться на уже открытый сеанс: вывести его окно наверх."""
    body = await req.json()
    try:
        pid = int(body.get('pid') or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return JSONResponse({'error': 'не передан pid сеанса'}, status_code=400)
    r = procs.focus_window(pid)
    if not r.get('ok'):
        return JSONResponse(r, status_code=400)
    return r


@app.post('/api/starter/focus')
def starter_focus():
    """Поднять окно стартера."""
    r = hotkey.focus_starter(PORT)
    return JSONResponse(r, status_code=200 if r.get('ok') else 400)


@app.post('/api/starter/hide')
def starter_hide():
    """Убрать окно стартера в трей (сервер продолжает работать)."""
    r = hotkey.hide_starter(PORT)
    return JSONResponse(r, status_code=200 if r.get('ok') else 400)


@app.post('/api/starter/toggle')
def starter_toggle():
    """Показать, если скрыто/не на переднем плане; иначе — убрать в трей."""
    return JSONResponse(hotkey.toggle_starter(PORT))


@app.get('/api/tray')
def tray_status():
    """Состояние трея: готова ли иконка, есть ли сообщение об ошибке."""
    return {**tray.status(), 'hotkey': hotkey.status()}


@app.get('/api/hotkey')
def hotkey_status():
    return hotkey.status()


@app.post('/api/platform/probe')
async def platform_probe(req: Request):
    """Есть ли дистрибутив версии на шаре и не установлена ли она уже."""
    body = await req.json()
    v = (body.get('version') or '').strip()
    if not v:
        return JSONResponse({'error': 'не передана версия платформы'}, status_code=400)
    return platforms_share.probe(v)


@app.post('/api/platform/install')
async def platform_install(req: Request):
    """Поставить отсутствующую платформу с шары (UAC подтверждает пользователь)."""
    body = await req.json()
    v = (body.get('version') or '').strip()
    kind = (body.get('kind') or 'setup64full').strip()
    if not v:
        return JSONResponse({'error': 'не передана версия платформы'}, status_code=400)
    try:
        return platforms_share.install(v, kind)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)


@app.post('/api/platform/reveal')
async def platform_reveal(req: Request):
    """Открыть папку дистрибутива на шаре в проводнике."""
    body = await req.json()
    v = (body.get('version') or '').strip()
    return platforms_share.reveal(v)


@app.post('/api/shutdown')
def shutdown():
    """Штатная остановка сервера (освобождает порт без сирот-воркеров).

    Нужна потому, что kill обёртки background-сессии оставляет python-ребёнка
    с занятым портом: следующий запуск падает с WinError 10048/10013.
    """
    import os
    import threading

    def _bye():
        os._exit(0)

    threading.Timer(0.3, _bye).start()
    return {'ok': True, 'bye': 'shutting down'}


@app.post('/api/quit')
def quit_app():
    """Полный выход: закрыть окно приложения, убрать иконку из трея, остановить сервер."""
    import os
    import threading

    def _bye():
        try:
            hotkey.close_starter(PORT)          # WM_CLOSE окну Edge --app
            tray.remove_icon()                  # значок не должен остаться «мёртвым»
        finally:
            os._exit(0)

    threading.Timer(0.4, _bye).start()
    return {'ok': True, 'bye': 'closing window and server'}


@app.post('/api/restart')
def restart_app():
    """Перезапуск: закрыть окно, поднять сервер и окно заново (boot.py, )."""
    import os
    import subprocess
    import sys
    import threading
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent     # корень репозитория, не app/
    boot = root / 'boot.py'
    pythonw = Path(sys.executable).with_name('pythonw.exe')

    def _bye():
        try:
            hotkey.close_starter(PORT)                  # окно закроем, новое откроет boot.py
            tray.remove_icon()
            # boot.py --wait-down дождётся освобождения порта, поднимет сервер и окно
            subprocess.Popen([str(pythonw if pythonw.is_file() else sys.executable),
                              str(boot), '--wait-down'], cwd=str(root),
                             close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
        finally:
            os._exit(0)

    threading.Timer(0.4, _bye).start()
    return {'ok': True, 'bye': 'restarting'}
