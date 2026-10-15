"""Real ASGI, SQLite and filesystem regressions for the repository audit."""
import asyncio
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import base64
import io
import tarfile
from unittest.mock import AsyncMock, Mock, patch
import fastapi as real_fastapi
import fastapi.responses as real_responses
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]


def load(name, path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class Protocol(unittest.TestCase):
    def test_standalone_bundle_matches_current_runtime_files(self):
        source=(ROOT/'run-local.sh').read_text().split('\n__EMBEDDED_CONTEXT_BEGIN__\n',1)[1]
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(source))) as bundle:
            for name in ('Dockerfile','requirements-render.lock','scripts/session-guard.py','scripts/chat-bridge.py',
                         'scripts/git-storage.py','scripts/runtime-dependencies.py',
                         'dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py'):
                self.assertEqual(bundle.extractfile(name).read(),(ROOT/name).read_bytes(),name)

    def test_first_connection_malformed_input_reconnect_and_shutdown(self):
        m=load('audit_bridge','scripts/chat-bridge.py')
        worker="import sys,json\nfor l in sys.stdin:\n m=json.loads(l);print(json.dumps({'id':m['id'],'result':{'ok':True}}),flush=True)"
        m.bridge=m.ChatBridge([sys.executable,'-u','-c',worker])
        app=real_fastapi.FastAPI();m.install_lifespan(app);m.install_lifespan(app)
        @app.websocket('/ws')
        async def socket(ws:real_fastapi.WebSocket):await m.bridge.handle_ws(ws)
        with TestClient(app) as client:
            with client.websocket_connect('/ws') as ws:
                ws.send_text('bad JSON');self.assertEqual(ws.receive_json()['error']['code'],-32700)
                ws.send_json([]);self.assertEqual(ws.receive_json()['error']['code'],-32600)
                ws.send_json({'id':1,'method':'ping'});self.assertTrue(ws.receive_json()['result']['ok'])
            with client.websocket_connect('/ws') as ws:
                ws.send_json({'id':2,'method':'ping'});self.assertEqual(ws.receive_json()['id'],2)
        self.assertIsNotNone(m.bridge.proc.returncode)

    def test_generated_patch_initializes_lifespan_before_handshake(self):
        m=load('audit_patch','scripts/patch-chat-bridge.py')
        source='''import sys
app = FastAPI()
@app.websocket('/api/ws')
async def gateway_ws(ws):
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        return
    if not _ws_client_is_allowed(ws):
        return
    from tui_gateway.ws import handle_ws
    await handle_ws(ws)
'''
        result=m.patch(source)
        self.assertEqual(m.patch(result),result)
        self.assertLess(result.index('install_lifespan(app)'),result.index('async def gateway_ws'))
        self.assertNotIn('app.add_event_handler',result)

    def test_request_cap_and_expiry(self):
        m=load('audit_limits','scripts/chat-bridge.py')
        async def check():
            b=m.ChatBridge();b.proc=Mock(returncode=None);b.proc.stdin.drain=AsyncMock()
            c=Mock();c.send_text=AsyncMock();b.client=c
            with patch.object(m,'MAX_PENDING',2):
                for i in range(3):await b.request({'id':i,'method':'ping'},c)
            self.assertEqual(len(b.pending),2)
            self.assertIn('error',json.loads(c.send_text.call_args.args[0]))
            b.pending={k:(*row[:4],0,row[5]) for k,row in b.pending.items()}
            task=asyncio.create_task(b.expire_pending());await asyncio.sleep(1.05)
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertFalse(b.pending)
        asyncio.run(check())


class Restore(unittest.TestCase):
    def setUp(self):
        self.m=load('audit_storage','scripts/git-storage.py')
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.work=self.root/'work';(self.work/'data').mkdir(parents=True)
        self.home=self.root/'home';self.home.mkdir()
        self.config=self.m.GitConfig('owner/repo','fixture',workdir=self.work)

    def test_directory_symlink_and_manifest_parent_traversal_are_blocked(self):
        outside=self.root/'outside';outside.mkdir();(outside/'file').write_text('original')
        (self.home/'link').symlink_to(outside);(self.work/'data/link').mkdir()
        (self.work/'data/link/file').write_text('backup')
        with self.assertRaises(self.m.GitStateError):self.m._materialize_tree(self.work,self.home,self.config)
        self.assertEqual((outside/'file').read_text(),'original')
        (self.work/self.m.MANIFEST_NAME).write_text(json.dumps({'empty_dirs':['../escape']}))
        with self.assertRaises(self.m.GitStateError):self.m._materialize_tree(self.work,self.home,self.config)
        self.assertFalse((self.root/'escape').exists())

    def test_leaf_symlink_is_replaced_without_following_it(self):
        outside=self.root/'outside';outside.write_text('original')
        (self.home/'file').symlink_to(outside);(self.work/'data/file').write_text('backup')
        self.m._materialize_tree(self.work,self.home,self.config)
        self.assertEqual(outside.read_text(),'original');self.assertFalse((self.home/'file').is_symlink())

    def test_remote_deletion_is_quarantined_unknown_local_file_stays(self):
        (self.home/'deleted').write_text('old');(self.home/'unknown').write_text('local')
        (self.work/'data/kept').write_text('remote');self.config._previous_managed_paths=['deleted','kept']
        self.m._materialize_tree(self.work,self.home,self.config)
        self.assertFalse((self.home/'deleted').exists());self.assertTrue((self.home/'unknown').exists())
        self.assertEqual(next((self.home/'.restore-quarantine').glob('*/deleted')).read_text(),'old')
        for profile in ('render-lite','colab-full'):
            with patch.dict(os.environ,{'HERMES_RUNTIME_PROFILE':profile}):
                self.assertTrue(self.m.is_excluded('.restore-quarantine/batch/deleted'))
        self.m._write_sync_base(self.config,'a'*40,'fingerprint',data_dir=self.home)
        self.assertEqual(self.m._read_sync_base(self.config)['managed_paths'],['kept'])


class Dependencies(unittest.TestCase):
    def test_completion_waiter_preserves_installer_failure(self):
        m=load('audit_deps','scripts/runtime-dependencies.py')
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{},clear=False):
            home=Path(td);run_id=m.prepare_restore(home);stop=threading.Event();results=[]
            thread=threading.Thread(target=lambda:results.append(m.wait_for_restore(home,stop,run_id,5)))
            thread.start();self.assertFalse(m.read(home/'.runtime-dependencies-status.json')['ready'])
            m.atomic(home/'.runtime-dependencies-status.json',{'ready':False,'state':'error','errors':['failed']})
            thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(results[0]['errors'],['failed'])

    def test_removed_packages_are_removed_but_incomplete_restore_keeps_desired_recipe(self):
        m=load('audit_inventory','scripts/runtime-dependencies.py')
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);m.atomic(home/'.runtime-dependencies.json',{'version':1,'python':{'agent':{'old':'1.0'}},'apt':['old'],'npm':{'old':'1.0'}})
            with patch.dict(os.environ,{'HERMES_PYTHON':sys.executable}),patch.object(m,'python_packages',return_value={}),patch.object(m,'command',side_effect=lambda args:Mock(returncode=0,stdout='{}' if args[0]=='npm' else '')):
                result=m.capture(home)
            self.assertEqual(result['python']['agent'],{});self.assertEqual(result['apt'],[]);self.assertEqual(result['npm'],{})
            m.atomic(home/'.runtime-dependencies-status.json',{'ready':False,'state':'error','errors':['failed']})
            with patch.object(m,'_capture',side_effect=AssertionError('Partial inventory must not replace recipe')):m.capture(home)


class Sessions(unittest.TestCase):
    def setUp(self):
        with patch.dict(sys.modules,{'fastapi':real_fastapi,'fastapi.responses':real_responses}):
            self.api=load('audit_chat','dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py')
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.home=Path(temp.name)
        self.api._home=lambda:self.home;self.api._require_session=lambda _:None
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.addCleanup(self.conn.close)
        self.conn.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,parent_session_id TEXT,title TEXT,started_at REAL,ended_at REAL,message_count INTEGER); CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,timestamp REAL,role TEXT,content TEXT,reasoning TEXT); INSERT INTO sessions VALUES('chat','web',NULL,'Old',1,NULL,100);")
        self.conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)',[(i,'chat',i,'user',str(i),'reason '+str(i)) for i in range(100)]);self.conn.commit()
        self.db=Mock(_conn=self.conn,_lock=threading.RLock())
        self.db.get_session=lambda sid:dict(row) if (row:=self.conn.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()) else None
        self.api._session_db=lambda:self.db

    def test_branch_uses_stored_id_and_preserves_source_and_reasoning(self):
        result=self.api.branch_from_message(None,{'session_id':'chat','message_id':'20'})
        rows=self.conn.execute('SELECT content,reasoning FROM messages WHERE session_id=? ORDER BY timestamp,id',(result['session_id'],)).fetchall()
        self.assertEqual(len(rows),21);self.assertEqual(tuple(rows[-1]),('20','reason 20'))
        self.assertIsNone(self.db.get_session('chat')['ended_at'])

    def test_branch_failure_is_atomic(self):
        self.conn.execute("CREATE TRIGGER no_copy BEFORE INSERT ON messages WHEN NEW.session_id != 'chat' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.api.branch_from_message(None,{'session_id':'chat','message_id':'20'})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)

    def test_unfinished_gateway_turn_cannot_be_deleted(self):
        (self.home/'.render-recovery.json').write_text(json.dumps({'lane':{'session_id':'chat','status':'pending'}}))
        with self.assertRaises(real_fastapi.HTTPException) as error:self.api.delete_chat_session(None,'chat')
        self.assertEqual(error.exception.status_code,409);self.db.delete_session.assert_not_called()

    def test_bridge_and_native_owners_cannot_be_deleted(self):
        bridge=types.SimpleNamespace(bridge=types.SimpleNamespace(owns_session=lambda _:True))
        native=types.ModuleType('hermes_cli.active_sessions');native.try_acquire_active_session=lambda **kw:(None,'Already owned')
        for modules in ({'_hermes_chat_bridge':bridge},{'hermes_cli.active_sessions':native}):
            with patch.dict(sys.modules,modules),self.assertRaises(real_fastapi.HTTPException) as error:self.api.delete_chat_session(None,'chat')
            self.assertEqual(error.exception.status_code,409)
        self.db.delete_session.assert_not_called()

    def test_delete_is_idempotent_after_rpc_removed_the_row(self):
        self.api._mutation_guard=lambda *_:nullcontext(self.api._session_guard())
        self.assertTrue(self.api.delete_chat_session(None,'absent')['ok']);self.db.delete_session.assert_not_called()

    def test_empty_search_returns_no_unrelated_recent_rows(self):
        self.api._list_rows=lambda *args:([{'id':'chat','title':'Old'}],False);self.api._search_hits=lambda *args:[]
        self.assertEqual(self.api.list_chat_sessions(None,q='no such phrase')['sessions'],[])
