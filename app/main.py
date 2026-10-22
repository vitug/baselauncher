# -*- coding: utf-8 -*-
"""REST-слой BaseLauncher (FastAPI). Только 127.0.0.1."""
import json
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core import config, hotkey, importer, launcher, platforms_share, procs, tray, v8i_writer

app = FastAPI(title='BaseLauncher', version='0.16.0')
PORT = int(__import__('os').environ.get('BL_PORT') or 8797)

INDEX = Path(__file__).parent / 'static' / 'index.html'
ICON = Path(__file__).parent.parent / 'assets' / 'baselauncher.ico'


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
    return {**importer.sessions_light(), 'version': app.version}


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
    pid = launcher.launch(cmd)
    launcher.log_launch(base['name'], mode, pid, cmd, base['guid'])
    # стартер не должен остаться на переднем плане: как только у базы появится настоящее
    # окно (не сплэш «Загрузка конфигурационной информации…»), выносим его наверх
    import threading
    threading.Thread(target=procs.wait_and_focus,
                     args=(lambda s: _session_matches(s, base), mode),
                     kwargs={'timeout': 120, 'settle': 3}, daemon=True).start()
    return {'ok': True, 'pid': pid, 'cmd': cmd, 'focus': 'queued'}


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


@app.delete('/api/base/{guid}')
def base_delete(guid: str):
    """Удалить базу/пустую папку из ibases.v8i."""
    try:
        name = v8i_writer.delete(guid)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    st = importer.build_state()
    return {'ok': True, 'deleted': name, 'bases': len(st['bases'])}


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
