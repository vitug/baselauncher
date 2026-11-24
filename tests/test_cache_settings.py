# -*- coding: utf-8 -*-
"""Кэши: бэкап настроек (*.pfl, def.usr) при очистке; чужие профили не удаляются.

Запуск: env/Scripts/python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import cache                                        # noqa: E402

GUID = '11111111-2222-3333-4444-555555555555'


class SettingsBackupTests(unittest.TestCase):
    """_rmtree/_backup_settings: pfl/def.usr переезжают в <имя>_backup."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bl_cache_'))
        self.d = self.tmp / GUID
        (self.d / 'Config').mkdir(parents=True)
        (self.d / '1cv8.pfl').write_text('settings', encoding='utf-8')
        (self.d / 'def.usr').write_text('usr', encoding='utf-8')
        (self.d / 'Config' / 'data.bin').write_bytes(b'x' * 100)
        (self.d / 'dbg.pfl').write_text('dbg', encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_settings_file(self):
        self.assertTrue(cache._is_settings_file('1cv8.pfl'))
        self.assertTrue(cache._is_settings_file('DBG.PFL'))
        self.assertTrue(cache._is_settings_file('def.usr'))
        self.assertFalse(cache._is_settings_file('data.bin'))
        self.assertFalse(cache._is_settings_file('pfl.txt'))

    def test_backup_on_rmtree(self):
        r = cache._rmtree(self.d, backup_settings=True)
        self.assertTrue(r['removed'])
        self.assertEqual(len(r['backed_up']), 3)
        b = self.tmp / (GUID + '_backup')
        self.assertTrue((b / '1cv8.pfl').is_file())
        self.assertTrue((b / 'def.usr').is_file())
        self.assertTrue((b / 'dbg.pfl').is_file())
        self.assertFalse((b / 'Config' / 'data.bin').exists())   # кэш — не настройки

    def test_no_backup(self):
        r = cache._rmtree(self.d, backup_settings=False)
        self.assertTrue(r['removed'])
        self.assertEqual(r['backed_up'], [])
        self.assertFalse((self.tmp / (GUID + '_backup')).exists())

    def test_backup_merge_overwrites(self):
        b = self.tmp / (GUID + '_backup')
        b.mkdir()
        (b / '1cv8.pfl').write_text('старый бэкап', encoding='utf-8')
        r = cache._rmtree(self.d, backup_settings=True)
        self.assertTrue(r['removed'])
        self.assertEqual((b / '1cv8.pfl').read_text(encoding='utf-8'), 'settings')


class OwnRootsOnlyTests(unittest.TestCase):
    """_cache_roots — только свой профиль; _scan_roots видит и чужие (не удаляемые)."""

    def test_cache_roots_no_foreign(self):
        # все корни — только свой LOCALAPPDATA/APPDATA/PROGRAMDATA
        mine = {str(cache.local_root()), str(cache.roaming_root()),
                str(cache.programdata_root())}
        for r in cache._cache_roots():
            self.assertIn(str(r), mine)

    def test_scan_roots_types(self):
        for item in cache._scan_roots():
            self.assertEqual(len(item), 2)     # (Path, owner)
            root, owner = item
            self.assertTrue(isinstance(root, Path))
            self.assertTrue(isinstance(owner, str))


class ClearDeadBackupTests(unittest.TestCase):
    """clear_dead: pfl уезжает в бэкап; no_backup — нет."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bl_cdead_'))
        self._env = {}
        for k in ('LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA'):
            self._env[k] = os.environ.get(k)
        os.environ['LOCALAPPDATA'] = str(self.tmp)
        os.environ['APPDATA'] = str(self.tmp)
        os.environ['PROGRAMDATA'] = str(self.tmp)
        d = self.tmp / '1C' / '1cv8' / GUID
        d.mkdir(parents=True)
        (d / '1cv8.pfl').write_text('s', encoding='utf-8')

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_by_default(self):
        res = cache.clear_dead(set())                          # GUID неизвестен
        self.assertEqual(res['removed'], 1)
        self.assertEqual(res['backed_up_count'], 1)
        self.assertTrue((self.tmp / '1C' / '1cv8' / (GUID + '_backup') / '1cv8.pfl').is_file())
        self.assertFalse((self.tmp / '1C' / '1cv8' / GUID).exists())

    def test_no_backup_flag(self):
        res = cache.clear_dead(set(), no_backup=True)
        self.assertEqual(res['removed'], 1)
        self.assertEqual(res['backed_up_count'], 0)
        self.assertFalse((self.tmp / '1C' / '1cv8' / (GUID + '_backup')).exists())

    def test_known_guid_kept(self):
        res = cache.clear_dead({GUID})
        self.assertEqual(res['removed'], 0)
        self.assertTrue((self.tmp / '1C' / '1cv8' / GUID / '1cv8.pfl').is_file())


class ClearBaseBackupTests(unittest.TestCase):
    """clear_base(what='user'): настройки в бэкап по умолчанию."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bl_cbase_'))
        self._env = {}
        for k in ('LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA'):
            self._env[k] = os.environ.get(k)
            os.environ[k] = str(self.tmp)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_level_backup(self):
        d = self.tmp / '1C' / '1cv8' / GUID
        d.mkdir(parents=True)
        (d / '1cv8.pfl').write_text('s', encoding='utf-8')
        (d / '-interface.bin').write_bytes(b'x' * 50)
        res = cache.clear_base(GUID, 'user')
        self.assertEqual(res['dirs_removed'], 1)
        self.assertEqual(res['backed_up_count'], 1)
        self.assertTrue((self.tmp / '1C' / '1cv8' / (GUID + '_backup') / '1cv8.pfl').is_file())
        self.assertFalse(d.exists())
        self.assertIs(res['no_backup'], False)

    def test_user_level_no_backup(self):
        d = self.tmp / '1C' / '1cv8' / GUID
        d.mkdir(parents=True)
        (d / '1cv8.pfl').write_text('s', encoding='utf-8')
        res = cache.clear_base(GUID, 'user', no_backup=True)
        self.assertEqual(res['backed_up_count'], 0)
        self.assertFalse((self.tmp / '1C' / '1cv8' / (GUID + '_backup')).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
