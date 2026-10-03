"""Exercise the real native dashboard, provider plugin and browser PTY in CI."""
import asyncio
import base64
import json
import re
import subprocess
import time
import urllib.error
import urllib.request

AUTH = "Basic " + base64.b64encode(b"hermes:ci-only-password").decode()
BASE = "http://127.0.0.1:10000"

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
result = json.loads(call(endpoint, {"name": "CI custom", "base_url": "https://example.invalid/v1", "api_key": "ci-provider-secret", "api_mode": "openai", "model": "ci-model"}, token=token))
assert result["ok"]
providers = call(endpoint, token=token)
assert "CI custom" in providers and "ci-provider-secret" not in providers
assert json.loads(call("/api/env", {"key": "CI_SAVED_ENV", "value": "ci-value"}, method="PUT", token=token))["ok"]

async def check_native_chat():
    import websockets
    async with websockets.connect("ws://127.0.0.1:10000/api/pty?token=" + token, additional_headers={"Authorization": AUTH}, open_timeout=30) as ws:
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
            async with websockets.connect("ws://127.0.0.1:10000/api/pty?token=" + token, additional_headers={"Authorization": AUTH}):
                raise AssertionError("second chat was accepted")
        except websockets.exceptions.InvalidStatus as error:
            assert error.response.status_code == 403
        await asyncio.sleep(15)
        peak = subprocess.check_output(["docker", "exec", "hermes-lite", "cat", "/sys/fs/cgroup/memory.peak"]).decode().strip()
        print("Native dashboard + one chat peak memory: %.1f MiB" % (int(peak) / 1048576), flush=True)

asyncio.run(check_native_chat())
state = json.loads(subprocess.check_output(["docker", "inspect", "hermes-lite", "--format", "{{json .State}}"] ))
assert state["Running"] and not state["OOMKilled"]
print("Native dashboard auth, custom provider CRUD, env save, browser chat and 512 MB boot passed.")
