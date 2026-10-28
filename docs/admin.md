# BaseLauncher — руководство администратора

Стартер баз 1С: Python (ядро `core/` — только stdlib), FastAPI-сервер + окно Edge `--app`. Список баз читается/пишется напрямую из `ibases.v8i` (общий с 1С формат), свои настройки — `data/config.json`. Порты: **прод 8797**, dev 8798. Слушает только 127.0.0.1.

## Установка и запуск

- Репозиторий: `D:\git_repos\baselauncher` (локальный git, remote нет). Дистрибутив: `python tools/make_release.py` → `dist/Baselauncher-<ver>/` + zip + `SHA256SUMS.txt`; в пакет не попадают `data/`, журналы, `*.bak-*`. Установка — `install.cmd` (venv + зависимости), запуск — `BaseLauncher.cmd`.
- Прод: `pythonw boot.py` (автозапуск/ярлык): поднимает `run_server.py`, ждёт `/api/state`, открывает окно. `--wait-down` — режим перезапуска.
- Штатная остановка — только `POST /api/shutdown`; полный выход — `POST /api/quit`; перезапуск — `POST /api/restart`.
- Статика отдаётся с диска (`Cache-Control: no-store`) — правки `index.html` видны после F5; **правки `core/` и `app/main.py` требуют рестарта** (UI сам предложит перезагрузку при смене версии).
- Dev-инстанс: `python dev.py` — порт 8798, песочница `data_dev/`, `BL_RELOAD=1`. Все доработки проверяются там живьём.
- Переменные окружения: `BL_PORT`, `BL_DATA`, `BL_IBASES`, `BL_DEV`, `BL_RELOAD`.

## Архитектура

```

run_server.py      uvicorn + FastAPI (app/main.py)
app/main.py        REST API, всё поведение
app/mcp.py         MCP-сервер (тот же порт, /mcp)
app/static/        index.html — весь UI
core/              СДК-логика, только stdlib:
  importer.py      ibases.v8i + config.json → состояние (дерево, платформы, сеансы, ver-файл депо, формат 1CD)
  v8i_writer.py    запись в ibases.v8i (UTF-8 BOM, \r\r\n у секций баз, атомарно, бэкап .bak-YYYYMMDD)
  launcher.py      сборка командной строки 1С (строкой, не списком), история запусков
  procs.py         живые сеансы (NtQueryInformationProcess), активация окон (EnumWindows)
  cache.py         кэш 1С: base_dirs {config,user,program}, clear_base, dead_caches, size_of
  scanner.py       платформы: 1cv8/1cv82/1cv81 (Program Files / x86), D:\storage1C\ПлатформаКвас
  netcheck.py      предпроверка соединения (TCP/файл/веб, версия веб-публикации)
  com.py           COM-детект версии сервера (comcntr.dll), пользователи ИБ
  config.py        data/config.json, seal/unseal паролей (bl1:, XOR+SHA256(host|user|salt))
  hotkey/tray.py   Ctrl+Shift+S, трей, память окна
```

Данные (`data/`, вне git): `config.json` (настройки + sealed-пароли), `launches.log` (история), `window.json`, `install/`, `errors/*.bmp` (снимки сработавшего сторожа).

## REST API (основное)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/state` | полное состояние (базы, папки, платформы, recent, sessions) |
| GET | `/api/sessions` | лёгкий опрос (~25 мс) — кто открыт |
| GET/PUT/DELETE | `/api/base/{guid}` | настройки базы в config.json; DELETE — секция из ibases.v8i |
| POST | `/api/base/new`, `/api/folder/new`, DELETE `/api/folder/{guid}` | запись в ibases.v8i |
| POST | `/api/base/{guid}/move` | перенос в папку (Folder=, OrderInTree) |
| POST | `/api/launch` | запуск (mode, force, skip_check, extra_keys); 409 если открыта/предпроверка |
| POST | `/api/dryrun` | команда без запуска |
| POST | `/api/activate` `{pid}` | поднять окно сеанса; `no_window:true` если окна нет |
| GET/POST | `/api/base/{guid}/cache` `/clear` | кэш базы: info/config/user/program/both |
| GET | `/api/cache/dead`, POST `/api/cache/dead/clear` `{guids?}` | мёртвые кэши (GUID без базы) |
| GET | `/api/cache/bases`, POST `/api/cache/bases/clear` | кэш по базам, очистка выбранных |
| GET | `/api/platforms/usage` | платформы: использование, размер, компоненты |
| GET | `/api/platforms/bases?from_src=` | базы платформы (+storage_dir, storage_ver) |
| POST | `/api/platforms/migrate` / `migrate_bases` | массовый перевод (dry_run поддерживается) |
| POST | `/api/platforms/delete` | удаление платформы; 409 при базах/сеансах |
| POST | `/api/platform/probe` / `install` | дистрибутивы на шаре (env `BL_SHARE` / `share_root` в data/config.json), установка с UAC |
| POST | `/api/base/{guid}/restore-dt` | восстановление из .dt через `ibcmd infobase restore` |
| POST | `/api/base/{guid}/check-db` | запустить chdbfl (оконная утилита) |
| POST | `/api/dump/start` `{root, guids[]}` | пакетная выгрузка конфигураций в XML (ibcmd config export); открытые базы пропускаются, манифест пишется при успехе |
| GET | `/api/dump/state` | статус фоновой выгрузки |
| POST | `/api/load/start` `{root, guids[], mode: full\|changed\|files, files[]?, update_db?}` | загрузка конфигураций из XML (ibcmd config import + apply --force) |
| GET | `/api/load/state` | статус фоновой загрузки |
| GET/POST | `/api/dump/paths` | запомненные каталоги выгрузки (до 3, в config.json → `dump_paths`); POST `{path}` добавляет |
| GET | `/api/precheck/{guid}`, `/api/launch/events` | предпроверка; события сторожа запуска |
| POST | `/api/shutdown`, `/api/restart`, `/api/quit` | управление |

Полный список — в README / `app/main.py`. Пароли наружу не отдаются (флаги `pwd_set`, `_no_pwd()`); `/P`/`/Pwd` в логах маскируются.

## Безопасность

- Bind 127.0.0.1, CORS не настроен — снаружи веб-страницей не достать; любой локальный процесс может звать API.
- `data/config.json` содержит sealed-пароли (не криптография — привязка к машине/юзеру; при переносе на другую машину пароли сбрасываются, `pwd_note='broken'` → подсказка «введите заново»). **Никогда не коммитить** (в .gitignore; история до `a5f5d1c` содержит старые — перед любым push делать `git filter-repo`).
- `/api/fs/list` листит любые каталоги — осознанно (выбор .dt), доступ только с localhost.

## Ключевые правила эксплуатации

- **Пароль COM-коннектора один на систему** (`HKCR\V83.COMConnector.1` → comcntr.dll конкретной сборки); файловая база, открытая другой сборкой, откажет «Существуют активные сеансы другой версии» — закрыть сеансы или сменить сборку.
- **Версия клиента = версия сервера** (8.3.x ↔ 8.5.x несовместимы; внутри 8.5 сборки тоже). Определение версии сервера: COM-детект при ошибке Connect, обучение по живым сеансам (`learnt`), ver-файл хранилища, формат 1CD. tcp://-хранилища не определяются в принципе.
- **Восстановление из .dt** — `ibcmd` той сборки, на которой база должна работать (формат 1CD становится равен версии ibcmd); каталог базы переименовать в `<dir>.restore-bak-<ts>` для отката; `ibcmd` держит глобальный lock рабочего каталога (зависший pid убить).
- **Мёртвые кэши** = GUID-каталоги в `%LOCALAPPDATA%/%APPDATA%/%PROGRAMDATA%\1C\1cv8` без соответствующего ID в ibases.v8i. Один GUID может лежать в нескольких корнях — инвертирование выбора идёт по уникальным GUID.
- **Удаление платформ** — guard 409 (базы по exe/подбору/`Version=` + живые сеансы по cmdline); Program Files — UAC-паттерн с маркер-файлом.
- Сеансы 1С убивать только по базе в CommandLine, не по имени процесса.

## Диагностика

- `curl -s --noproxy '*' http://127.0.0.1:8797/api/state` — жив ли сервер.
- `data/launches.log` — история; `data/focus.log` — трей/окно; `data/install/` — логи установок платформ; `data/errors/*.bmp` — снимки окон ошибок 1С.
- **`data/audit.log`** — журнал действий пользователя (JSONL): каждое не-GET обращение к API — запуск, сохранение настроек, добавление/удаление/перенос баз, чистки кэша, переводы платформ, установка/удаление платформ. Поля с `pwd/pass/password/secret` маскируются `***` до записи. Ротация 2 МБ (хранится один предыдущий `.log.1`). Чтение: `GET /api/audit?tail=N` (последние N, новые в конце). Реализация: `core/audit.py` + middleware в `app/main.py`.
- `GET /api/tray`, `/api/hotkey` — состояние трея/горячей клавиши (`RegisterHotKey` вернул 0 = уже занято нашим сервером).
- Проблемы запуска 1С: текст окон 1С не читается — ориентироваться на предпроверку (`/api/precheck`) и события сторожа (`/api/launch/events`).

## Проверка после изменений

1. `python -c "import ast; ast.parse(open('app/main.py').read())"` — до рестарта.
2. Dev-инстанс :8798 — правки живьём (CDP-тесты headless Edge: `--remote-allow-origins=*`, WPy64 python + websocket-client).
3. Коммит на каждую фичу; версия — в `FastAPI(version=…)`.
4. После волны правок — рестарт прода (`POST /api/shutdown` + `pythonw boot.py`) и проверка новых эндпоинтов на :8797.
5. Дистрибутив — только по явной просьбе (`tools/make_release.py`), приёмка на распакованном архиве.
