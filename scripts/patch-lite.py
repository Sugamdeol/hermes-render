"""Small, exact patches for the pinned Hermes release. Fail closed on upgrades."""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes")
path = root / "gateway/run.py"
old = "_AGENT_CACHE_MAX_SIZE = 128\n_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h\n"
new = '''_AGENT_CACHE_MAX_SIZE = max(1, int(os.environ.get("HERMES_AGENT_CACHE_MAX_SIZE", "2")))
_AGENT_CACHE_IDLE_TTL_SECS = max(1.0, float(os.environ.get("HERMES_AGENT_CACHE_IDLE_TTL_SECONDS", "120")))
'''
text = path.read_text()
assert old in text, "gateway cache patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# JSON-RPC clients must also run their agent outside the dashboard process.
# Keep the native protocol by forwarding to Hermes' existing stdio backend.
path = root / "hermes_cli/web_server.py"
text = path.read_text()
old = "    from tui_gateway.ws import handle_ws\n\n    await handle_ws(ws)"
new = '''    if _lite_pty_lock.locked():
        await ws.close(code=4429)
        return
    async with _lite_pty_lock:
        await ws.accept()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '/opt/render-tools/agent-budget.py', 'run', 'chat',
            sys.executable, '-m', 'tui_gateway.entry',
            cwd=str(PROJECT_ROOT), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )
        async def output():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                await ws.send_text(line.decode('utf-8').rstrip('\\n'))
        async def input_loop():
            while True:
                line = await ws.receive_text()
                proc.stdin.write((line + '\\n').encode('utf-8'))
                await proc.stdin.drain()
        tasks = [asyncio.create_task(output()), asyncio.create_task(input_loop())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            try:
                await ws.close(code=1013)
            except Exception:
                pass
'''
assert old in text, "JSON-RPC isolation patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

path = root / "hermes_cli/web_server.py"
old = '@app.websocket("/api/pty")\nasync def pty_ws(ws: WebSocket) -> None:\n'
new = '''_lite_pty_lock = asyncio.Lock()


@app.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    # Reserve room for Node plus Python before admitting another agent.
    try:
        from hermes_cli.render_memory import container_memory
        _, used = container_memory()
    except (OSError, ValueError):
        used = 0
    if used > 320 * 1024 * 1024:
        await ws.close(code=4429)
        return
    if _lite_pty_lock.locked():
        await ws.close(code=4429)
        return
    async with _lite_pty_lock:
        async def memory_guard():
            while True:
                await asyncio.sleep(1)
                try:
                    _, used = container_memory()
                except (OSError, ValueError):
                    continue
                if used > 400 * 1024 * 1024:
                    try:
                        await ws.send_text("\\r\\nRender memory limit approaching; chat closed. Reopen after the active Telegram task finishes.\\r\\n")
                        await ws.close(code=1013)
                    except Exception:
                        pass
                    return
        guard = asyncio.create_task(memory_guard())
        try:
            await _lite_pty_ws(ws)
        finally:
            guard.cancel()
            try:
                await guard
            except asyncio.CancelledError:
                pass


async def _lite_pty_ws(ws: WebSocket) -> None:
'''
text = path.read_text()
assert old in text, "native chat patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# A shorter TTL is ineffective when the native watcher wakes only every 5 min.
path = root / "gateway/run.py"
text = path.read_text()
old = "async def _session_expiry_watcher(self, interval: int = 300):"
assert old in text, "cache sweep patch no longer matches pinned source"
path.write_text(text.replace(old, "async def _session_expiry_watcher(self, interval: int = 30):", 1))

# The PTY leader spawns Node and a Python backend. Killing only that leader
# can leave its descendants consuming RAM after every browser reconnect.
path = root / "hermes_cli/pty_bridge.py"
text = path.read_text()
old = """            if not self._proc.isalive():
                break
            try:
                self._proc.kill(sig)
            except Exception:
                pass
"""
new = """            try:
                # PtyProcess.spawn creates a new session/process group.
                # Signal it even when the leader has already exited.
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
"""
assert old in text, "PTY cleanup patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# All native browser agents (including descendants) enter the shared budget.
path = root / "hermes_cli/pty_bridge.py"
text = path.read_text()
old = "            list(argv),\n            cwd=cwd,"
new = "            [sys.executable, '/opt/render-tools/agent-budget.py', 'run', 'chat', *argv],\n            cwd=cwd,"
assert old in text, "PTY budget wrapper patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

path = root / "hermes_cli/web_server.py"
text = path.read_text()
old = '    cmd = [sys.executable, "-m", "hermes_cli.main", *subcommand]'
new = old + "\n    if name == 'gateway-restart':\n        cmd = [sys.executable, '/opt/render-tools/agent-budget.py', 'run', 'gateway', *cmd]"
assert old in text, "gateway action budget patch no longer matches pinned source"
text = text.replace(old, new, 1)
old = "            if chunk is None:  # EOF\n                return"
new = "            if chunk is None:  # EOF\n                try:\n                    await ws.close(code=1013)\n                except Exception:\n                    pass\n                return"
assert old in text, "PTY EOF patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))

# Apply allocation limits inside Python agent backends, never the Node launcher.
(root / "hermes_cli/render_memory.py").write_text(Path(__file__).with_name("worker-memory.py").read_text())
path = root / "gateway/run.py"
text = path.read_text()
old = "    def __init__(self, config: Optional[GatewayConfig] = None):\n"
new = old + "        from hermes_cli.render_memory import apply_worker_limit\n        apply_worker_limit()\n"
assert old in text, "gateway allocation limit patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))
path = root / "tui_gateway/entry.py"
text = path.read_text()
old = "from tui_gateway import server\n"
new = "from hermes_cli.render_memory import apply_worker_limit\napply_worker_limit()\n\n" + old
assert old in text, "TUI allocation limit patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))
