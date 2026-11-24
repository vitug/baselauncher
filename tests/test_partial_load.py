# -*- coding: utf-8 -*-
"""Частичная загрузка в t_run_batch: платформенные ключи -listFile/-partial/-updateConfigDumpInfo.

Регресс: раньше сервер дописывал несуществующий ключ «-files p1,p2» — конфигуратор его
не понимал; рабочий синтаксис (проверен на 8.5.1) — /LoadConfigFromFiles -listFile <f> -partial
-updateConfigDumpInfo -Format Hierarchical.

Запуск: env/Scripts/python -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import mcp                                            # noqa: E402


class _FakeProc:
    returncode = 0

    def __init__(self):
        self.stdout = b''
        self.stderr = b''


def _payload(res: dict) -> dict:
    """Достать JSON из MCP-конверта (_text-ответ); _err-ответ — как {ok: False, error}."""
    import json as _json
    txt = res['content'][0]['text']
    if res.get('isError'):
        return {'ok': False, 'error': txt}
    return _json.loads(txt)


class PartialLoadTests(unittest.TestCase):
    """t_run_batch с files: ключи команды и содержимое load_list.txt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bl_part_'))
        self.src = self.tmp / 'dump'
        (self.src / 'CommonModules' / 'Мод1' / 'Ext').mkdir(parents=True)
        (self.src / 'CommonModules' / 'Мод1' / 'Ext' / 'Module.bsl').write_text(
            '// код', encoding='utf-8')
        (self.src / 'ConfigDumpInfo.xml').write_text('<info/>', encoding='utf-8')
        self.base = {'guid': 'g1', 'name': 'Тест', 'connect': 'File="C:/b";',
                     'running_pids': {}, 'our': {'dump_dir': str(self.tmp)},
                     'storage': {'dir': 'tcp://h:1/repo', 'user': 'dev1'}}
        mcp._state = lambda: {}                                 # noqa: B010
        mcp._resolve = lambda st, q: self.base                  # noqa: B010
        mcp._build_cmd = lambda b, a: (' '.join(['"C:/1cv8.exe"', 'DESIGNER']
                                               + a['extra_keys']), 'designer')
        self.captured = {}

        def fake_run(cmd, timeout=0, capture_output=False):
            self.captured['cmd'] = cmd
            return _FakeProc()

        self._run = mock.patch.object(subprocess, 'run', side_effect=fake_run)
        self._run.start()
        self._jr = mock.patch.object(mcp, '_journal_add')
        self._jr.start()

    def tearDown(self):
        self._run.stop()
        self._jr.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, files):
        return mcp.call_tool('run_batch', {
            'base': 'Тест', 'timeout': 30,
            'extra_keys': ['/LoadConfigFromFiles"%s"' % self.src],
            'files': files})

    def test_platform_keys(self):
        res = _payload(self._call(['CommonModules/Мод1/Ext/Module.bsl']))
        self.assertTrue(res.get('ok'), res)
        cmd = self.captured['cmd']
        # несуществующего ключа -files больше нет
        self.assertNotIn('-files', cmd)
        # платформенная тройка + Format
        for k in ('-listFile', '-partial', '-updateConfigDumpInfo'):
            self.assertIn(k, cmd)
        self.assertIn('-Format', cmd)
        self.assertIn('Hierarchical', cmd)
        # список файлов сгенерирован и валиден
        lf = res['list_file']
        self.assertTrue(os.path.isfile(lf))
        content = Path(lf).read_text(encoding='utf-8-sig')
        self.assertEqual(content.strip(), 'CommonModules/Мод1/Ext/Module.bsl')

    def test_object_name_expands(self):
        res = _payload(self._call(['ОбщийМодуль.Мод1']))
        self.assertTrue(res.get('ok'), res)
        content = Path(res['list_file']).read_text(encoding='utf-8-sig')
        self.assertIn('Module.bsl', content)
        self.assertEqual(res['files_loaded'], 1)

    def test_support_files_excluded(self):
        res = _payload(self._call(['ConfigDumpInfo.xml', 'CommonModules/Мод1/Ext/Module.bsl']))
        self.assertTrue(res.get('ok'), res)
        content = Path(res['list_file']).read_text(encoding='utf-8-sig')
        self.assertNotIn('ConfigDumpInfo', content)
        self.assertEqual(res['skipped_support'], 1)

    def test_only_support_files_is_error(self):
        res = _payload(self._call(['ConfigDumpInfo.xml']))
        self.assertFalse(res.get('ok'))
        self.assertIn('поддержки', res.get('error', ''))

    def test_missing_file_is_error(self):
        res = _payload(self._call(['НетТакогоФайла.bsl']))
        self.assertFalse(res.get('ok'))

    def test_format_passed_through(self):
        res = _payload(self._call(['CommonModules/Мод1/Ext/Module.bsl']))
        self.assertTrue(res.get('ok'), res)
        # -Format добавлен ровно один раз
        self.assertEqual(self.captured['cmd'].count('-Format'), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
