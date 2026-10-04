"""Exercise the real native dashboard, provider plugin and browser PTY in CI."""
import asyncio
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

AUTH = "Basic " + base64.b64encode(b"hermes:ci-only-password").decode()
PORT = os.environ.get("SMOKE_PORT", "10000")
CONTAINER = os.environ.get("SMOKE_CONTAINER", "hermes-lite")
BASE = "http://127.0.0.1:" + PORT

def call(path, data=None, method=None, token=None, authenticated=True):
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = AUTH
    if token:
        headers["X-Hermes-Session-Token"] = token
    request = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()

for attempt in range(120):
    try:
        call("/healthz", authenticated=False)
        break
    except (urllib.error.URLError, TimeoutError):
        time.sleep(2)
else:
    raise AssertionError("native dashboard did not become healthy")

try:
    call("/", authenticated=False)
    raise AssertionError("public dashboard must require a password")
except urllib.error.HTTPError as error:
    assert error.code == 401
html = call("/")
token = re.search(r'__HERMES_SESSION_TOKEN__="([^"]+)"', html).group(1)
endpoint = "/api/plugins/render-api-providers/custom-providers"
result = json.loads(call(endpoint, {"name": "ci-custom", "base_url": "https://example.invalid/v1", "api_key": "ci-provider-secret", "api_mode": "chat_completions", "model": "ci-model"}, token=token))
assert result["ok"]
providers = call(endpoint, token=token)
assert "ci-custom" in providers and "ci-provider-secret" not in providers
assert json.loads(call("/api/env", {"key": "CI_SAVED_ENV", "value": "ci-value"}, method="PUT", token=token))["ok"]

async def check_native_chat():
    import websockets
    async with websockets.connect("ws://127.0.0.1:" + PORT + "/api/pty?token=" + token, additional_headers={"Authorization": AUTH}, open_timeout=30) as ws:
        seen = b""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            part = await asyncio.wait_for(ws.recv(), timeout=30)
            seen += part.encode() if isinstance(part, str) else part
            if len(seen) > 200:
                break
        assert len(seen) > 200, "native TUI produced no output"
        assert b"Chat unavailable" not in seen and b"Chat failed" not in seen
        # Opening a second native chat must not launch another Node/Python pair.
        try:
            async with websockets.connect("ws://127.0.0.1:" + PORT + "/api/pty?token=" + token, additional_headers={"Authorization": AUTH}):
                raise AssertionError("second chat was accepted")
        except websockets.exceptions.InvalidStatus as error:
            assert error.response.status_code == 403
        await asyncio.sleep(15)
        peak = subprocess.check_output(["docker", "exec", CONTAINER, "cat", "/sys/fs/cgroup/memory.peak"]).decode().strip()
        print("Native dashboard + one chat peak memory: %.1f MiB" % (int(peak) / 1048576), flush=True)
        # Raise total usage to ~440 MiB without approaching the 512 MiB cap.
        # The guard must close chat gracefully rather than lose the container.
        allocator = "import time; from pathlib import Path; used=int(Path('/sys/fs/cgroup/memory.current').read_text()); payload=bytearray(max(0,440*1048576-used)); time.sleep(8)"
        pressure = subprocess.Popen(["docker", "exec", CONTAINER, "/opt/hermes/.venv/bin/python", "-c", allocator])
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=10)
                except websockets.exceptions.ConnectionClosed as error:
                    assert error.rcvd.code == 1013, error
                    print("Memory pressure closed browser chat gracefully", flush=True)
                    break
            else:
                raise AssertionError("browser chat did not yield under memory pressure")
        finally:
            pressure.wait(timeout=15)

async def check_json_rpc_isolation():
    import websockets
    await asyncio.sleep(1)
    async with websockets.connect("ws://127.0.0.1:" + PORT + "/api/ws?token=" + token, additional_headers={"Authorization": AUTH}, open_timeout=30) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        assert ready["params"]["type"] == "gateway.ready"
        await ws.send("not JSON")
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        assert reply["error"]["code"] == -32700
    print("Native JSON-RPC protocol works in an isolated budgeted worker", flush=True)

asyncio.run(check_native_chat())
asyncio.run(check_json_rpc_isolation())
logs = subprocess.check_output(["docker", "logs", CONTAINER], stderr=subprocess.STDOUT).decode()
assert "budget_alive=True" in logs, "worker budget has no healthy heartbeat"
assert "monitor failed" not in logs, "budget monitor failed during normal container operation"
print("Budget monitor heartbeat stayed healthy", flush=True)
state = json.loads(subprocess.check_output(["docker", "inspect", CONTAINER, "--format", "{{json .State}}"] ))
assert state["Running"] and not state["OOMKilled"]
print("Native dashboard auth, custom provider CRUD, env save, browser chat and container memory-limit boot passed.")
