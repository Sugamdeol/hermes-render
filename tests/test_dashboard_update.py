import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import fastapi
import fastapi.responses
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class DashboardUpdateTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.directory = Path(t.name)
        env = patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full', 'HERMES_COLAB_UPDATE_DIR':str(self.directory)})
        env.start(); self.addCleanup(env.stop)
        with patch.dict(sys.modules, {'fastapi':fastapi, 'fastapi.responses':fastapi.responses}):
            self.api = load('dashboard_update_api', 'dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py')
        self.api._require_session = lambda request: None
        self.control = load('dashboard_update_control', 'scripts/colab-update-control.py')
        self.control.write(self.directory/'heartbeat.json', {'time':time.time()})
        app = fastapi.FastAPI(); app.include_router(self.api.router)
        self.client = TestClient(app); self.addCleanup(self.client.close)
        self.headers = {'X-Hermes-Update':'1'}

    def test_auth_is_required_for_both_routes(self):
        def denied(request): raise fastapi.HTTPException(401, 'Unauthorized')
        self.api._require_session = denied
        self.assertEqual(self.client.get('/agent-update').status_code, 401)
        self.assertEqual(self.client.post('/agent-update', json={'action':'check'}, headers=self.headers).status_code, 401)

    def test_queue_does_not_execute_in_gateway_and_rejects_duplicates(self):
        response = self.client.post('/agent-update', json={'action':'check'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        request = json.loads((self.directory/'request.json').read_text())
        self.assertEqual(request['action'], 'check')
        self.assertEqual((self.directory/'request.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.post('/agent-update', json={'action':'check'}, headers=self.headers).status_code, 409)

    def test_custom_header_and_exact_checked_commit_required(self):
        self.assertEqual(self.client.post('/agent-update', json={'action':'check'}).status_code, 403)
        for ref in ('main', '--evil', '../main'):
            self.assertEqual(self.client.post('/agent-update', json={'action':'update','ref':ref}, headers=self.headers).status_code, 422)
        target = 'a'*40
        self.assertEqual(self.client.post('/agent-update', json={'action':'update','ref':target}, headers=self.headers).status_code, 409)
        self.control.write(self.directory/'status.json', {'phase':'succeeded','action':'check','result':{'target':target}})
        self.assertEqual(self.client.post('/agent-update', json={'action':'update','ref':target}, headers=self.headers).status_code, 200)

    def test_stale_or_unsupported_worker_disables_controls(self):
        self.control.write(self.directory/'heartbeat.json', {'time':time.time()-60})
        self.assertFalse(self.client.get('/agent-update').json()['available'])
        self.assertEqual(self.client.post('/agent-update', json={'action':'check'}, headers=self.headers).status_code, 503)
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'other'}):
            self.assertFalse(self.client.get('/agent-update').json()['available'])

    def test_notebook_worker_returns_result_and_redacts_failure(self):
        agent = Mock(); agent.process.poll.return_value = None
        ns = {'HERMES_COLAB':agent,'TOOLS':ROOT/'scripts','redact_secrets':lambda text, env:text.replace('secret','[redacted]')}
        result = {'target':'a'*40,'update_available':True}
        with patch.object(self.control.runpy, 'run_path', return_value={'check':Mock(return_value=result)}):
            self.control.execute(ns, self.directory, {'id':'job1','action':'check','ref':'main'})
        status = self.control.read(self.directory/'status.json')
        self.assertEqual(status['phase'], 'succeeded'); self.assertEqual(status['result'], result)
        with patch.object(self.control.runpy, 'run_path', side_effect=RuntimeError('secret failed')):
            self.control.execute(ns, self.directory, {'id':'job2','action':'check','ref':'main'})
        status = self.control.read(self.directory/'status.json')
        self.assertEqual(status['phase'], 'failed'); self.assertNotIn('secret', status['message'])

    def test_dashboard_component_states_and_duplicate_prevention(self):
        import subprocess
        subprocess.run(['node', str(ROOT/'tests/dashboard_update_probe.cjs'), str(ROOT/'dashboard-plugins/hermes-chat-dashboard/dashboard/bundle/index.js')], check=True)

    def test_notebook_rerun_reuses_worker_with_new_launcher_globals(self):
        import threading
        existing = {'ns':{'old':True}, 'thread':Mock()}
        existing['thread'].is_alive.return_value = True
        with patch.object(threading, '_hermes_colab_update_control', existing, create=True), patch.object(threading, 'Thread') as create:
            current = {'new':True}
            self.control.start(current)
            self.assertIs(existing['ns'], current)
            create.assert_not_called()
