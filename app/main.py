# -*- coding: utf-8 -*-
"""REST-слой BaseLauncher (FastAPI). Только 127.0.0.1."""
import datetime
import json
import re
import subprocess
import time
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core import (audit, cache, com, config, dbtools, hotkey, importer, launcher, netcheck,
                  platforms_share, procs, tray, v8i_writer, watch)

app = FastAPI(title='BaseLauncher', version='0.34.1')
PORT = int(__import__('os').environ.get('BL_PORT') or 8797)


# ── аудит действий: каждое не-GET обращение к API пишется в data/audit.log ──
# (запуски, настройки, удаления, чистки кэша, переводы платформ; пароли → '***')
@app.middleware('http')
async def audit_middleware(request: Request, call_next):
    if request.method == 'GET':
        return await call_next(request)                 # чтение — не действие
    detail: dict = {}
    try:
        body = await request.body()
        if body and len(body) < 64 * 1024:
            try:
                detail = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = {'body': body[:200].decode('utf-8', 'replace')}
    except Exception:                              # noqa: BLE001
        pass
    if request.url.query:
        detail['?query'] = request.url.query
    try:
        response = await call_next(request)
        audit.log('%s %s' % (request.method, request.url.path),
                  dict(detail, _status=response.status_code))
        return response
    except Exception:
        audit.log('%s %s' % (request.method, request.url.path), detail)
        raise

INDEX = Path(__file__).parent / 'static' / 'index.html'
ICON = Path(__file__).parent.parent / 'assets' / 'baselauncher.ico'
ROOT = Path(__file__).parent.parent


@app.on_event('startup')
def _startup():
    """Глобальная клавиша Ctrl+Shift+S (показать / убрать в трей) и иконка в трее.

    Dev-режим (BL_DEV=1, отдельный инстанс для разработки): клавишу и трей не
    занимает (они уже принадлежат рабочему инстансу), а своё окно открывает на
    отдельном виртуальном столе Windows — см. core/vdesk.py.
    """
    from core import paths
    if not paths.DEV:
        hotkey.start(PORT)      # клавиша Ctrl+Shift+S + сторож геометрии окна
    else:
        paths_log('dev-режим: глобальная клавиша и трей отключены '
                  '(заняты рабочим инстансом на :8797)')

    def _ensure_window():
        import time
        time.sleep(0.8)
        try:
            if not hotkey.find_starter_window(PORT):
                hotkey.focus_starter(PORT)      # окна нет — открыть (и одно!)
            if paths.DEV:
                # окно только что открылось (или уже было) — увезти на dev-стол.
                # Матчинг по порту: чужое окно не ловим — в заголовке метка
                # «:8797»/«:8798» (find_starter_window отсекает чужие порты),
                # а «BaseLauncher DEV» в заголовке отметим сами (UI).
                from core import vdesk
                for _ in range(6):              # до ~9 с: окно может открываться долго
                    wins = hotkey.find_starter_window(PORT)
                    if wins:
                        hwnd = wins[0][0]
                        try:
                            res = vdesk.move_window_to_dev_desktop(hwnd, switch=False)
                            paths_log('dev-окно hwnd=%s перенесено на '
                                      'виртуальный стол: %s' % (hwnd, res.get('desktop')))
                        except OSError as e:
                            paths_log('vdesk перенос не удался: %r' % (e,))
                        break
                    time.sleep(1.5)
        except Exception as e:                  # noqa: BLE001
            try:
                paths_log('vdesk: %r' % (e,))
            except Exception:                   # noqa: BLE001
                pass

    threading.Thread(target=_ensure_window, daemon=True, name='bl-window').start()
    if not paths.DEV:
        tray.start(PORT)


def paths_log(msg):
    """Лог в data-каталог текущего (dev или боевого) инстанса."""
    try:
        from core.paths import DATA_DIR
        with (DATA_DIR / 'server.log').open('a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')
    except OSError:
        pass


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
    """Полное состояние: базы (со своим конфигом), папки, платформы, семейства.

    Пароли наружу не отдаются (_no_pwd): UI их не показывает, плейнтекст нужен
    только внутри launch/COM. Иначе любой локальный GET вынимал бы все пароли."""
    st = importer.build_state()
    st['bases'] = [_no_pwd(b) for b in st['bases']]
    from core import paths as _p
    return {**st, 'dev': _p.DEV, 'version': app.version}


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
    if not (base.get('server') or base.get('ws')):
        # файловая база: «версия сервера» — это версия самого COM-коннектора, то есть того
        # клиента, которым мы подключились. Закреплять её за базой нельзя: получится петля
        # «старый клиент → COM сообщает его же версию → снова закрепляем старый клиент».
        out['note'] = ('файловая база: версию по COM не определить — платформа выбирается '
                       'по самой базе (новая открывает старые)')
        out['pin_skipped'] = True
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


def _no_pwd(b: dict) -> dict:
    """Копия базы без паролей — для отдачи наружу (UI/MCP не должны видеть плейнтекст).

    Запуску и COM пароли нужны внутри сервера; наружу достаточно pwd_note/флагов.
    UI-редактор настроек показывает «сохранён» и отправляет pwd только при смене.
    """
    if not b or not isinstance(b, dict):
        return b
    d = dict(b)
    for k in ('pwd', 'pwd_cfg'):
        if d.get(k):
            d[k] = ''
            d['%s_set' % k] = True
    storage = d.get('storage')
    if isinstance(storage, dict) and storage.get('password'):
        d['storage'] = {**storage, 'password': '', 'pwd_set': True}
    return d


@app.get('/api/base/{guid}')
def base_detail(guid: str):
    st, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    return {'base': _no_pwd(base), 'platforms': st['platforms']}


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
    return {'ok': True, 'base': _no_pwd(importer.find_base(st, guid))}


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
    # Конфигуратор есть только у толстого клиента. Если за базой закреплён 1cv8c.exe
    # (тонкий — например, 1С сама на него переключилась), команда DESIGNER молча
    # откроет Предприятие: тонкий клиент ключ DESIGNER игнорирует.
    if mode == 'designer' and exe and exe.lower().endswith('1cv8c.exe') and not body.get('exe'):
        thick = _thick_client(exe)
        if thick:
            exe = thick
        else:
            return JSONResponse({'error': 'толстого клиента (1cv8.exe) этого семейства не установлено — '
                                          'конфигуратора нет; поставьте полную платформу'},
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


def _thick_client(thin_exe: str) -> str:
    """Толстый клиент (1cv8.exe) для запуска Конфигуратора вместо тонкого 1cv8c.exe.

    Порядок: рядом с тем же 1cv8c.exe → толстый той же версии → ближайший по версии
    в том же семействе (8.3/8.5). Тонкий клиент ключ DESIGNER игнорирует и открывает
    Предприятие, поэтому конфигуратор без толстого клиента невозможен.
    """
    if not thin_exe:
        return ''
    sib = Path(thin_exe).with_name('1cv8.exe')
    if sib.is_file():
        return str(sib)
    plats = importer.build_state().get('platforms', [])
    fam = (thin_exe.split('\\bin\\')[0].split('\\')[-1] or '')[:3]
    target = (Path(thin_exe).parent.parent.name or '')
    if not fam:
        fam = target[:3]
    cands = []
    for p in plats:
        exe = p.get('exe') or ''
        if not exe or not Path(exe).is_file() or (p.get('family') or '') != fam:
            continue
        cands.append((p.get('key') or '', exe))
    if not cands:
        return ''
    exact = [c for c in cands if c[0] == target]
    if exact:
        return exact[0][1]
    def dist(key: str) -> int:
        try:
            a = [int(x) for x in key.split('.')]
            b = [int(x) for x in target.split('.')]
        except ValueError:
            return 10 ** 6
        a += [0] * (4 - len(a)); b += [0] * (4 - len(b))
        return sum(abs(x - y) * 10 ** (3 * (3 - i)) for i, (x, y) in enumerate(zip(a, b)))
    return sorted(cands, key=lambda c: dist(c[0]))[0][1]


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


def _dbg(msg: str) -> None:
    """Журнал автоповтора платформы (data/retry.log): почему не переключилось."""
    try:
        from core.paths import DATA_DIR as _DD
        with (_DD / 'retry.log').open(
                'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (datetime.datetime.now().strftime('%H:%M:%S'), msg))
    except Exception:                       # noqa: BLE001
        pass


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


def _ver_key(s: str):
    """'8.3.27.2325' -> (8,3,27,2325) для сравнения версий."""
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r'[.\-]', s or ''))


def _newest_same_family(base: dict, mode: str = 'enterprise') -> dict | None:
    """Самая новая установленная платформа ТОГО ЖЕ семейства.

    Новая платформа открывает базы, созданные старыми, а обратное неверно: файловая база,
    сделанная в 8.3.27, на 8.3.25 просто закрывается без окна («нужна версия не меньше…»).
    Поэтому при непонятной ошибке логичнее поднимать клиента, а не искать «ровно ту» версию.
    """
    fam = (base.get('family') or '8.3')
    designer = (mode or '').lower().startswith('config')
    best = None
    for p in importer.build_state().get('platforms', []):
        if p.get('family') != fam:
            continue
        thick = p.get('exe') or ''
        thin = p.get('exe_thin') or ''
        if designer:
            exe = thick if Path(thick).is_file() else ''
        else:
            exe = thick if Path(thick).is_file() else (thin if Path(thin).is_file() else '')
        if not exe:
            continue
        key = p.get('key') or ''
        rank = (1 if Path(thick).is_file() else 0, _ver_key(key))
        if best is None or rank > best['rank']:
            best = {'exe': exe, 'version': key, 'family': fam, 'rank': rank}
    if best:
        best.pop('rank', None)
    return best


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
    _dbg('старт автоповтора для pid %s (%s)' % (pid, base.get('name')))
    ev = _wait_event(pid)
    if not ev or ev.get('kind') not in ('dialog', 'exited'):
        _dbg('  события от сторожа не было — выходим (pid %s)' % pid)
        return
    _dbg('  событие: %s' % ev.get('kind'))
    # 1) 1С могла сама перезапуститься нужной версией (/AppAutoCheckVersion)
    for _ in range(5):
        npid, exe2 = _switched_client(base, pid)
        if npid and exe2:
            _dbg('  1С сама перезапустилась: pid %s, %s — закрепляю' % (npid, exe2))
            _pin_exe(guid, exe2)
            ev['switched'] = {'pid': npid, 'exe': exe2}
            watch._drop(ev.get('id') or '')
            return
        time.sleep(1.5)
    # 2) версию сервера через COM спрашиваем только у серверных баз: у файловой COM вернёт
    #    версию собственного коннектора (то есть того же старого клиента) и может висеть до 90 с
    cands: list[dict] = []
    if base.get('server') or base.get('ws'):
        det = detect_platform(base, pin=False)
        alt_exe = det.get('exe') or ''
        if alt_exe and alt_exe.lower() != (base.get('exe') or '').lower():
            cands.append({'exe': alt_exe, 'version': det.get('version') or '',
                          'family': (det.get('version') or '')[:3]})
    else:
        _dbg('  файловая база: COM-детект версии пропускаю')
    # файловая база: пробуем самую новую платформу того же семейства (она откроет и старую базу)
    same = _newest_same_family(base, mode)
    if same and same['exe'].lower() != (base.get('exe') or '').lower() \
            and not any(o['exe'].lower() == same['exe'].lower() for o in cands):
        cands.insert(0, same)
    # 3) сервер не ответил — перебираем другое семейство
    for c in _other_family(base, mode)[:3]:
        if not any(o['exe'].lower() == c['exe'].lower() for o in cands):
            cands.append(c)
    cands = cands[:3]
    _dbg('  кандидаты: %s' % ', '.join('%s (%s)' % (c.get('version'), c.get('exe')) for c in cands))
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
            _dbg('  пробую %s → pid %s' % (cand.get('version'), pid2))
            if _wait_event(pid2, 45) is None:       # открылась — закрепляем
                _dbg('  открылась на %s — закрепляю' % cand.get('version'))
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


@app.get('/api/audit')
def audit_tail(n: int = 50):
    """Последние записи аудита действий (data/audit.log, новые в конце)."""
    return audit.tail(n)


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
    return {'ok': True, 'guid': guid, 'base': _no_pwd(importer.find_base(st, guid))}


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
    return {**res, 'base': _no_pwd(importer.find_base(importer.build_state(), base['guid']) or base)}


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


@app.post('/api/base/{guid}/restore-dt')
async def base_restore_dt(guid: str, req: Request):
    """Загрузить файловую базу из выгрузки .dt (полная перезапись, старый каталог — в .restore-bak-*)."""
    st, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    try:
        body = await req.json()
    except Exception:                                # noqa: BLE001
        body = {}
    dt = str(body.get('path') or '').strip()
    if not dt:
        return JSONResponse({'error': 'не указан файл .dt'}, status_code=400)
    res = dbtools.start_restore(guid, base, dt, backup=bool(body.get('backup', True)))
    return JSONResponse(res, status_code=200 if res.get('ok') else 409)


@app.get('/api/base/{guid}/restore-state')
def base_restore_state(guid: str):
    return {'ok': True, 'state': dbtools.state_of(guid)}


@app.post('/api/base/{guid}/check-db')
def base_check_db(guid: str):
    """Проверка базы данных файловой версии (chdbfl.exe) — открывает окно утилиты 1С."""
    st, base = _base_or_404(guid)
    if not base:
        return JSONResponse({'error': 'база не найдена'}, status_code=404)
    res = dbtools.start_check(guid, base)
    return JSONResponse(res, status_code=200 if res.get('ok') else 409)


@app.get('/api/fs/list')
def fs_list(dir: str = ''):
    """Каталоги и файлы .dt для выбора выгрузки в окне «Загрузить базу из .dt…». """
    return dbtools.list_dir(dir)


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
    """Очистить кэш базы: what=config (кэш программы, LOCALAPPDATA) | user (настройки,
    APPDATA) | program (PROGRAMDATA) | both.

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


@app.get('/api/cache/dead')
def cache_dead():
    """Мёртвые кэши 1С: GUID-каталоги, не привязанные ни к одной базе из ibases.v8i/config."""
    known = set()
    from core import parsers
    try:
        for keys in parsers.parse_v8i():
            g = (keys.get('ID') or '').strip().lower()
            if g:
                known.add(g)
    except Exception:                            # noqa: BLE001
        pass
    try:
        known.update((str(g) or '').strip().lower() for g in (config.load().get('bases') or {}).keys())
    except Exception:                            # noqa: BLE001
        pass
    return cache.dead_caches(known)


@app.post('/api/cache/dead/clear')
async def cache_dead_clear(req: Request):
    """Удалить мёртвые кэши (все или выбранные guids)."""
    body = await req.json()
    guids = body.get('guids') or None
    known = set()
    from core import parsers
    try:
        for keys in parsers.parse_v8i():
            g = (keys.get('ID') or '').strip().lower()
            if g:
                known.add(g)
    except Exception:                            # noqa: BLE001
        pass
    try:
        known.update((str(g) or '').strip().lower() for g in (config.load().get('bases') or {}).keys())
    except Exception:                            # noqa: BLE001
        pass
    res = cache.clear_dead(known, guids)
    res['sessions_running'] = len(procs.running_sessions())
    return res


@app.get('/api/cache/bases')
def cache_bases():
    """Кэш по каждой базе (уровень config и user): для выбора, что чистить.

    Тяжёлый обход размеров — в threadpool (def, не async), с бюджетом времени на каталог.
    """
    st = importer.build_state()
    out = []
    for b in st.get('bases') or []:
        guid = b.get('guid') or ''
        if not guid:
            continue
        try:
            info = cache.base_info(guid, budget=0.45)
        except Exception:                        # noqa: BLE001
            continue
        cfg = info.get('config') or {}
        usr = info.get('user') or {}
        if not cfg.get('dirs') and not usr.get('dirs'):
            continue
        out.append({'guid': guid, 'name': b.get('name') or guid,
                    'family': b.get('family') or '',
                    'running': bool(b.get('running')),
                    'config_bytes': cfg.get('bytes') or 0, 'config_human': cfg.get('human') or '0 Б',
                    'config_partial': bool(cfg.get('partial')),
                    'user_bytes': usr.get('bytes') or 0, 'user_human': usr.get('human') or '0 Б'})
    out.sort(key=lambda x: -x['config_bytes'])
    return {'ok': True, 'count': len(out), 'bases': out}


@app.post('/api/cache/bases/clear')
async def cache_bases_clear(req: Request):
    """Очистить кэш выбранных баз: guids + what (config | user | program | both)."""
    body = await req.json()
    guids = [str(g).strip().lower() for g in (body.get('guids') or []) if str(g).strip()]
    what = str(body.get('what') or 'config').strip().lower()
    if not guids:
        return JSONResponse({'error': 'не переданы guids'}, status_code=400)
    st = importer.build_state()
    by_guid = {b['guid']: b for b in (st.get('bases') or []) if b.get('guid')}
    freed = 0
    removed = 0
    cleared = []
    errors = []
    skipped = []
    for g in guids:
        b = by_guid.get(g)
        if not b:
            skipped.append(g)
            continue
        try:
            r = cache.clear_base(g, what)
        except ValueError as e:
            errors.append('%s: %s' % (b.get('name') or g, e))
            continue
        freed += r.get('freed') or 0
        removed += r.get('dirs_removed') or 0
        cleared.append(b.get('name') or g)
        errors.extend('%s: %s' % ((b.get('name') or g), e) for e in (r.get('errors') or [])[:2])
    return {'ok': True, 'what': what, 'cleared': cleared, 'count': len(cleared),
            'dirs_removed': removed, 'freed': freed, 'freed_human': cache.human(freed),
            'skipped': skipped[:5], 'errors': errors[:10],
            'sessions_running': len(procs.running_sessions())}


# ---- платформы: использование, массовый перевод, удаление ----------------------

def _v8i_versions() -> dict[str, str]:
    """guid базы → Version= из ibases.v8i."""
    out = {}
    from core import parsers
    try:
        for keys in parsers.parse_v8i():
            if not (keys.get('Connect') or '').strip():
                continue
            g = (keys.get('ID') or '').strip().lower()
            if g:
                out[g] = (keys.get('Version') or '').strip()
    except Exception:                            # noqa: BLE001
        pass
    return out


def _base_versions(b: dict, ver_by_guid: dict[str, str]) -> set[str]:
    """Все версии платформы, относящиеся к базе: закреплённый exe, подобранный exe, Version= из v8i."""
    vers = set()
    for exe in (b.get('exe') or '', ((b.get('our') or {}).get('exe') or '')):
        for part in Path(exe.replace('\\', '/')).parts:
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', part):
                vers.add(part)
    v8i_ver = ver_by_guid.get(b['guid']) or ''
    if v8i_ver:
        vers.add(v8i_ver)
    return vers


def _bases_on(st: dict, src: str) -> list[dict]:
    """Базы платформы src: семейство ('8.3') или точная версия ('8.3.27.2325')."""
    ver_by_guid = _v8i_versions()
    hits = []
    for b in st.get('bases') or []:
        if src == (b.get('family') or '') or src in _base_versions(b, ver_by_guid):
            hits.append(b)
    return hits


def _platform_extras(p: dict) -> dict:
    """Размер каталога версии и компоненты платформы (по файлам в bin)."""
    out = {'size_human': '—', 'size_partial': False, 'components': {}}
    exe = p.get('exe') or p.get('exe_thin') or ''
    if not exe:
        return out
    bin_dir = Path(exe).parent
    ver_dir = bin_dir.parent
    from core import cache as _cache
    s = _cache.size_of(ver_dir, budget=0.8)
    out['size_human'] = s['human']
    out['size_partial'] = s['partial']
    checks = {
        'конфигуратор (1cv8.exe)': '1cv8.exe',
        'тонкий клиент (1cv8c.exe)': '1cv8c.exe',
        'сервер (ragent.exe)': 'ragent.exe',
        'COM (comcntr.dll)': 'comcntr.dll',
    }
    comps = []
    for label, fname in checks.items():
        try:
            if (bin_dir / fname).is_file():
                comps.append(label)
        except OSError:
            continue
    out['components'] = comps
    return out


@app.get('/api/platforms/usage')
def platforms_usage():
    """По каждой установленной платформе — сколько баз на ней (закреплено / подобрано / Version=),
    размер каталога версии и компоненты (конфигуратор, тонкий клиент, сервер, COM)."""
    st = importer.build_state()
    ver_by_guid = _v8i_versions()
    out = []
    for p in st.get('platforms') or []:
        exes = {e.lower() for e in (p.get('exe'), p.get('exe_thin')) if e}
        pinned = []
        resolved = []
        by_version = []
        for b in st.get('bases') or []:
            pin = ((b.get('our') or {}).get('exe') or '').lower()
            res = (b.get('exe') or '').lower()
            if pin in exes:
                pinned.append(b['name'])
            if res in exes:
                resolved.append(b['name'])
            if p.get('key') and ver_by_guid.get(b['guid']) == p['key']:
                by_version.append(b['name'])
        out.append({'key': p.get('key'), 'family': p.get('family'), 'bitness': p.get('bitness'),
                    'source': p.get('source'), 'exe': p.get('exe'), 'exe_thin': p.get('exe_thin'),
                    'pinned': pinned, 'resolved': resolved, 'by_version': by_version,
                    'used': bool(pinned or resolved or by_version),
                    **_platform_extras(p)})
    out.sort(key=lambda x: ((x.get('family') or ''), x.get('key') or ''), reverse=True)
    return {'platforms': out}


@app.post('/api/platforms/migrate')
async def platforms_migrate(req: Request):
    """Массовый перевод баз на другую платформу: from (family|version) → to_version.

    Для каждой подходящей базы: закрепляем exe в config.json и пишем Version= в ibases.v8i.
    dry_run=true — только показать, что попадёт в перевод.
    """
    body = await req.json()
    src = (body.get('from') or '').strip()
    to_version = (body.get('to_version') or '').strip()
    dry = bool(body.get('dry_run'))
    if not src or not to_version:
        return JSONResponse({'error': 'нужны from (семейство или версия) и to_version'}, status_code=400)
    st = importer.build_state()
    target = next((p for p in st.get('platforms') or [] if p.get('key') == to_version), None)
    if not target:
        return JSONResponse({'error': 'платформа %s не установлена' % to_version}, status_code=400)
    to_exe = target.get('exe') or target.get('exe_thin') or ''
    hits = _bases_on(st, src)
    if dry:
        return {'ok': True, 'dry_run': True, 'count': len(hits),
                'names': [b['name'] for b in hits][:100]}
    done = []
    errors = []
    for b in hits:
        try:
            config.set_base(b['guid'], {'exe': to_exe, 'family': target.get('family') or '', 'learnt': ''})
            v8i_writer.set_version(b['guid'], to_version)
            done.append(b['name'])
        except Exception as e:                   # noqa: BLE001
            errors.append('%s: %s' % (b['name'], e))
    importer._BASES_CACHE.clear()
    return {'ok': not errors, 'count': len(done), 'names': done[:100],
            'to_version': to_version, 'to_exe': to_exe, 'errors': errors[:10]}


@app.get('/api/platforms/bases')
def platforms_bases(from_src: str = ''):
    """Базы одной платформы (from=семейство или версия) — для окна перевода с чекбоксами."""
    src = (from_src or '').strip()
    if not src:
        return {'ok': True, 'bases': []}
    st = importer.build_state()
    ver_by_guid = _v8i_versions()
    out = []
    for b in _bases_on(st, src):
        storage = b.get('storage') or {}
        out.append({'guid': b['guid'], 'name': b.get('name') or b['guid'],
                    'family': b.get('family') or '',
                    'ver': ver_by_guid.get(b['guid']) or '',
                    'has_storage': bool(storage.get('dir')),
                    'storage_dir': storage.get('dir') or '',
                    'storage_ver': storage.get('ver') or '',
                    'running': bool(b.get('running'))})
    out.sort(key=lambda x: x['name'])
    return {'ok': True, 'count': len(out), 'bases': out}


@app.post('/api/platforms/migrate_bases')
async def platforms_migrate_bases(req: Request):
    """Перевести выбранные базы на платформу to_version (закрепление exe + Version= в ibases.v8i)."""
    body = await req.json()
    guids = [str(g).strip().lower() for g in (body.get('guids') or []) if str(g).strip()]
    to_version = (body.get('to_version') or '').strip()
    if not guids:
        return JSONResponse({'error': 'не переданы guids'}, status_code=400)
    st = importer.build_state()
    target = next((p for p in st.get('platforms') or [] if p.get('key') == to_version), None)
    if not target:
        return JSONResponse({'error': 'платформа %s не установлена' % to_version}, status_code=400)
    to_exe = target.get('exe') or target.get('exe_thin') or ''
    by_guid = {b['guid']: b for b in (st.get('bases') or []) if b.get('guid')}
    done = []
    errors = []
    for g in guids:
        b = by_guid.get(g)
        if not b:
            errors.append('%s: база не найдена' % g)
            continue
        try:
            config.set_base(g, {'exe': to_exe, 'family': target.get('family') or '', 'learnt': ''})
            v8i_writer.set_version(g, to_version)
            done.append(b['name'])
        except Exception as e:                   # noqa: BLE001
            errors.append('%s: %s' % (b.get('name') or g, e))
    importer._BASES_CACHE.clear()
    return {'ok': not errors, 'count': len(done), 'names': done[:100],
            'to_version': to_version, 'errors': errors[:10]}


@app.post('/api/platforms/delete')
async def platforms_delete(req: Request):
    """Удалить установленную платформу с диска — только если на ней нет баз и живых сеансов."""
    body = await req.json()
    version = (body.get('version') or '').strip()
    if not version:
        return JSONResponse({'error': 'не передана версия платформы'}, status_code=400)
    st = importer.build_state()
    p = next((x for x in st.get('platforms') or [] if x.get('key') == version), None)
    if not p:
        return JSONResponse({'error': 'платформа %s не найдена' % version}, status_code=404)
    usage = platforms_usage()
    u = next((x for x in usage['platforms'] if x['key'] == version), None)
    if u and (u['pinned'] or u['resolved'] or u['by_version']):
        return JSONResponse({'error': 'на платформе %s есть базы' % version,
                             'pinned': u['pinned'][:50], 'resolved': u['resolved'][:50],
                             'by_version': u['by_version'][:50]}, status_code=409)
    exes = [e for e in (p.get('exe'), p.get('exe_thin')) if e]
    live = []
    for s in procs.running_sessions():
        cmd = procs.cmdline(s['pid']) or ''
        if any(e.lower() in cmd.lower() for e in exes):
            live.append(s['pid'])
    if live:
        return JSONResponse({'error': 'есть живые сеансы этой платформы (PID %s)' % ', '.join(map(str, live))},
                            status_code=409)
    # каталог версии: <source>\<key>
    import shutil as _sh
    from core import scanner
    src_root = p.get('source') or ''
    vdir = Path(src_root) / version
    if not vdir.is_dir():
        return JSONResponse({'error': 'каталог %s не найден' % vdir}, status_code=404)
    try:
        _sh.rmtree(vdir)
        ok = True
    except OSError:
        ok = False
    if not ok:
        # Program Files и прочие защищённые — через UAC (пользователь подтверждает)
        marker = Path(__import__('tempfile').gettempdir()) / 'bl_platdel.txt'
        ps = ('Start-Process cmd -ArgumentList \'/c rmdir /s /q "%s" & '
              'if exist "%s" (echo FAIL> "%s") else (echo OK> "%s")\' -Verb RunAs -Wait') % (
            vdir, vdir, marker, marker)
        try:
            subprocess.run(['powershell', '-NoProfile', '-Command', ps], timeout=120, check=False)
            ok = marker.is_file() and marker.read_text().strip() == 'OK'
        except Exception as e:                   # noqa: BLE001
            return JSONResponse({'error': 'UAC-удаление не удалось: %s' % e}, status_code=500)
    if not ok:
        return JSONResponse({'error': 'не удалось удалить %s' % vdir}, status_code=500)
    importer._BASES_CACHE.clear()
    return {'ok': True, 'removed': str(vdir)}


@app.post('/api/platform/open')
async def platform_open(req: Request):
    """Открыть в проводнике каталог установленной платформы."""
    body = await req.json()
    version = (body.get('version') or '').strip()
    st = importer.build_state()
    p = next((x for x in st.get('platforms') or [] if x.get('key') == version), None)
    if not p:
        return JSONResponse({'error': 'платформа не найдена'}, status_code=404)
    d = Path(p.get('source') or '') / version
    if not d.is_dir():
        return JSONResponse({'error': 'каталог не найден'}, status_code=404)
    subprocess.Popen(['explorer', str(d)])
    return {'ok': True, 'dir': str(d)}


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
