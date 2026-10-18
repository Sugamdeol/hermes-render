"""Avoid replaying both reasoning aliases to unknown custom provider schemas."""
from pathlib import Path
import sys

OLD = 'frozenset({"ollama-cloud", "custom", "lmstudio", "upstage"})'
NEW = 'frozenset({"ollama-cloud", "lmstudio", "upstage"})'


def patch(root):
    path = Path(root) / "agent/message_sanitization.py"
    text = path.read_text()
    if NEW in text and OLD not in text:
        return False
    if text.count(OLD) != 1:
        raise ValueError("Reasoning route table changed; refusing an unverified patch")
    # Unknown remote custom endpoints get the standard reasoning_content carrier.
    # Known vendor hosts and local endpoints retain their native carrier policy.
    updated = text.replace(OLD, NEW, 1)
    compile(updated, str(path), "exec")
    temporary = path.with_suffix(".reasoning.tmp")
    temporary.write_text(updated)
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)
    return True


if __name__ == "__main__":
    changed = patch(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes")
    print("[reasoning] custom provider alias compatibility " + ("repaired" if changed else "already repaired"))
