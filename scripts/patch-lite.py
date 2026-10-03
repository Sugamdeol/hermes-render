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

path = root / "hermes_cli/web_server.py"
old = '@app.websocket("/api/pty")\nasync def pty_ws(ws: WebSocket) -> None:\n'
new = '''_lite_pty_lock = asyncio.Lock()


@app.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    # A browser chat starts Node plus a Python agent. Keep one active on 512 MB.
    if _lite_pty_lock.locked():
        await ws.close(code=4429)
        return
    async with _lite_pty_lock:
        await _lite_pty_ws(ws)


async def _lite_pty_ws(ws: WebSocket) -> None:
'''
text = path.read_text()
assert old in text, "native chat patch no longer matches pinned source"
path.write_text(text.replace(old, new, 1))
