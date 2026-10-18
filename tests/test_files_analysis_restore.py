import ast
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_patch():
    spec = importlib.util.spec_from_file_location('files_patch', ROOT / 'scripts/patch-files-analysis.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FilesPatchTests(unittest.TestCase):
    def test_idempotent_and_native_page_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path in ('hermes_cli/web_routers', 'web/src/pages', 'web/src/components'):
                (root / path).mkdir(parents=True, exist_ok=True)
            server = root / 'hermes_cli/web_server.py'
            server.write_text('app.include_router(_files_routes.router)\n')
            page = root / 'web/src/pages/FilesPage.tsx'
            page.write_text('const activePath = "";\n<PluginSlot name="files:top" />\n// existing upload controls')
            helper = load_patch()
            self.assertTrue(helper.patch(root))
            self.assertFalse(helper.patch(root))
            self.assertIn('existing upload controls', page.read_text())
            self.assertEqual(page.read_text().count('<FilesStorageTools '), 1)

    def test_unknown_upstream_not_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'hermes_cli').mkdir()
            (root / 'web/src/pages').mkdir(parents=True)
            server = root / 'hermes_cli/web_server.py'
            server.write_text('changed = True')
            (root / 'web/src/pages/FilesPage.tsx').write_text('changed')
            with self.assertRaises(RuntimeError):
                load_patch().patch(root)
            self.assertEqual(server.read_text(), 'changed = True')


class FilesApiTests(unittest.TestCase):
    def setUp(self):
        import fnmatch, heapq, stat, time
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        class HTTPException(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
        def token(request):
            if request.headers.get('X-Test-Token') != 'yes':
                raise HTTPException(401, 'Authentication required')
        def resolve(path, request):
            target = Path(path).resolve() if path else self.root
            if not target.is_relative_to(self.root):
                raise HTTPException(403, 'Outside root')
            return None, target, str(target)
        async def run_in_threadpool(fn, *args):
            return fn(*args)
        namespace = dict(fnmatch=fnmatch, heapq=heapq, os=os, Path=Path, stat=stat, time=time,
                         MAX_NODES=500000, MAX_SECONDS=15, HTTPException=HTTPException,
                         _require_token=token, _resolve_managed_path=resolve,
                         _hosted_fs_read_guard=lambda path, request: None,
                         _is_sensitive_path=lambda path: path.name.startswith('.env') or 'mcp-tokens' in path.parts or path.name == 'auth.json',
                         run_in_threadpool=run_in_threadpool, Request=object)
        tree = ast.parse((ROOT / 'scripts/files-analysis-router.py').read_text())
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in functions:
            node.decorator_list = []
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual scanner and routes>', 'exec'), namespace)
        self.module = types.SimpleNamespace(scan=namespace['scan'], os=os)
        def get(url, headers=None):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = {key: value[0] for key, value in parse_qs(parsed.query).items()}
            request = types.SimpleNamespace(headers=headers or {})
            endpoint = namespace['search' if parsed.path.endswith('/search') else 'analysis']
            try:
                data = asyncio.run(endpoint(request, **params))
                return types.SimpleNamespace(status_code=200, json=lambda: data)
            except HTTPException as error:
                return types.SimpleNamespace(status_code=error.status_code)
        self.client = types.SimpleNamespace(get=get)
        self.headers = {'X-Test-Token': 'yes'}

    def test_search_analysis_hide_credentials_and_symlinks(self):
        (self.root / 'nested').mkdir()
        (self.root / 'nested/report.txt').write_text('hello')
        (self.root / '.env').write_text('secret')
        (self.root / 'mcp-tokens').mkdir()
        (self.root / 'mcp-tokens/report-secret.txt').write_text('secret')
        (self.root / 'link').symlink_to('/etc')
        response = self.client.get('/api/fs/analysis', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertEqual(report['bytes'], 5)
        self.assertEqual(report['files'], 1)
        self.assertEqual(report['children'][0]['name'], 'nested')
        self.assertTrue(report['children'][0]['is_directory'])
        response = self.client.get('/api/fs/search?q=REPORT*', headers=self.headers)
        self.assertEqual([item['name'] for item in response.json()['matches']], ['nested/report.txt'])

    def test_auth_and_root_boundary(self):
        self.assertEqual(self.client.get('/api/fs/analysis').status_code, 401)
        self.assertEqual(self.client.get('/api/fs/search?q=x').status_code, 401)
        self.assertEqual(self.client.get('/api/fs/analysis?path=/etc', headers=self.headers).status_code, 403)

    def test_budget_is_explicit_and_results_capped(self):
        for index in range(205):
            (self.root / f'report{index}').write_text('x')
        report = self.module.scan(self.root, 'report')
        self.assertEqual(len(report['matches']), 200)
        self.assertTrue(self.module.scan(self.root, max_nodes=1)['partial'])
        self.assertTrue(self.module.scan(self.root, max_seconds=-1)['partial'])

    def test_stat_errors_are_not_silently_hidden(self):
        with patch.object(self.module.os, 'scandir', side_effect=PermissionError):
            response = self.client.get('/api/fs/analysis', headers=self.headers)
        self.assertEqual(response.status_code, 500)
