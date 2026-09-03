#!/opt/hermes/.venv/bin/python
"""Make tui_gateway's persistent slash-command worker opt-out.

Every chat session in the in-browser dashboard spawns a *second* full
Python interpreter -- ``python -m tui_gateway.slash_worker``, which imports
the complete ``cli.HermesCLI`` stack -- just to answer the interactive TUI's
slash-menu commands. That is tens of MB of resident RAM per open
conversation on a 512 MB Free instance, and the web chat plugin never drives
the slash menu. Upstream has no env knob for it (the three ``_SlashWorker``
construction sites spawn unconditionally), so this pinned-source patch gates
each spawn site behind HERMES_TUI_DISABLE_SLASH_WORKER:

    if os.environ.get("HERMES_TUI_DISABLE_SLASH_WORKER", "").strip().lower()
            not in ("1", "true", "yes", "on"):
        ... original spawn ...

When disabled the worker simply never starts; ``slash.exec`` already has a
hard-failure fallback ("chat still works without slash worker") and every
normal chat/tool request goes through the in-process agent, not the worker.

Like patch-model-discovery.py, every replacement is exact and the script
fails loudly if the pinned upstream source changes underneath the pin.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "render-tools: slash worker opt-out"
GATE = ('if os.environ.get("HERMES_TUI_DISABLE_SLASH_WORKER", "")'
        '.strip().lower() not in ("1", "true", "yes", "on"):  # ' + MARKER)


def _indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(
        pad + line if line.strip() else line
        for line in text.splitlines()
    )


def gated(old: str) -> str:
    """Wrap an exact spawn-site snippet in the env gate, re-indented.

    The snippet's own first line sits at the gate's indentation; every line
    of the body moves right by one extra indent (4 spaces).
    """
    leading = len(old) - len(old.lstrip())
    pad = " " * leading
    body = _indent_block(old.lstrip("\n"), 4)
    return pad + GATE + "\n" + body


# The three unconditional spawn sites in tui_gateway/server.py.
REPLACEMENTS = [
    """            try:
                worker = _SlashWorker(key, getattr(agent, "model", _resolve_model()))
                current["slash_worker"] = worker
            except Exception:
                pass""",
    """    try:
        _sessions[sid]["slash_worker"] = _SlashWorker(
            key, getattr(agent, "model", _resolve_model())
        )
    except Exception:
        # Defer hard-failure to slash.exec; chat still works without slash worker.
        _sessions[sid]["slash_worker"] = None""",
    """    try:
        session["slash_worker"] = _SlashWorker(
            session["session_key"],
            getattr(session.get("agent"), "model", _resolve_model()),
        )
    except Exception:
        session["slash_worker"] = None""",
]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: patch-slash-worker.py <path/to/tui_gateway/server.py>",
              file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[render-tools] {path} already patched; nothing to do")
        return 0
    for index, old in enumerate(REPLACEMENTS, start=1):
        if old not in text:
            raise SystemExit(
                f"expected slash-worker spawn site #{index} not found in "
                f"{path}; the pinned Hermes source changed and this patch "
                "needs updating"
            )
        text = text.replace(old, gated(old), 1)
    path.write_text(text, encoding="utf-8")
    print(f"[render-tools] patched {path}: slash worker now gated by "
          "HERMES_TUI_DISABLE_SLASH_WORKER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
