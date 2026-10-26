# -*- coding: utf-8 -*-
"""Модель данных BaseLauncher."""
from dataclasses import dataclass, field, asdict


@dataclass
class Platform:
    key: str        # версия-каталог, напр. '8.5.1.1580'
    exe: str        # полный путь 1cv8.exe
    exe_thin: str   # 1cv8c.exe (может быть '')
    bitness: str    # 'x64' | 'x86' | '?'
    source: str     # корень, где нашли: Program Files / (x86) / D:\storage1C\ПлатформаКвас
    family: str     # '8.3' | '8.5' | ...

    def as_dict(self):
        return asdict(self)


@dataclass
class BaseEntry:
    guid: str           # ID из ibases.v8i (= секция v8config.smc)
    name: str           # имя секции v8i (= что показывает стартер)
    folder: str         # Folder из v8i ('/Разработка')
    connect: str        # сырое значение Connect (File="..."; или Srvr="...";Ref="...")
    path: str           # каталог файловой базы ('' для серверных)
    server: str         # Srvr для серверных баз ('')
    ref: str            # Ref для серверных баз ('')
    ws: str             # URL веб-базы (Connect=ws="https://…")
    app: str            # App из v8i (Auto/Thick/Thin)
    order: str          # OrderInTree
    sm: dict = field(default_factory=dict)        # пусто (оставлено для совместимости UI)
    exe: str = ''           # эффективный exe платформы
    exe_src: str = ''       # откуда: our | config | auto | none
    family: str = ''        # семейство эффективной платформы ('8.3'...)
    thin: bool = False      # тонкий клиент (1cv8c.exe)
    has_designer: bool = True  # есть ли конфигуратор: веб-база (ws) — нет; только тонкий клиент — нет
    storage: dict = field(default_factory=dict)  # хранилище конфигурации: dir/user/password
    extra_keys: list = field(default_factory=list)  # доп. ключи запуска
    our: dict = field(default_factory=dict)        # наши правки (overrides.json) как есть
    path_ok: bool | None = None  # папка файловой базы на месте (None — серверная/неизвестно)
    path_info: str = ''          # 'ok' | 'папки базы нет' | 'нет файла 1Cv8.1CD' | 'серверная'
    ib_fmt: str = ''             # формат 1CD из заголовка 1Cv8.1CD ('8.3.8'/'8.2.14'…) — минимальная платформа
    exe_ok: bool = False         # exe платформы существует на диске
    fav: bool = False        # избранное (наша отметка)
    default_mode: str = ''   # режим запуска по клику: 'enterprise' | 'designer' | '' (как в шапке)
    descr: str = ''          # описание (эффективное: наше или из SM)
    user: str = ''           # пользователь ИБ /N для Предприятия
    user_cfg: str = ''       # пользователь ИБ /N для Конфигуратора ('' — как для Предприятия)
    pwd: str = ''            # пароль ИБ (Предприятие) /P — расшифрован из config.json
    pwd_cfg: str = ''        # пароль для Конфигуратора (свой, если отличается)
    pwd_note: str = ''       # '' | 'set' (есть пароль) | 'broken' (не расшифровался: чужая машина)
    ibname: str = ''         # имя секции из ibases.v8i (до наших переименований)

    def as_dict(self):
        return asdict(self)
