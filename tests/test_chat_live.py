import ast
import asyncio
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'dashboard-plugins/hermes-chat-dashboard/dashboard'

class ChatHistorySources(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((PLUGIN / 'plugin_api.py').read_text())
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'_list_rows','_row_summary'}]
        self.ns = {'Any':Any}
        exec(compile(ast.Module(body=funcs,type_ignores=[]), '<chat history>', 'exec'),self.ns)

    def test_projected_telegram_continuation_remains_visible(self):
        entry = self.ns['_row_summary']({'id':'tip','source':'telegram','parent_session_id':'compressed-root'})
        self.assertFalse(entry['_child'])
        self.assertTrue(self.ns['_row_summary']({'id':'worker','source':'tool'})['_child'])

    def test_native_source_paging_and_activity_order(self):
        calls = []
        def rich(**kwargs):
            calls.append(kwargs)
            return [{'id':str(i),'source':'telegram'} for i in range(4)]
        rows, more = self.ns['_list_rows'](SimpleNamespace(list_sessions_rich=rich),3,120,'telegram')
        self.assertTrue(more)
        self.assertEqual(len(rows),3)
        self.assertEqual(calls[0], {'source':'telegram','limit':4,'offset':120,'order_by_last_active':True})

class ChatBridge(unittest.IsolatedAsyncioTestCase):
    async def test_long_completion_frame_crosses_pipe_without_disconnect(self):
        source = (ROOT / 'scripts/patch-lite.py').read_text()
        self.assertIn("sys.executable, '-u', '-m', 'tui_gateway.entry'", source)
        match = re.search(r'stdout=asyncio.subprocess.PIPE, limit=([^,]+), start_new_session=True',source)
        self.assertIsNotNone(match)
        limit = eval(match.group(1), {'__builtins__':{}})
        proc = await asyncio.create_subprocess_exec(sys.executable,'-u','-c',
            "import json; print(json.dumps({'type':'message.complete','text':'x'*200000})); print('next-frame')",
            stdout=asyncio.subprocess.PIPE,limit=limit)
        try:
            frame = await asyncio.wait_for(proc.stdout.readline(),5)
            self.assertGreater(len(frame),200000)
            self.assertEqual(await proc.stdout.readline(),b'next-frame\n')
            self.assertEqual(await proc.wait(),0)
        finally:
            if proc.returncode is None: proc.kill(); await proc.wait()
