# -*- coding: utf-8 -*-
"""Расшифровка паролей SM 1.4 (модуль crypto-module).

Реверс launcher.exe 1.4.0.157 (REA + Ghidra 12.1.4, 2026-10-22):

- чтение v8config.smc: функция по 0x8480e8 читает ключи EntUser/EntPassword,
  CfgUser/CfgPassword, StorageUser/StorageDir/StoragePassword;
- для пароля вызывается 0x848018 → при «старом методе»
  (settings.cnf: <NewMethodEncryption>false</NewMethodEncryption>)
  это 0x847d48 — цикл:

      plain[i] = (cipher[i] - (key[i mod len(key)] - Ord('0'))) mod 256

  с ключом-константой 'REDACTED' (строка @0x8480d8, она же @0x8439fc);
- пароль-«новый метод» помечен первым символом '~' (@0x847d90: сравнение
  первого символа с маркером, затем ветка 0x847b2c) — этот вариант здесь
  не поддерживается, такие значения пропускаются.

Хранение: в INI шифртекст записан как ANSI(cp1251)-байты, закодированные
в UTF-8, поэтому перед побайтовой расшифровкой строка кодируется в cp1251.

Проверено живьём на v8config.smc пользователя (2026-10-22): 65 из 67 непустых
значений дают осмысленный текст, напр. 'TNDw’vt' -> 'REDACTED',
'‰Ћr‰ЏrnU07' -> 'fractalQ01', 'TNDWMD' -> '123123'.
"""
KEY = b'REDACTED'
NEW_METHOD_MARK = '~'


def is_new_method(text: str) -> bool:
    """Пароль сохранён новым методом SM (не поддерживается)."""
    return bool(text) and text[0] == NEW_METHOD_MARK


def decrypt(text: str) -> str:
    """Расшифровать значение пароля из v8config.smc. '' — если не получилось."""
    if not text:
        return ''
    if is_new_method(text):
        return ''
    try:
        cb = text.encode('cp1251')
    except UnicodeEncodeError:
        return ''
    out = bytearray()
    n = len(KEY)
    for i, c in enumerate(cb):
        shift = (KEY[i % n] - 0x30) & 0xFF
        out.append((c - shift) & 0xFF)
    for enc in ('utf-8', 'cp1251'):
        try:
            s = bytes(out).decode(enc)
        except UnicodeDecodeError:
            continue
        if s and all(ch.isprintable() for ch in s):
            return s
    return ''


def encrypt(text: str, encoding: str = 'cp1251') -> str:
    """Обратная операция (для тестов/round-trip)."""
    raw = text.encode(encoding)
    out = bytearray()
    n = len(KEY)
    for i, c in enumerate(raw):
        shift = (KEY[i % n] - 0x30) & 0xFF
        out.append((c + shift) & 0xFF)
    return bytes(out).decode('cp1251')
