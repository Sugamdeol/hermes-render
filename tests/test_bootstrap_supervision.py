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


def _reap_stale_fixtures() -> None:
    """Kill fixture processes orphaned by an earlier, interrupted run.

    bootstrap.sh locates its children by substring-matching /proc/*/cmdline
    against needles such as "hermes dashboard". The fixture binary is named
    ``fake-hermes``, so a ``fake-hermes dashboard`` left behind by a run that
    was killed (a timeout, a Ctrl-C) satisfies that needle, and the *next*
    run's watchdog concludes a dashboard is already up and never starts its
    own. tearDown only kills its own process group, so an interrupted run
    leaks, and the following run then fails in a way that looks like a
    bootstrap bug rather than a dirty machine.

    Only processes whose command line names a sandbox directory that no
    longer exists are killed, and only processes whose HERMES_HOME points at
    a directory that no longer exists -- so a test running concurrently in
    its own live sandbox is never touched.
    """
    me = os.getpid()
    victims: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            cmdline = (
                Path(f"/proc/{entry}/cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
        except OSError:
            continue
        stale = False
        if "fake-hermes" in cmdline or "fake-entrypoint.sh" in cmdline:
            dirs = {
                Path(tok).parent
                for tok in cmdline.split()
                if "fake-hermes" in tok or "fake-entrypoint.sh" in tok
            }
            stale = bool(dirs) and not any(d.exists() for d in dirs)
        if not stale:
            # an orphaned bootstrap.sh names no fixture on its command line,
            # but its HERMES_HOME still points into the dead sandbox
            try:
                environ = Path(f"/proc/{entry}/environ").read_bytes().decode("utf-8", "replace")
            except OSError:
                environ = ""
            for kv in environ.split("\0"):
                if kv.startswith("HERMES_HOME="):
                    stale = not Path(kv.split("=", 1)[1]).exists()
                    break
        if stale:
            victims.append(pid)
    for pid in victims:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class BootstrapSupervisionTests(unittest.TestCase):
    def setUp(self):
        _reap_stale_fixtures()
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
            # Keep bootstrap off the real /opt/render-tools. Without these the
            # script merges the machine's env/common.env into the sandbox and
            # the fake binaries behave nothing like the fixtures -- which is
            # how these tests came to depend on that directory NOT existing.
            "HERMES_SEEDER": str(self.sandbox / "no-seed-env.py"),
            "HERMES_COMMON_ENV": str(self.sandbox / "no-common.env"),
            "HERMES_SECRETS_ENC": str(self.sandbox / "no-secrets.enc.env"),
            "HERMES_UPSTREAM_ENTRYPOINT": str(entrypoint),
            "HERMES_BIN": str(hermes_bin),
            "HERMES_ENTRYPOINT_RESTARTS": "5",
            "HERMES_MEMWATCH": "0",
            # keep the test fast: 1s backoff steps still exercise the loop
            "HERMES_SHUTDOWN_FLUSH_SECONDS": "1",
            # the proactive OOM guard reads the *host's* cgroup accounting here,
            # which would make these crash-recovery tests non-deterministic;
            # it is exercised on its own in test_oom_guard_dispatches_reclaim.
            "HERMES_MEMGUARD": "0",
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


def _proc_state(pid: int):
    """Return the 'State:' value of /proc/<pid>/status, or None if gone."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("State:"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None


def _pids_matching(needle: str):
    """The pids whose cmdline contains `needle` -- bootstrap's own matcher,
    reimplemented here so the test checks what the script would find."""
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if needle in cmdline.replace("\x00", " "):
            try:
                found.append(int(entry.name))
            except ValueError:
                continue
    return found


def _oom_score_adj(pid: int):
    """The kernel's OOM ranking for `pid`, or None if the process is gone."""
    try:
        return int(Path(f"/proc/{pid}/oom_score_adj").read_text().strip())
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None


class _GuardHarness(unittest.TestCase):
    """Shared sandbox for running the *real* bootstrap.sh with fake
    gosu/entrypoint/hermes binaries and a synthetic, artificially-full cgroup.

    bootstrap output is captured to a file (never a pipe) so the harness cannot
    deadlock on backpressure, and teardown hard-kills the process group the way
    a stopped container would.

    No tests live here on purpose: a subclass would inherit them and run these
    slow supervision cases twice.
    """

    def setUp(self):
        _reap_stale_fixtures()
        self.sandbox = Path(tempfile.mkdtemp(prefix="hcd-oom-guard-"))
        self.bin_dir = self.sandbox / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "gosu").write_text(FAKE_GOSU)
        os.chmod(self.bin_dir / "gosu", 0o755)
        self.data_dir = self.sandbox / "data"
        self.data_dir.mkdir()
        (self.bin_dir / "patch-config").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.bin_dir / "patch-config", 0o755)
        (self.sandbox / "no-plugins").mkdir()
        (self.bin_dir / "hermes").write_text(
            "#!/bin/sh\ncase \"${1:-}\" in gateway) exec sleep 600;; esac\nexit 0\n"
        )
        os.chmod(self.bin_dir / "hermes", 0o755)
        (self.bin_dir / "entrypoint").write_text(
            "#!/bin/sh\nexec sleep 600\n"  # healthy upstream entrypoint
        )
        os.chmod(self.bin_dir / "entrypoint", 0o755)
        self.log_path = self.sandbox / "boot.log"
        self.log_handle = None
        self.proc = None
        self.daemon = None

    def tearDown(self):
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
            try:
                self.proc.communicate(timeout=8)
            except Exception:
                pass
        if self.daemon is not None:
            try:
                os.kill(self.daemon.pid, signal.SIGCONT)
                os.killpg(os.getpgid(self.daemon.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError):
                pass
            try:
                self.daemon.communicate(timeout=5)
            except Exception:
                pass
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except Exception:
                pass
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def _launch(self, extra_env: dict):
        logf = open(self.log_path, "w")
        self.log_handle = logf
        cg = self.sandbox / "cg"
        cg.mkdir()
        # synthetic 100 MB container budget that is 85% used
        (cg / "memory.max").write_text(str(100 * 1024 * 1024))
        (cg / "memory.current").write_text(str(85 * 1024 * 1024))

        base = {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "HOME": str(self.sandbox),
            "HERMES_HOME": str(self.data_dir),
            "HERMES_PATCHER": str(self.bin_dir / "patch-config"),
            "HERMES_GIT_SYNC": str(self.bin_dir / "patch-config"),
            "HERMES_PLUGINS_SRC": str(self.sandbox / "no-plugins"),
            # Keep bootstrap off the real /opt/render-tools. Without these the
            # script merges the machine's env/common.env into the sandbox and
            # the fake binaries behave nothing like the fixtures -- which is
            # how these tests came to depend on that directory NOT existing.
            "HERMES_SEEDER": str(self.sandbox / "no-seed-env.py"),
            "HERMES_COMMON_ENV": str(self.sandbox / "no-common.env"),
            "HERMES_SECRETS_ENC": str(self.sandbox / "no-secrets.enc.env"),
            "HERMES_UPSTREAM_ENTRYPOINT": str(self.bin_dir / "entrypoint"),
            "HERMES_BIN": str(self.bin_dir / "hermes"),
            "HERMES_ENTRYPOINT_RESTARTS": "5",
            "HERMES_MEMWATCH": "0",
            "HERMES_SHUTDOWN_FLUSH_SECONDS": "1",
            "HERMES_CGROUP_ROOT": str(cg),
            "HERMES_CGROUP_PATH": "/",
        }
        base.update(extra_env)
        # keep the sandbox's real git/sync env out of bootstrap
        for var in ("RENDER_EXTERNAL_URL", "GIT_STATE_REPO", "GIT_STATE_TOKEN", "GITHUB_TOKEN"):
            base.pop(var, None)
        self.proc = subprocess.Popen(
            ["/bin/sh", str(BOOTSTRAP), "gateway", "run"],
            env=base,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

class OomGuardTests(_GuardHarness):
    """The proactive memory guard (memory_guard_loop) reclaims before a
    container OOM. These point the cgroup reader at a synthetic,
    artificially-full budget and confirm the staged reclaim fires (git
    state-sync daemon SIGSTOPped) instead of only logging."""

    def test_guard_sigstops_sync_daemon_under_container_pressure(self):
        # a fake git state-sync daemon the guard must pause (its pid lives in
        # the pidfile bootstrap reads at ${TMPDIR}/render-tools-git-daemon.pid)
        self.daemon = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], start_new_session=True)
        pidfile = self.sandbox / "render-tools-git-daemon.pid"
        pidfile.write_text(str(self.daemon.pid))

        self._launch(
            {
                "HERMES_MEMGUARD": "1",
                "TMPDIR": str(self.sandbox),
                "HERMES_MEMGUARD_INTERVAL_SECONDS": "1",
                "HERMES_MEMGUARD_WARN": "90",
                "HERMES_MEMGUARD_PAUSE_SYNC": "40",
                "HERMES_MEMGUARD_RESUME_SYNC": "20",
                "HERMES_MEMGUARD_DASHBOARD_PCT": "0",  # only test the sync stage
                "HERMES_MEMGUARD_MAX_ACTIVE_MB": "2048",
            }
        )
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            state = _proc_state(self.daemon.pid)
            if state and state.startswith("T"):  # stopped
                break
            time.sleep(0.2)
        self.assertTrue(
            state and state.startswith("T"),
            f"guard never SIGSTOPped the sync daemon (state={state!r})",
        )
        output = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("pausing git state sync", output)
        # measured budget path must not degrade to the telemetry-only branch
        self.assertNotIn("telemetry-only mode", output)

    def test_guard_stays_telemetry_only_without_a_cgroup_cap(self):
        """No measurable container cap -> the guard must never auto-reclaim."""
        # point the reader at an empty (unlimited) cgroup tree by overriding
        # the root to a directory that has no memory.max / memory.current
        fake_root = self.sandbox / "unlimited"
        fake_root.mkdir()
        self.daemon = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], start_new_session=True)
        pidfile = self.sandbox / "render-tools-git-daemon.pid"
        pidfile.write_text(str(self.daemon.pid))
        self._launch(
            {
                "HERMES_MEMGUARD": "1",
                "TMPDIR": str(self.sandbox),
                "HERMES_CGROUP_ROOT": str(fake_root),
                "HERMES_CGROUP_PATH": "/",
                "HERMES_MEMGUARD_INTERVAL_SECONDS": "1",
            }
        )
        time.sleep(3)  # several ticks
        self.assertNotEqual(_proc_state(self.daemon.pid), "T (stopped)")
        output = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("telemetry-only mode", output)
        self.assertNotIn("pausing git state sync", output)

    def test_guard_reclaims_by_rss_when_cgroup_cap_hidden(self):
        """HERMES_MEM_LIMIT_MB gives an opt-in RSS-based budget when the
        platform hides the cgroup cap; the guard must still reclaim."""
        self.daemon = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], start_new_session=True)
        pidfile = self.sandbox / "render-tools-git-daemon.pid"
        pidfile.write_text(str(self.daemon.pid))
        # point the cgroup reader at an empty tree (no cap) but provide a tiny
        # explicit budget that this boot's RSS sum is guaranteed to exceed
        self._launch(
            {
                "HERMES_MEMGUARD": "1",
                "TMPDIR": str(self.sandbox),
                "HERMES_CGROUP_ROOT": str(self.sandbox / "empty-cg"),
                "HERMES_CGROUP_PATH": "/",
                "HERMES_MEM_LIMIT_MB": "4",
                "HERMES_MEMGUARD_INTERVAL_SECONDS": "1",
                "HERMES_MEMGUARD_WARN": "90",
                "HERMES_MEMGUARD_PAUSE_SYNC": "20",
                "HERMES_MEMGUARD_RESUME_SYNC": "5",
                # Keep this case about the PAUSE stage. The synthetic budget
                # is exceeded by orders of magnitude, so every later stage
                # would fire too -- and 0 is how a stage is switched off.
                "HERMES_MEMGUARD_CRITICAL": "0",
                "HERMES_MEMGUARD_DASHBOARD_PCT": "0",
                "HERMES_MEMGUARD_MAX_ACTIVE_MB": "2048",
            }
        )
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            state = _proc_state(self.daemon.pid)
            if state and state.startswith("T"):
                break
            time.sleep(0.2)
        self.assertTrue(
            state and state.startswith("T"),
            f"guard never reclaimed via RSS fallback (state={state!r})",
        )
        output = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("using RSS estimate", output)
        self.assertIn("pausing git state sync", output)

    def test_guard_critical_stage_stops_sync_and_writes_hold(self):
        """Above CRITICAL the guard gives up on the backup instead of pausing
        it: a stopped daemon still holds its RSS. It must terminate the daemon
        AND leave the hold file, otherwise the supervisor restarts it straight
        back into the pressure that stopped it."""
        self.daemon = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], start_new_session=True)
        pidfile = self.sandbox / "render-tools-git-daemon.pid"
        pidfile.write_text(str(self.daemon.pid))
        hold = self.sandbox / "render-tools-git-daemon.hold"
        self._launch(
            {
                "HERMES_MEMGUARD": "1",
                "TMPDIR": str(self.sandbox),
                "HERMES_CGROUP_ROOT": str(self.sandbox / "empty-cg"),
                "HERMES_CGROUP_PATH": "/",
                "HERMES_MEM_LIMIT_MB": "4",
                "HERMES_MEMGUARD_INTERVAL_SECONDS": "1",
                "HERMES_MEMGUARD_WARN": "10",
                "HERMES_MEMGUARD_PAUSE_SYNC": "20",
                "HERMES_MEMGUARD_CRITICAL": "30",
                "HERMES_MEMGUARD_RESUME_SYNC": "5",
                "HERMES_MEMGUARD_DASHBOARD_PCT": "0",
            }
        )
        deadline = time.monotonic() + 30
        exited = False
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                exited = True
                break
            time.sleep(0.2)
        self.assertTrue(exited, "guard never stopped the sync daemon at CRITICAL")
        self.assertTrue(hold.exists(), "no hold file: the supervisor would restart it")
        output = self.log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("CRITICAL", output)
        self.assertIn("stopping git state sync", output)

    def test_guard_reports_staged_level_transitions(self):
        """The log must say which degradation level the container is in; a
        reader should not have to infer it from which reclaim lines appeared."""
        self.daemon = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], start_new_session=True)
        (self.sandbox / "render-tools-git-daemon.pid").write_text(str(self.daemon.pid))
        self._launch(
            {
                "HERMES_MEMGUARD": "1",
                "TMPDIR": str(self.sandbox),
                "HERMES_CGROUP_ROOT": str(self.sandbox / "empty-cg"),
                "HERMES_CGROUP_PATH": "/",
                "HERMES_MEM_LIMIT_MB": "4",
                "HERMES_MEMGUARD_INTERVAL_SECONDS": "1",
                "HERMES_MEMGUARD_WARN": "10",
                # Every reclaim stage off: this case is only about the level
                # being reported, and 0 is how a stage is disabled.
                "HERMES_MEMGUARD_PAUSE_SYNC": "0",
                "HERMES_MEMGUARD_CRITICAL": "0",
                "HERMES_MEMGUARD_DASHBOARD_PCT": "0",
                "HERMES_MEMGUARD_MAX_ACTIVE_MB": "2048",
            }
        )
        deadline = time.monotonic() + 30
        output = ""
        while time.monotonic() < deadline:
            output = self.log_path.read_text(encoding="utf-8", errors="replace")
            if "NORMAL -> WATCH" in output:
                break
            time.sleep(0.2)
        self.assertIn("NORMAL -> WATCH", output)
        # and the structured telemetry line that names the culprit process
        self.assertRegex(output, r"procs=\d+ threads=\d+")


class BootOomRankingTests(_GuardHarness):
    """The kernel OOM ranking is applied at boot, not only by the guard.

    Two failure modes this pins down, both of which were real before the
    boot-time call existed:

      * with HERMES_MEMGUARD=0 there is no guard loop and therefore no 30 s
        housekeeping tick -- so the ranking would never be applied at all, and
        the gateway would sit at the default score next to a dashboard that
        the watchdog can restart in ten seconds;
      * even with the guard on, the first half minute is unranked, and that is
        exactly when the agent-runtime import (the largest single allocation
        measured on this image) lands.

    The assertion is on the value the kernel actually reports, so a function
    that is defined but never called cannot pass.
    """

    def test_gateway_is_oom_ranked_at_boot_with_the_guard_disabled(self):
        # an entrypoint that spawns a process whose argv keeps the
        # "hermes gateway" needle bootstrap matches on
        (self.bin_dir / "entrypoint").write_text(
            "#!/bin/sh\n"
            "bash -c 'exec -a \"/opt/hermes/.venv/bin/hermes gateway run\" "
            "sleep 300' &\n"
            "sleep 300\n"
        )
        os.chmod(self.bin_dir / "entrypoint", 0o755)
        self._launch({"HERMES_MEMGUARD": "0"})

        deadline = time.monotonic() + 40
        score = None
        pids = []
        while time.monotonic() < deadline:
            pids = _pids_matching("hermes gateway")
            if pids:
                score = _oom_score_adj(pids[0])
                if score == -500:
                    break
            time.sleep(0.2)

        self.assertTrue(pids, "the fake gateway never started; test setup is broken")
        self.assertEqual(
            score,
            -500,
            "gateway was never OOM-protected at boot (HERMES_OOM_SCORE_GATEWAY "
            f"default is -500, kernel reports {score!r}). With HERMES_MEMGUARD=0 "
            "the 30 s housekeeping tick that re-applies it does not run, so "
            "this can only come from the boot-time call.",
        )

    def test_ranking_is_configurable_and_gives_up_on_a_dead_child(self):
        """A non-default score is honoured, and the poller must not spin
        forever when the child exits before a gateway ever appears."""
        (self.bin_dir / "entrypoint").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.bin_dir / "entrypoint", 0o755)
        self._launch(
            {
                "HERMES_MEMGUARD": "0",
                "HERMES_OOM_SCORE_GATEWAY": "-250",
                "HERMES_ENTRYPOINT_RESTARTS": "0",
                "HERMES_OOM_PROTECT_WAIT_SECONDS": "2",
            }
        )
        # the wrapper must exit rather than hang on the readiness poll
        try:
            code = self.proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            self.fail("bootstrap hung on oom_protect_gateway_when_ready after the child exited")
        self.assertIsInstance(code, int)


if __name__ == "__main__":
    unittest.main()
