from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


def load_seed_env():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "seed-env.py"
    spec = importlib.util.spec_from_file_location("seed_env", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ParseDotenvTests(unittest.TestCase):
    def setUp(self):
        self.seed_env = load_seed_env()

    def test_parses_plain_export_and_quoted_forms(self):
        text = (
            "# a comment\n"
            "\n"
            "PLAIN=value1\n"
            "export EXPORTED=value2\n"
            'DQUOTED="value3"\n'
            "SQUOTED='value4'\n"
        )

        self.assertEqual(
            self.seed_env.parse_dotenv(text),
            [
                ("PLAIN", "value1"),
                ("EXPORTED", "value2"),
                ("DQUOTED", "value3"),
                ("SQUOTED", "value4"),
            ],
        )

    def test_values_are_literal_so_keys_with_dollars_survive(self):
        text = "TOKEN=abc$HOME{}def\n"

        self.assertEqual(self.seed_env.parse_dotenv(text), [("TOKEN", "abc$HOME{}def")])

    def test_ignores_malformed_lines(self):
        text = "NO_EQUALS\n=novalue\n9BAD=x\nGOOD=y\n"

        self.assertEqual(self.seed_env.parse_dotenv(text), [("GOOD", "y")])


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.seed_env = load_seed_env()

    def test_existing_env_value_wins_over_repo_secret(self):
        env_text = "ANTHROPIC_API_KEY=set-from-dashboard\n"

        merged, added = self.seed_env.merge(
            env_text, [("ANTHROPIC_API_KEY", "from-repo")], force=False
        )

        self.assertEqual(added, [])
        self.assertEqual(merged, env_text)

    def test_missing_keys_are_appended(self):
        env_text = "EXISTING=keep\n"

        merged, added = self.seed_env.merge(
            env_text, [("EXISTING", "ignored"), ("NEW_KEY", "added")], force=False
        )

        self.assertEqual(added, ["NEW_KEY"])
        self.assertIn("EXISTING=keep", merged)
        self.assertIn("NEW_KEY=added", merged)
        self.assertNotIn("EXISTING=ignored", merged)

    def test_force_rewrites_in_place_without_duplicating(self):
        env_text = "# header\nROTATED=old-value\nOTHER=untouched\n"

        merged, added = self.seed_env.merge(
            env_text, [("ROTATED", "new-value")], force=True
        )

        self.assertEqual(added, ["ROTATED"])
        self.assertEqual(merged.count("ROTATED="), 1)
        self.assertIn("ROTATED=new-value", merged)
        self.assertIn("# header", merged)
        self.assertIn("OTHER=untouched", merged)

    def test_comments_and_unmanaged_keys_are_preserved(self):
        env_text = "# keep me\nUNMANAGED=yes\n"

        merged, _ = self.seed_env.merge(env_text, [("NEW", "1")], force=False)

        self.assertIn("# keep me", merged)
        self.assertIn("UNMANAGED=yes", merged)

    def test_missing_trailing_newline_does_not_join_lines(self):
        env_text = "NO_NEWLINE=1"

        merged, _ = self.seed_env.merge(env_text, [("NEW", "2")], force=False)

        self.assertIn("NO_NEWLINE=1\n", merged)
        self.assertIn("NEW=2", merged)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.seed_env = load_seed_env()

    def run_main(self, secrets: str, env: str = "", argv_extra=()):
        with TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "secrets.env"
            env_path = Path(tmp) / ".env"
            secrets_path.write_text(secrets, encoding="utf-8")
            if env:
                env_path.write_text(env, encoding="utf-8")
            code = self.seed_env.main(
                ["--secrets", str(secrets_path), "--env-file", str(env_path), *argv_extra]
            )
            result = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            mode = env_path.stat().st_mode & 0o777 if env_path.exists() else None
            return code, result, mode

    def test_writes_env_with_owner_only_permissions(self):
        code, text, mode = self.run_main("SECRET_KEY=abc\n")

        self.assertEqual(code, 0)
        self.assertIn("SECRET_KEY=abc", text)
        self.assertEqual(mode, 0o600)

    def test_empty_placeholder_values_are_skipped(self):
        code, text, _ = self.run_main("DECLARED_BUT_UNSET=\nREAL=1\n", env="DECLARED_BUT_UNSET=live\n")

        self.assertEqual(code, 0)
        self.assertIn("DECLARED_BUT_UNSET=live", text)
        self.assertIn("REAL=1", text)

    def test_missing_secrets_file_is_not_an_error(self):
        with TemporaryDirectory() as tmp:
            code = self.seed_env.main(
                [
                    "--secrets", str(Path(tmp) / "absent.env"),
                    "--env-file", str(Path(tmp) / ".env"),
                ]
            )

        self.assertEqual(code, 0)

    def test_print_exports_skips_variables_already_in_process_env(self):
        with TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "secrets.env"
            secrets_path.write_text("IN_ENV=repo-value\nNOT_IN_ENV=repo-value\n", encoding="utf-8")
            stdout = []
            with mock.patch.dict(os.environ, {"IN_ENV": "live-value"}, clear=False):
                with mock.patch.object(
                    self.seed_env.sys, "stdout", new=_Capture(stdout)
                ):
                    code = self.seed_env.main(
                        [
                            "--secrets", str(secrets_path),
                            "--env-file", str(Path(tmp) / ".env"),
                            "--print-exports",
                        ]
                    )

        self.assertEqual(code, 0)
        # Substring checks would be fooled by NOT_IN_ENV containing IN_ENV.
        exported = {line.split("=", 1)[0].removeprefix("export ")
                    for line in "".join(stdout).splitlines() if line.strip()}
        self.assertEqual(exported, {"NOT_IN_ENV"})

    def test_print_exports_respects_values_already_owned_by_the_env_file(self):
        """A repo value must not reach the process env when .env already owns
        that key: a dotenv loader will not override an already-set variable,
        so exporting it would let the repo beat the dashboard."""
        with TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "secrets.env"
            env_path = Path(tmp) / ".env"
            secrets_path.write_text("OWNED=from-repo\nFRESH=from-repo\n", encoding="utf-8")
            env_path.write_text("OWNED=from-dashboard\n", encoding="utf-8")
            stdout = []
            with mock.patch.object(self.seed_env.sys, "stdout", new=_Capture(stdout)):
                self.seed_env.main(
                    ["--secrets", str(secrets_path), "--env-file", str(env_path),
                     "--print-exports"]
                )
            final_env = env_path.read_text(encoding="utf-8")

        exported = {line.split("=", 1)[0].removeprefix("export ")
                    for line in "".join(stdout).splitlines() if line.strip()}
        self.assertEqual(exported, {"FRESH"})
        self.assertIn("OWNED=from-dashboard", final_env)

    def test_force_lets_repo_value_reach_the_process_env(self):
        with TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "secrets.env"
            env_path = Path(tmp) / ".env"
            secrets_path.write_text("ROTATED=new-value\n", encoding="utf-8")
            env_path.write_text("ROTATED=stale-value\n", encoding="utf-8")
            stdout = []
            with mock.patch.object(self.seed_env.sys, "stdout", new=_Capture(stdout)):
                self.seed_env.main(
                    ["--secrets", str(secrets_path), "--env-file", str(env_path),
                     "--print-exports", "--force"]
                )
            final_env = env_path.read_text(encoding="utf-8")

        self.assertIn("export ROTATED=new-value", "".join(stdout))
        self.assertIn("ROTATED=new-value", final_env)
        self.assertNotIn("stale-value", final_env)

    def test_exports_are_shell_quoted(self):
        with TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "secrets.env"
            secrets_path.write_text("TRICKY=a b'c$d\n", encoding="utf-8")
            stdout = []
            with mock.patch.object(self.seed_env.sys, "stdout", new=_Capture(stdout)):
                self.seed_env.main(
                    [
                        "--secrets", str(secrets_path),
                        "--env-file", str(Path(tmp) / ".env"),
                        "--print-exports",
                    ]
                )

        line = "".join(stdout).strip()
        self.assertTrue(line.startswith("export TRICKY="))
        # Round-trips through a shell without word-splitting or expansion.
        import shlex
        self.assertEqual(shlex.split(line)[1], "TRICKY=a b'c$d")


class _Capture:
    def __init__(self, sink):
        self.sink = sink

    def write(self, text):
        self.sink.append(text)

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()
