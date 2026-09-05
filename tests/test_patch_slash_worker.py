from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "patch-slash-worker.py"
)
_spec = importlib.util.spec_from_file_location("patch_slash_worker", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_patch)


# The three unconditional _SlashWorker spawn sites, exactly as the pinned
# upstream tui_gateway/server.py ships them (v2026.5.7).
UPSTREAM_SNIPPETS = [
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


class PatchSlashWorkerTests(unittest.TestCase):
    def test_every_spawn_site_is_wrapped_in_the_env_gate(self):
        for index, snippet in enumerate(UPSTREAM_SNIPPETS, start=1):
            with self.subTest(site=index):
                out = _patch.gated(snippet)
                # The gate guard appears at the site's own indentation.
                self.assertIn("HERMES_TUI_DISABLE_SLASH_WORKER", out)
                self.assertIn(_patch.MARKER, out)
                # The original spawn is still fully present, just re-indented.
                for line in snippet.splitlines():
                    if line.strip():
                        self.assertIn(line.strip(), out)
                # Wrapped result stays valid Python.
                ast.parse("def f():\n" + out)

    def test_gate_body_is_indented_exactly_four_spaces_deeper(self):
        out = _patch.gated(UPSTREAM_SNIPPETS[2])
        lines = out.splitlines()
        gate_line = next(l for l in lines if l.lstrip().startswith("if os.environ"))
        self.assertTrue(gate_line.startswith("    if "))
        body_line = next(l for l in lines if "session[\"slash_worker\"] = _SlashWorker(" in l)
        self.assertTrue(body_line.startswith("        "))

    def test_patch_runs_end_to_end_via_main(self):
        # A module-shaped fixture: import os, then the three spawn sites at
        # their real indentation columns (module level 4, nested block 12).
        # Site 1 ships at a 12-space indent (nested inside a build
        # function/thread); sites 2-3 at 4 spaces (function bodies). Put site
        # 1 inside an 8-space block: dedent the snippet by one level (4
        # spaces) and let the caller's 4-space indent bring it back to 12.
        source_lines = [
            "import os", "",
            "def spawn(agent, current, key):",
            "    if True:",
        ]
        dedented = "\n".join(
            line[4:] if line.startswith("    ") else line
            for line in UPSTREAM_SNIPPETS[0].splitlines()
        )
        source_lines += [
            "    " + line if line.strip() else line
            for line in dedented.splitlines()
        ]
        source_lines += [""]
        source_lines += ["def spawn_two(sid, agent, key):"]
        source_lines += UPSTREAM_SNIPPETS[1].splitlines()
        source_lines += [""]
        source_lines += ["def spawn_three(session):"]
        source_lines += UPSTREAM_SNIPPETS[2].splitlines()
        source = "\n".join(source_lines) + "\n"
        ast.parse(source)  # fixture sanity

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server.py"
            target.write_text(source, encoding="utf-8")

            rc = _patch.main(["patch-slash-worker.py", str(target)])
            self.assertEqual(rc, 0)
            patched = target.read_text(encoding="utf-8")

        # All three sites gated, module still parses, spawn no longer runs
        # unconditionally.
        self.assertEqual(patched.count(_patch.MARKER), 3)
        ast.parse(patched)
        self.assertIn("HERMES_TUI_DISABLE_SLASH_WORKER", patched)

        # Idempotent: a second run reports "already patched" and changes
        # nothing.
        before = patched
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server.py"
            target.write_text(before, encoding="utf-8")
            rc = _patch.main(["patch-slash-worker.py", str(target)])
            self.assertEqual(rc, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_main_fails_loudly_when_a_spawn_site_is_missing(self):
        # If upstream reshapes one site, the exact replacement no longer
        # matches and the build must fail rather than silently ship
        # unguarded spawns.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server.py"
            target.write_text("import os\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                _patch.main(["patch-slash-worker.py", str(target)])

    def test_marker_marks_already_patched_sources(self):
        # main() treats a source containing the marker as patched and leaves
        # it untouched; the marker string is what it writes.
        self.assertIn("render-tools", _patch.MARKER)


if __name__ == "__main__":
    unittest.main()
