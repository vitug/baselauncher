# -*- coding: utf-8 -*-
"""Юнит-тесты блокировки обслуживания: статусы, авто-снятие, завершение сеансов по базе.

Запуск: env/Scripts/python -m unittest discover -s tests -v
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import baselock                                # noqa: E402


def _base(**kw):
    d = {'guid': 'G1', 'name': 'Тест', 'connect': 'File="C:\\bases\\test";'}
    d.update(kw)
    return d


class _Rc:
    def __init__(self, rc):
        self.returncode = rc


class TestStatus(unittest.TestCase):
    def test_inactive(self):
        self.assertIs(baselock.status(_base())['active'], False)
        self.assertIs(baselock.status(_base(maintenance_until=0))['active'], False)

    def test_active(self):
        until = time.time() + 600
        st = baselock.status(_base(maintenance_until=until, maintenance_note='dt'))
        self.assertIs(st['active'], True)
        self.assertTrue(0 < st['remaining_min'] <= 10)
        self.assertEqual(st['note'], 'dt')


class TestCheckBlocked(unittest.TestCase):
    def test_active_message(self):
        with mock.patch.object(baselock.config, 'set_base') as sb:
            b = _base(maintenance_until=time.time() + 300, maintenance_note='загрузка dt')
            msg = baselock.check_blocked(b)
            self.assertIn('обслуживание', msg)
            sb.assert_not_called()               # активная — конфиг не трогаем

    def test_expired_autoclears(self):
        with mock.patch.object(baselock.config, 'set_base') as sb:
            b = _base(maintenance_until=time.time() - 1)
            self.assertEqual(baselock.check_blocked(b), '')
            sb.assert_called_once()
            guid, patch = sb.call_args[0]
            self.assertEqual((guid, patch['maintenance_until']), ('G1', 0))


class TestLockUnlock(unittest.TestCase):
    def test_lock(self):
        with mock.patch.object(baselock.config, 'set_base') as sb:
            res = baselock.lock('G1', 30, 'тест', base=_base())
            self.assertTrue(res['ok'])
            self.assertEqual(res['minutes'], 30)
            guid, patch = sb.call_args[0]
            self.assertEqual(guid, 'G1')
            self.assertEqual(patch['maintenance_note'], 'тест')
            self.assertGreater(patch['maintenance_until'], time.time())

    def test_unlock(self):
        with mock.patch.object(baselock.config, 'set_base') as sb:
            res = baselock.unlock('G1')
            self.assertTrue(res['ok'])
            self.assertEqual(sb.call_args[0][1]['maintenance_until'], 0)

    def test_lock_error(self):
        with mock.patch.object(baselock.config, 'set_base',
                               side_effect=RuntimeError('битый конфиг')):
            res = baselock.lock('G1')
            self.assertIs(res['ok'], False)
            self.assertIn('битый конфиг', res['error'])

    def test_no_kick_by_default(self):
        with mock.patch.object(baselock.config, 'set_base'), \
             mock.patch.object(baselock, 'terminate_sessions') as tk:
            baselock.lock('G1', 5, base=_base())
            tk.assert_not_called()
            baselock.lock('G2', 5, base=_base(), kick_sessions=True)
            tk.assert_called_once()


class TestSessions(unittest.TestCase):
    def test_file_match(self):
        fake = [{'pid': 1, 'path': 'C:\\Bases\\Test\\', 'server': '', 'ref': ''},
                {'pid': 2, 'path': 'C:\\other\\', 'server': '', 'ref': ''}]
        with mock.patch.object(baselock.procs, 'running_sessions', lambda: fake):
            got = baselock.sessions_of(_base(connect='File="c:/bases/test";'))
        self.assertEqual([s['pid'] for s in got], [1])

    def test_server_match(self):
        fake = [{'pid': 1, 'path': '', 'server': 'srv:1541', 'ref': 'db1'},
                {'pid': 2, 'path': '', 'server': 'srv:1541', 'ref': 'db2'}]
        with mock.patch.object(baselock.procs, 'running_sessions', lambda: fake):
            got = baselock.sessions_of(_base(connect='Srvr="srv:1541";Ref="db1";'))
        self.assertEqual([s['pid'] for s in got], [1])

    def test_terminate_only_matching(self):
        fake = [{'pid': 11, 'path': 'C:\\bases\\test\\', 'server': '', 'ref': ''},
                {'pid': 22, 'path': 'C:\\other\\', 'server': '', 'ref': ''}]
        with mock.patch.object(baselock.procs, 'running_sessions', lambda: fake), \
             mock.patch.object(baselock.subprocess, 'run', return_value=_Rc(0)) as run:
            res = baselock.terminate_sessions(_base())
        self.assertTrue(res['ok'])
        self.assertEqual(res['killed'], [11])
        self.assertEqual(res['count'], 1)
        # убили только pid 11 — по базе, не «по имени образа»
        self.assertEqual(len(run.call_args_list), 1)
        self.assertIn('11', ' '.join(map(str, run.call_args.args[0])))

    def test_terminate_failure(self):
        fake = [{'pid': 33, 'path': 'C:\\bases\\test\\', 'server': '', 'ref': ''}]
        with mock.patch.object(baselock.procs, 'running_sessions', lambda: fake), \
             mock.patch.object(baselock.subprocess, 'run', return_value=_Rc(1)):
            res = baselock.terminate_sessions(_base())
        self.assertIs(res['ok'], False)
        self.assertEqual(res['failed'], [33])


if __name__ == '__main__':
    unittest.main()
