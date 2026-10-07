"""Upgrade only the authenticated chat WS body; keep all access checks."""
import ast
import re
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

class BridgeCompatibilityError(RuntimeError):
    pass


def patch(source):
    tree = ast.parse(source)
    functions = {n.name:n for n in tree.body if isinstance(n,ast.AsyncFunctionDef)}
    routes = [n for n in functions.values() if any(
        isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr=='websocket'
        and d.args and isinstance(d.args[0],ast.Constant) and d.args[0].value=='/api/ws'
        for d in n.decorator_list)]
    handler = routes[0] if len(routes)==1 else functions.get('gateway_ws')
    if handler is None:
        raise BridgeCompatibilityError('No supported /api/ws handler found; keeping the installed chat backend.')
    wrapper = None
    # Older builds put the auth guards inside a helper behind a lock wrapper.
    raw = ast.get_source_segment(source,handler) or ''
    if '_SESSION_TOKEN' not in raw and '_lite_pty_lock' in raw:
        delegates = [n.func.id for n in ast.walk(handler) if isinstance(n,ast.Call)
                     and isinstance(n.func,ast.Name) and n.func.id in functions
                     and n.func.id != handler.name]
        if len(set(delegates))==1:
            wrapper,handler = handler,functions[delegates[0]]
    if len(handler.args.args)!=1:
        raise BridgeCompatibilityError('Unsupported chat handler arguments; keeping the installed chat backend.')
    parameter = handler.args.args[0].arg
    lines = source.splitlines(keepends=True)
    guards = [n for n in handler.body if isinstance(n,ast.If) and
              ('_SESSION_TOKEN' in (ast.get_source_segment(source,n) or '') or
               '_ws_client_is_allowed' in (ast.get_source_segment(source,n) or ''))]
    guard_text = '\n'.join(ast.get_source_segment(source,n) or '' for n in guards)
    if '_SESSION_TOKEN' not in guard_text or '_ws_client_is_allowed' not in guard_text:
        raise BridgeCompatibilityError('Unknown chat authentication guards; keeping the installed chat backend.')
    start = max(n.end_lineno for n in guards)
    tail = '\n'.join(lines[start:handler.end_lineno])
    # Only recognize our bridge and known Hermes WS/stdio implementations.
    if not any(marker in tail for marker in ('tui_gateway', '_hermes_chat_bridge', '_lite_pty_lock')):
        raise BridgeCompatibilityError('Unrecognized chat transport; keeping the installed chat backend.')
    # Do not remove additional access checks after the known auth prefix.
    sensitive = ('_SESSION_TOKEN','_ws_client_is_allowed','hmac.compare_digest','_require_token')
    if any(marker in tail for marker in sensitive):
        raise BridgeCompatibilityError('Additional chat access checks found; keeping the installed chat backend.')
    body = BRIDGE_BODY
    if wrapper is not None:
        body = body[:body.index('    if _lite_pty_lock.locked():')] + '    await module.handle_ws(ws)\n'
    body = re.sub(r'\bws\b',parameter,body)
    lines[start:handler.end_lineno] = [body]
    if wrapper is None and not any(isinstance(n,(ast.Assign,ast.AnnAssign)) and
            '_lite_pty_lock' in (ast.get_source_segment(source,n) or '') for n in tree.body):
        position = min([handler.lineno]+[d.lineno for d in handler.decorator_list])-1
        lines[position:position] = ['import asyncio\n_lite_pty_lock = asyncio.Lock()\n\n']
    result = ''.join(lines)
    compile(result,'web_server.py','exec')
    return result

if __name__ == '__main__':
    path=Path(sys.argv[1]);path.write_text(patch(path.read_text()))
