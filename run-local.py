#!/usr/bin/env python3
"""Single-file Hermes launcher for Windows, macOS and Linux. Requires Docker.

Run: python run-local.py [start|logs|status|backup|stop|password]
The launcher downloads/builds Sugamdeol/hermes-render and restores the same
private Sugamdeol/hermes-storage@state backup. Secrets are prompted privately.
Stop Render/the other launcher and wait for its backup before switching hosts.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.request

CODE_URL = "https://github.com/Sugamdeol/hermes-render.git"
NAME = "hermes-shared-local"
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
            if not result.get('ok'): raise SystemExit('Backup failed; keep this host running and check logs.')
            print('Private GitHub backup saved.'); break
    except (OSError,ValueError): pass
    time.sleep(.5)
else: raise SystemExit('Backup timed out; this host has NOT been stopped.')
'''


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def runtime_env(token, key):
    values = {
        "GIT_STATE_REPO": os.environ.get("GIT_STATE_REPO", "Sugamdeol/hermes-storage"),
        "GIT_STATE_BRANCH": os.environ.get("GIT_STATE_BRANCH", "state"),
        "GIT_STATE_TOKEN": token, "STORAGE_ENCRYPTION_KEY": key,
        "GIT_STATE_ENV_MODE": "encrypt", "HERMES_FAILOVER": "0",
        "HERMES_HOME": "/opt/data", "GIT_STATE_WORKDIR": "/tmp/hermes-git-cache/state",
        "HERMES_UID": "", "HERMES_GID": "",
        "HERMES_RECOVERY_CHECKPOINT_FILE": "/tmp/hermes-recovery-checkpoint.json",
        "HERMES_INSTANCE_ID": "local-" + secrets.token_hex(6),
        "PORT": "10000", "RENDER_EXTERNAL_URL": "",
        "TELEGRAM_WEBHOOK_URL": "", "TELEGRAM_WEBHOOK_PORT": "",
        "TELEGRAM_WEBHOOK_SECRET": "",
    }
    values["HERMES_ENV_OVERRIDE_KEYS"] = ",".join([*values, "PATH", "HERMES_ENV_OVERRIDE_KEYS"])
    if any("\n" in v or "\r" in v for v in values.values()):
        raise ValueError("Bootstrap values must not contain newlines")
    if not token or not key:
        raise ValueError("GitHub token and existing encryption key are required")
    return values


def running():
    p = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", NAME],
                       capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


def backup():
    if not running():
        raise RuntimeError("The local container is not running")
    # Ask the existing storage daemon to save. Never start a second Git writer
    # or overwrite the captured runtime environment from a minimal exec env.
    run(["docker", "exec", "--user", "hermes", NAME,
         "/opt/hermes/.venv/bin/python", "-c", CHECKPOINT])


def password():
    code = ("import importlib.util,json;from pathlib import Path;"
            "s=importlib.util.spec_from_file_location('seed','/opt/render-tools/seed-env.py');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "v=dict(m.parse_dotenv(Path('/opt/data/.env').read_text()));"
            "p=Path('/opt/data/.render-runtime-env.json');"
            "v.update(json.loads(p.read_text()).get('variables',{}) if p.exists() else {});"
            "print(v.get('HERMES_GATEWAY_TOKEN','Password is not ready yet'))")
    run(["docker", "exec", "--user", "hermes", NAME,
         "/opt/hermes/.venv/bin/python", "-c", code])


def start(port):
    if running():
        print(f"Already running: http://127.0.0.1:{port}")
        return
    print("Before switching: stop the Render/Colab copy after its latest backup.")
    if input("Type SWITCH to confirm the other copy is stopped: ").strip() != "SWITCH":
        raise RuntimeError("No changes made")
    token = os.environ.get("GIT_STATE_TOKEN") or getpass.getpass("GitHub storage token: ")
    key = os.environ.get("STORAGE_ENCRYPTION_KEY") or getpass.getpass("Existing storage encryption key: ")
    values = runtime_env(token, key)
    cache = Path.home() / ".cache" / "hermes-shared-launcher"
    cache.mkdir(parents=True, exist_ok=True)
    source = cache / "source"
    git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if not source.exists():
        run(["git", "clone", "--depth", "1", "--branch", "main", CODE_URL, str(source)], env=git_env)
    else:
        if not (source / ".git").is_dir():
            raise RuntimeError(f"Unexpected cache contents at {source}; choose another cache directory")
        origin = run(["git", "remote", "get-url", "origin"], cwd=source,
                     capture_output=True, text=True).stdout.strip()
        if origin != CODE_URL:
            raise RuntimeError("Cache belongs to a different repository")
        run(["git", "fetch", "--depth", "1", "origin", "main"], cwd=source, env=git_env)
        run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=source)
    revision = run(["git", "rev-parse", "HEAD"], cwd=source,
                   capture_output=True, text=True).stdout.strip()
    image = "hermes-shared-local:" + revision[:12]
    exists = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if exists.returncode:
        run(["docker", "build", "-t", image, str(source)])
    # Mount the cache's parent, not the clone itself: Git must be able to
    # replace/recreate its clone, and the unprivileged Hermes user must own it.
    run(["docker", "run", "--rm", "--entrypoint", "/bin/sh",
         "-v", NAME + "-git:/tmp/hermes-git-cache", image, "-c",
         "chown hermes:hermes /tmp/hermes-git-cache && chmod 700 /tmp/hermes-git-cache"])
    old = subprocess.run(["docker", "inspect", NAME], capture_output=True)
    if old.returncode == 0:
        run(["docker", "rm", NAME])  # Stopped container only; data volumes stay.
    fd, filename = tempfile.mkstemp(prefix="hermes-bootstrap-", suffix=".env")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")
        run(["docker", "run", "-d", "--name", NAME, "--restart", "unless-stopped",
             "--stop-timeout", "120", "-p", f"127.0.0.1:{port}:10000",
             "--env-file", filename,
             "-v", NAME + "-data:/opt/data",
             "-v", NAME + "-git:/tmp/hermes-git-cache", image])
    finally:
        Path(filename).unlink(missing_ok=True)
    print("Restoring private state. First build/restore can take several minutes.")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if not running():
            raise RuntimeError("Container stopped; run: python run-local.py logs")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
                health = json.load(response)
            if health.get("gateway_running") and health.get("gateway_state") == "running":
                print(f"Dashboard: http://127.0.0.1:{port}\nUsername: hermes\nPassword:")
                password()
                print("Telegram uses polling here. Saved providers, memories and skills are restored.")
                print("Before moving back to Render: python run-local.py stop")
                return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("Startup is still pending; run: python run-local.py logs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "logs", "status", "backup", "stop", "password"])
    parser.add_argument("--port", type=int, default=10000)
    args = parser.parse_args()
    if not shutil.which("docker"):
        raise RuntimeError("Install/start Docker Desktop (Windows/macOS) or Docker Engine (Linux), then retry")
    run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.command == "start":
        if not shutil.which("git"):
            raise RuntimeError("Install Git, then retry")
        start(args.port)
    elif args.command == "logs":
        run(["docker", "logs", "--follow", "--tail", "100", NAME])
    elif args.command == "status":
        print("Running" if running() else "Stopped")
    elif args.command == "backup":
        backup()
    elif args.command == "password":
        password()
    elif running():
        backup()  # A failed save aborts stop, leaving the agent/data available.
        run(["docker", "stop", "--time", "120", NAME])
        print("Stopped. Persistent local volumes kept. You can now start Render/Colab.")
    else:
        print("Already stopped")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
    except KeyboardInterrupt:
        print("\nLocal Docker container keeps running. Use the stop command to save and stop it.")
