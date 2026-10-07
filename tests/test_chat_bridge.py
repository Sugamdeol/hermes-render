import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('chat_bridge_test',ROOT/'scripts/chat-bridge.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

WORKER=r'''
import json,sys,threading,time
lock=threading.Lock()
def emit(msg):
 with lock: print(json.dumps(msg),flush=True)
def event(kind,payload):emit({'method':'event','params':{'type':kind,'session_id':'live','payload':payload}})
def reply():
 event('message.start',{})
 for i in range(8):
  event('message.delta',{'text':str(i)+' '});time.sleep(.07)
 event('message.complete',{'text':'0 1 2 3 4 5 6 7 '})
for line in sys.stdin:
 msg=json.loads(line);method=msg['method'];result={}
 if method=='session.create':result={'session_id':'live'}
 elif method=='session.status':result={'output':'Session ID: saved\nAgent Running: Yes'}
 elif method=='prompt.submit':threading.Thread(target=reply,daemon=True).start()
 elif method=='session.resume':raise RuntimeError('Must reattach; never create another live session')
 emit({'jsonrpc':'2.0','id':msg['id'],'result':result})
'''
class Client:
 def __init__(self):self.queue=asyncio.Queue();self.messages=[]
 async def accept(self):pass
 async def close(self,code):await self.queue.put(ConnectionError('closed'))
 async def send_text(self,raw):self.messages.append(json.loads(raw))
 async def receive_text(self):
  value=await self.queue.get()
  if isinstance(value,Exception):raise value
  return value
 async def request(self,method,params,rid):await self.queue.put(json.dumps({'id':rid,'method':method,'params':params}))

class RefreshBridge(unittest.IsolatedAsyncioTestCase):
 async def wait_for(self,condition):
  async with asyncio.timeout(5):
   while not condition():await asyncio.sleep(.01)

 async def test_refresh_keeps_worker_partial_reply_and_reconnects_same_turn(self):
  bridge=module.ChatBridge([sys.executable,'-u','-c',WORKER])
  old=Client();task=asyncio.create_task(bridge.handle_ws(old));new=None;newtask=None
  try:
   await old.request('session.create',{},'one')
   await self.wait_for(lambda:any(m.get('id')=='one' for m in old.messages))
   pid=bridge.proc.pid
   await old.request('prompt.submit',{'session_id':'live','text':'Question'},'two')
   await old.request('session.status',{'session_id':'live'},'three')
   await self.wait_for(lambda:bridge.snapshot('saved') and bridge.snapshot('saved')['text'])
   before=bridge.snapshot('saved')['text']
   await old.queue.put(ConnectionError('browser refresh'))
   with self.assertRaises(ConnectionError):await task
   self.assertIsNone(bridge.proc.returncode)
   new=Client();newtask=asyncio.create_task(bridge.handle_ws(new))
   # Reuse an old request id: no stale response may leak across clients.
   await new.request('session.resume',{'session_id':'saved'},'one')
   await self.wait_for(lambda:any(m.get('id')=='one' for m in new.messages))
   result=next(m['result'] for m in new.messages if m.get('id')=='one')
   self.assertEqual(result['session_id'],'live')
   self.assertTrue(result['live_snapshot']['text'].startswith(before))
   self.assertEqual(bridge.proc.pid,pid)
   await self.wait_for(lambda:not bridge.snapshot('saved')['running'])
   self.assertEqual(bridge.snapshot('saved')['text'],'0 1 2 3 4 5 6 7 ')
   self.assertTrue(any(m.get('params',{}).get('type')=='message.complete' for m in new.messages))
  finally:
   if newtask and not newtask.done():
    await new.queue.put(ConnectionError('closed'))
    with self.assertRaises(ConnectionError):await newtask
   if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
   await bridge.shutdown()
   if bridge.reader:await asyncio.gather(bridge.reader,return_exceptions=True)
   if bridge.reaper:bridge.reaper.cancel();await asyncio.gather(bridge.reaper,return_exceptions=True)

 def test_live_snapshot_is_bounded_and_detached(self):
  bridge=module.ChatBridge()
  bridge.record({'params':{'session_id':'live','type':'message.delta','payload':{'text':'x'*300000}}})
  snapshot=bridge.snapshot('live')
  self.assertEqual(len(snapshot['text']),module.MAX_TEXT)
  self.assertTrue(snapshot['truncated'])
  snapshot['text']='changed'
  self.assertNotEqual(bridge.snapshot('live')['text'],'changed')

class RefreshFrontend(unittest.TestCase):
 def test_live_snapshot_merges_without_duplicate_messages(self):
  import subprocess
  subprocess.run(['node',str(ROOT/'tests/chat_refresh_probe.cjs'),str(ROOT/'dashboard-plugins/hermes-chat-dashboard/dashboard/bundle/index.js')],check=True)

class BridgePatch(unittest.TestCase):
 def test_patch_keeps_authentication_and_is_idempotent(self):
  import runpy
  patch=runpy.run_path(str(ROOT/'scripts/patch-chat-bridge.py'))['patch']
  source='''async def gateway_ws(ws):
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        return
    if not _ws_client_is_allowed(ws):
        return
    from tui_gateway.ws import handle_ws

    await handle_ws(ws)
'''
  fixed=patch(source)
  self.assertEqual(fixed.split('    import importlib.util')[0],source.split('    from tui_gateway.ws')[0])
  self.assertEqual(patch(fixed),fixed)
  self.assertIn('app.add_event_handler("shutdown", module.bridge.shutdown)',fixed)
