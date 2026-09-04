"""The bounded-session-registry patch (scripts/patch-session-cap.py).

An unbounded ``tui_gateway._sessions`` is the quiet OOM path: every entry is a
live AIAgent, and browser tabs that vanish without a clean close leave them
behind, so the growth is linear in how many there are. These tests pin the
patch's contract -- that it applies to the pinned source shape, that eviction
keeps the transcript, and that a session mid-turn is never torn down.
"""
from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "patch-session-cap.py"
)
_spec = importlib.util.spec_from_file_location("patch_session_cap", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_patch)


def _fixture() -> str:
    """A module shaped like the pinned tui_gateway/server.py, minus 6000 lines."""
    return "\n".join([
        "import threading",
        "",
        _patch.REGISTRY_ANCHOR.rstrip("\n"),
        "",
        "closed = []",
        "finalized = []",
        "",
        "",
        "def _finalize_session(session, end_reason=\"tui_close\"):",
        "    finalized.append((session.get('session_key'), end_reason))",
        "",
        "",
        "class Agent:",
        "    def close(self):",
        "        closed.append('agent')",
        "",
        "",
        "def create_session(sid):",
        "    _sessions[sid] = {'session_key': sid, 'agent': Agent(),",
        "                      'slash_worker': None, 'running': False}",
        "",
    ]) + "\n"


class PatchSessionCapTests(unittest.TestCase):
    def test_patch_applies_and_result_parses(self):
        source = _fixture() + "\n".join([
            "def session_create(sid):",
            "    build_timer = threading.Timer(0.05, _deferred_build)",
            "    build_timer.daemon = True",
            "    build_timer.start()",
            "",
            "    return _ok(",
            "        rid,",
            "        {'session_id': sid},",
            "    )",
            "",
        ]) + "\n"
        ast.parse(source)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server.py"
            target.write_text(source, encoding="utf-8")
            self.assertEqual(_patch.main(["patch-session-cap.py", str(target)]), 0)
            patched = target.read_text(encoding="utf-8")
        ast.parse(patched)
        self.assertIn(_patch.MARKER, patched)
        self.assertIn("_render_tools_evict_sessions(sid)", patched)

    def test_patch_is_idempotent(self):
        source = _fixture() + _patch.CREATE_ANCHOR + "        rid,\n    )\n"
        source = source.replace("    return _ok(\n", "    return _ok(\n", 1)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server.py"
            target.write_text(source, encoding="utf-8")
            _patch.main(["patch-session-cap.py", str(target)])
            once = target.read_text(encoding="utf-8")
            self.assertEqual(_patch.main(["patch-session-cap.py", str(target)]), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), once)
            self.assertEqual(once.count(_patch.MARKER), 2)

    def test_patch_fails_loudly_when_the_anchor_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server.py"
            target.write_text("import threading\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                _patch.main(["patch-session-cap.py", str(target)])

    def test_eviction_keeps_the_cap_and_commits_the_transcript(self):
        """The behaviour that matters: over-cap sessions are finalized (so the
        transcript survives in the session DB) and their agent is closed."""
        namespace = self._evict_namespace(cap="2")
        evict = namespace["_render_tools_evict_sessions"]
        sessions = namespace["_sessions"]
        for sid in ("a", "b", "c", "d"):
            namespace["create_session"](sid)
        evict("d")
        self.assertEqual(len(sessions), 2)
        # oldest evicted, newest (and the one just created) kept
        self.assertEqual(sorted(sessions), ["c", "d"])
        self.assertEqual([k for k, _ in namespace["finalized"]], ["a", "b"])
        self.assertEqual({reason for _, reason in namespace["finalized"]},
                         {"session_cap"})
        self.assertEqual(len(namespace["closed"]), 2)

    def test_a_running_session_is_never_evicted(self):
        namespace = self._evict_namespace(cap="1")
        evict = namespace["_render_tools_evict_sessions"]
        sessions = namespace["_sessions"]
        namespace["create_session"]("busy")
        sessions["busy"]["running"] = True
        namespace["create_session"]("new")
        evict("new")
        self.assertIn("busy", sessions, "mid-turn session must survive eviction")

    def test_cap_zero_disables_eviction(self):
        namespace = self._evict_namespace(cap="0")
        for sid in ("a", "b", "c"):
            namespace["create_session"](sid)
        namespace["_render_tools_evict_sessions"]("c")
        self.assertEqual(len(namespace["_sessions"]), 3)

    def _evict_namespace(self, cap: str) -> dict:
        """Exec the patched helper with HERMES_TUI_MAX_SESSIONS held at `cap`.

        The cap is read when eviction runs, not when the module is imported,
        so the variable has to stay set for the whole test -- addCleanup
        restores it rather than a try/finally around the exec.
        """
        import os

        previous = os.environ.get("HERMES_TUI_MAX_SESSIONS")
        os.environ["HERMES_TUI_MAX_SESSIONS"] = cap

        def restore():
            if previous is None:
                os.environ.pop("HERMES_TUI_MAX_SESSIONS", None)
            else:
                os.environ["HERMES_TUI_MAX_SESSIONS"] = previous

        self.addCleanup(restore)
        namespace = {"__name__": "fixture"}
        exec(compile(_fixture() + _patch.HELPER, "<fixture>", "exec"), namespace)
        return namespace


if __name__ == "__main__":
    unittest.main()
