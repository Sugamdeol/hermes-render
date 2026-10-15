"""One isolated native chat worker that survives browser refreshes.

Never replay a prompt. Reattach session.resume to the existing live session,
keep a bounded live snapshot, and reap disconnected workers only when idle.
"""
import asyncio
import copy
import json
import os
import re
import signal
import sys
import threading
import time
import runpy
from pathlib import Path
from contextlib import asynccontextmanager
from starlette.websockets import WebSocketDisconnect

MAX_TEXT = 200000
MAX_SESSIONS = 16
MAX_PENDING = 128
MAX_REQUEST_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 90
_guard = runpy.run_path(str(Path(__file__).with_name('session-guard.py')))

class ChatBridge:
    def __init__(self, command=None):
        self.cwd = None if command else "/opt/hermes"
        self.command = command or [sys.executable, '/opt/render-tools/agent-budget.py', 'run', 'chat',
                                   sys.executable, '-u', '-m', 'tui_gateway.entry']
        self.proc = None
        self.client = None
        self.serial = 0
        self.pending = {}
        self.aliases = {}
        self.states = {}
        self.lock = threading.RLock()
        self.last_active = time.monotonic()
        self.reader = None
        self.reaper = None
        self.expirer = None
        self.owners = set()

    def owns_session(self, sid):
        with self.lock:
            return bool(self.proc and self.proc.returncode is None and
                        (sid in self.owners or sid in self.aliases or sid in self.states))

    def snapshot(self, sid):
        with self.lock:
            state = self.states.get(self.aliases.get(sid, sid))
            return copy.deepcopy(state) if state else None

    def state(self, sid):
        if sid not in self.states:
            if len(self.states) >= MAX_SESSIONS:
                victim = next((k for k,v in self.states.items() if not v['running']), None)
                if victim: self.states.pop(victim)
                else: raise RuntimeError('Live session snapshot limit reached')
            self.states[sid] = {'session_id':sid, 'running':False, 'text':'', 'reasoning':'',
                                'user':'', 'timestamp':time.time(), 'truncated':False}
        return self.states[sid]

    async def ensure_worker(self):
        if self.proc and self.proc.returncode is None:
            return
        self.pending.clear()
        self.aliases.clear()
        self.owners.clear()
        with self.lock: self.states.clear()
        self.proc = await asyncio.create_subprocess_exec(*self.command, cwd=self.cwd,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    limit=8*1024*1024, start_new_session=True)
        self.reader = asyncio.create_task(self.read_worker(self.proc))
        if not self.reaper or self.reaper.done():
            self.reaper = asyncio.create_task(self.reap_idle())
        if not self.expirer or self.expirer.done():
            self.expirer = asyncio.create_task(self.expire_pending())

    async def error(self, client, rid, code, message):
        await self.deliver(client, {'jsonrpc':'2.0', 'id':rid,
                                  'error':{'code':code, 'message':message}})

    async def expire_pending(self):
        while self.proc and self.proc.returncode is None:
            await asyncio.sleep(1)
            now = time.monotonic()
            for wire_id, row in list(self.pending.items()):
                if now >= row[4]:
                    self.pending.pop(wire_id, None)
                    await self.error(row[0], row[1], -32000, 'Worker request timed out')

    async def deliver(self, client, message):
        if client is None or client is not self.client: return
        try:
            await client.send_text(json.dumps(message))
        except Exception:
            if self.client is client: self.client = None

    async def request(self, message, client):
        if (not isinstance(message, dict) or message.get('jsonrpc', '2.0') != '2.0'
                or not isinstance(message.get('method'), str)
                or not isinstance(message.get('params', {}), dict)
                or isinstance(message.get('id'), (dict, list, bool))):
            await self.error(client, message.get('id') if isinstance(message, dict) else None,
                             -32600, 'Invalid Request')
            return
        if len(self.pending) >= MAX_PENDING:
            await self.error(client, message.get('id'), -32000, 'Too many outstanding requests')
            return
        method, params = message.get('method',''), message.get('params') or {}
        sid = str(params.get('session_id') or '')
        live = self.aliases.get(sid, sid)
        if method == 'session.resume' and live in self.states:
            await self.deliver(client, {'jsonrpc':'2.0','id':message.get('id'),
                  'result':{'session_id':live, 'live_snapshot':self.snapshot(live)}})
            return
        self.serial += 1
        wire_id = 'bridge-' + str(self.serial)
        if method in ('session.create', 'session.resume') and len({self.aliases.get(key,key) for key in self.owners}) >= MAX_SESSIONS and not self.owns_session(sid):
            await self.error(client, message.get('id'), -32000, 'Close a live session before opening another')
            return
        had_owner = self.owns_session(sid)
        def admit():
            with _guard['mutation_lock']():
                home = Path(os.environ.get('HERMES_HOME','/opt/data'))
                keys = {sid} | {key for key,value in self.aliases.items() if value == live}
                if sid and any(_guard['deleted'](home,key) for key in keys):
                    raise RuntimeError('This conversation was deleted; start a new session')
                with self.lock:
                    if method == 'session.resume' and sid:
                        self.owners.add(sid)
                    if method in ('prompt.submit','prompt.background'):
                        state = self.state(live)
                        state.update(running=True, text='', reasoning='', user=str(params.get('text') or '')[-MAX_TEXT:],
                                     timestamp=time.time(), truncated=False)
        if method in ('session.resume','prompt.submit','prompt.background'):
            try:
                await asyncio.to_thread(admit)
            except (RuntimeError, OSError, ValueError):
                await self.error(client, message.get('id'), -32000, 'Session is unavailable; refresh or start a new chat')
                return
        self.pending[wire_id] = (client, message.get('id'), method, dict(params),
                                 time.monotonic() + REQUEST_TIMEOUT, had_owner)
        self.last_active = time.monotonic()
        outgoing = dict(message, id=wire_id)
        try:
            self.proc.stdin.write((json.dumps(outgoing)+'\n').encode())
            await self.proc.stdin.drain()
        except Exception:
            self.pending.pop(wire_id, None)
            await self.error(client, message.get('id'), -32000, 'Worker is unavailable')

    def record(self, message):
        params = message.get('params') or {}
        sid = str(params.get('session_id') or '')
        event = params.get('type','')
        payload = params.get('payload') or {}
        if not sid: return
        with self.lock:
            state = self.state(sid)
            if event == 'message.start':
                state.update(running=True, text='', reasoning='', truncated=False)
            elif event == 'message.delta':
                text = state['text'] + str(payload.get('text') or payload.get('content') or '')
                state['truncated'] = state['truncated'] or len(text)>MAX_TEXT
                state['text'] = text[-MAX_TEXT:]
                state['reasoning'] = (state['reasoning'] + str(payload.get('reasoning') or payload.get('thinking') or ''))[-MAX_TEXT:]
            elif event == 'message.complete':
                state['running'] = False
                if payload.get('text') is not None: state['text'] = str(payload['text'])[-MAX_TEXT:]
            elif event in ('error','message.error'):
                state['running'] = False
        self.last_active = time.monotonic()

    async def read_worker(self, proc):
        try:
            while True:
                line = await proc.stdout.readline()
                if not line: break
                try: message = json.loads(line)
                except (ValueError, UnicodeError): continue
                wire_id = message.get('id')
                if wire_id is not None:
                    pending = self.pending.pop(wire_id, None)
                    if not pending: continue
                    client, original_id, method, params, _, had_owner = pending
                    result = message.get('result') or {}
                    if method == 'session.status':
                        match = re.search(r'^Session ID: (.+)$',str(result.get('output','')),re.M)
                        if match:
                            key = match.group(1).strip()
                            self.aliases[key] = str(params.get('session_id',''))
                            self.owners.add(key)
                    elif method == 'session.create' and result.get('session_id'):
                        self.owners.add(str(result['session_id']))
                    elif method == 'session.resume' and result.get('session_id'):
                        self.aliases[str(params.get('session_id',''))] = str(result['session_id'])
                        self.owners.update((str(params.get('session_id','')), str(result['session_id'])))
                    elif method == 'session.close' and not message.get('error'):
                        closing = str(params.get('session_id',''))
                        live = self.aliases.get(closing, closing)
                        keys = {closing, live} | {k for k,v in self.aliases.items() if v == live}
                        for key in keys:
                            self.owners.discard(key); self.states.pop(key, None); self.aliases.pop(key, None)
                    if message.get('error') and method.startswith('prompt.'):
                        with self.lock: self.state(str(params.get('session_id','')))['running'] = False
                    if message.get('error') and method == 'session.resume' and not had_owner:
                        self.owners.discard(str(params.get('session_id','')))
                    message['id'] = original_id
                    await self.deliver(client, message)
                else:
                    self.record(message)
                    await self.deliver(self.client, message)
        finally:
            with self.lock:
                for state in self.states.values(): state['running'] = False
            self.pending.clear()
            self.owners.clear()
            client = self.client
            self.client = None
            if client:
                try: await client.close(code=1013)
                except Exception: pass
            try:
                await asyncio.wait_for(proc.wait(), .2)
            except asyncio.TimeoutError:
                await self.shutdown()

    async def shutdown(self):
        proc = self.proc
        if proc and proc.returncode is None:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: await asyncio.wait_for(proc.wait(),5)
            except asyncio.TimeoutError:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await proc.wait()
        current = asyncio.current_task()
        for task in (self.expirer, self.reaper):
            if task and task is not current:
                task.cancel()
        self.pending.clear()
        self.owners.clear()

    async def reap_idle(self):
        while self.proc and self.proc.returncode is None:
            await asyncio.sleep(30)
            if self.client is None and not any(s['running'] for s in self.states.values()) and time.monotonic()-self.last_active>120:
                await self.shutdown()
                return

    async def handle_ws(self, ws):
        if self.client is not None:
            await ws.close(code=4429)
            return
        await ws.accept()
        self.client = ws
        try:
            await self.ensure_worker()
            while True:
                raw = await ws.receive_text()
                if len(raw.encode()) > MAX_REQUEST_BYTES:
                    await self.error(ws, None, -32600, 'Request exceeds size limit')
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    await self.error(ws, None, -32700, 'Parse error')
                    continue
                await self.request(message, ws)
        except WebSocketDisconnect:
            pass
        finally:
            if self.client is ws: self.client = None
            self.pending = {k:(None, *row[1:]) if row[0] is ws else row
                            for k,row in self.pending.items()}
            self.last_active = time.monotonic()
            # Deliberately leave the native worker and stream reader alive.

bridge = ChatBridge()
async def handle_ws(ws): await bridge.handle_ws(ws)
def snapshot(sid): return bridge.snapshot(sid)

def install_lifespan(app):
    if getattr(app.state, '_render_bridge_lifespan', False):
        return
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original(application) as state:
                yield state
        finally:
            await bridge.shutdown()
    app.router.lifespan_context = lifespan
    app.state._render_bridge_lifespan = True
