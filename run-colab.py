#!/usr/bin/env python3
"""Paste this whole file into ONE Colab code cell and run it.

It installs the reviewed current upstream Hermes on Colab or Kaggle with the full notebook profile, restores the private
Sugamdeol/hermes-storage@state repository and starts Telegram in polling mode.
In Colab the agent runs as root, like notebook cells do, so `!apt-get
install ...` and other system downloads from the agent's own tools work.
Only GIT_STATE_TOKEN and the existing STORAGE_ENCRYPTION_KEY are needed.
Stop your other Hermes host after its successful backup before starting here.

After launch, use another cell:
    HERMES_COLAB.backup()    # wait for a confirmed GitHub save
    HERMES_COLAB.status()    # gateway/Telegram health
    HERMES_COLAB.stop()      # save, stop; then move back to Render/local
    HERMES_COLAB.stop(force=True)  # last resort when a save keeps failing: stop WITHOUT saving

Colab runtimes are temporary. An abrupt runtime deletion cannot run a final
save; automatic backups preserve only work that reached GitHub beforehand.
"""
import getpass
import html
import math
import threading
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
from urllib.parse import urlsplit

CODE_URL = "https://github.com/Sugamdeol/hermes-render.git"
def notebook_host():
    if os.environ.get("KAGGLE_KERNEL_RUN_TYPE") or os.environ.get("KAGGLE_CONTAINER_NAME") or Path("/kaggle/working").is_dir():
        return "kaggle"
    return "colab"


NOTEBOOK_HOST = notebook_host()
NOTEBOOK_ROOT = Path("/kaggle/temp/hermes") if NOTEBOOK_HOST == "kaggle" else Path("/content")
SOURCE = NOTEBOOK_ROOT / "hermes-render-source"
INSTALL = Path("/opt/hermes")
TOOLS = Path("/opt/render-tools")
DATA = Path("/opt/data")
LOG = NOTEBOOK_ROOT / ("hermes-" + NOTEBOOK_HOST + ".log")
CHECKPOINT = r'''
import json,os,secrets,tempfile,time
from pathlib import Path
p=Path('/tmp/hermes-recovery-checkpoint.json'); ack=p.with_suffix('.ack')
nonce=secrets.token_hex(16)
fd,n=tempfile.mkstemp(dir=p.parent,prefix='.launcher-checkpoint-')
with os.fdopen(fd,'w') as f: json.dump({'nonce':nonce},f)
os.replace(n,p)
deadline=time.monotonic()+1200
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
# Why a save did not reach GitHub, read from the storage daemon's log lines
# (scripts/git-storage.py). Most specific first: a failed checkpoint is logged as
# "recovery checkpoint failed: <reason>", so the reason text decides the case.
BACKUP_REASONS = (
    ("advanced since",
     "Another copy saved newer state to this backup repository, so this runtime will not "
     "overwrite it. Stop that copy first. To take its newer state, run HERMES_COLAB.stop(force=True) "
     "here (this runtime's unsaved changes are lost), then press \u25b6."),
    ("could not confirm",
     "GitHub could not confirm the backup repository is private (API unreachable, or the token "
     "lacks access). Nothing was pushed. Wait a minute, then retry backup()."),
    ("git state push failed",
     "Pushing to GitHub failed, usually a network problem. The daemon retries by itself. "
     "Wait a minute, then retry backup()."),
    ("recovery checkpoint failed",
     "The storage daemon could not save this state. Fix the reason in the log line above, "
     "then retry backup()."),
)
NO_REPLY = ("The storage daemon did not confirm a save within 20 minutes and logged no reason. It may be "
            "stuck on a slow GitHub upload, or it may have stopped. Retry backup() once. If it stays silent, "
            "HERMES_COLAB.stop(force=True) stops without saving, and anything not yet pushed is lost.")
NO_REASON = ("The save failed and the storage log has no reason for it. Check the output above and the "
             "log tail, then retry backup().")


def explain_backup_failure(output, log_text):
    """Return (advice, log line) for a failed save.

    `log_text` must hold only the storage log written during this save attempt,
    so an older failure is never blamed for a new one.
    """
    for line in reversed(log_text.splitlines()):
        for marker, advice in BACKUP_REASONS:
            if marker in line:
                return advice, line.strip()
    if "timed out" in output:
        return NO_REPLY, None
    return NO_REASON, None


def log_offset():
    try:
        return LOG.stat().st_size
    except OSError:
        return 0


def log_since(offset):
    try:
        with LOG.open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def redact_secrets(text, env):
    for name, value in env.items():
        if value and re.search(r"TOKEN|KEY|PASSWORD|SECRET", name):
            text = text.replace(value, "[redacted]")
    return text


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def runtime_env(token, key, dashboard_password=None):
    values = {
        "GIT_STATE_REPO": os.environ.get("GIT_STATE_REPO", "Sugamdeol/hermes-storage"),
        "GIT_STATE_BRANCH": os.environ.get("GIT_STATE_BRANCH", "state"),
        "GIT_STATE_TOKEN": token, "STORAGE_ENCRYPTION_KEY": key,
        "GIT_STATE_ENV_MODE": "encrypt", "HERMES_FAILOVER": "0",
        "GIT_STATE_WATCH": "1", "GIT_STATE_EVENT_WATCH": "1",
        "GIT_STATE_WATCH_SECONDS": "1",
        "GIT_STATE_DEBOUNCE_SECONDS": "0",
        "GIT_STATE_MIN_PUSH_INTERVAL_SECONDS": "0",
        "GIT_STATE_INTERVAL_SECONDS": "60",
        # GitHub may spend over a minute preparing a large shallow clone.
        "GIT_STATE_HTTP_LOW_SPEED_LIMIT": "0",
        "GIT_STATE_GIT_TIMEOUT_SECONDS": "1800",
        # Full state must finish before startup; large cold restores need room to complete.
        "HERMES_RESTORE_TIMEOUT_SECONDS": "7200",
        "HERMES_COLAB_STARTUP_TIMEOUT_SECONDS": "7200",
        "HERMES_HOME": str(DATA), "GIT_STATE_WORKDIR": str(SOURCE.parent / "hermes-git-cache/state"),
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
        "HERMES_RUNTIME_PROFILE": "colab-full",
        "HERMES_NOTEBOOK_HOST": NOTEBOOK_HOST,
        "HERMES_RESTORE_COPY_WORKERS": os.environ.get("HERMES_RESTORE_COPY_WORKERS", "4"),
        "HERMES_COLAB_UPDATE_DIR": "/tmp/hermes-colab-control",
        "HERMES_MAX_CONCURRENT_SESSIONS": "10",
        "HERMES_RESUME_ALL_INTERRUPTED": "1",
        "HERMES_CRON_MAX_PARALLEL": "10",
        "HERMES_TUI_RPC_POOL_WORKERS": "10",
        "HERMES_WEB_BUILD_MAX_OLD_SPACE_SIZE": "4096",
        # Colab only: the hermes account is uid 0 on an ephemeral VM, so no later
        # non-root run can be broken by root-owned files, which is the risk the
        # upstream "refusing to run the gateway as root" guard protects against.
        # Render and Docker never set this; see run-local.py and the Dockerfile.
        "HERMES_ALLOW_ROOT_GATEWAY": "1",
        "HERMES_RUNTIME_DEPS_AUTORESTORE": "1",
        "HERMES_RUNTIME_DEPS_BACKGROUND": os.environ.get("HERMES_RUNTIME_DEPS_BACKGROUND", "1"),
        "HERMES_COLAB_SYSTEM_PYTHON": sys.executable,
        "HERMES_RUNTIME_CACHE_DIR": str(cache_directory()),
        "UV_CACHE_DIR": str(cache_directory()/'uv'),
        "npm_config_cache": str(cache_directory()/'npm'),
    }
    if dashboard_password:
        values["HERMES_GATEWAY_TOKEN"] = dashboard_password
    if NOTEBOOK_HOST in ('colab', 'kaggle'):
        # These belong to this notebook, never a restored InstaBox deployment.
        values.update({
            'GIT_STATE_BASE_FILE': str(SOURCE.parent / 'hermes-git-cache/state.base.json'),
            'GIT_STATE_MAX_COMMITS': '200',
            'HERMES_DASHBOARD_FILES_ROOT': '',
            'HERMES_DASHBOARD_URL': '',
            'PYTHONPATH': str(INSTALL),
            'PYTHONHOME': '',
        })
    values["HERMES_ENV_OVERRIDE_KEYS"] = ",".join([*values, "PATH", "HERMES_ENV_OVERRIDE_KEYS", "CLOUDFLARE_TUNNEL_TOKEN"])
    values["PATH"] = str(INSTALL / ".venv/bin") + ":/usr/local/bin:" + os.environ.get("PATH", "")
    if not token or not key:
        raise ValueError("GitHub token and existing encryption key are required")
    # Do not capture Colab's own notebook/runtime credentials into agent state.
    base = {key: os.environ[key] for key in ("HOME", "LANG", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if key in os.environ}
    return dict(base, **values)


def notebook_secret(name):
    value = os.environ.get(name, '')
    if value:
        return value
    try:
        if NOTEBOOK_HOST == 'kaggle':
            from kaggle_secrets import UserSecretsClient
            return str(UserSecretsClient().get_secret(name) or '')
        from google.colab import userdata
        return str(userdata.get(name) or '')
    except Exception:
        # Provider exceptions can contain private credential details.
        return ''


def get_bootstrap_secret(name, prompt):
    value = notebook_secret(name)
    if value:
        return value
    if NOTEBOOK_HOST == 'kaggle':
        raise RuntimeError('Add ' + name + ' in Kaggle Add-ons → Secrets and enable notebook access, then rerun Start.')
    return getpass.getpass(prompt)


def optional_colab_setting(name):
    # Keep the old name for saved notebooks and existing integrations.
    return notebook_secret(name).strip()


def dashboard_tunnel_settings():
    token = optional_colab_setting('CLOUDFLARE_TUNNEL_TOKEN')
    url = optional_colab_setting('HERMES_DASHBOARD_URL')
    if not token and not url:
        return '', ''
    if not token or not url:
        raise RuntimeError('For a fixed dashboard URL, set both CLOUDFLARE_TUNNEL_TOKEN and HERMES_DASHBOARD_URL in your notebook Secrets with notebook access enabled.')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or parsed.hostname.endswith('.trycloudflare.com')):
        raise ValueError('HERMES_DASHBOARD_URL must be your named tunnel HTTPS hostname, such as https://hermes.example.com')
    return token, url.rstrip('/')


def install_cloudflared():
    binary = shutil.which("cloudflared")
    if binary:
        return binary
    arch = "arm64" if os.uname().machine in ("aarch64", "arm64") else "amd64"
    # Fixed official release avoids GitHub's unauthenticated API rate limit
    # on shared Colab IPs and verifies the published binary checksum.
    checksums = {
        "amd64": "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db",
        "arm64": "e6422b9d4f72d3194bc5a38676f13667c06666523217b842a877d72a80b5ac08",
    }
    url = "https://github.com/cloudflare/cloudflared/releases/download/2026.10.0/cloudflared-linux-" + arch
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Hermes-Colab"})
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    if hashlib.sha256(payload).hexdigest() != checksums[arch]:
        raise RuntimeError("cloudflared checksum mismatch")
    target = Path("/usr/local/bin/cloudflared")
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".cloudflared-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755)
        run([temporary, "--version"], capture_output=True)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(target)


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


def root_account_plan(existing_uid):
    """Commands that make the Colab hermes account uid 0.

    Real Colab cells run as root, and so should the agent they launch:
    `!apt-get install ...` from the agent has to work exactly like it does
    in a notebook cell. `gosu hermes` keeps uid 0 when hermes is uid 0, so
    bootstrap.sh and the upstream entrypoint need no special casing and
    every runtime file has one consistent owner. Render and Docker keep
    their unprivileged hermes user; this account exists only on the Colab VM.
    """
    if existing_uid == 0:
        return []
    if existing_uid is None:
        return [["groupadd", "-f", "hermes"],
                ["useradd", "-m", "-o", "-u", "0", "-g", "hermes", "-s", "/bin/bash", "hermes"]]
    return [["usermod", "-o", "-u", "0", "hermes"]]


def keep_root_entrypoint(text):
    """Skip the upstream gosu drop when the hermes account is already root.

    The stock entrypoint re-execs itself through `gosu hermes`; when hermes
    is uid 0 that would loop forever, so the drop must be conditional on it
    actually changing the uid. Idempotent, and it fails closed if upstream
    ever rewrites the privilege-drop block.
    """
    old = 'if [ "$(id -u)" = "0" ]; then'
    new = 'if [ "$(id -u)" = "0" ] && [ "$(id -u hermes)" != "0" ]; then'
    if new in text:
        return text
    if old not in text:
        raise RuntimeError("upstream entrypoint changed; root privilege-drop patch no longer matches")
    return text.replace(old, new, 1)


def ensure_root_entrypoint():
    entrypoint = INSTALL / "docker/entrypoint.sh"
    entrypoint.write_text(keep_root_entrypoint(entrypoint.read_text()))
    entrypoint.chmod(0o755)


CORE_PACKAGES = ('git','curl','ca-certificates','bash','gosu','tini','nginx-light','age','ripgrep','openssh-client','build-essential','pkg-config','openssl')
UI_CACHE_PATHS = ('hermes_cli/web_dist', 'ui-tui/dist', 'ui-tui/packages/hermes-ink/dist', 'ui-tui/node_modules')


def cache_directory():
    explicit = os.environ.get('HERMES_NOTEBOOK_CACHE_DIR') or os.environ.get('HERMES_COLAB_CACHE_DIR')
    drive = Path('/content/drive/MyDrive')
    cache = Path(explicit) if explicit else (drive/'HermesRuntimeCache' if NOTEBOOK_HOST == 'colab' and drive.is_dir() else NOTEBOOK_ROOT/'hermes-runtime-cache')
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def prepare_cache():
    if NOTEBOOK_HOST == 'colab' and os.environ.get('HERMES_COLAB_USE_DRIVE_CACHE') == '1':
        try:
            from google.colab import drive
            print('Connecting Drive cache. Only public builds and package downloads are cached here.')
            drive.mount('/content/drive')
        except (ImportError, OSError, ValueError):
            print('Drive cache unavailable; using this VM cache instead.')
    return cache_directory()


def runtime_key(upstream):
    # Code/plugin/docs changes do not invalidate the Python or Node install.
    recipe = [3, upstream, os.uname().machine, 'python3.14', 'node24',
              '.[all,telegram]', 'python-telegram-bot[webhooks]>=22.6,<23',
              'aiohttp>=3.13.3,<4', 'cryptography']
    return hashlib.sha256(json.dumps(recipe).encode()).hexdigest()


def restore_ui_cache(cache, key):
    archive = cache / (key+'.ui.tar.gz')
    digest = archive.with_suffix('.sha256')
    if not archive.exists() or not digest.exists():
        return False
    try:
        with archive.open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest() != digest.read_text().strip():
                return False
        with tarfile.open(archive) as package:
            # Keep archive members inside the four public build directories.
            for member in package.getmembers():
                if not any(member.name == p or member.name.startswith(p+'/') for p in UI_CACHE_PATHS):
                    return False
                target = (INSTALL/member.name).resolve()
                roots = [(INSTALL/p).resolve() for p in UI_CACHE_PATHS]
                if not any(target.is_relative_to(p) for p in roots):return False
                if member.issym() or member.islnk():
                    link = ((target.parent/member.linkname) if member.issym() else (INSTALL/member.linkname)).resolve()
                    if not any(link.is_relative_to(p) for p in roots):return False
            package.extractall(INSTALL, filter='data')
        return (INSTALL/'hermes_cli/web_dist/index.html').is_file() and (INSTALL/'ui-tui/dist/entry.js').is_file()
    except (OSError, tarfile.TarError, ValueError):
        print('UI cache could not be read; rebuilding it.')
        return False


def save_ui_cache(cache, key):
    # Cache only public build output and npm dependencies, never /opt/data,
    # environment files, credentials, or chat histories.
    archive = cache / (key+'.ui.tar.gz')
    fd, temporary = tempfile.mkstemp(dir=cache,prefix='.ui-cache-')
    os.close(fd)
    try:
        with tarfile.open(temporary,'w:gz',compresslevel=1) as package:
            for name in UI_CACHE_PATHS:
                if (INSTALL/name).exists():package.add(INSTALL/name,arcname=name)
        with open(temporary,'rb') as f:
            digest = hashlib.file_digest(f,'sha256').hexdigest()
        os.replace(temporary,archive)
        archive.with_suffix('.sha256').write_text(digest+'\n')
    finally:
        Path(temporary).unlink(missing_ok=True)


class StatePrefetch:
    """Restore full private state while independent runtime dependencies install."""
    def __init__(self, env, script):
        self.env, self.script, self.process, self.log = env, script, None, None
        self.restoring = False

    def __enter__(self):
        if not self.env or not self.env.get('GIT_STATE_TOKEN') or not self.env.get('GIT_STATE_REPO'):
            return self
        path = SOURCE.parent / 'hermes-state-prefetch.log'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        self.log = os.fdopen(fd, 'w')
        try:
            # Existing notebook crypto can decrypt state before the agent venv exists.
            # If unavailable, overlap the download and let normal bootstrap decrypt.
            self.restoring = importlib.util.find_spec('cryptography') is not None
            command = 'prepare-startup' if self.restoring else 'prefetch'
            self.process = subprocess.Popen(
                [sys.executable, str(self.script), command, str(DATA)],
                env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
                start_new_session=True)
        except BaseException:
            self.log.close()
            raise
        print('Restoring all saved data alongside runtime installation…' if self.restoring
              else 'Downloading saved state alongside runtime installation…')
        return self

    def __exit__(self, kind, value, traceback):
        if self.process is None:
            return False
        try:
            if kind is None:
                try:
                    code = self.process.wait(timeout=int(self.env.get('HERMES_RESTORE_TIMEOUT_SECONDS', '1800')))
                    if code:
                        print('State preparation needs a retry; normal restore will check it before starting.')
                    elif self.restoring:
                        print('Full state preparation finished; bootstrap will recheck the saved revision before starting.')
                except subprocess.TimeoutExpired:
                    print('Background state download timed out; the normal restore will retry.')
        finally:
            try:
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait()
            finally:
                self.log.close()
        return False


def install(env=None):
    # dpkg status is local: do not hit apt repositories on every relaunch.
    missing = []
    for package in CORE_PACKAGES:
        result = subprocess.run(['dpkg-query','-W','-f=${Status}',package],capture_output=True,text=True)
        if result.returncode or result.stdout.strip() != 'install ok installed':missing.append(package)
    if missing:
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "--no-install-recommends", *missing])
    try:
        existing_uid = pwd.getpwnam("hermes").pw_uid
    except KeyError:
        existing_uid = None
    for command in root_account_plan(existing_uid):
        run(command)
    cache = SOURCE.parent / "hermes-git-cache"
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
    with StatePrefetch(env, SOURCE / 'scripts/git-storage.py'):
        install_runtime(revision)


def install_runtime(revision, target=None):
    marker = INSTALL / ".colab-launcher-revision"
    core_ready = (INSTALL/".venv/bin/python").exists() and all(p.exists() and p.stat().st_size > 0 for p in (INSTALL/"hermes_cli/web_dist/index.html", INSTALL/"ui-tui/dist/entry.js"))
    if target is None and core_ready and (INSTALL / ".colab-native-updater").exists() and marker.exists() and marker.read_text().strip() == revision:
        # Cached install: still enforce the root fix so an old runtime gains
        # it on relaunch (update-chat-ui.py restarts through this path).
        # Native Colab startup does not use the Docker entrypoint.
        print("Using the installed Hermes version.")
        return
    if INSTALL.exists() and not marker.exists() and not (INSTALL / ".git").exists():
        raise RuntimeError("An unrelated /opt/hermes installation exists; use a fresh Colab runtime")
    selection = cache_directory() / "selected-upstream.txt"
    upstream = target or (selection.read_text().strip() if selection.exists() else
                          (SOURCE / "colab-upstream.txt").read_text().strip())
    if not re.fullmatch(r"[0-9a-f]{40}", upstream):
        raise RuntimeError("Invalid reviewed Colab upstream revision")
    # A completed native update owns this checkout; never roll it back to the bootstrap pin.
    if target is None and (INSTALL / '.colab-native-updater').exists():
        upstream = run(['git','-c',f'safe.directory={INSTALL}','rev-parse','HEAD'],cwd=INSTALL,capture_output=True,text=True).stdout.strip()
    cache = cache_directory()
    key = runtime_key(upstream)
    runtime_marker = INSTALL / '.colab-runtime-key'
    ready = core_ready
    reuse = ready and runtime_marker.exists() and runtime_marker.read_text().strip() == key
    # Adopt a healthy pre-cache installation without reinstalling it once.
    if ready and marker.exists() and not runtime_marker.exists() and (INSTALL/'.git').exists():
        installed_ref = subprocess.run(['git','-c',f'safe.directory={INSTALL}','rev-parse','HEAD'],cwd=INSTALL,capture_output=True,text=True)
        reuse = installed_ref.returncode == 0 and installed_ref.stdout.strip() == upstream
    INSTALL.mkdir(parents=True, exist_ok=True)
    # Runtime files belong to hermes; the root installer still needs to
    # update this one known checkout on subsequent runs.
    git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    git = ["git", "-c", f"safe.directory={INSTALL}"]
    if not (INSTALL / ".git").exists():
        run([*git, "init", "."], cwd=INSTALL)
        run([*git, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git"], cwd=INSTALL)
    origin = run([*git, "remote", "get-url", "origin"], cwd=INSTALL,
                 capture_output=True, text=True).stdout.strip()
    if origin != "https://github.com/NousResearch/hermes-agent.git":
        raise RuntimeError("Refusing to replace an unrelated /opt/hermes checkout")
    exists = subprocess.run([*git,'cat-file','-e',upstream+'^{commit}'],cwd=INSTALL,capture_output=True).returncode == 0
    if not exists:
        run([*git, "fetch", "--depth", "1", "origin", upstream], cwd=INSTALL, env=git_env)
    run([*git, "checkout", "--force", "-B", "main", upstream], cwd=INSTALL)
    install_node()
    if importlib.util.find_spec('uv') is None:
        run([sys.executable, "-m", "pip", "install", "--quiet", "uv"])
    uv = [sys.executable, "-m", "uv"]
    uv_env = dict(os.environ, UV_PYTHON_INSTALL_DIR="/opt/hermes-python", UV_LINK_MODE="copy", UV_CACHE_DIR=str(cache/'uv'))
    if not reuse:
        run([*uv, "python", "install", "3.14"], env=uv_env)
        # Recreate the runtime after an upstream/Python recipe change.
        run([*uv, "venv", "--python", "3.14", "--clear", INSTALL / ".venv"], env=uv_env)
    python = INSTALL / ".venv/bin/python"
    if not reuse:
        run([*uv, "pip", "install", "--python", python, "-e", ".[all,telegram]", "cryptography", "PyYAML"], cwd=INSTALL, env=uv_env)
    if reuse:
        print('Reusing Python dependencies and built dashboards; refreshing code only.')
        if not (cache/(key+'.ui.tar.gz')).exists():
            try:save_ui_cache(cache,key)
            except (OSError,tarfile.TarError):print('UI cache could not be saved; this install remains usable.')
    elif restore_ui_cache(cache,key):
        print('Restored built dashboards from cache; skipping npm installs and builds.')
    else:
        npm_env = dict(os.environ,npm_config_cache=str(cache/'npm'),HERMES_PYTHON=str(python))
        run(["npm", "ci", "--no-audit", "--no-fund"], cwd=INSTALL, env=npm_env)
        run(["node", "scripts/generate-icons.mjs"], cwd=INSTALL, env=npm_env)
        for directory in ("web", "ui-tui"):
            run(["npm", "run", "build"], cwd=INSTALL / directory, env=npm_env)
        try:save_ui_cache(cache,key)
        except (OSError,tarfile.TarError):print('UI cache could not be saved; this install remains usable.')
    TOOLS.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE / "scripts", TOOLS, dirs_exist_ok=True)
    for name, destination in (("env", "env"), ("skills", "skills-local"),
                              ("dashboard-plugins", "dashboard-plugins")):
        shutil.copytree(SOURCE / name, TOOLS / destination, dirs_exist_ok=True)
    run([python, TOOLS / "patch-colab-full.py", INSTALL])
    for path in TOOLS.glob("*"):
        if path.suffix in (".py", ".sh"):
            path.chmod(0o755)
    for name in ("ui-tui/packages/hermes-ink/dist/ink-bundle.js", "ui-tui/dist/entry.js"):
        target = INSTALL / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    DATA.mkdir(parents=True, exist_ok=True)
    # Colab's hermes account is root, so it can read existing files without
    # recursively touching every tool and session on each adapter update.
    run(["chown", "hermes:hermes", INSTALL, TOOLS, DATA, "/opt/hermes-python"])
    marker.write_text(revision + "\n")
    (INSTALL / '.colab-native-updater').touch()
    runtime_marker.write_text(key+'\n')


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

    def stop_tunnel(self):
        process = getattr(self, "tunnel_process", None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        token_file = getattr(self, 'tunnel_token_file', None)
        if token_file:
            Path(token_file).unlink(missing_ok=True)
        self.tunnel_token_file = None
        self.tunnel_process = None
        self.tunnel_url = None

    def start_tunnel(self):
        existing = getattr(self, "tunnel_process", None)
        if existing and existing.poll() is None and getattr(self, "tunnel_url", None):
            return self.tunnel_url
        ColabAgent.stop_tunnel(self)
        token, fixed_url = dashboard_tunnel_settings()
        print("Starting fixed Cloudflare dashboard tunnel…" if fixed_url else "Starting Cloudflare tunnel…")
        binary = install_cloudflared()
        home = Path(NOTEBOOK_ROOT / "hermes-cloudflare")
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = home / "tunnel.log"
        ready_url = None
        command = [binary, 'tunnel', '--no-autoupdate', '--protocol', 'http2']
        if fixed_url:
            fd, token_path = tempfile.mkstemp(dir=home, prefix='named-', suffix='.token')
            with os.fdopen(fd, 'w') as handle:
                handle.write(token)
            self.tunnel_token_file = token_path
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                metrics_port = reservation.getsockname()[1]
            command += ['--metrics', f'127.0.0.1:{metrics_port}', 'run', '--token-file', token_path]
            ready_url = f'http://127.0.0.1:{metrics_port}/ready'
        else:
            command += ['--url', 'http://127.0.0.1:10000']
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as logfile:
            self.tunnel_process = subprocess.Popen(
                command,
                env={**{key: os.environ[key] for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")
                      if key in os.environ}, "HOME": str(home), "PATH": os.environ.get("PATH", "")},
                stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.tunnel_process.poll() is not None:
                ColabAgent.stop_tunnel(self)
                detail = log.read_text(errors="replace")[-2000:]
                raise RuntimeError("Cloudflare tunnel exited: " + (detail.replace(token, '[REDACTED]') if token else detail))
            if fixed_url:
                try:
                    with urllib.request.urlopen(ready_url, timeout=2) as response:
                        if response.status == 200:
                            self.tunnel_url = fixed_url
                            return self.tunnel_url
                except OSError:
                    pass
            else:
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text(errors="replace"))
                if match:
                    self.tunnel_url = match.group(0)
                    return self.tunnel_url
            time.sleep(1)
        ColabAgent.stop_tunnel(self)
        detail = log.read_text(errors="replace")[-2000:]
        raise RuntimeError("Cloudflare tunnel timed out: " + (detail.replace(token, '[REDACTED]') if token else detail))

    def dashboard(self):
        """Open the dashboard in Colab's supported embedded browser context."""
        try:
            cloudflare_url = ColabAgent.start_tunnel(self)
        except Exception as exc:
            cloudflare_url = None
            detail = redact_secrets(str(exc), getattr(self, "env", {}))
            print("Cloudflare link unavailable:", type(exc).__name__, detail[:2000])
            print("Tunnel logs:", NOTEBOOK_ROOT / "hermes-cloudflare/tunnel.log", "Retry HERMES_COLAB.dashboard().")
        colab_url = None
        try:
            if NOTEBOOK_HOST != "colab":
                raise ImportError("Colab embedding is unavailable on this host")
            from google.colab import output
            colab_url = output.eval_js("google.colab.kernel.proxyPort(10000)")
            if not isinstance(colab_url, str) or not colab_url.startswith(("https://", "http://")):
                colab_url = None
        except Exception:
            output = None
        if cloudflare_url:
            from IPython.display import HTML, display
            display(HTML('<a target="_blank" rel="noopener noreferrer" href="' +
                         html.escape(cloudflare_url, quote=True) + '">Open Cloudflare Dashboard ↗</a>'))
            print("Cloudflare dashboard:", cloudflare_url)
        if colab_url:
            print("Colab dashboard (embedded view):", colab_url)
        if not cloudflare_url and not colab_url:
            print("Local dashboard: http://127.0.0.1:10000")
        self.password()
        if colab_url and output:
            try:
                output.serve_kernel_port_as_iframe(10000, height=850, cache_in_notebook=False)
            except Exception:
                print("Embedded view unavailable. Use the Cloudflare link above.")
        return {"cloudflare": cloudflare_url, "colab": colab_url}

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

    def check_update(self, ref="main"):
        """Check official upstream without changing the running agent."""
        import runpy
        return runpy.run_path(str(TOOLS / "colab-update.py"))["check"](globals(), ref)

    def update(self, ref="main"):
        """Back up, validate upstream patches, update and restart; roll back runtime on failure."""
        import runpy
        return runpy.run_path(str(TOOLS / "colab-update.py"))["update"](globals(), self, ref)

    def dependencies(self):
        path = DATA/'.runtime-dependencies-status.json'
        return json.loads(path.read_text()) if path.exists() else {'ready':False,'phase':'not recorded yet'}

    def backup(self):
        """Save now and wait until the storage daemon confirms GitHub has the state.

        On failure, print the storage log's reason for this attempt and raise.
        The agent keeps running, so the save can be retried.
        """
        if self.process.poll() is not None:
            raise RuntimeError("Agent is not running; keep this runtime's files for recovery")
        recipe = TOOLS/'runtime-dependencies.py'
        if recipe.exists() and not Path('/tmp/hermes-dependencies-restoring').exists():
            try:run([str(INSTALL/'.venv/bin/python'),recipe,'capture',DATA],env=self.env)
            except subprocess.CalledProcessError:
                print('Package inventory could not refresh; keeping its existing recipe and saving chat state.')
        offset = log_offset()
        result = subprocess.run(["gosu", "hermes", INSTALL / ".venv/bin/python", "-c", CHECKPOINT],
                                env=self.env, capture_output=True, text=True)
        output = redact_secrets(result.stdout + result.stderr, self.env).strip()
        if output:
            print(output)
        if result.returncode == 0:
            return
        advice, line = explain_backup_failure(output, redact_secrets(log_since(offset), self.env))
        if line:
            print("Storage log:", line)
        raise RuntimeError("Backup not confirmed; the agent is still running. " + advice)

    def stop(self, force=False):
        """Save, then stop the agent and everything it started.

        Without force, a failed backup() raises before anything is stopped.
        force=True skips the save entirely, so a stuck runtime can always be
        restarted. It accepts losing anything the storage daemon has not pushed
        to GitHub, including the last changes before the save gate failed (the
        daemon may still push during its shutdown). The next start restores the
        last confirmed backup.
        """
        if force:
            print("Force stop: no save is attempted. Anything not yet pushed to GitHub is lost.")
        else:
            self.backup()  # On failure, leave the agent and local data running.
        ColabAgent.stop_tunnel(self)
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
        if force:
            print("Stopped without a confirmed save. Press \u25b6 to restart from the last GitHub backup.")
        else:
            print("Saved and stopped. You can now start the Render/local copy.")


class StartupProgress:
    STAGES = ('Preparing Hermes', 'Restoring saved sessions', 'Checking core tools',
              'Starting dashboard and Telegram', 'Connecting dashboard URL')

    def __init__(self, cache, warm=False, clock=time.monotonic):
        self.path = Path(cache) / 'startup-timings.json'
        self.kind = 'relaunch' if warm else 'fresh'
        self.clock = clock
        self.started = self.stage_started = clock()
        self.index = 0
        self.durations = {}
        self.history = {}
        self.handle = None
        self.state = 'loading'
        self.restore_detail = ''
        self.done = threading.Event()
        self.lock = threading.RLock()
        try:
            if self.path.stat().st_size <= 16384:
                raw = json.loads(self.path.read_text())
                for kind in ('fresh', 'relaunch'):
                    row = raw.get(kind, {})
                    if isinstance(row, dict) and all(isinstance(row.get(stage), (int, float))
                            and not isinstance(row[stage], bool) and math.isfinite(row[stage])
                            and 0 <= row[stage] <= 86400 for stage in self.STAGES):
                        self.history[kind] = row
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def duration(seconds):
        seconds = max(0, int(seconds))
        return f'{seconds // 60}m {seconds % 60:02d}s'

    def snapshot(self):
        with self.lock:
            elapsed = self.clock() - self.started
            percent = 100 if self.state == 'ready' else self.index * 20
            stage = 'Hermes is ready' if self.state == 'ready' else self.STAGES[self.index]
            if self.state == 'paused':
                stage = 'Startup needs attention. Check the message below.'
            previous = self.history.get(self.kind)
            if self.state == 'ready':
                eta = 'Ready'
            elif self.state == 'paused':
                eta = 'Estimate paused'
            elif previous:
                current_remaining = previous[self.STAGES[self.index]] - (self.clock() - self.stage_started)
                if current_remaining <= 0:
                    eta = 'Taking longer than last time; estimate updating'
                else:
                    remaining = current_remaining + sum(previous[stage] for stage in self.STAGES[self.index + 1:])
                    eta = 'About ' + self.duration(remaining) + ' left (based on your last similar start)'
            else:
                eta = 'Calculating time left on this first ' + self.kind + ' start'
            return {'stage':stage, 'percent':percent, 'elapsed':self.duration(elapsed), 'eta':eta, 'detail':self.restore_detail}

    def render(self):
        try:
            self._render()
        except Exception:
            # Notebook output is optional; it must not stop the agent.
            self.handle = None

    def _render(self):
        view = self.snapshot()
        if self.handle is not None:
            from IPython.display import HTML
            content = f'''<div role="status" aria-live="polite" style="padding:16px;border:1px solid #93c5fd;border-radius:16px;background:#eff6ff;color:#172554;max-width:640px;font-family:system-ui">
<div style="font-weight:700">{html.escape(view['stage'])}</div>
<progress aria-label="Startup steps completed" max="100" value="{view['percent']}" style="width:100%;height:12px;accent-color:#2563eb;margin:10px 0"></progress>
<div>Elapsed: {view['elapsed']} · {html.escape(view['eta'])}</div>
<div>{html.escape(view['detail'])}</div>
<small>Bar shows completed steps. Download speed can change the estimate.</small></div>'''
            self.handle.update(HTML(content))
        else:
            print(f"[startup] {view['stage']} | elapsed {view['elapsed']} | {view['eta']}", flush=True)

    def step(self, index):
        with self.lock:
            if index <= self.index:
                return
            now = self.clock()
            self.durations[self.STAGES[self.index]] = now - self.stage_started
            for skipped in range(self.index + 1, index):
                self.durations[self.STAGES[skipped]] = 0
            self.index, self.stage_started = index, now
        self.render()

    def observe(self, text):
        # Only fixed milestones affect the UI. Log contents and secrets are
        # never interpolated into the notebook progress display.
        chunks = re.findall(r'\[restore\] Assembling .*?: (\d+)/(\d+) bytes, chunk (\d+)/(\d+)', text)
        if chunks:
            done, total, part, parts = map(int, chunks[-1])
            self.restore_detail = f'Large backup: {done / 1048576:.1f} / {total / 1048576:.1f} MiB · chunk {part}/{parts}'
            self.render()
        if '[restore] Large backups verified; restoring local files' in text:
            self.restore_detail = 'Large backups verified. Restoring local files…'
            self.render()
        files = re.findall(r'\[restore\] Local files: (\d+)/(\d+) processed', text)
        if files:
            done, total = map(int, files[-1])
            self.restore_detail = f'Local files: {done:,} / {total:,} processed'
            self.render()
        if '[render-tools] state restore finished' in text:
            self.step(2)
        if '[render-tools] starting one foreground' in text:
            self.step(3)

    def finish(self):
        with self.lock:
            self.durations[self.STAGES[self.index]] = self.clock() - self.stage_started
            self.state = 'ready'
            self.history[self.kind] = {stage:self.durations.get(stage, 0) for stage in self.STAGES}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.history))
                temporary.replace(self.path)
            except OSError:
                pass
        self.render()

    def __enter__(self):
        try:
            from IPython import get_ipython
            from IPython.display import HTML, display
            if get_ipython() is not None:
                self.handle = display(HTML('Starting Hermes…'), display_id=True)
        except Exception:
            self.handle = None
        self.render()
        def refresh():
            while not self.done.wait(1 if self.handle is not None else 10):
                try:
                    self.render()
                except Exception:
                    # Display errors cannot interrupt startup or its backup controls.
                    pass
        self.thread = threading.Thread(target=refresh, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *error):
        self.done.set()
        self.thread.join(timeout=2)
        if self.state != 'ready':
            self.state = 'paused'
            self.render()


def main(confirm_switch=False):
    if sys.version_info < (3, 11):
        raise RuntimeError("Use a current notebook Python runtime (Python 3.11 or newer)")
    previous = globals().get("HERMES_COLAB")
    if previous and previous.process.poll() is None:
        print("Already running. Use HERMES_COLAB.status() or HERMES_COLAB.stop().")
        ColabAgent.dashboard(previous)
        return previous
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("This installer needs a root Linux notebook runtime (Colab or Kaggle)")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 10000)) == 0:
            raise RuntimeError("Port 10000 is already occupied; stop the old launcher first")
    NOTEBOOK_ROOT.mkdir(parents=True, exist_ok=True)
    print("Notebook host:", NOTEBOOK_HOST.title())
    print("Stop your other Hermes host after its successful backup before switching.")
    if not confirm_switch and input("Type SWITCH to confirm the other copy is stopped: ").strip() != "SWITCH":
        raise RuntimeError("No changes made")
    token = get_bootstrap_secret("GIT_STATE_TOKEN", "GitHub storage token: ")
    key = get_bootstrap_secret("STORAGE_ENCRYPTION_KEY", "Existing storage encryption key: ")
    prepare_cache()
    password = get_bootstrap_secret("HERMES_DASHBOARD_PASSWORD", "Dashboard password: ")
    env = runtime_env(token, key, password)
    warm = all((INSTALL / name).exists() for name in (
        '.venv/bin/python', 'hermes_cli/web_dist/index.html', 'ui-tui/dist/entry.js'))
    with StartupProgress(cache_directory(), warm=warm) as progress:
        return launch_with_progress(env, progress)


def start_update_control():
    import runpy
    runpy.run_path(str(TOOLS / "colab-update-control.py"))["start"](globals())


def launch_with_progress(env, progress):
    install(env)
    progress.step(1)
    offset = log_offset()
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as logfile:
        process = subprocess.Popen(["bash", str(TOOLS / "bootstrap.sh"), "sleep", "infinity"],
                                   env=env, cwd=INSTALL, stdout=logfile, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    agent = ColabAgent(process, env)
    # Keep the stop/backup handle even if initialization later times out.
    globals()["HERMES_COLAB"] = agent
    print(f"Restoring saved data. Startup logs: {LOG}")
    deadline = time.monotonic() + int(env.get("HERMES_COLAB_STARTUP_TIMEOUT_SECONDS", "1800"))
    while time.monotonic() < deadline:
        try:
            with LOG.open('rb') as startup_log:
                startup_log.seek(max(offset, LOG.stat().st_size - 32768))
                log_text = startup_log.read().decode('utf-8', errors='replace')
                progress.observe(log_text)
                if 'private state restore failed; refusing to start' in log_text:
                    raise RuntimeError('Saved-state restore failed. Keep this runtime open. '
                                       'Verified chunks are cached for retry; inspect ' + str(LOG))
        except OSError:
            pass
        if process.poll() is not None:
            raise RuntimeError(f"Startup exited. Inspect {LOG}; data has not been deleted")
        try:
            with urllib.request.urlopen("http://127.0.0.1:10000/healthz", timeout=3) as response:
                health = json.load(response)
            if health.get("gateway_running") and health.get("gateway_state") in ("running", "degraded"):
                if health.get("gateway_state") == "degraded":
                    print("Hermes is running with a platform warning. Use HERMES_COLAB.status() to check it.")
                break
        except (OSError, ValueError):
            pass
        time.sleep(2)
    else:
        raise RuntimeError(f"Startup is still pending. Inspect {LOG} and use HERMES_COLAB.status()")
    progress.step(4)
    start_update_control()
    agent.dashboard()
    progress.finish()
    print("Hermes is running. Use HERMES_COLAB.status() to check Telegram and other platforms, and save before switching hosts.")
    return agent


if __name__ == "__main__":
    HERMES_COLAB = main()


