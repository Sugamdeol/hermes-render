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

When disabled the worker simply never starts. ``slash.exec`` answers with an
explicit error instead of quietly spawning one anyway, and every normal
chat/tool request goes through the in-process agent rather than the worker.

There are FOUR construction sites, not three. The fourth is the lazy start
inside ``slash.exec`` -- it exists precisely as the "no worker yet" fallback,
so gating only the eager sites leaves the flag meaning "don't start it up
front" rather than "never". That distinction is worth ~60-85 MB of RSS per
conversation on a 512 MB instance, which is the whole point of the flag.

After patching, the script re-scans the file and fails if ANY ``_SlashWorker(``
construction site is left outside a gate. That is the part that keeps this
honest across a Hermes upgrade: a future tag that adds a fifth site breaks the
build with a message naming the line, instead of silently reintroducing a
per-conversation interpreter.

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


# The three *eager* spawn sites in tui_gateway/server.py, each wrapped in the
# gate by gated() below.
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


# The fourth site: the lazy start inside the ``slash.exec`` handler. It cannot
# be wrapped the same way -- the handler needs to answer the caller -- so it
# becomes an explicit refusal instead. Chat and tool traffic never reach this
# code path; only a literal slash command typed into a client that drives the
# TUI menu does, and the web dashboard does not.
LAZY_SITE_OLD = """    worker = session.get("slash_worker")
    if not worker:
        try:
            worker = _SlashWorker(
                session["session_key"],
                getattr(session.get("agent"), "model", _resolve_model()),
            )
            session["slash_worker"] = worker
        except Exception as e:
            return _err(rid, 5030, f"slash worker start failed: {e}")
"""

LAZY_SITE_NEW = """    worker = session.get("slash_worker")
    if not worker:
        if os.environ.get("HERMES_TUI_DISABLE_SLASH_WORKER", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):  # {marker}
            return _err(
                rid,
                5030,
                "slash commands are disabled on this instance "
                "(HERMES_TUI_DISABLE_SLASH_WORKER=1); chat and tools are unaffected",
            )
        try:
            worker = _SlashWorker(
                session["session_key"],
                getattr(session.get("agent"), "model", _resolve_model()),
            )
            session["slash_worker"] = worker
        except Exception as e:
            return _err(rid, 5030, f"slash worker start failed: {{e}}")
""".format(marker=MARKER)


def ungated_sites(text: str) -> "list[int]":
    """1-based line numbers of ``_SlashWorker(`` constructions outside a gate.

    Scans after patching, so a Hermes upgrade that adds a new spawn site fails
    the build with the line number instead of quietly putting a per-session
    interpreter back into a 512 MB container.
    """
    lines = text.splitlines()
    bad: "list[int]" = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if "_SlashWorker(" not in stripped:
            continue
        # the class definition itself, and any comment/doc mention
        if stripped.startswith("class _SlashWorker") or stripped.startswith("#"):
            continue
        window = "\n".join(lines[max(0, index - 12):index + 1])
        if MARKER not in window:
            bad.append(index + 1)
    return bad


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
    if LAZY_SITE_OLD not in text:
        raise SystemExit(
            f"expected the lazy slash.exec spawn site not found in {path}; "
            "the pinned Hermes source changed and this patch needs updating"
        )
    text = text.replace(LAZY_SITE_OLD, LAZY_SITE_NEW, 1)

    leftover = ungated_sites(text)
    if leftover:
        raise SystemExit(
            f"{path}: _SlashWorker() is still constructed outside the "
            f"HERMES_TUI_DISABLE_SLASH_WORKER gate at line(s) "
            f"{', '.join(str(n) for n in leftover)}. Upstream added a spawn "
            "site this patch does not know about; gate it too, or a dashboard "
            "chat session will start a second full Python interpreter again."
        )
    path.write_text(text, encoding="utf-8")
    print(f"[render-tools] patched {path}: slash worker now gated by "
          "HERMES_TUI_DISABLE_SLASH_WORKER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
