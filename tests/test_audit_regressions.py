import ast
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py"

class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        super().__init__(detail)

class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        tree = ast.parse(SOURCE.read_text())
        selected = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                    '_read_json', '_write_json', '_safe_filename', 'upload_attachment'}:
                node.decorator_list = []
                node.args.defaults = []
                for arg in node.args.args: arg.annotation = None
                node.returns = None
                selected.append(node)
        self.ns = dict(Any=Any, Path=Path, json=json, os=os, tempfile=tempfile,
                       re=re, secrets=secrets, time=time, HTTPException=HTTPError,
                       _json_path=lambda name:self.home/name, _home=lambda:self.home,
                       _require_session=lambda request:None, MAX_UPLOAD_BYTES=4,
                       SAFE_NAME_RE=re.compile(r'[^a-zA-Z0-9._-]+'))
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    def tearDown(self): self.temp.cleanup()

    def test_corrupt_state_is_not_replaced_by_default(self):
        (self.home/'state.json').write_text('{damaged')
        with self.assertRaises(HTTPError) as caught:
            self.ns['_read_json']('state.json', {})
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual((self.home/'state.json').read_text(), '{damaged')
        self.assertEqual(self.ns['_read_json']('missing.json', {}), {})

    def test_concurrent_atomic_writes_leave_valid_private_file(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i:self.ns['_write_json']('state.json', {'value':i}), range(40)))
        self.assertIn(self.ns['_read_json']('state.json', {})['value'], range(40))
        self.assertEqual((self.home/'state.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.home.glob('*.tmp')))

    def test_failed_and_oversized_uploads_leave_no_partial_files(self):
        class Upload:
            filename='test.txt';content_type='text/plain';closed=False
            def __init__(self, failure): self.failure=failure;self.count=0
            async def read(self, size):
                self.count+=1
                if self.count==1:return b'123'
                if self.failure:raise OSError('disconnected')
                return b'456'
            async def close(self):self.closed=True
        for failure in (True, False):
            upload=Upload(failure)
            with self.assertRaises((OSError, HTTPError)):
                asyncio.run(self.ns['upload_attachment'](None, upload, 'session'))
            self.assertTrue(upload.closed)
            self.assertFalse([p for p in self.home.rglob('*') if p.is_file()])

    def test_successful_upload_is_private_and_complete(self):
        class Upload:
            filename='test.txt';content_type='text/plain';count=0
            async def read(self, size):
                self.count+=1
                return b'abc' if self.count==1 else b''
            async def close(self):pass
        result=asyncio.run(self.ns['upload_attachment'](None,Upload(),'session'))
        path=Path(result['path'])
        self.assertEqual(path.read_bytes(),b'abc')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)

class JavascriptAudit(unittest.TestCase):
    def test_browser_helpers(self):
        import subprocess
        subprocess.run(['node', str(ROOT/'tests/chat_audit_probe.cjs'),
                        str(SOURCE.parent/'bundle/index.js')], check=True)
