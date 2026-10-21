# BaseLauncher

Свой аналог SM 1.4: список баз 1С (импорт из ibases.v8i + настроек SM)
и запуск выбранной платформой. Веб-UI на FastAPI — отладка в браузере (DevTools + reload).

## Запуск

```bash
D:/python/WPy64-31241/python-3.12.4.amd64/python.exe run_server.py
# → http://127.0.0.1:8790
```

uvicorn запущен с `reload=True`: правки кода подхватываются сами, обнови страницу.

## Структура

- `core/` — логика на чистом stdlib (движко-независимая):
  - `parsers.py` — ibases.v8i, v8config.smc, settings.cnf (форматы разведаны реверсом launcher.exe);
  - `scanner.py` — поиск платформ в Program Files и `D:\storage1C\ПлатформаКвас`;
  - `launcher.py` — сборка команды (`/IBConnectionString` из Connect) + Popen + лог;
  - `importer.py` — слияние источников → `data/bases.json`.
- `app/main.py` — REST: `GET /api/state`, `POST /api/launch|/api/dryrun`, `GET /api/log`.
- `app/static/index.html` — UI (без зависимостей).
- `data/overrides.json` — свои правки поверх импорта (высший приоритет).

## Приоритет exe платформы для базы

1. `data/overrides.json` → `bases[guid].exe`
2. v8config.smc: `V8UseAppFile=1` + `V8AppFile` (пер-базовый обход из SM)
3. settings.cnf: слот семейства `V8xAppFile` (слот 8.4 = 8.5 на этом хосте)
4. новейшая установленная платформа семейства `default_family` (по умолчанию 8.3)

## Отладка

- Dry-run: кнопка `cmd` в строке базы — показывает точную команду без запуска.
- Лог запусков: `data/launches.log` (JSON-строки) + `GET /api/log`.
- Сервер только на 127.0.0.1.
