# -*- coding: utf-8 -*-
"""Протоколный слой MCP-сервера: JSON-RPC handle(), tools_list, call_tool.

Без 1С и без портов — чистые функции. Запуск: env/Scripts/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import mcp  # noqa: E402


def rpc(method, params=None, rid=1):
    return mcp.handle({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}})


class HandleTests(unittest.TestCase):
    def test_invalid_request(self):
        r = mcp.handle({'jsonrpc': '1.0', 'id': 5})
        self.assertEqual(r['error']['code'], -32600)
        self.assertEqual(r['id'], 5)

    def test_initialize(self):
        r = rpc('initialize', {'protocolVersion': '2025-06-18'})
        self.assertEqual(r['result']['protocolVersion'], '2025-06-18')

    def test_initialize_unknown_version_falls_back(self):
        r = rpc('initialize', {'protocolVersion': '1999-99-99'})
        self.assertEqual(r['result']['protocolVersion'], mcp.DEFAULT_PROTOCOL)

    def test_tools_list_shape(self):
        r = rpc('tools/list')
        tools = r['result']['tools']
        self.assertGreater(len(tools), 20)
        names = {t['name'] for t in tools}
        self.assertIn('run_batch', names)
        self.assertIn('dump_changes', names)
        self.assertIn('git_baseline', names)
        for t in tools:
            sch = t['inputSchema']
            self.assertEqual(sch['type'], 'object')
            self.assertEqual(sch['additionalProperties'], False)
            self.assertIsInstance(t['description'], str)
            self.assertTrue(t['description'].strip())
            for req in sch['required']:
                self.assertIn(req, sch['properties'], t['name'])

    def test_notification_no_response(self):
        r = mcp.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertIsNone(r)

    def test_unknown_tool_error(self):
        r = rpc('tools/call', {'name': 'no_such_tool', 'arguments': {}})
        self.assertIn('неизвестный инструмент', r['result']['content'][0]['text'])
        self.assertTrue(r['result'].get('isError'))

    def test_result_is_json_text(self):
        r = rpc('tools/call', {'name': 'no_such_tool', 'arguments': {}})
        self.assertEqual(r['result']['content'][0]['type'], 'text')


class ToolMapConsistencyTests(unittest.TestCase):
    """Схемы/REQUIRED согласованы с TOOLS; каждое REQUIRED-имя существует."""

    def test_required_names_exist(self):
        for name in mcp.REQUIRED:
            self.assertIn(name, mcp.TOOL_MAP, name)

    def test_required_fields_in_schema(self):
        for name, fields in mcp.REQUIRED.items():
            schema = mcp.TOOL_MAP[name][1]
            for f in fields:
                self.assertIn(f, schema, '%s.%s' % (name, f))

    def test_schema_props_wellformed(self):
        for name, _d, schema, _fn in mcp.TOOLS:
            for prop, spec in schema.items():
                self.assertIsInstance(spec, dict, '%s.%s' % (name, prop))
                self.assertIn('type', spec, '%s.%s' % (name, prop))

    def test_new_tools_registered(self):
        for t in ('git_baseline', 'dump_changes', 'run_batch'):
            self.assertIn(t, mcp.TOOL_MAP)


class CallToolTests(unittest.TestCase):
    def test_meta_attached(self):
        r = mcp.call_tool('no_such_tool', {})
        self.assertEqual(r['meta']['tool'], 'no_such_tool')
        self.assertIsInstance(r['meta']['ms'], int)

    def test_exception_wrapped_not_raised(self):
        # _resolve кидает ValueError на несуществующей базе — наружу текст, не 500
        r = mcp.call_tool('dump_changes', {'base': 'нет такой базы 000'})
        self.assertTrue(r.get('isError'))
        self.assertIn('не найдена', r['content'][0]['text'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
