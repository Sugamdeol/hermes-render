import ast
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py'


class ChatPaging(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_get_messages_page']
        namespace = {'Any': Any, 'json': json}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'), namespace)
        self.page = namespace['_get_messages_page']
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, content TEXT, tool_calls TEXT)')
        self.conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?)',
                             [(i, 'long-chat', i // 2, 'x' * 10000, '[]') for i in range(1000)])
        self.decoded = 0

        def decode(content):
            self.decoded += 1
            return content

        self.db = SimpleNamespace(_conn=self.conn, _lock=nullcontext(), _decode_content=decode,
                                  get_messages=lambda *_: self.fail('Must not load the entire transcript'))

    def tearDown(self):
        self.conn.close()

    def test_latest_page_reads_only_requested_rows(self):
        rows, total = self.page(self.db, 'long-chat', 80, -80)
        self.assertEqual(total, 1000)
        self.assertEqual([r['id'] for r in rows], list(range(920, 1000)))
        self.assertEqual(self.decoded, 80)

    def test_all_earlier_pages_are_accessible_in_order(self):
        ids = []
        for offset in range(0, 1000, 80):
            rows, total = self.page(self.db, 'long-chat', 80, offset)
            ids.extend(row['id'] for row in rows)
            self.assertEqual(total, 1000)
        self.assertEqual(ids, list(range(1000)))

    def test_parameterized_session_and_content_decoding(self):
        rows, total = self.page(self.db, "long-chat' OR 1=1 --", 80, 0)
        self.assertEqual((rows, total), ([], 0))
