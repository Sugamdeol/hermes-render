"""Upgrade only the authenticated chat WS body; keep all access checks."""
import ast
from pathlib import Path
import sys

BRIDGE_BODY = '''    import importlib.util
    import sys
    module = sys.modules.get('_hermes_chat_bridge')
    if module is None:
        spec = importlib.util.spec_from_file_location('_hermes_chat_bridge', '/opt/render-tools/chat-bridge.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules['_hermes_chat_bridge'] = module
        spec.loader.exec_module(module)
        app.add_event_handler("shutdown", module.bridge.shutdown)
    if _lite_pty_lock.locked():
        await ws.close(code=4429)
        return
    async with _lite_pty_lock:
        await module.handle_ws(ws)
'''

def patch(source):
    tree = ast.parse(source)
    handler = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='gateway_ws')
    lines = source.splitlines(keepends=True)
    start = next(i for i in range(handler.lineno,handler.end_lineno) if lines[i].startswith((
        '    from tui_gateway.ws import handle_ws', '    if _lite_pty_lock.locked():', '    import importlib.util')))
    guards = ''.join(lines[handler.lineno-1:start])
    if '_SESSION_TOKEN' not in guards or '_ws_client_is_allowed' not in guards:
        raise RuntimeError('Unknown chat authentication guards; refusing to modify the bridge')
    lines[start:handler.end_lineno] = [BRIDGE_BODY]
    result = ''.join(lines)
    compile(result,'web_server.py','exec')
    return result

if __name__ == '__main__':
    path=Path(sys.argv[1]);path.write_text(patch(path.read_text()))
