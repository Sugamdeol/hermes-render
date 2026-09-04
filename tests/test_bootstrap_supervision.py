"""Supervision behaviour of scripts/bootstrap.sh.

Runs the real bootstrap script in a throwaway sandbox (fake gosu/entrypoint/
hermes binaries, git backend off) and asserts the crash-recovery contract
that production depends on:

  1. an unexpected gateway exit is restarted IN PLACE (gateway-only when the
     dashboard side-process is still alive) instead of recycling the
     container through a full restore+boot cycle;
  2. the restart budget is bounded — after HERMES_ENTRYPOINT_RESTARTS the
     wrapper exits with the child's status so Render does a full restart;
  3. SIGTERM to the process group (what tini -g does on container stop) is a
     clean stop: no restart attempt, exit status preserved, so the shutdown
     flush path still runs.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"

FAKE_ENTRYPOINT = """#!/bin/sh
# Scenario-driven fake upstream entrypoint.
COUNT_FILE="{count}"
SCENARIO="{scenario}"
n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$COUNT_FILE"
case "$SCENARIO" in
  spawn-then-crash)
    if [ "$n" -eq 1 ]; then
      # First run: background a fake dashboard (argv matches the
      # "hermes dashboard" needle), then die like an OOM-killed gateway.
      bash -c 'exec -a "/opt/hermes/.venv/bin/hermes dashboard --host 0.0.0.0 --port 10000 --no-open --insecure" sleep 600' &
      sleep 0.3
      exit 137
    fi
    exit 137
    ;;
  always-crash)
    exit 137
    ;;
  healthy)
    sleep 600
    ;;
esac
exit 0
"""

FAKE_HERMES = """#!/bin/sh
# Fake `hermes` binary: gateway + dashboard subcommands for supervision tests.
COUNT_FILE="{count}"
case "${1:-}" in
  dashboard)
    n=$(cat "$COUNT_FILE.dash" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$COUNT_FILE.dash"
    echo "dash-up"
    sleep 600
    ;;
  gateway)
    n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$COUNT_FILE"
    if [ "$n" -ge 2 ]; then
      sleep 600   # second run stays up
    fi
    exit 137
    ;;
esac
exit 0
"""

FAKE_GOSU = "#!/bin/sh\n# real gosu is `gosu USER CMD...`; drop the user and exec the rest\nshift\nexec \"$@\"\n"


class BootstrapSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="hcd-boot-sup-"))
        self.bin_dir = self.sandbox / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "gosu").write_text(FAKE_GOSU)
        os.chmod(self.bin_dir / "gosu", 0o755)
        self.data_dir = self.sandbox / "data"
        self.data_dir.mkdir()
        # stubs for the helper paths bootstrap would use in the image
        patcher = self.bin_dir / "patch-config"
        patcher.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(patcher, 0o755)
        self.ep_runs = self.sandbox / "entrypoint.runs"
        self.gw_runs = self.sandbox / "gateway.runs"

    def tearDown(self):
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def _write_script(self, path: Path, body: str):
        path.write_text(body)
        os.chmod(path, 0o755)

    def _launch(self, scenario: str, extra_env: dict | None = None):
        entrypoint = self.sandbox / "fake-entrypoint.sh"
        hermes_bin = self.sandbox / "fake-hermes"
        self._write_script(
            entrypoint,
            FAKE_ENTRYPOINT.replace("{count}", str(self.ep_runs)).replace("{scenario}", scenario),
        )
        self._write_script(
            hermes_bin,
            FAKE_HERMES.replace("{count}", str(self.gw_runs)),
        )

        env = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "HOME": str(self.sandbox),
            "HERMES_HOME": str(self.data_dir),
            "HERMES_PATCHER": str(self.bin_dir / "patch-config"),
            "HERMES_GIT_SYNC": str(self.bin_dir / "patch-config"),
            "HERMES_PLUGINS_SRC": str(self.sandbox / "no-plugins"),
            "HERMES_UPSTREAM_ENTRYPOINT": str(entrypoint),
            "HERMES_BIN": str(hermes_bin),
            "HERMES_ENTRYPOINT_RESTARTS": "5",
            "HERMES_MEMWATCH": "0",
            # keep the test fast: 1s backoff steps still exercise the loop
            "HERMES_SHUTDOWN_FLUSH_SECONDS": "1",
        }
        env.pop("RENDER_EXTERNAL_URL", None)
        env.pop("GIT_STATE_REPO", None)
        env.pop("GIT_STATE_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env.pop("HERMES_DASHBOARD", None)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.Popen(
            ["/bin/sh", str(BOOTSTRAP), "gateway", "run"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, like tini -g
        )
        return proc

    def _wait_for(self, predicate, timeout=30.0, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise AssertionError(f"timed out waiting for {message}")

    def _stop_group(self, proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return proc.wait(timeout=10)

    def test_gateway_crash_restarts_in_place_when_dashboard_alive(self):
        """A crashed gateway is restarted directly while the dashboard lives."""
        proc = self._launch("spawn-then-crash")
        try:
            self._wait_for(
                lambda: (self.gw_runs.exists()
                         and self.gw_runs.read_text().strip() == "2"),
                timeout=40,
                message="gateway to be restarted twice",
            )
            output = b""
            # let it settle, then stop like a container shutdown
            time.sleep(0.5)
        finally:
            code = self._stop_group(proc)

        # entrypoint ran once; the gateway was restarted directly (fake hermes
        # gateway subcommand ran twice: crash, then up)
        self.assertEqual(self.ep_runs.read_text().strip(), "1")
        self.assertEqual(self.gw_runs.read_text().strip(), "2")
        self.assertNotEqual(code, 0)

    def test_exhausted_restart_budget_exits_for_full_container_restart(self):
        """After HERMES_ENTRYPOINT_RESTARTS crashes the wrapper gives up."""
        proc = self._launch("always-crash", {"HERMES_ENTRYPOINT_RESTARTS": "1"})
        try:
            code = proc.wait(timeout=60)
            output = proc.stdout.read().decode() if proc.stdout else ""
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        self.assertEqual(code, 137)
        self.assertEqual(self.ep_runs.read_text().strip(), "2")  # initial + 1 restart
        self.assertIn("giving up", output)
        self.assertIn("status 137", output)
        self.assertIn("OOM", output)

    def test_sigterm_is_clean_stop_without_restart(self):
        """tini -g's group TERM stops the wrapper without a restart attempt."""
        proc = self._launch("healthy")
        try:
            self._wait_for(
                lambda: self.ep_runs.exists() and self.ep_runs.read_text().strip() == "1",
                timeout=15,
                message="entrypoint to start",
            )
            time.sleep(0.5)
        finally:
            code = self._stop_group(proc)
            try:
                output = proc.stdout.read().decode() if proc.stdout else ""
            except ValueError:
                output = ""

        self.assertEqual(code, 143)  # 128 + SIGTERM
        self.assertEqual(self.ep_runs.read_text().strip(), "1")
        self.assertNotIn("restarting", output)
        self.assertIn("gateway exited (status 143", output)


    def test_dashboard_watchdog_restarts_dead_dashboard(self):
        """A dead dashboard side-process is restarted by the health loop."""
        proc = self._launch(
            "healthy",
            {
                "HERMES_DASHBOARD": "1",
                "HERMES_HEALTH_INTERVAL_SECONDS": "1",
                "HERMES_HEALTH_GRACE_SECONDS": "2",
                "HERMES_DASHBOARD_PORT": "10000",
            },
        )
        dash_counter = self.sandbox / "gateway.runs.dash"
        try:
            # the fake entrypoint never starts a dashboard; after the 2s
            # grace the health loop must start one itself
            self._wait_for(
                lambda: dash_counter.exists() and dash_counter.read_text().strip() == "1",
                timeout=20,
                message="watchdog to start the dashboard",
            )
            time.sleep(2.5)  # one more tick: must NOT start a second copy
            self.assertEqual(dash_counter.read_text().strip(), "1")
        finally:
            code = self._stop_group(proc)
            try:
                output = proc.stdout.read().decode() if proc.stdout else ""
            except ValueError:
                output = ""
        self.assertIn("starting dashboard on 0.0.0.0:10000 (supervised)", output)
        self.assertIn("[dashboard] dash-up", output)


if __name__ == "__main__":
    unittest.main()
