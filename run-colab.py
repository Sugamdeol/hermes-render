#!/usr/bin/env python3
"""Paste this whole file into ONE Colab code cell and run it.

It installs the same patched Hermes used on Render, restores the private
Sugamdeol/hermes-storage@state repository and starts Telegram in polling mode.
Only GIT_STATE_TOKEN and the existing STORAGE_ENCRYPTION_KEY are needed.
Stop the Render/local copy after its successful backup before starting here.

After launch, use another cell:
    HERMES_COLAB.backup()    # wait for a confirmed GitHub save
    HERMES_COLAB.status()    # gateway/Telegram health
    HERMES_COLAB.stop()      # save, stop; then move back to Render/local

Colab runtimes are temporary. An abrupt runtime deletion cannot run a final
save; automatic backups preserve only work that reached GitHub beforehand.
"""
import getpass
import html
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

CODE_URL = "https://github.com/Sugamdeol/hermes-render.git"
SOURCE = Path("/content/hermes-render-source")
INSTALL = Path("/opt/hermes")
TOOLS = Path("/opt/render-tools")
DATA = Path("/opt/data")
LOG = Path("/content/hermes-colab.log")
CHECKPOINT = r'''
import json,os,secrets,tempfile,time
from pathlib import Path
p=Path('/tmp/hermes-recovery-checkpoint.json'); ack=p.with_suffix('.ack')
nonce=secrets.token_hex(16)
fd,n=tempfile.mkstemp(dir=p.parent,prefix='.launcher-checkpoint-')
with os.fdopen(fd,'w') as f: json.dump({'nonce':nonce},f)
os.replace(n,p)
deadline=time.monotonic()+300
while time.monotonic()<deadline:
    try:
        result=json.loads(ack.read_text())
        if result.get('nonce')==nonce:
            if not result.get('ok'): raise SystemExit('Backup failed; keep Colab running and check the storage logs.')
            print('Private GitHub backup saved.'); break
    except (OSError,ValueError): pass
    time.sleep(.5)
else: raise SystemExit('Backup timed out; the agent has NOT been stopped.')
'''


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def runtime_env(token, key):
    values = {
        "GIT_STATE_REPO": os.environ.get("GIT_STATE_REPO", "Sugamdeol/hermes-storage"),
        "GIT_STATE_BRANCH": os.environ.get("GIT_STATE_BRANCH", "state"),
        "GIT_STATE_TOKEN": token, "STORAGE_ENCRYPTION_KEY": key,
        "GIT_STATE_ENV_MODE": "encrypt", "HERMES_FAILOVER": "0",
        "HERMES_HOME": str(DATA), "GIT_STATE_WORKDIR": "/content/hermes-git-cache/state",
        "HERMES_UID": "", "HERMES_GID": "",
        "HERMES_RECOVERY_CHECKPOINT_FILE": "/tmp/hermes-recovery-checkpoint.json",
        "HERMES_INSTANCE_ID": "colab-" + secrets.token_hex(6),
        "PORT": "10000", "RENDER_EXTERNAL_URL": "",
        "TELEGRAM_WEBHOOK_URL": "", "TELEGRAM_WEBHOOK_PORT": "",
        "TELEGRAM_WEBHOOK_SECRET": "",
        "HERMES_TUI_DIR": str(INSTALL / "ui-tui"),
        "VIRTUAL_ENV": str(INSTALL / ".venv"),
        "PYTHONUNBUFFERED": "1", "HERMES_DASHBOARD_TUI": "1",
        "HERMES_DASHBOARD": "1", "HERMES_DASHBOARD_HOST": "127.0.0.1",
        "HERMES_DASHBOARD_PORT": "9119",
        "MALLOC_ARENA_MAX": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    }
    values["HERMES_ENV_OVERRIDE_KEYS"] = ",".join([*values, "PATH", "HERMES_ENV_OVERRIDE_KEYS"])
    values["PATH"] = str(INSTALL / ".venv/bin") + ":/usr/local/bin:" + os.environ.get("PATH", "")
    if not token or not key:
        raise ValueError("GitHub token and existing encryption key are required")
    # Do not capture Colab's own notebook/runtime credentials into agent state.
    base = {key: os.environ[key] for key in ("HOME", "LANG", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if key in os.environ}
    return dict(base, **values)


def get_bootstrap_secret(name, prompt):
    """Reuse account-owned Colab Secrets without copying them into a notebook."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        from google.colab import userdata
    except ImportError:
        userdata = None
    if userdata is not None:
        try:
            value = userdata.get(name)
        except Exception:
            # Missing/disabled secrets still work through a hidden prompt.
            # Never print exception text, which may contain credential details.
            value = None
        if value:
            return value
    return getpass.getpass(prompt)


def install_node():
    if shutil.which("node"):
        version = run(["node", "--version"], capture_output=True, text=True).stdout.strip()
        if int(version.lstrip("v").split(".")[0]) >= 24:
            return
    # Use Node's official release manifest and verify the archive digest.
    base = "https://nodejs.org/dist/latest-v24.x/"
    with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=60) as response:
        sums = response.read().decode()
    arch = "arm64" if os.uname().machine in ("aarch64", "arm64") else "x64"
    matches = [line.split() for line in sums.splitlines()
               if line.endswith(f"-linux-{arch}.tar.xz")]
    if len(matches) != 1:
        raise RuntimeError("Could not resolve the official Node 24 archive")
    expected, filename = matches[0]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive = root / filename
        urllib.request.urlretrieve(base + filename, archive)
        with archive.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != expected:
            raise RuntimeError("Node archive checksum mismatch")
        with tarfile.open(archive) as package:
            package.extractall(root, filter="data")
        node = root / filename.removesuffix(".tar.xz")
        shutil.copy2(node / "bin/node", "/usr/local/bin/node")
        shutil.copytree(node / "lib/node_modules/npm", "/usr/local/lib/node_modules/npm", dirs_exist_ok=True)
        for name, entry in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
            link = Path("/usr/local/bin") / name
            link.unlink(missing_ok=True)
            link.symlink_to(Path("/usr/local/lib/node_modules/npm/bin") / entry)


def install():
    run(["apt-get", "update", "-qq"])
    run(["apt-get", "install", "-y", "--no-install-recommends", "git", "curl",
         "ca-certificates", "bash", "gosu", "tini", "nginx-light", "age", "ripgrep", "openssh-client"])
    try:
        pwd.getpwnam("hermes")
    except KeyError:
        run(["groupadd", "-f", "hermes"])
        run(["useradd", "-m", "-g", "hermes", "-s", "/bin/bash", "hermes"])
    cache = Path("/content/hermes-git-cache")
    cache.mkdir(parents=True, exist_ok=True)
    run(["chown", "hermes:hermes", cache])
    cache.chmod(0o700)
    git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if not SOURCE.exists():
        run(["git", "clone", "--depth", "1", "--branch", "main", CODE_URL, SOURCE], env=git_env)
    else:
        origin = run(["git", "remote", "get-url", "origin"], cwd=SOURCE,
                     capture_output=True, text=True).stdout.strip()
        if origin != CODE_URL:
            raise RuntimeError("Source cache belongs to a different repository")
        run(["git", "fetch", "--depth", "1", "origin", "main"], cwd=SOURCE, env=git_env)
        run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=SOURCE)
    revision = run(["git", "rev-parse", "HEAD"], cwd=SOURCE,
                   capture_output=True, text=True).stdout.strip()
    marker = INSTALL / ".colab-launcher-revision"
    if marker.exists() and marker.read_text().strip() == revision:
        print("Using the installed Hermes version.")
        return
    if INSTALL.exists() and not marker.exists() and not (INSTALL / ".git").exists():
        raise RuntimeError("An unrelated /opt/hermes installation exists; use a fresh Colab runtime")
    dockerfile = (SOURCE / "Dockerfile").read_text()
    match = re.search(r"^ARG HERMES_REF=([0-9a-f]{40})$", dockerfile, re.M)
    if not match:
        raise RuntimeError("The Dockerfile has no pinned Hermes revision")
    upstream = match.group(1)
    INSTALL.mkdir(parents=True, exist_ok=True)
    # Runtime files belong to hermes; the root installer still needs to
    # update this one known checkout on subsequent runs.
    git = ["git", "-c", f"safe.directory={INSTALL}"]
    if not (INSTALL / ".git").exists():
        run([*git, "init", "."], cwd=INSTALL)
        run([*git, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git"], cwd=INSTALL)
    origin = run([*git, "remote", "get-url", "origin"], cwd=INSTALL,
                 capture_output=True, text=True).stdout.strip()
    if origin != "https://github.com/NousResearch/hermes-agent.git":
        raise RuntimeError("Refusing to replace an unrelated /opt/hermes checkout")
    run([*git, "fetch", "--depth", "1", "origin", upstream], cwd=INSTALL, env=git_env)
    run([*git, "checkout", "--force", "--detach", "FETCH_HEAD"], cwd=INSTALL)
    install_node()
    run([sys.executable, "-m", "pip", "install", "--quiet", "uv"])
    uv = [sys.executable, "-m", "uv"]
    uv_env = dict(os.environ, UV_PYTHON_INSTALL_DIR="/opt/hermes-python", UV_LINK_MODE="copy")
    run([*uv, "python", "install", "3.12"], env=uv_env)
    if not (INSTALL / ".venv/bin/python").exists():
        run([*uv, "venv", "--python", "3.12", INSTALL / ".venv"], env=uv_env)
    python = INSTALL / ".venv/bin/python"
    run([*uv, "pip", "install", "--python", python, "-e", ".[web,mcp,pty,cli]",
         "python-telegram-bot[webhooks]>=22.6,<23", "aiohttp>=3.13.3,<4", "cryptography"], cwd=INSTALL, env=uv_env)
    for directory, command in (("web", ["npm", "ci", "--no-audit", "--no-fund"]),
                               ("ui-tui", ["npm", "install", "--no-audit", "--no-fund"])):
        run(command, cwd=INSTALL / directory)
        run(["npm", "run", "build"], cwd=INSTALL / directory)
    TOOLS.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE / "scripts", TOOLS, dirs_exist_ok=True)
    for name, destination in (("env", "env"), ("skills", "skills-local"),
                              ("dashboard-plugins", "dashboard-plugins")):
        shutil.copytree(SOURCE / name, TOOLS / destination, dirs_exist_ok=True)
    run([python, TOOLS / "patch-lite.py", INSTALL])
    run([python, TOOLS / "patch-model-discovery.py", INSTALL / "hermes_cli/model_switch.py"])
    for path in TOOLS.glob("*"):
        if path.suffix in (".py", ".sh"):
            path.chmod(0o755)
    (INSTALL / "docker/entrypoint.sh").chmod(0o755)
    for name in ("ui-tui/packages/hermes-ink/dist/ink-bundle.js", "ui-tui/dist/entry.js"):
        target = INSTALL / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    DATA.mkdir(parents=True, exist_ok=True)
    run(["chown", "-R", "hermes:hermes", INSTALL, TOOLS, DATA, "/opt/hermes-python"])
    marker.write_text(revision + "\n")


def descendants(parent):
    parents = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            parents[int(proc.name)] = (int(stat[1]), stat[19])
        except (OSError, ValueError, IndexError):
            pass
    found = {}
    pending = [parent]
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        if pid in parents:
            found[pid] = parents[pid][1]
            pending.extend(child for child, (ppid, _) in parents.items() if ppid == pid)
    return found


class ColabAgent:
    def __init__(self, process, env):
        self.process, self.env = process, env
        self.started_at = time.monotonic()

    def status(self):
        with urllib.request.urlopen("http://127.0.0.1:10000/healthz", timeout=5) as response:
            payload = json.load(response)
        result = {name: payload.get(name) for name in ("gateway_running", "gateway_state", "gateway_platforms", "active_sessions")}
        result["agent_uptime_hours"] = round((time.monotonic() - self.started_at) / 3600, 2)
        print(json.dumps(result, indent=2))
        return result

    def dashboard(self):
        """Show a clickable dashboard link through the Colab browser proxy."""
        try:
            from google.colab import output
        except ImportError:
            print("Dashboard: http://127.0.0.1:10000")
            return "http://127.0.0.1:10000"
        try:
            url = output.eval_js("google.colab.kernel.proxyPort(10000)")
        except Exception:
            print("Could not create the browser link. Retry HERMES_COLAB.dashboard() in a notebook cell.")
            return None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise RuntimeError("Colab did not return a valid dashboard URL")
        from IPython.display import HTML, display
        display(HTML('<a href="' + html.escape(url, quote=True) +
                     '" target="_blank" rel="noopener noreferrer" '
                     'style="display:inline-block;padding:12px 20px;border-radius:10px;'
                     'background:#087f70;color:white;text-decoration:none;font-weight:600">'
                     'Open Dashboard ↗</a>'))
        print("Username: hermes. Run HERMES_COLAB.password() for your saved password.")
        return url

    def password(self):
        """Display the dashboard password only when explicitly requested."""
        spec = importlib.util.spec_from_file_location("seed", TOOLS / "seed-env.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        values = dict(seed.parse_dotenv((DATA / ".env").read_text()))
        saved = DATA / ".render-runtime-env.json"
        if saved.exists():
            values.update(json.loads(saved.read_text()).get("variables", {}))
        print("Username: hermes\nPassword:", values.get("HERMES_GATEWAY_TOKEN", "Password is not ready yet"))

    def backup(self):
        if self.process.poll() is not None:
            raise RuntimeError("Agent is not running; keep this runtime's files for recovery")
        run(["gosu", "hermes", INSTALL / ".venv/bin/python", "-c", CHECKPOINT], env=self.env)

    def stop(self):
        self.backup()  # On failure, leave the agent and local data running.
        processes = descendants(self.process.pid)
        for pid in reversed(processes):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            pass
        for pid, start in processes.items():
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if fields[19] == start:  # Avoid killing a reused process ID.
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError, IndexError):
                pass
        subprocess.run(["nginx", "-s", "quit"], capture_output=True)
        print("Saved and stopped. You can now start the Render/local copy.")


def main(confirm_switch=False):
    if sys.version_info < (3, 11):
        raise RuntimeError("Use a current Colab Python runtime (Python 3.11 or newer)")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("This installer needs the root Linux runtime provided by Colab")
    previous = globals().get("HERMES_COLAB")
    if previous and previous.process.poll() is None:
        print("Already running. Use HERMES_COLAB.status() or HERMES_COLAB.stop().")
        ColabAgent.dashboard(previous)
        return previous
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 10000)) == 0:
            raise RuntimeError("Port 10000 is already occupied; stop the old launcher first")
    print("Stop the Render/local copy after its latest successful backup before switching.")
    if not confirm_switch and input("Type SWITCH to confirm the other copy is stopped: ").strip() != "SWITCH":
        raise RuntimeError("No changes made")
    token = get_bootstrap_secret("GIT_STATE_TOKEN", "GitHub storage token: ")
    key = get_bootstrap_secret("STORAGE_ENCRYPTION_KEY", "Existing storage encryption key: ")
    env = runtime_env(token, key)
    install()
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as logfile:
        process = subprocess.Popen(["bash", str(TOOLS / "bootstrap.sh"), "sleep", "infinity"],
                                   env=env, cwd=INSTALL, stdout=logfile, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    agent = ColabAgent(process, env)
    # Keep the stop/backup handle even if initialization later times out.
    globals()["HERMES_COLAB"] = agent
    print(f"Restoring saved data. Startup logs: {LOG}")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Startup exited. Inspect {LOG}; data has not been deleted")
        try:
            with urllib.request.urlopen("http://127.0.0.1:10000/healthz", timeout=3) as response:
                health = json.load(response)
            if health.get("gateway_running") and health.get("gateway_state") == "running":
                break
        except (OSError, ValueError):
            pass
        time.sleep(2)
    else:
        raise RuntimeError(f"Startup is still pending. Inspect {LOG} and use HERMES_COLAB.status()")
    agent.dashboard()
    print("Telegram polling is running. Use HERMES_COLAB.backup() and HERMES_COLAB.stop() before switching hosts.")
    return agent


if __name__ == "__main__":
    HERMES_COLAB = main()
