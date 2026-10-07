import ast
import importlib.util
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_hermes_dashboard_runtime', ROOT/'scripts/dashboard-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
sys.modules[spec.name] = runtime
patch = runpy.run_path(str(ROOT/'scripts/patch-dashboard.py'))['patch']


class DashboardReliability(unittest.TestCase):
    def test_actual_pinned_endpoints_read_old_database_without_migration(self):
        source = (ROOT/'tests/fixtures/dashboard-pinned.txt').read_text()
        updated = patch(source)
        self.assertEqual(patch(updated), updated)
        tree = ast.parse(updated)
        functions = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ('get_usage_analytics', 'get_models_analytics'):
                node.decorator_list=[]; functions.append(node)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'state.db'
            conn=sqlite3.connect(path)
            conn.executescript('''CREATE TABLE sessions(id TEXT, started_at REAL, model TEXT, input_tokens INTEGER, output_tokens INTEGER);
                CREATE TABLE messages(session_id TEXT, role TEXT, tool_calls TEXT, timestamp REAL);''')
            conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('s',time.time(),'custom-model',123,45))
            calls=[{'function':None}, {'function':{'name':'skill_view','arguments':'{"name":"test"}'}}]
            conn.execute('INSERT INTO messages VALUES(?,?,?,?)', ('s','assistant',json.dumps(calls),time.time()))
            conn.commit(); conn.close()
            old=runtime.AnalyticsDB
            runtime.AnalyticsDB=lambda:old(path)
            namespace={'time':time}
            try:
                exec(compile(ast.Module(body=functions,type_ignores=[]),'endpoints','exec'),namespace)
                report=namespace['get_usage_analytics'](30)
                self.assertEqual(report['totals']['total_input'],123)
                self.assertEqual(report['totals']['total_actual_cost'],0)
                self.assertEqual(report['skills']['top_skills'][0]['total_count'],1)
                models=namespace['get_models_analytics'](30)
                self.assertEqual(models['models'][0]['output_tokens'],45)
                # Chart readers do not acquire a migration write lock.
                writer=sqlite3.connect(path); writer.execute('BEGIN IMMEDIATE')
                self.assertEqual(namespace['get_usage_analytics'](30)['totals']['total_sessions'],1)
                writer.rollback(); writer.close()
                check=sqlite3.connect(path)
                self.assertNotIn('actual_cost_usd',[r[1] for r in check.execute('PRAGMA table_info(sessions)')])
                check.close()
            finally: runtime.AnalyticsDB=old

    def test_plugin_missing_bundle_is_diagnostic_and_alternate_bundle_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            plugin={'name':'example','label':'Example','_dir':td,'entry':'dist/index.js','css':'dist/style.css'}
            missing=runtime.validate_plugin(plugin)
            self.assertEqual(missing['entry'],'__hermes_plugin_error__.js')
            self.assertIn('missing',missing['load_error'])
            self.assertIn('register',runtime.error_bundle(missing))
            (base/'bundle').mkdir(); (base/'bundle/index.js').write_text('(function(){return;})();')
            valid=runtime.validate_plugin(plugin)
            self.assertEqual(valid['entry'],'bundle/index.js')
            self.assertIsNone(valid['css'])
            self.assertNotIn('load_error',valid)

    def test_browser_syntax_check_catches_top_level_return_without_running_code(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'index.js';p.write_text('if (!window.SDK) { return; }')
            self.assertIn('invalid syntax',runtime.syntax_error(p))
            p.write_text('throw new Error("MUST NOT EXECUTE");')
            self.assertIsNone(runtime.syntax_error(p))

    def test_duplicate_plugin_routes_are_separated(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'index.js').write_text('void 0;')
            plugins=[{'name':name,'_dir':td,'entry':'index.js','tab':{'path':'/debug'}}
                     for name in ('debug-doctor','debug-master')]
            validated=runtime.validate_plugins(plugins)
            self.assertEqual(validated[0]['tab']['path'],'/debug')
            self.assertEqual(validated[1]['tab']['path'],'/plugin-debug-master')
            self.assertEqual(plugins[1]['tab']['path'],'/debug')

    def test_repair_api_keeps_authentication_and_uses_unique_helper_module(self):
        repair=runpy.run_path(str(ROOT/'scripts/repair-dashboard-plugins.py'))
        old='''import sys
sys.path.append("/opt/data/plugins/debug-master")
from __init__ import _dump_memory, _dump_jobs, _dump_session_history, _load_state
def _require_session(request: Request) -> None:
    pass
async def snapshot(request: Request):
    _require_session(request)
'''
        fixed=repair['repaired_api'](old)
        self.assertIn('_has_valid_session_token(request)',fixed)
        self.assertIn('status_code=401',fixed)
        self.assertNotIn('from __init__ import',fixed)
        self.assertIn('def snapshot',fixed)


if __name__=='__main__':unittest.main()
