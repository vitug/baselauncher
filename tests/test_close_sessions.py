# -*- coding: utf-8 -*-
"""close_sessions (лёгкое завершение без обслуживания) и отбор сеансов по CommandLine.

Запуск: env/Scripts/python -m unittest tests.test_close_sessions -v
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import baselock                                   # noqa: E402

OUR_PATH = r'D:\bases1C\ТестБаза'
OTHER_PATH = r'D:\bases1C\ДругаяБаза'


def _sess(pid: int, cmd: str) -> dict:
    from core import procs
    s = procs.parse_session(cmd)
    s.update({'pid': pid, 'exe': '1cv8.exe', 'title': '', 'window': True})
    return s


FAKE_SESSIONS = [
    _sess(101, r'"C:\Program Files\1cv8\8.3.25\bin\1cv8.exe" enterprise /F"D:\bases1C\ТестБаза"'),
    _sess(102, r'"C:\Program Files\1cv8\8.3.25\bin\1cv8.exe" designer /F"D:\bases1C\ТестБаза"'),
    _sess(103, r'"C:\Program Files\1cv8\8.3.25\bin\1cv8.exe" enterprise /F"D:\bases1C\ДругаяБаза"'),
    _sess(104, r'"C:\Program Files\1cv8\8.3.25\bin\1cv8c.exe" DESIGNER /F"D:\bases1C\ДругаяБаза"'),
    _sess(105, r'"C:\Program Files\1cv8\8.3.25\bin\1cv8.exe" enterprise '
               r'Srvr="srv-1c-demo";Ref="ДругаяСерверная";'),
]

OUR = {'name': 'ТестБаза', 'connect': 'File="%s";' % OUR_PATH, 'guid': 'g-1'}


class SelectionTests(unittest.TestCase):
    """sessions_of: только наша база — enterprise и designer; чужие не трогаются."""

    def test_sessions_of_our_base_only(self):
        with mock.patch.object(baselock.procs, 'running_sessions',
                               return_value=FAKE_SESSIONS):
            got = baselock.sessions_of(OUR)
        self.assertEqual(sorted(s['pid'] for s in got), [101, 102])

    def test_server_ref_selection(self):
        base = {'name': 'Серверная', 'connect': 'Srvr="srv-1c-demo";Ref="ДругаяСерверная";'}
        with mock.patch.object(baselock.procs, 'running_sessions',
                               return_value=FAKE_SESSIONS):
            got = baselock.sessions_of(base)
        self.assertEqual([s['pid'] for s in got], [105])


class _FakeTaskkill:
    """subprocess.run(['taskkill', ...]) → всегда успех; запоминает вызовы."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return mock.Mock(returncode=0)


class CloseSessionsTests(unittest.TestCase):
    """close_sessions: /F по своим pid; таймаут=0 → сразу принудительно."""

    def test_force_immediate(self):
        tk = _FakeTaskkill()
        with mock.patch.object(baselock.procs, 'running_sessions',
                               return_value=FAKE_SESSIONS), \
             mock.patch.object(baselock.subprocess, 'run', tk):
            res = baselock.close_sessions(OUR)
        self.assertTrue(res['ok'])
        self.assertEqual(res['count'], 2)
        self.assertEqual(res['killed'], [101, 102])
        self.assertEqual(res['soft_closed'], [])
        self.assertFalse(res['locked'])            # обслуживание не ставится
        # все вызовы — /F и только наши pid
        for argv in tk.calls:
            self.assertIn('/F', argv)
            self.assertIn(argv[argv.index('/PID') + 1], ('101', '102'))

    def test_no_sessions_is_not_error(self):
        with mock.patch.object(baselock.procs, 'running_sessions', return_value=[]):
            res = baselock.close_sessions(OUR)
        self.assertTrue(res['ok'])
        self.assertEqual(res['count'], 0)
        self.assertEqual(res['killed'], [])

    def test_foreign_never_killed(self):
        tk = _FakeTaskkill()
        with mock.patch.object(baselock.procs, 'running_sessions',
                               return_value=FAKE_SESSIONS), \
             mock.patch.object(baselock.subprocess, 'run', tk):
            baselock.close_sessions(OUR)
        pids = {argv[argv.index('/PID') + 1] for argv in tk.calls}
        self.assertNotIn('103', pids)
        self.assertNotIn('104', pids)               # дизайнер другой базы — жив
        self.assertNotIn('105', pids)

    def test_soft_then_force(self):
        """timeout>0: сначала без /F, если сеанс остался — добиваем /F."""
        tk = _FakeTaskkill()
        # после мягкого закрытия сеанс 102 «не вышел» — остаётся живым
        alive = FAKE_SESSIONS

        def sessions():
            # 101 закрылся мягко, 102 держится
            return [s for s in alive if s['pid'] == 102]
        with mock.patch.object(baselock.procs, 'running_sessions',
                               side_effect=lambda: (FAKE_SESSIONS
                                                    if not tk.calls else sessions())), \
             mock.patch.object(baselock.subprocess, 'run', tk), \
             mock.patch.object(baselock.time, 'sleep'):
            res = baselock.close_sessions(OUR, timeout=2)
        self.assertIn(102, res['soft_closed'] + res['killed'])
        # был вызов без /F (мягкий)
        self.assertTrue(any('/F' not in argv for argv in tk.calls))
        # и в конце — принудительный
        self.assertTrue(any('/F' in argv for argv in tk.calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
