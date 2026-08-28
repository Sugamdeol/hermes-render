from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "git-storage.py"


def load_storage():
    spec = importlib.util.spec_from_file_location("git_storage", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_config(storage, **overrides):
    defaults = dict(repo="owner/repo", token="ghp_secret", branch="state",
                    instance_id="tester")
    defaults.update(overrides)
    return storage.GitConfig(**defaults)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_backend_disabled_until_repo_and_token_are_both_set(self):
        for env in ({}, {"GIT_STATE_REPO": "o/r"}, {"GIT_STATE_TOKEN": "t"}):
            os.environ.clear()
            os.environ.update(self._env)
            for key in ("GIT_STATE_REPO", "GIT_STATE_TOKEN", "GITHUB_TOKEN"):
                os.environ.pop(key, None)
            os.environ.update(env)
            self.assertIsNone(self.storage.GitConfig.from_env(),
                              f"expected no config for {env}")

    def test_token_falls_back_to_github_token(self):
        os.environ["GIT_STATE_REPO"] = "owner/repo"
        os.environ.pop("GIT_STATE_TOKEN", None)
        os.environ["GITHUB_TOKEN"] = "ghp_fallback"
        config = self.storage.GitConfig.from_env()
        self.assertIsNotNone(config)
        self.assertEqual(config.token, "ghp_fallback")

    def test_remote_url_embeds_token_but_repr_does_not_leak_it(self):
        config = make_config(self.storage)
        self.assertIn("ghp_secret", config.remote_url)
        self.assertNotIn("ghp_secret", self.storage.redact(config.remote_url))


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_redacts_credentials_embedded_in_a_clone_url(self):
        message = "fatal: could not read from https://x-access-token:ghp_abc123@github.com/o/r"
        cleaned = self.storage.redact(message)
        self.assertNotIn("ghp_abc123", cleaned)

    def test_redacts_bare_tokens(self):
        for secret in ("ghp_" + "a" * 36, "github_pat_" + "b" * 22):
            self.assertNotIn(secret, self.storage.redact(f"boom {secret} boom"))

    def test_keeps_ordinary_text_intact(self):
        self.assertEqual(self.storage.redact("plain failure"), "plain failure")


class LeaseRefTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_lease_ref_round_trips(self):
        ref = self.storage.lease_ref(50, "render", 1700000000)
        self.assertEqual(self.storage.parse_lease_ref(ref),
                         (50, "render", 1700000000.0))

    def test_lease_ref_round_trips_with_hyphenated_instance_id(self):
        ref = self.storage.lease_ref(7, "my-laptop-2", 1700000001)
        self.assertEqual(self.storage.parse_lease_ref(ref),
                         (7, "my-laptop-2", 1700000001.0))

    def test_float_timestamp_produces_the_same_ref_as_the_int(self):
        # release_lease rebuilds refs from parsed (float) timestamps; if these
        # disagreed, the delete would silently miss and leases would never free.
        self.assertEqual(self.storage.lease_ref(50, "render", 1700000000),
                         self.storage.lease_ref(50, "render", 1700000000.0))

    def test_priority_is_zero_padded_so_refs_sort_correctly(self):
        self.assertIn("/050-", self.storage.lease_ref(50, "a", 1))
        self.assertIn("/100-", self.storage.lease_ref(100, "a", 1))

    def test_rejects_foreign_refs(self):
        for ref in ("refs/heads/state", "refs/hermes-lease/bad", ""):
            self.assertIsNone(self.storage.parse_lease_ref(ref))


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_alone_means_active(self):
        self.assertEqual(
            self.storage.decide_role([], instance_id="render", priority=50,
                                     now=1000, ttl=600),
            self.storage.ROLE_ACTIVE)

    def test_higher_priority_peer_wins(self):
        leases = [(100, "laptop", 1000)]
        self.assertEqual(
            self.storage.decide_role(leases, instance_id="render", priority=50,
                                     now=1000, ttl=600),
            self.storage.ROLE_STANDBY)

    def test_lower_priority_peer_yields(self):
        leases = [(10, "pi", 1000)]
        self.assertEqual(
            self.storage.decide_role(leases, instance_id="render", priority=50,
                                     now=1000, ttl=600),
            self.storage.ROLE_ACTIVE)

    def test_expired_peer_is_ignored_so_the_survivor_takes_over(self):
        leases = [(100, "laptop", 1000)]
        self.assertEqual(
            self.storage.decide_role(leases, instance_id="render", priority=50,
                                     now=2000, ttl=600),
            self.storage.ROLE_ACTIVE)

    def test_our_own_stale_lease_never_demotes_us(self):
        leases = [(50, "render", 1)]
        self.assertEqual(
            self.storage.decide_role(leases, instance_id="render", priority=50,
                                     now=1000, ttl=600),
            self.storage.ROLE_ACTIVE)

    def test_equal_priority_is_broken_by_name_deterministically(self):
        a = self.storage.decide_role([(50, "b", 1000)], instance_id="a",
                                     priority=50, now=1000, ttl=600)
        b = self.storage.decide_role([(50, "a", 1000)], instance_id="b",
                                     priority=50, now=1000, ttl=600)
        self.assertEqual({a, b},
                         {self.storage.ROLE_ACTIVE, self.storage.ROLE_STANDBY})


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_compacts_once_the_cap_is_reached(self):
        self.assertFalse(self.storage.should_compact(4, 5))
        self.assertTrue(self.storage.should_compact(5, 5))
        self.assertTrue(self.storage.should_compact(9, 5))

    def test_zero_disables_compaction(self):
        self.assertFalse(self.storage.should_compact(10_000, 0))

    def test_generation_is_read_from_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            self.assertEqual(self.storage.read_generation(workdir), 0)
            (workdir / self.storage.MANIFEST_NAME).write_text(
                json.dumps({"generation": 7}))
            self.assertEqual(self.storage.read_generation(workdir), 7)

    def test_unreadable_manifest_restarts_the_count(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            (workdir / self.storage.MANIFEST_NAME).write_text("not json")
            self.assertEqual(self.storage.read_generation(workdir), 0)


class MirrorPlanTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def _plan(self, data_dir):
        excludes, _ = self.storage.excludes_and_matcher()
        return set(self.storage.plan_mirror(data_dir, excludes))

    def test_durable_state_is_mirrored_and_volatile_paths_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: x\n")
            (data_dir / "memories").mkdir()
            (data_dir / "memories" / "a.md").write_text("note")
            (data_dir / "logs").mkdir()
            (data_dir / "logs" / "gateway.log").write_text("chatter")

            planned = self._plan(data_dir)

            self.assertIn("config.yaml", planned)
            self.assertIn("memories/a.md", planned)
            self.assertNotIn("logs/gateway.log", planned)

    def test_plan_is_sorted_so_commits_are_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            for name in ("c.md", "a.md", "b.md"):
                (data_dir / name).write_text("x")
            excludes, _ = self.storage.excludes_and_matcher()
            planned = list(self.storage.plan_mirror(data_dir, excludes))
            self.assertEqual(planned, sorted(planned))


class SecretHandlingTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_env_is_omitted_rather_than_committed_in_the_clear(self):
        """No age recipient must mean .env is skipped, never pushed plaintext."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / ".env").write_text("HERMES_API_KEY=super-secret\n")
            (data_dir / "config.yaml").write_text("model: x\n")
            workdir = root / "work"
            workdir.mkdir()

            config = make_config(self.storage, age_recipient="")
            manifest = self.storage.build_worktree(data_dir, workdir, config)

            mirrored = {
                p.relative_to(workdir).as_posix()
                for p in workdir.rglob("*") if p.is_file()
            }
            self.assertIn("data/config.yaml", mirrored)
            self.assertNotIn("data/.env", mirrored)
            self.assertIn(".env", manifest["omitted"])
            blob = "\n".join(
                p.read_text(errors="ignore")
                for p in workdir.rglob("*") if p.is_file()
            )
            self.assertNotIn("super-secret", blob)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_refuses_to_push_to_a_public_repo(self):
        config = make_config(self.storage)
        config.allow_public = False
        self.storage.repo_is_private = lambda cfg: False
        with self.assertRaises(self.storage.GitStateError):
            self.storage.ensure_safe_to_push(config)

    def test_unknown_visibility_is_treated_as_unsafe(self):
        config = make_config(self.storage)
        config.allow_public = False
        self.storage.repo_is_private = lambda cfg: None
        with self.assertRaises(self.storage.GitStateError):
            self.storage.ensure_safe_to_push(config)

    def test_private_repo_is_allowed(self):
        config = make_config(self.storage)
        config.allow_public = False
        self.storage.repo_is_private = lambda cfg: True
        self.storage.ensure_safe_to_push(config)

    def test_opt_out_allows_a_public_repo(self):
        config = make_config(self.storage)
        config.allow_public = True
        self.storage.repo_is_private = lambda cfg: False
        self.storage.ensure_safe_to_push(config)


class MirrorRoundTripTests(unittest.TestCase):
    """End-to-end against a real local git remote: deletes must propagate."""

    def setUp(self):
        self.storage = load_storage()
        if not self._git_available():
            self.skipTest("git is not installed")

    @staticmethod
    def _git_available():
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def _remote_tree(self, remote, branch="state"):
        proc = subprocess.run(
            ["git", f"--git-dir={remote}", "ls-tree", "-r", "--name-only", branch],
            capture_output=True, text=True)
        return set(proc.stdout.split())

    def test_deleting_a_local_file_removes_it_from_the_remote(self):
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            data_dir = root / "data"
            (data_dir / "memories").mkdir(parents=True)
            (data_dir / "memories" / "keep.md").write_text("keep me")
            (data_dir / "memories" / "drop.md").write_text("delete me")

            storage.GitConfig.remote_url = property(lambda self: str(remote))
            storage.GitConfig.api_repo = property(lambda self: "local/test")
            config = make_config(storage, workdir=root / "work")
            config._visibility_checked = True

            storage.sync_once(data_dir, config)
            tree = self._remote_tree(remote)
            self.assertIn("data/memories/keep.md", tree)
            self.assertIn("data/memories/drop.md", tree)

            (data_dir / "memories" / "drop.md").unlink()
            storage.sync_once(data_dir, config)
            tree = self._remote_tree(remote)
            self.assertIn("data/memories/keep.md", tree)
            self.assertNotIn("data/memories/drop.md", tree)

    def test_restore_rebuilds_state_in_an_empty_directory(self):
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            data_dir = root / "data"
            (data_dir / "memories").mkdir(parents=True)
            (data_dir / "memories" / "a.md").write_text("remembered")
            (data_dir / "config.yaml").write_text("model: x\n")

            storage.GitConfig.remote_url = property(lambda self: str(remote))
            storage.GitConfig.api_repo = property(lambda self: "local/test")
            config = make_config(storage, workdir=root / "work")
            config._visibility_checked = True
            storage.sync_once(data_dir, config)

            fresh = root / "fresh"
            fresh.mkdir()
            restore_config = make_config(storage, workdir=root / "work2")
            restore_config._visibility_checked = True
            self.assertTrue(storage.restore(fresh, restore_config))

            self.assertEqual((fresh / "memories" / "a.md").read_text(), "remembered")
            self.assertEqual((fresh / "config.yaml").read_text(), "model: x\n")

    def test_unchanged_state_does_not_create_a_commit(self):
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "config.yaml").write_text("model: x\n")

            storage.GitConfig.remote_url = property(lambda self: str(remote))
            storage.GitConfig.api_repo = property(lambda self: "local/test")
            config = make_config(storage, workdir=root / "work")
            config._visibility_checked = True

            storage.sync_once(data_dir, config)
            first = subprocess.run(
                ["git", f"--git-dir={remote}", "rev-parse", "state"],
                capture_output=True, text=True).stdout.strip()
            storage.sync_once(data_dir, config)
            second = subprocess.run(
                ["git", f"--git-dir={remote}", "rev-parse", "state"],
                capture_output=True, text=True).stdout.strip()

            self.assertEqual(first, second, "an idle sync should not push a commit")


if __name__ == "__main__":
    unittest.main()
