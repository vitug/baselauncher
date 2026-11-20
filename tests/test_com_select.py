# -*- coding: utf-8 -*-
"""Юнит-тесты COM-слоя: подбор платформы-коннектора, парсинг несовпадения версий, расширения.

Запуск: env/Scripts/python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import com                                        # noqa: E402


def _mkroot():
    return Path(tempfile.mkdtemp(prefix='bl_com_'))


def _mkplat(root: Path, v: str, with_comcntr=True):
    bin_ = root / v / 'bin'
    bin_.mkdir(parents=True, exist_ok=True)
    (bin_ / '1cv8.exe').write_bytes(b'x')
    if with_comcntr:
        (bin_ / 'comcntr.dll').write_bytes(b'x')
    return str(bin_ / '1cv8.exe')


class TestVersionKey(unittest.TestCase):
    def test_key(self):
        self.assertEqual(com._version_key('8.3.25.1336'), [8, 3, 25, 1336])
        self.assertEqual(com._version_key('8.3'), [8, 3, 0, 0])
        self.assertEqual(com._version_key(''), [0, 0, 0, 0])
        self.assertEqual(com._version_key('8.3.x'), [8, 3, 0, 0])
        self.assertGreater(com._version_key('8.10.1'), com._version_key('8.9.9'))


class TestPickConnector(unittest.TestCase):
    def setUp(self):
        self.root = _mkroot()
        self.roots = [str(self.root)]

    def test_own_exe_has_connector(self):
        exe = _mkplat(self.root, '8.3.25.1336')
        self.assertEqual(com.pick_connector_exe(exe, roots=self.roots), exe)

    def test_exact_base_version(self):
        _mkplat(self.root, '8.3.20.1')
        _mkplat(self.root, '8.3.25.1336')
        thin = _mkplat(self.root, '8.5.4.57', with_comcntr=False)
        got = com.pick_connector_exe(thin, base_version='8.3.20.1', roots=self.roots)
        self.assertIn('8.3.20.1', got)

    def test_nearest_above(self):
        _mkplat(self.root, '8.3.20.1')
        _mkplat(self.root, '8.3.25.1336')
        _mkplat(self.root, '8.3.27.100')
        thin = _mkplat(self.root, '8.5.4.57', with_comcntr=False)
        got = com.pick_connector_exe(thin, base_version='8.3.23.0', roots=self.roots)
        self.assertIn('8.3.25.1336', got)

    def test_family_newest_fallback(self):
        fam83 = _mkplat(self.root, '8.3.20.1')
        self.assertEqual(com.pick_connector_exe(fam83, roots=self.roots), fam83)
        _mkplat(self.root, '8.3.25.1336')
        thin = _mkplat(self.root, '8.3.30.99', with_comcntr=False)
        got = com.pick_connector_exe(thin, roots=self.roots)
        self.assertIn('8.3.25.1336', got)

    def test_nothing_available(self):
        self.assertEqual(com.pick_connector_exe('', roots=self.roots), '')
        thin = _mkplat(self.root, '8.5.4.57', with_comcntr=False)
        self.assertEqual(com.pick_connector_exe(thin, roots=self.roots), '')

    def test_exact_beats_newest(self):
        _mkplat(self.root, '8.3.20.1')
        newest = _mkplat(self.root, '8.3.27.100')
        thin = _mkplat(self.root, '8.5.4.57', with_comcntr=False)
        got = com.pick_connector_exe(thin, base_version='8.3.20.1', roots=self.roots)
        self.assertNotEqual(got, newest)
        self.assertIn('8.3.20.1', got)


class TestParseMismatch(unittest.TestCase):
    def test_russian_message(self):
        msg = ('Ошибка при выполнении операции\nРазличаются версии клиента и сервера '
               '(8.3.25.1336 - 8.3.27.101)')
        self.assertEqual(com._parse_version_mismatch(msg), ('8.3.25.1336', '8.3.27.101'))

    def test_no_version(self):
        self.assertEqual(com._parse_version_mismatch('нет версии тут'), ('', ''))
        self.assertEqual(com._parse_version_mismatch(''), ('', ''))

    def test_en_dash(self):
        self.assertEqual(com._parse_version_mismatch('(8.5.4.57 – 8.5.5.12)'),
                         ('8.5.4.57', '8.5.5.12'))


class TestCandidatePlatforms(unittest.TestCase):
    def setUp(self):
        self.root = _mkroot()
        self.roots = [str(self.root)]

    def test_order_exact_first_then_desc(self):
        _mkplat(self.root, '8.3.20.1')
        _mkplat(self.root, '8.3.25.1336')
        _mkplat(self.root, '8.3.27.100')
        plats = com.candidate_platforms('', '8.3.20.1', roots=self.roots)
        vers = [com._version_of(p) for p in plats]
        self.assertEqual(vers, ['8.3.20.1', '8.3.27.100', '8.3.25.1336'])

    def test_family_filter(self):
        _mkplat(self.root, '8.3.25.1336')
        exe85 = _mkplat(self.root, '8.5.100.1')
        plats = com.candidate_platforms(exe85, '', roots=self.roots)
        self.assertEqual(len(plats), 1)
        self.assertIn('8.5.100.1', plats[0])


class TestExtensions(unittest.TestCase):
    def test_support(self):
        self.assertIs(com.extensions_supported(''), True)      # актуальный режим
        self.assertIs(com.extensions_supported('8.3.11'), True)
        self.assertIs(com.extensions_supported('8.3.16'), True)
        self.assertIs(com.extensions_supported('8.3.25'), True)
        self.assertIs(com.extensions_supported('8.2.16'), False)
        self.assertIs(com.extensions_supported('8.3.10'), False)


class TestRegisterBin(unittest.TestCase):
    def test_no_dll(self):
        err = com._register_bin('C:\\nonexistent-dir-xyz')
        self.assertTrue(err)
        self.assertIn('comcntr.dll', err)


if __name__ == '__main__':
    unittest.main()
