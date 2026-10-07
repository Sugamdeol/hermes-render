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

MAX_TEXT = 200000
MAX_SESSIONS = 16

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

    def snapshot(self, sid):
        with self.lock:
            state = self.states.get(self.aliases.get(sid, sid))
            return copy.deepcopy(state) if state else None

    def state(self, sid):
        if sid not in self.states:
            if len(self.states) >= MAX_SESSIONS:
                victim = next((k for k,v in self.states.items() if not v['running']), None)
                if victim: self.states.pop(victim)
            self.states[sid] = {'session_id':sid, 'running':False, 'text':'', 'reasoning':'',
                                'user':'', 'timestamp':time.time(), 'truncated':False}
        return self.states[sid]

    async def ensure_worker(self):
        if self.proc and self.proc.returncode is None:
            return
        self.pending.clear()
        self.aliases.clear()
        with self.lock: self.states.clear()
        self.proc = await asyncio.create_subprocess_exec(*self.command, cwd=self.cwd,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    limit=8*1024*1024, start_new_session=True)
        self.reader = asyncio.create_task(self.read_worker(self.proc))
        if not self.reaper or self.reaper.done():
            self.reaper = asyncio.create_task(self.reap_idle())

    async def deliver(self, client, message):
        if client is None or client is not self.client: return
        try:
            await client.send_text(json.dumps(message))
        except Exception:
            if self.client is client: self.client = None

    async def request(self, message, client):
        method, params = message.get('method',''), message.get('params') or {}
        sid = str(params.get('session_id') or '')
        live = self.aliases.get(sid, sid)
        if method == 'session.resume' and live in self.states:
            await self.deliver(client, {'jsonrpc':'2.0','id':message.get('id'),
                  'result':{'session_id':live, 'live_snapshot':self.snapshot(live)}})
            return
        self.serial += 1
        wire_id = 'bridge-' + str(self.serial)
        self.pending[wire_id] = (client, message.get('id'), method, dict(params))
        if method in ('prompt.submit','prompt.background'):
            with self.lock:
                state = self.state(live)
                state.update(running=True, text='', reasoning='', user=str(params.get('text') or '')[-MAX_TEXT:],
                             timestamp=time.time(), truncated=False)
        self.last_active = time.monotonic()
        outgoing = dict(message, id=wire_id)
        self.proc.stdin.write((json.dumps(outgoing)+'\n').encode())
        await self.proc.stdin.drain()

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
                    client, original_id, method, params = pending
                    result = message.get('result') or {}
                    if method == 'session.status':
                        match = re.search(r'^Session ID: (.+)$',str(result.get('output','')),re.M)
                        if match: self.aliases[match.group(1).strip()] = str(params.get('session_id',''))
                    elif method == 'session.resume' and result.get('session_id'):
                        self.aliases[str(params.get('session_id',''))] = str(result['session_id'])
                    if message.get('error') and method.startswith('prompt.'):
                        with self.lock: self.state(str(params.get('session_id','')))['running'] = False
                    message['id'] = original_id
                    await self.deliver(client, message)
                else:
                    self.record(message)
                    await self.deliver(self.client, message)
        finally:
            with self.lock:
                for state in self.states.values(): state['running'] = False
            self.pending.clear()
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
                message = json.loads(raw)
                await self.request(message, ws)
        finally:
            if self.client is ws: self.client = None
            self.last_active = time.monotonic()
            # Deliberately leave the native worker and stream reader alive.

bridge = ChatBridge()
async def handle_ws(ws): await bridge.handle_ws(ws)
def snapshot(sid): return bridge.snapshot(sid)
