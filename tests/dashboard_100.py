"""Measure controls-only mode; this does not claim that the agent fits 100 MB."""
import json
import subprocess
import time
import urllib.request

for attempt in range(40):
    try:
        with urllib.request.urlopen("http://127.0.0.1:10002/healthz", timeout=2) as response:
            assert response.status == 200
        break
    except Exception:
        time.sleep(1)
else:
    raise AssertionError("dashboard-only mode did not start within 100 MB")
time.sleep(15)
peak = int(subprocess.check_output(["docker", "exec", "hermes-100-dashboard", "cat", "/sys/fs/cgroup/memory.peak"]))
events = subprocess.check_output(["docker", "exec", "hermes-100-dashboard", "cat", "/sys/fs/cgroup/memory.events"]).decode()
assert "oom_kill 0" in events
print("100 MB dashboard-only peak: %.1f MiB; agent and browser chat NOT tested or enabled." % (peak / 1048576))
