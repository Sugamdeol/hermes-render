import ast
import importlib.util
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch as mock_patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_hermes_dashboard_runtime', ROOT/'scripts/dashboard-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
sys.modules[spec.name] = runtime
patch = runpy.run_path(str(ROOT/'scripts/patch-dashboard.py'))['patch']


class DashboardReliability(unittest.TestCase):
    def test_tab_checks_authenticate_and_report_errors_without_response_data(self):
        import io
        import urllib.error
        checker=runpy.run_path(str(ROOT/'scripts/check-dashboard.py'))
        def fetch(request,timeout):
            if isinstance(request,str):return io.BytesIO(b'<script>window.__HERMES_SESSION_TOKEN__="private-test";</script>')
            self.assertEqual(request.headers['X-hermes-session-token'],'private-test')
            if request.full_url.endswith('/api/skills'):
                raise urllib.error.HTTPError(request.full_url,502,'Bad Gateway',{},io.BytesIO(b'do-not-print'))
            return io.BytesIO(b'{}')
        with mock_patch('urllib.request.urlopen',fetch):report=checker['check']()
        self.assertEqual(report[-1]['status'],502)
        self.assertTrue(report[0]['ok'])
        self.assertNotIn('private-test',json.dumps(report))
        self.assertNotIn('do-not-print',json.dumps(report))

    def test_dashboard_supervisor_restarts_and_stops_without_relaunching(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);marker=home/'started';worker=home/'hermes'
            worker.write_text('#!'+sys.executable+'''\nimport os,time
from pathlib import Path
p=Path(os.environ['TEST_MARKER'])
count=int(p.read_text())+1 if p.exists() else 1
p.write_text(str(count))
if count<3:raise SystemExit(1)
time.sleep(60)
''')
            worker.chmod(0o755)
            env={**os.environ,'HERMES_BIN':str(worker),'TEST_MARKER':str(marker),'HERMES_DASHBOARD_RESTART_DELAY_SECONDS':'0.01'}
            process=subprocess.Popen(['sh',str(ROOT/'scripts/dashboard-supervisor.sh'),'--no-open'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if marker.exists() and marker.read_text()=='3':break
                    time.sleep(.02)
                self.assertEqual(marker.read_text(),'3')
                process.terminate();process.wait(timeout=3)
                self.assertEqual(process.returncode,0)
                time.sleep(.05)
                self.assertEqual(marker.read_text(),'3')
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=3)

    def test_v1_dashboard_upgrade_moves_blocking_scans_off_event_loop(self):
        source='# render dashboard reliability v1\nasync def get_skills():\n    return []\nasync def get_plugins_hub(request):\n    return {}\n'
        result=patch(source)
        self.assertIn('reliability v2',result)
        self.assertNotIn('async def',result)
        self.assertEqual(patch(result),result)

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

    def test_repair_api_keeps_authentication_and_avoids_broken_helpers(self):
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
        self.assertNotIn('hermes_tools',fixed)
        self.assertIn('LIMIT 20',fixed)
        self.assertIn('def snapshot',fixed)

    def test_debug_snapshot_is_authenticated_bounded_and_excludes_keys(self):
        from datetime import datetime, timezone
        source=(ROOT/'scripts/debug-master-api.py').read_text()
        functions=[]
        for node in ast.parse(source).body:
            if isinstance(node,ast.FunctionDef):
                node.decorator_list=[]; functions.append(node)
        class Denied(Exception):
            def __init__(self,status_code,detail):self.status_code=status_code
        auth=types.ModuleType('hermes_cli.web_server')
        auth._has_valid_session_token=lambda request:request=='valid'
        namespace={'Path':Path,'Request':object,'HTTPException':Denied,'json':json,
                   'datetime':datetime,'timezone':timezone}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'debug-api','exec'),namespace)
        with tempfile.TemporaryDirectory() as td, mock_patch.dict(sys.modules, {'hermes_cli.web_server':auth}):
            home=Path(td);namespace['_home']=lambda:home
            with self.assertRaises(Denied) as error:namespace['snapshot']('invalid')
            self.assertEqual(error.exception.status_code,401)
            (home/'MEMORY.md').write_text('x'*100000)
            (home/'cron').mkdir();(home/'cron/jobs.json').write_text(json.dumps([{'id':i} for i in range(100)]))
            (home/'state.json').write_text(json.dumps({'balance':4,'api_key':'must-not-appear'}))
            conn=sqlite3.connect(home/'state.db');conn.execute('CREATE TABLE sessions(id TEXT, started_at REAL)')
            conn.executemany('INSERT INTO sessions VALUES(?,?)', [(str(i),i) for i in range(100)])
            conn.commit();conn.close()
            snapshot=namespace['snapshot']('valid')
            self.assertEqual(len(snapshot['memory']['MEMORY.md']),16000)
            self.assertEqual(len(snapshot['jobs']),20)
            self.assertEqual(len(snapshot['history_preview']),20)
            self.assertEqual(snapshot['runtime_state'],{'balance':4})
            self.assertNotIn('must-not-appear',json.dumps(snapshot))


if __name__=='__main__':unittest.main()
