# -*- coding: utf-8 -*-
"""Юнит-тесты MCP-хелперов BaseLauncher: журнал загрузок, git-режим, верификация.

Запуск: env/Scripts/python -m unittest discover -s tests -v
(стендовая машинка: любой python3; git должен быть в PATH).
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import mcp  # noqa: E402


class TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bl_tests_'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class JournalTests(TmpBase):
    """changes.json: запись/чтение/обрезка до 200."""

    def setUp(self):
        super().setUp()
        self._orig = mcp._journal_path
        mcp._journal_path = lambda guid: self.tmp / 'manifests' / (guid + '.changes.json')

    def tearDown(self):
        mcp._journal_path = self._orig
        super().tearDown()

    def test_roundtrip(self):
        mcp._journal_add('g1', {'ts': '2026-01-01', 'files': {'A.xml': 'aa'}})
        j = mcp._journal_read('g1')
        self.assertEqual(len(j['entries']), 1)
        self.assertEqual(j['entries'][0]['files']['A.xml'], 'aa')

    def test_missing_returns_empty(self):
        self.assertEqual(mcp._journal_read('nope')['entries'], [])

    def test_corrupt_returns_empty(self):
        mcp._journal_path('g2').parent.mkdir(parents=True, exist_ok=True)
        mcp._journal_path('g2').write_text('{broken', encoding='utf-8')
        self.assertEqual(mcp._journal_read('g2')['entries'], [])

    def test_trim_200(self):
        for i in range(230):
            mcp._journal_add('g3', {'ts': str(i)})
        j = mcp._journal_read('g3')
        self.assertEqual(len(j['entries']), 200)
        self.assertEqual(j['entries'][0]['ts'], '30')     # старые срезались
        self.assertEqual(j['entries'][-1]['ts'], '229')


class ShaTests(TmpBase):
    def test_known_hash(self):
        p = self.tmp / 'f.bin'
        p.write_bytes(b'hello')
        self.assertEqual(mcp._sha256(p), hashlib.sha256(b'hello').hexdigest()[:16])

    def test_missing(self):
        self.assertEqual(mcp._sha256(self.tmp / 'nope'), '')


class RepoVersionTests(unittest.TestCase):
    def test_varianty(self):
        self.assertEqual(mcp._repo_version_from_log('Версия 5'), 5)
        self.assertEqual(mcp._repo_version_from_log('версии №12'), 12)
        self.assertEqual(mcp._repo_version_from_log('завершено Версией 3'), 3)
        self.assertEqual(mcp._repo_version_from_log(''), None)
        self.assertIsNone(mcp._repo_version_from_log('нет чисел'))
        self.assertIsNone(mcp._repo_version_from_log(None))
        # берём ПОСЛЕДНЕЕ упоминание
        self.assertEqual(mcp._repo_version_from_log('Версия 2 ... Версия 9'), 9)


class GitHelperTests(TmpBase):
    """_git/_git_status_names на настоящем git во временном каталоге."""

    def _repo(self):
        code, out = mcp._git(self.tmp, 'init', '-b', 'xml-state')
        assert code == 0, out
        (self.tmp / 'Configuration.xml').write_text('<a/>', encoding='utf-8')
        mcp._git(self.tmp, 'add', '-A')
        code, out = mcp._git(self.tmp, '-c', 'user.name=t', '-c', 'user.email=t@t',
                             'commit', '-m', 'baseline')
        assert code == 0, out
        return self.tmp

    def test_git_ok(self):
        self._repo()
        self.assertEqual(mcp._git_status_names(self.tmp), [])

    def test_modified_staged_and_unstaged(self):
        # регресс: ' M file' (unstaged) и 'M  file' (staged) — обе записи
        # раньше парсер срезал первую букву пути у staged-варианта
        self._repo()
        (self.tmp / 'Configuration.xml').write_text('<b/>', encoding='utf-8')
        self.assertEqual(mcp._git_status_names(self.tmp), ['Configuration.xml'])
        mcp._git(self.tmp, 'add', '-A')
        self.assertEqual(mcp._git_status_names(self.tmp), ['Configuration.xml'])

    def test_untracked(self):
        self._repo()
        (self.tmp / 'Catalogs').mkdir()
        (self.tmp / 'Catalogs' / 'X.xml').write_text('<c/>', encoding='utf-8')
        self.assertEqual(mcp._git_status_names(self.tmp), ['Catalogs/X.xml'])

    def test_path_with_spaces(self):
        self._repo()
        (self.tmp / 'Обработки Тест').mkdir()
        (self.tmp / 'Обработки Тест' / 'А Б.xml').write_text('<d/>', encoding='utf-8')
        names = mcp._git_status_names(self.tmp)
        self.assertIn('Обработки Тест/А Б.xml', names)

    def test_deleted(self):
        self._repo()
        (self.tmp / 'Configuration.xml').unlink()
        self.assertEqual(mcp._git_status_names(self.tmp), ['Configuration.xml'])

    def test_no_repo(self):
        self.assertEqual(mcp._git_status_names(self.tmp), [])
        self.assertEqual(mcp._git(self.tmp, 'status')[0] != 0, True)


class GitSectionTests(TmpBase):
    def test_no_repo_empty(self):
        (self.tmp / 'x.xml').write_text('1', encoding='utf-8')
        self.assertEqual(mcp._git_section(self.tmp, {}), {})

    def test_repo_section(self):
        code, out = mcp._git(self.tmp, 'init', '-b', 'xml-state')
        assert code == 0, out
        (self.tmp / 'Configuration.xml').write_text('<a/>', encoding='utf-8')
        mcp._git(self.tmp, 'add', '-A')
        mcp._git(self.tmp, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-m', 'b')
        (self.tmp / 'Configuration.xml').write_text('<zzz/>', encoding='utf-8')
        g = mcp._git_section(self.tmp, {})
        self.assertTrue(g['repo'])
        self.assertEqual(g['changed_count'], 1)
        self.assertIn('diff_stat', g)
        g2 = mcp._git_section(self.tmp, {'diff_file': 'Configuration.xml'})
        self.assertIn('zzz', g2['diff'])
        g3 = mcp._git_section(self.tmp, {'diff_file': 'нет_такого.xml'})
        self.assertIn('нет отличий', g3['diff'])


class StrictCheckLogicTests(TmpBase):
    """Логика сверки «изменённое в git минус разрешённый список» (как в t_run_batch)."""

    def test_extras(self):
        repo_files = {'Configuration.xml', 'Forms/Макет.xml'}
        allowed = ['Configuration.xml']
        extras = sorted(repo_files - set(allowed))
        self.assertEqual(extras, ['Forms/Макет.xml'])

    def test_no_extras(self):
        repo_files = {'Configuration.xml'}
        allowed = ['Configuration.xml']
        self.assertEqual(sorted(repo_files - set(allowed)), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
