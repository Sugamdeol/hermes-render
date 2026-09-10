#!/opt/hermes/.venv/bin/python
"""Merge repo-managed secrets into Hermes' runtime environment.

The Blueprint's durable source for secrets is Render's Environment tab. This
script adds a second, version-controlled source: a SOPS-encrypted dotenv file
committed to the repo (`env/secrets.enc.env`), decrypted at boot by
bootstrap.sh and piped in here.

Precedence, highest first:

  1. The real process environment. Render's Environment tab and anything you
     export locally always win; repo values never override a live deploy knob.
  2. An existing value in $HERMES_HOME/.env. That file is written by the
     dashboard's API Keys tab and restored from the state repo, so a key
     someone set from the UI is not silently reverted to the committed one.
  3. The repo's encrypted secrets. These fill in whatever is still missing.

Pass --force to invert rule 2 and make the repo authoritative over the .env
file, which is what you want when rotating a key in git.

The merge is line-preserving: comments, ordering, and unmanaged keys in the
existing .env are kept as-is, and only missing keys are appended under a
marked block.

Two outputs:
  * The merged .env is written back to disk (0600).
  * With --print-exports, shell-quoted assignments are written to stdout for
    variables that are neither in the process environment nor already owned by
    the .env file, so the caller can eval them into the gateway's environment.
    Exporting a repo value for a key the .env already defines would invert
    rule 2, since a dotenv loader will not override a variable that is already
    set in the environment. config.yaml's ${RENDER_MCP_API_KEY}
    substitution reads the process env, so this is what makes MCP auth work.

Never log the values. stdout is meant for `eval`, not for a terminal.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

MANAGED_HEADER = "# --- added from the repo's encrypted secrets (env/secrets.enc.env) ---"


def parse_dotenv(text: str) -> "list[tuple[str, str]]":
    """Parse dotenv text into ordered (key, value) pairs.

    Deliberately small: `KEY=value`, optional `export ` prefix, optional
    matching single/double quotes, `#` comments, blank lines. Values are taken
    literally otherwise -- no interpolation, because an API key containing `$`
    must survive a round trip.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not is_valid_key(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs.append((key, value))
    return pairs


def is_valid_key(key: str) -> bool:
    return key.replace("_", "").isalnum() and not key[0].isdigit()


def existing_keys(text: str) -> "set[str]":
    return {key for key, _ in parse_dotenv(text)}


def merge(env_text: str, secrets: "list[tuple[str, str]]", *, force: bool) -> "tuple[str, list[str]]":
    """Return (new_env_text, keys_added)."""
    present = existing_keys(env_text)
    added: list[str] = []
    kept: list[tuple[str, str]] = []

    for key, value in secrets:
        if key in present and not force:
            continue
        kept.append((key, value))
        added.append(key)

    if not kept:
        return env_text, []

    if force and present:
        # Rewrite in place for keys that already exist, so we don't end up with
        # a duplicate definition where the last one silently wins.
        overridden = {key: value for key, value in kept if key in present}
        if overridden:
            lines_out = []
            for raw in env_text.splitlines():
                stripped = raw.strip()
                candidate = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
                if stripped and not stripped.startswith("#") and "=" in candidate:
                    key = candidate.split("=", 1)[0].strip()
                    if key in overridden:
                        lines_out.append(f"{key}={overridden[key]}")
                        continue
                lines_out.append(raw)
            env_text = "\n".join(lines_out)
            if env_text and not env_text.endswith("\n"):
                env_text += "\n"
            kept = [(k, v) for k, v in kept if k not in overridden]

    if not kept:
        return env_text, added

    if env_text and not env_text.endswith("\n"):
        env_text += "\n"
    block = [MANAGED_HEADER] + [f"{key}={value}" for key, value in kept]
    return env_text + "\n".join(block) + "\n", added


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"[render-tools] cannot read {path}: {exc}", file=sys.stderr)
        return ""


def write_env(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return True
    except OSError as exc:
        print(f"[render-tools] cannot write {path}: {exc}", file=sys.stderr)
        return False


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", required=True,
                        help="decrypted dotenv file, or - to read stdin")
    parser.add_argument("--env-file", required=True,
                        help="target .env, normally $HERMES_HOME/.env")
    parser.add_argument("--force", action="store_true",
                        help="let repo secrets override values already in the .env file")
    parser.add_argument("--print-exports", action="store_true",
                        help="write shell-quoted exports for vars missing from the process env")
    args = parser.parse_args(argv)

    if args.secrets == "-":
        secrets_text = sys.stdin.read()
    else:
        secrets_path = Path(args.secrets)
        if not secrets_path.exists():
            print("[render-tools] no repo secrets file; skipping", file=sys.stderr)
            return 0
        secrets_text = read_text(secrets_path)

    secrets = parse_dotenv(secrets_text)
    # An empty placeholder value means "declared but not set"; skip it so a
    # scaffolded key never blanks out a real one.
    secrets = [(k, v) for k, v in secrets if v != ""]
    if not secrets:
        print("[render-tools] repo secrets file has no values; skipping", file=sys.stderr)
        return 0

    env_path = Path(args.env_file)
    env_text = read_text(env_path)
    # Captured before the merge: these are the keys the dashboard or a state
    # restore already owns, which outrank the repo unless --force.
    preexisting = existing_keys(env_text)
    new_text, added = merge(env_text, secrets, force=args.force)

    if added and not write_env(env_path, new_text):
        return 1

    # Names only -- never values.
    if added:
        print(f"[render-tools] seeded {len(added)} secret(s) into {env_path}: "
              f"{', '.join(sorted(added))}", file=sys.stderr)
    else:
        print("[render-tools] repo secrets already present; nothing to seed", file=sys.stderr)

    if args.print_exports:
        for key, value in secrets:
            if os.environ.get(key):
                continue  # the live environment always wins
            if key in preexisting and not args.force:
                continue  # ...and so does a value already in the .env file
            sys.stdout.write(f"export {key}={shlex.quote(value)}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
