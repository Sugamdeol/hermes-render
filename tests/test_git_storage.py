from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
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
        return set(self.storage.plan_mirror(data_dir, self.storage.DEFAULT_EXCLUDES))

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
            planned = list(self.storage.plan_mirror(
                data_dir, self.storage.DEFAULT_EXCLUDES))
            self.assertEqual(planned, sorted(planned))


class SecretHandlingTests(unittest.TestCase):
    """GIT_STATE_ENV_MODE decides how .env reaches the branch."""

    def setUp(self):
        self.storage = load_storage()
        self._saved_age_encrypt = self.storage.age_encrypt

    def tearDown(self):
        self.storage.age_encrypt = self._saved_age_encrypt

    def _mirror(self, env_mode=None, **overrides):
        """Build a worktree over a data dir holding a .env, return the result."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        data_dir = root / "data"
        data_dir.mkdir()
        (data_dir / ".env").write_text("HERMES_API_KEY=super-secret\n")
        (data_dir / "config.yaml").write_text("model: x\n")
        workdir = root / "work"
        workdir.mkdir()

        if env_mode is not None:
            overrides.setdefault("env_mode", env_mode)
        config = make_config(self.storage, **overrides)
        manifest = self.storage.build_worktree(data_dir, workdir, config)
        mirrored = {
            p.relative_to(workdir).as_posix()
            for p in workdir.rglob("*") if p.is_file()
        }
        blob = "\n".join(
            p.read_text(errors="ignore") for p in workdir.rglob("*") if p.is_file()
        )
        return manifest, mirrored, blob

    def test_env_is_committed_in_the_clear_by_default(self):
        """The point of the change: the dotenv is part of the backup."""
        manifest, mirrored, blob = self._mirror()
        self.assertIn("data/config.yaml", mirrored)
        self.assertIn("data/.env", mirrored)
        self.assertIn("super-secret", blob)
        self.assertEqual(manifest["omitted"], [])
        self.assertEqual(manifest["plaintext_env"], [".env"])

    def test_the_plaintext_mode_says_so_in_the_log(self):
        """Live keys going into git should never be a silent decision."""
        with self.assertLogs("hermes-git-storage", level="WARNING") as logged:
            self._mirror()
        self.assertTrue(any("in the clear" in line for line in logged.output),
                        logged.output)

    def test_encrypt_mode_seals_the_dotenv(self):
        # A fake that says what it sealed without echoing the plaintext back,
        # so the "no secret in the tree" assertion below has teeth.
        self.storage.age_encrypt = lambda payload, config: (
            b"SEALED(" + str(len(payload)).encode() + b")")
        manifest, mirrored, blob = self._mirror(env_mode="encrypt",
                                                age_recipient="age1test")
        self.assertIn("data/.env.enc", mirrored)
        self.assertNotIn("data/.env", mirrored)
        self.assertNotIn("super-secret", blob)
        self.assertIn("SEALED(28)", blob)
        self.assertEqual(manifest["encrypted"], [".env"])
        self.assertEqual(manifest["plaintext_env"], [])

    def test_encrypt_mode_omits_rather_than_falling_back_to_plaintext(self):
        """No working age setup must never quietly mean "in the clear"."""
        self.storage.age_encrypt = lambda payload, config: None
        manifest, mirrored, blob = self._mirror(env_mode="encrypt",
                                                age_recipient="")
        self.assertNotIn("data/.env", mirrored)
        self.assertNotIn("super-secret", blob)
        self.assertIn(".env", manifest["omitted"])

    def test_omit_mode_leaves_the_dotenv_out(self):
        manifest, mirrored, blob = self._mirror(env_mode="omit")
        self.assertNotIn("data/.env", mirrored)
        self.assertNotIn("super-secret", blob)
        self.assertIn(".env", manifest["omitted"])

    def test_an_unrecognised_mode_falls_back_to_the_default(self):
        config = make_config(self.storage, env_mode="encrypt-ish")
        self.assertEqual(config.env_mode, self.storage.DEFAULT_ENV_MODE)


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


class LocalRemoteTests(unittest.TestCase):
    """Shared plumbing: point the backend at a throwaway bare repo."""

    def setUp(self):
        self.storage = load_storage()
        if not self._git_available():
            self.skipTest("git is not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.remote = self.root / "remote"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self._saved = (self.storage.GitConfig.remote_url,
                       self.storage.GitConfig.api_repo)
        remote = str(self.remote)
        self.storage.GitConfig.remote_url = property(lambda self, _r=remote: _r)
        self.storage.GitConfig.api_repo = property(lambda self: "local/test")

    def tearDown(self):
        self.storage.GitConfig.remote_url = self._saved[0]
        self.storage.GitConfig.api_repo = self._saved[1]
        self._tmp.cleanup()

    @staticmethod
    def _git_available():
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def make_config(self, **overrides):
        defaults = dict(repo="owner/repo", token="ghp_secret", branch="state",
                        instance_id="tester", workdir=self.root / "work")
        defaults.update(overrides)
        config = self.storage.GitConfig(**defaults)
        config._visibility_checked = True
        return config

    def remote_tree(self):
        proc = subprocess.run(
            ["git", f"--git-dir={self.remote}", "ls-tree", "-r", "--name-only",
             "state"],
            capture_output=True, text=True)
        return set(proc.stdout.split())


class RemoteProbeTests(LocalRemoteTests):
    """`empty` and `unreachable` must never be confused: only empty is seeded."""

    def test_a_fresh_repo_is_empty(self):
        self.assertEqual(self.storage.probe_state(self.make_config()),
                         self.storage.REMOTE_EMPTY)

    def test_a_repo_with_state_reports_has_state(self):
        storage = self.storage
        data_dir = self.root / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("model: x\n")
        storage.sync_once(data_dir, self.make_config())
        self.assertEqual(storage.probe_state(self.make_config(workdir=self.root / "w2")),
                         storage.REMOTE_HAS_STATE)

    def test_an_unreachable_repo_is_unknown_rather_than_empty(self):
        config = self.make_config(workdir=self.root / "w3")
        config.repo = "file:///nonexistent/definitely-not-here.git"
        self.storage.GitConfig.remote_url = property(
            lambda self: "file:///nonexistent/definitely-not-here.git")
        self.assertEqual(self.storage.probe_state(config),
                         self.storage.REMOTE_UNKNOWN)

    def test_a_branch_without_a_data_tree_is_empty(self):
        # A branch can exist (from a lease anchor commit) and still hold no
        # state. That is an empty repo for our purposes: safe to seed.
        storage = self.storage
        workdir = self.root / "seed-work"
        workdir.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "state", "."], cwd=workdir,
                       check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)],
                       cwd=workdir, check=True, capture_output=True)
        (workdir / "MANIFEST.json").write_text("{}\n")
        subprocess.run(["git", "add", "-A"], cwd=workdir, check=True,
                       capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "-m", "anchor"], cwd=workdir, check=True,
                       capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "state"], cwd=workdir,
                       check=True, capture_output=True)
        self.assertEqual(storage.probe_state(self.make_config(workdir=self.root / "w4")),
                         storage.REMOTE_EMPTY)


class SeedTests(LocalRemoteTests):
    """First launch: the GitHub branch is empty and has to be filled."""

    def _data_dir(self, contents="built locally"):
        data_dir = self.root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text("model: x\n")
        (data_dir / "memories").mkdir(exist_ok=True)
        (data_dir / "memories" / "note.md").write_text(contents)
        return data_dir

    def test_seed_fills_an_empty_branch_from_restored_state(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()

        # What bootstrap.sh flags on a first launch: the branch is empty, so
        # the tree this instance built locally is the only copy there is.
        self.assertFalse(storage.restore(self.root / "empty-dir", config))
        self.assertTrue(storage.seed(data_dir, config))

        self.assertIn("data/memories/note.md", self.remote_tree())
        self.assertEqual(storage.probe_state(self.make_config(workdir=self.root / "w2")),
                         storage.REMOTE_HAS_STATE)

    def test_state_restored_from_github_on_the_second_launch(self):
        storage = self.storage
        storage.seed(self._data_dir(), self.make_config())

        # Next boot: the branch has state, so restore comes from GitHub.
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertTrue(storage.restore(fresh, self.make_config(workdir=self.root / "w2")))
        self.assertEqual((fresh / "memories" / "note.md").read_text(),
                         "built locally")

    def test_seed_refuses_to_overwrite_state_that_is_already_there(self):
        storage = self.storage
        storage.seed(self._data_dir("newer github state"), self.make_config())
        # A second boot that somehow still thinks it is seeding must not
        # replace what is on the branch.
        with self.assertRaises(storage.GitStateError):
            storage.seed(self._data_dir("older local copy"),
                         self.make_config(workdir=self.root / "w2"))
        restored = self.root / "restored"
        restored.mkdir()
        storage.restore(restored, self.make_config(workdir=self.root / "w3"))
        self.assertEqual((restored / "memories" / "note.md").read_text(),
                         "newer github state")

    def test_seed_force_allows_a_deliberate_overwrite(self):
        storage = self.storage
        storage.seed(self._data_dir("github copy"), self.make_config())
        self.assertTrue(
            storage.seed(self._data_dir("operator says this wins"),
                         self.make_config(workdir=self.root / "w2", seed_force=True)))
        restored = self.root / "restored"
        restored.mkdir()
        storage.restore(restored, self.make_config(workdir=self.root / "w3"))
        self.assertEqual((restored / "memories" / "note.md").read_text(),
                         "operator says this wins")

    def test_seed_refuses_when_the_repo_cannot_be_reached(self):
        self.storage.GitConfig.remote_url = property(
            lambda self: "file:///nonexistent/nope.git")
        with self.assertRaises(self.storage.GitStateError):
            self.storage.seed(self._data_dir(), self.make_config())

    def test_a_boot_without_restore_does_not_overwrite_github_state(self):
        """The guard bootstrap.sh arms when GitHub was unreachable at boot."""
        storage = self.storage
        storage.seed(self._data_dir("state already in github"), self.make_config())

        # This instance booted without restoring from GitHub, so its tree is
        # not derived from the branch. Pushing would clobber the newer copy.
        config = self.make_config(workdir=self.root / "w2", seed_on_boot=True)
        with self.assertRaises(storage.GitStateError):
            storage.sync_once(self._data_dir("older local copy"), config)

        restored = self.root / "restored"
        restored.mkdir()
        storage.restore(restored, self.make_config(workdir=self.root / "w3"))
        self.assertEqual((restored / "memories" / "note.md").read_text(),
                         "state already in github")

    def test_the_guard_lets_an_empty_branch_be_filled(self):
        """The guard must not block the very case it exists for."""
        self.assertTrue(
            self.storage.seed(self._data_dir(),
                              self.make_config(seed_on_boot=True)))

    def test_successful_seed_disarms_the_guard(self):
        config = self.make_config(seed_on_boot=True)
        self.storage.seed(self._data_dir(), config)
        self.assertFalse(config.seed_on_boot)
        # Later saves go through normally.
        self.assertTrue(self.storage.sync_once(self._data_dir("second save"), config))


class ChangeWatcherTests(unittest.TestCase):
    """Change-driven saves: debounce a burst into one push."""

    def setUp(self):
        self.storage = load_storage()

    def _watcher(self, debounce=10, sampler=None):
        storage = self.storage
        clock = self.clock = _FakeClock()
        config = make_config(storage, debounce_seconds=debounce)
        return storage.ChangeWatcher(Path("/tmp/does-not-matter"), config,
                                     clock=clock, sampler=sampler or _FakeSampler())

    def test_an_unchanged_tree_never_demands_a_push(self):
        watcher = self._watcher()
        for _ in range(5):
            self.clock.advance(5)
            self.assertFalse(watcher.poll())

    def test_a_change_is_pushed_once_the_tree_settles(self):
        sampler = _FakeSampler()
        watcher = self._watcher(debounce=10, sampler=sampler)

        self.clock.advance(1)
        sampler.value = "changed"
        self.assertFalse(watcher.poll(), "a fresh change must not push instantly")

        self.clock.advance(9)
        self.assertFalse(watcher.poll(), "still inside the debounce window")

        self.clock.advance(1)
        self.assertTrue(watcher.poll(), "quiet for the debounce: push now")

    def test_a_burst_of_changes_collapses_into_one_push(self):
        sampler = _FakeSampler()
        watcher = self._watcher(debounce=10, sampler=sampler)

        for tick in range(1, 5):
            self.clock.advance(2)
            sampler.value = f"change-{tick}"
            self.assertFalse(watcher.poll())

        self.clock.advance(10)
        self.assertTrue(watcher.poll(), "one push for the whole burst")
        watcher.mark_pushed()
        self.assertFalse(watcher.poll(), "and not a second one")

    def test_a_later_change_is_pushed_too(self):
        sampler = _FakeSampler()
        watcher = self._watcher(debounce=10, sampler=sampler)

        self.clock.advance(1)
        sampler.value = "first"
        self.assertFalse(watcher.poll(), "noticed on this tick")
        self.clock.advance(10)
        self.assertTrue(watcher.poll(), "pushed once it settled")
        watcher.mark_pushed()

        self.clock.advance(1)
        sampler.value = "second"
        self.assertFalse(watcher.poll())
        self.clock.advance(10)
        self.assertTrue(watcher.poll(), "the next change still gets pushed")

    def test_a_failed_push_is_retried_after_the_backoff(self):
        sampler = _FakeSampler()
        watcher = self._watcher(debounce=10, sampler=sampler)

        self.clock.advance(1)
        sampler.value = "changed"
        self.assertFalse(watcher.poll())
        self.clock.advance(10)
        self.assertTrue(watcher.poll())

        watcher.defer(30)
        self.clock.advance(10)
        self.assertFalse(watcher.poll(), "backing off, not hammering the remote")
        self.clock.advance(21)
        self.assertTrue(watcher.poll(), "and retried once the backoff expires")

    def test_debounce_can_be_zero_for_immediate_pushes(self):
        sampler = _FakeSampler()
        watcher = self._watcher(debounce=0, sampler=sampler)
        self.clock.advance(1)
        sampler.value = "changed"
        self.assertTrue(watcher.poll())

    def test_fingerprint_notices_a_new_file(self):
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: x\n")
            before = storage.state_fingerprint(data_dir)
            (data_dir / "memories").mkdir()
            (data_dir / "memories" / "new.md").write_text("a thought")
            self.assertNotEqual(before, storage.state_fingerprint(data_dir))

    def test_fingerprint_notices_a_size_change(self):
        # Size is part of the fingerprint, so a longer write is always seen.
        # A same-size rewrite is not asserted on: on filesystems with coarse
        # mtime granularity it is indistinguishable from no change at the
        # metadata level, which is exactly what the interval sync covers --
        # it compares contents against the remote, not metadata.
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: x\n")
            before = storage.state_fingerprint(data_dir)
            (data_dir / "config.yaml").write_text("model: x\nmodel: y\n")
            self.assertNotEqual(before, storage.state_fingerprint(data_dir))

    def test_fingerprint_ignores_excluded_churn(self):
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: x\n")
            before = storage.state_fingerprint(data_dir)
            (data_dir / "gateway.log").write_text("line one\n")
            (data_dir / "gateway.log").write_text("line one\nline two\n")
            self.assertEqual(before, storage.state_fingerprint(data_dir))


class _FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


class _FakeSampler:
    def __init__(self, value="baseline"):
        self.value = value

    def __call__(self):
        return self.value


class LeaseOnEmptyRepoTests(LocalRemoteTests):
    def test_claiming_a_lease_creates_the_first_commit(self):
        """Regression: `git rev-parse HEAD` prints "HEAD" with no commits.

        That read like a valid refspec and every lease push on a fresh repo
        failed with "src refspec HEAD does not match any".
        """
        storage = self.storage
        config = self.make_config(failover=True, priority=50)

        role = storage.claim_lease(config)

        self.assertEqual(role, storage.ROLE_ACTIVE)
        leases = storage.read_leases(config)
        self.assertEqual([instance for _, instance, _ in leases],
                         [config.instance_id])


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


class StartupSeedTests(LocalRemoteTests):
    """The daemon seeds an empty branch after boot, not the boot wrapper.

    A first state push is the largest upload this service makes. Doing it
    inline delayed the port bind until Render's scan expired and the deploy
    failed with "no open ports detected", so the seed moved into the daemon.
    """

    def _data_dir(self, contents="built locally"):
        data_dir = self.root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text("model: x\n")
        (data_dir / "memories").mkdir(exist_ok=True)
        (data_dir / "memories" / "note.md").write_text(contents)
        return data_dir

    def test_an_empty_branch_is_seeded_from_the_restored_tree(self):
        storage = self.storage
        config = self.make_config(seed_on_boot=True, push_retry_seconds=0)

        self.assertTrue(storage.seed_on_startup(self._data_dir(), config))

        self.assertIn("data/memories/note.md", self.remote_tree())
        self.assertFalse(config.seed_on_boot,
                         "a landed seed must disarm the push guard")

    def test_nothing_is_pushed_when_the_flag_is_not_set(self):
        """A boot that restored from GitHub has no business seeding."""
        storage = self.storage
        config = self.make_config(seed_on_boot=False)

        self.assertFalse(storage.seed_on_startup(self._data_dir(), config))

        self.assertEqual(self.remote_tree(), set())

    def test_existing_github_state_is_never_seeded_over(self):
        storage = self.storage
        storage.seed(self._data_dir("newer github state"), self.make_config())

        # This instance booted without restoring from GitHub, so its tree is
        # not derived from the branch. The seed has to stand down and leave
        # the guard armed.
        config = self.make_config(workdir=self.root / "w2", seed_on_boot=True)
        self.assertFalse(storage.seed_on_startup(self._data_dir("older local copy"),
                                                 config))

        self.assertTrue(config.seed_on_boot,
                        "the guard must stay armed when the seed stands down")
        restored = self.root / "restored"
        restored.mkdir()
        storage.restore(restored, self.make_config(workdir=self.root / "w3"))
        self.assertEqual((restored / "memories" / "note.md").read_text(),
                         "newer github state")

    def test_an_unreachable_branch_is_left_for_a_later_tick(self):
        self.storage.GitConfig.remote_url = property(
            lambda self: "file:///nonexistent/nope.git")
        config = self.make_config(seed_on_boot=True)

        self.assertFalse(self.storage.seed_on_startup(self._data_dir(), config))

        self.assertTrue(config.seed_on_boot)

    def test_a_standby_instance_does_not_seed(self):
        storage = self.storage
        config = self.make_config(seed_on_boot=True, failover=True)
        config.role = storage.ROLE_STANDBY

        self.assertFalse(storage.seed_on_startup(self._data_dir(), config))

        self.assertEqual(self.remote_tree(), set())


class DaemonStartupSeedTests(LocalRemoteTests):
    """The seed really happens inside a running daemon, after the wait."""

    def test_a_running_daemon_seeds_the_branch_once_the_gateway_is_up(self):
        storage = self.storage
        data_dir = self.root / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("model: x\n")
        (data_dir / "memories").mkdir()
        (data_dir / "memories" / "note.md").write_text("built locally")

        config = self.make_config(seed_on_boot=True, interval=1,
                                  watch_seconds=1, push_retry_seconds=0)
        stop = threading.Event()
        thread = threading.Thread(
            target=storage.run_daemon, args=(data_dir, config, stop), daemon=True)
        thread.start()
        try:
            deadline = time.time() + 45
            while time.time() < deadline and "data/memories/note.md" not in self.remote_tree():
                time.sleep(0.5)
        finally:
            stop.set()
            thread.join(timeout=30)

        self.assertFalse(thread.is_alive(), "the daemon did not stop when asked")
        self.assertIn("data/memories/note.md", self.remote_tree())
        self.assertFalse(config.seed_on_boot,
                         "a landed seed must disarm the push guard")


class PushRobustnessTests(LocalRemoteTests):
    """GitHub drops large pushes often enough that one attempt is not an answer."""

    def _workdir_with_a_second_commit(self, config):
        storage = self.storage
        workdir = storage.ensure_clone(config)
        storage.ensure_initial_commit(workdir, config)
        (workdir / "MANIFEST.json").write_text('{"generation": 2}\n', encoding="utf-8")
        storage.run_git(["add", "-A"], cwd=workdir, config=config)
        storage.run_git(["commit", "-q", "-m", "second"], cwd=workdir, config=config)
        return workdir

    def test_a_transient_push_failure_is_retried_until_it_lands(self):
        storage = self.storage
        config = self.make_config(push_attempts=3, push_retry_seconds=0)
        workdir = self._workdir_with_a_second_commit(config)

        real_run_git = storage.run_git
        attempts = []

        def flaky(args, **kwargs):
            if args[0] == "push":
                attempts.append(args)
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(
                        args, 1, "",
                        "error: RPC failed; HTTP 408 curl 22 The requested URL "
                        "returned error: 408")
            return real_run_git(args, **kwargs)

        storage.run_git = flaky
        try:
            storage.push_branch(workdir, config)
        finally:
            storage.run_git = real_run_git

        self.assertEqual(len(attempts), 2, "one retry should have been enough")
        self.assertEqual(storage.remote_ref_sha(config),
                         storage.head_commit(workdir, config))

    def test_a_push_that_landed_but_reported_failure_is_not_lost(self):
        """The 408 in the incident log came with "Everything up-to-date".

        The ref had already moved server-side; believing the exit code would
        have reported a missing backup that was in fact safe, and retried a
        force-push over it.
        """
        storage = self.storage
        config = self.make_config(push_attempts=3, push_retry_seconds=0)
        workdir = self._workdir_with_a_second_commit(config)

        real_run_git = storage.run_git
        attempts = []

        def lied_about(args, **kwargs):
            proc = real_run_git(args, **kwargs)
            if args[0] == "push":
                attempts.append(args)
                return subprocess.CompletedProcess(
                    args, 1,
                    "error: RPC failed; HTTP 408 curl 22\n"
                    "send-pack: unexpected disconnect while reading sideband packet\n"
                    "fatal: the remote end hung up unexpectedly\n"
                    "Everything up-to-date",
                    proc.stdout)
            return proc

        storage.run_git = lied_about
        try:
            storage.push_branch(workdir, config)
        finally:
            storage.run_git = real_run_git

        self.assertEqual(len(attempts), 1, "the remote already had it: no retry")
        self.assertEqual(storage.remote_ref_sha(config),
                         storage.head_commit(workdir, config))

    def test_a_push_that_never_lands_gives_up_after_every_attempt(self):
        storage = self.storage
        config = self.make_config(push_attempts=2, push_retry_seconds=0)
        workdir = self._workdir_with_a_second_commit(config)

        real_run_git = storage.run_git
        attempts = []

        def never(args, **kwargs):
            if args[0] == "push":
                attempts.append(args)
                return subprocess.CompletedProcess(args, 1, "",
                                                   "error: RPC failed; HTTP 408")
            return real_run_git(args, **kwargs)

        storage.run_git = never
        try:
            with self.assertRaises(storage.GitStateError):
                storage.push_branch(workdir, config)
        finally:
            storage.run_git = real_run_git

        self.assertEqual(len(attempts), 2)

    def test_the_token_never_reaches_a_push_failure_message(self):
        storage = self.storage
        config = self.make_config(push_attempts=1, push_retry_seconds=0)
        workdir = self._workdir_with_a_second_commit(config)

        real_run_git = storage.run_git

        def leaky(args, **kwargs):
            if args[0] == "push":
                return subprocess.CompletedProcess(
                    args, 1,
                    f"fatal: unable to access '{config.remote_url}': HTTP 408", "")
            return real_run_git(args, **kwargs)

        storage.run_git = leaky
        try:
            with self.assertRaises(storage.GitStateError) as caught:
                storage.push_branch(workdir, config)
        finally:
            storage.run_git = real_run_git

        self.assertNotIn("ghp_secret", str(caught.exception))


class _RecordingSubprocess:
    """Stands in for the subprocess module so run_git can be inspected."""

    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def run(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(cmd, 0, "", "")


class TransportTuningTests(unittest.TestCase):
    """The git transport settings that stop GitHub answering 408."""

    def setUp(self):
        self.storage = load_storage()
        self._saved = self.storage.subprocess

    def tearDown(self):
        self.storage.subprocess = self._saved

    def test_a_large_push_is_sent_unchunked_over_http_1_1(self):
        fake = _RecordingSubprocess()
        self.storage.subprocess = fake
        config = make_config(self.storage)

        self.storage.run_git(["--version"], config=config)

        cmd = fake.calls[0][0]
        self.assertIn(f"http.postBuffer={config.http_post_buffer}", cmd)
        self.assertIn("http.version=HTTP/1.1", cmd)
        self.assertIn(f"http.lowSpeedLimit={config.http_low_speed_limit}", cmd)
        self.assertIn(f"http.lowSpeedTime={config.http_low_speed_time}", cmd)

    def test_the_post_buffer_defaults_big_enough_for_a_whole_state_tree(self):
        config = make_config(self.storage)
        # Must stay unchunked (so GitHub does not answer an oversized chunked
        # receive-pack with HTTP 408) while not reserving more RAM than a
        # 512 MB Free instance can hand one `git push` (250 MB used to be the
        # floor, which is how pushes OOM-killed the gateway).
        self.assertGreaterEqual(config.http_post_buffer, 50 * 1024 * 1024,
                                "git chunks the body above this and GitHub 408s")
        self.assertLessEqual(config.http_post_buffer, 96 * 1024 * 1024,
                             "the post buffer is eagerly malloced; it must "
                             "not reserve half of a 512 MB instance")

    def test_pack_memory_is_capped_for_small_instances(self):
        config = make_config(self.storage)
        self.assertLessEqual(config.pack_window_memory, 64 * 1024 * 1024,
                             "unbounded pack.windowMemory lets a push spike "
                             "past the 512 MB OOM line")
        self.assertEqual(config.pack_threads, 1,
                         "pack.threads multiplies peak pack memory by core count")

    def test_git_calls_carry_pack_memory_guardrails(self):
        fake = _RecordingSubprocess()
        self.storage.subprocess = fake
        config = make_config(self.storage)

        self.storage.run_git(["--version"], config=config)

        cmd = " ".join(fake.calls[0][0])
        self.assertIn(f"pack.windowMemory={config.pack_window_memory}", cmd)
        self.assertIn("pack.threads=1", cmd)
        self.assertIn("pack.deltaCacheSize=", cmd)

    def test_every_git_call_is_bounded_by_a_timeout(self):
        fake = _RecordingSubprocess()
        self.storage.subprocess = fake
        config = make_config(self.storage, git_timeout_seconds=42)

        self.storage.run_git(["status"], config=config)

        self.assertEqual(fake.calls[0][1]["timeout"], 42)

    def test_a_hung_git_raises_instead_of_blocking_the_boot(self):
        fake = _RecordingSubprocess(
            raises=subprocess.TimeoutExpired(cmd=["git", "push"], timeout=1))
        self.storage.subprocess = fake

        with self.assertRaises(self.storage.GitStateError) as caught:
            self.storage.run_git(["push", "origin", "state"],
                                 config=make_config(self.storage))

        self.assertIn("timed out", str(caught.exception))

    def test_the_push_knobs_come_from_the_environment(self):
        saved = dict(os.environ)
        try:
            os.environ.update({
                "GIT_STATE_REPO": "owner/repo",
                "GIT_STATE_TOKEN": "t",
                "GIT_STATE_PUSH_ATTEMPTS": "5",
                "GIT_STATE_PUSH_RETRY_SECONDS": "2",
                "GIT_STATE_HTTP_POST_BUFFER_MB": "64",
                "GIT_STATE_GIT_TIMEOUT_SECONDS": "90",
            })
            config = self.storage.GitConfig.from_env()
        finally:
            os.environ.clear()
            os.environ.update(saved)

        self.assertEqual(config.push_attempts, 5)
        self.assertEqual(config.push_retry_seconds, 2)
        self.assertEqual(config.git_timeout_seconds, 90)
        self.assertEqual(config.http_post_buffer, 64 * 1024 * 1024)


class BootstrapOrderTests(unittest.TestCase):
    """The boot wrapper must not block the port bind on a state push."""

    def setUp(self):
        bootstrap = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.sh"
        self.source = bootstrap.read_text(encoding="utf-8")

    def test_bootstrap_never_runs_the_seed_inline(self):
        """An inline seed is what pushed the dashboard past Render's port scan."""
        inline_seed = r"\$\{GIT_SYNC\}\"?\s+seed\b"
        # Prove the pattern catches the form it guards against. Without this,
        # the assertion below passes just as happily against a pattern that
        # matches nothing at all.
        self.assertRegex('gosu hermes "${GIT_SYNC}" seed "${DATA_DIR}"', inline_seed)

        for line in self.source.splitlines():
            code = line.split("#", 1)[0]
            self.assertNotRegex(
                code, inline_seed,
                f"bootstrap.sh still seeds inline: {line.strip()}")

    def test_bootstrap_flags_the_seed_for_the_daemon_instead(self):
        self.assertIn("GIT_STATE_SEED_ON_BOOT=1", self.source)

    def test_the_restore_cannot_run_past_its_budget(self):
        """A hung download has to cost the state, not the deploy."""
        self.assertIn("HERMES_RESTORE_TIMEOUT_SECONDS", self.source)
        self.assertIn("run_bounded", self.source)

    def test_the_daemon_seeds_what_bootstrap_defers(self):
        daemon = (Path(__file__).resolve().parents[1]
                  / "scripts" / "git-storage.py").read_text(encoding="utf-8")
        self.assertIn("seed_on_startup(data_dir, config)", daemon)

    def test_the_state_probe_cannot_run_past_its_budget(self):
        """`state` clones the branch, so it is on the port-bind path too."""
        for line in self.source.splitlines():
            code = line.split("#", 1)[0]
            if '"${GIT_SYNC}" state' in code:
                self.assertIn(
                    "run_bounded", code,
                    f"bootstrap.sh probes the state branch without a bound: "
                    f"{line.strip()}")


class RestoreFidelityTests(LocalRemoteTests):
    """A restore has to give back the tree that was backed up."""

    def _round_trip(self, build, *, data_name="data"):
        """Back up a data dir built by `build` and restore it elsewhere."""
        storage = self.storage
        data_dir = self.root / data_name
        data_dir.mkdir(parents=True)
        build(data_dir)
        config = self.make_config()
        storage.sync_once(data_dir, config, force=True)

        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertTrue(
            storage.restore(fresh, self.make_config(workdir=self.root / "work2")))
        return fresh

    def test_an_executable_script_comes_back_runnable(self):
        def build(data_dir):
            script = data_dir / "hook.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)

        fresh = self._round_trip(build)
        mode = (fresh / "hook.sh").stat().st_mode & 0o777
        self.assertTrue(mode & 0o111,
                        f"the restored hook is not executable: {oct(mode)}")

    def test_a_restored_dotenv_stays_owner_only(self):
        def build(data_dir):
            env = data_dir / ".env"
            env.write_text("K=v\n")
            env.chmod(0o644)

        fresh = self._round_trip(build)
        self.assertEqual((fresh / ".env").stat().st_mode & 0o777, 0o600)

    def test_a_private_file_does_not_come_back_world_readable(self):
        """git stores only the exec bit, so 0600 has to ride in the manifest."""
        def build(data_dir):
            key = data_dir / "id_ed25519"
            key.write_text("PRIVATE\n")
            key.chmod(0o600)

        fresh = self._round_trip(build)
        self.assertEqual((fresh / "id_ed25519").stat().st_mode & 0o777, 0o600)

    def test_an_empty_directory_survives_the_round_trip(self):
        def build(data_dir):
            (data_dir / "memories" / "archive").mkdir(parents=True)
            (data_dir / "config.yaml").write_text("model: x\n")

        fresh = self._round_trip(build)
        self.assertTrue((fresh / "memories" / "archive").is_dir(),
                        "an empty directory was dropped from the backup")

    def test_a_relative_symlink_survives_the_round_trip(self):
        def build(data_dir):
            (data_dir / "config.yaml").write_text("model: x\n")
            os.symlink("config.yaml", data_dir / "current.yaml")

        fresh = self._round_trip(build)
        self.assertTrue((fresh / "current.yaml").is_symlink(),
                        "the symlink was dropped from the backup")
        self.assertEqual(os.readlink(fresh / "current.yaml"), "config.yaml")
        self.assertEqual((fresh / "current.yaml").read_text(), "model: x\n")

    def test_a_symlink_pointing_out_of_the_data_dir_is_not_backed_up(self):
        """The backup must not become a way to write outside the tree."""
        storage = self.storage
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("nope\n")

        data_dir = self.root / "data"
        data_dir.mkdir()
        os.symlink("../outside/secret", data_dir / "escape")
        os.symlink(str(outside / "secret"), data_dir / "absolute")
        (data_dir / "config.yaml").write_text("model: x\n")

        self.assertEqual(
            storage.plan_mirror(data_dir, storage.DEFAULT_EXCLUDES),
            ["config.yaml"])
        _, _, symlinks = storage.plan_tree(data_dir, storage.DEFAULT_EXCLUDES)
        self.assertEqual(symlinks, {})

    def test_a_tampered_manifest_cannot_restore_outside_the_data_dir(self):
        storage = self.storage
        workdir = self.root / "work"
        (workdir / storage.DATA_SUBDIR).mkdir(parents=True)
        (workdir / storage.MANIFEST_NAME).write_text(json.dumps({
            "symlinks": {"evil": "../../../../../../tmp/hermes-escape-test"},
            "empty_dirs": ["/etc/hermes"],
        }), encoding="utf-8")

        data_dir = self.root / "restored"
        data_dir.mkdir()
        storage.materialize(workdir, data_dir, self.make_config())

        self.assertFalse(Path("/tmp/hermes-escape-test").exists())
        self.assertFalse(Path("/etc/hermes").exists())
        self.assertEqual(list(data_dir.iterdir()), [])


class ReusedWorkdirTests(LocalRemoteTests):
    """A clone left over from another repository must not be trusted."""

    def test_a_reused_workdir_follows_the_configured_repository(self):
        storage = self.storage
        configured = storage.GitConfig.remote_url  # the throwaway remote setUp made
        other = self.root / "other-remote"
        subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)

        data_dir = self.root / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("model: x\n")

        # Point the shared workdir at `other` first, as a previous
        # GIT_STATE_REPO would have left it.
        storage.GitConfig.remote_url = property(lambda self: str(other))
        storage.sync_once(data_dir, self.make_config())
        self.assertIn("data/config.yaml", self._tree_of(other))
        before = self._tree_of(other)

        # The operator moves the state repo; the same workdir is still there.
        storage.GitConfig.remote_url = configured
        storage.sync_once(data_dir, self.make_config())

        self.assertIn("data/config.yaml", self.remote_tree(),
                      "the configured repository never received the state")
        self.assertEqual(self._tree_of(other), before,
                         "state was pushed to the repository we moved away from")

    def _tree_of(self, remote):
        proc = subprocess.run(
            ["git", f"--git-dir={remote}", "ls-tree", "-r", "--name-only", "state"],
            capture_output=True, text=True)
        return set(proc.stdout.split())


class ManifestModeTests(unittest.TestCase):
    """A mode from a manifest is not trusted blindly."""

    def setUp(self):
        self.storage = load_storage()

    def test_octal_strings_are_parsed(self):
        parse = self.storage.manifest_mode
        self.assertEqual(parse("600"), 0o600)
        self.assertEqual(parse("750"), 0o750)
        self.assertEqual(parse(448), 0o700)

    def test_unusable_values_are_ignored(self):
        parse = self.storage.manifest_mode
        for bad in (None, "", "not-a-mode", [], {}, True, "999999999999999999999"):
            self.assertIsNone(parse(bad), f"{bad!r} should not produce a mode")


class MidPushChangeTests(unittest.TestCase):
    """A write made while a push is in flight must not be adopted as the
    baseline, or it is never backed up."""

    def setUp(self):
        self.storage = load_storage()

    def _watcher(self, sampler):
        clock = self.clock = _FakeClock()
        return self.storage.ChangeWatcher(
            Path("/tmp/does-not-matter"), make_config(self.storage, debounce_seconds=0),
            clock=clock, sampler=sampler)

    def test_the_daemon_snapshots_before_the_push_and_restores_that_baseline(self):
        sampler = _FakeSampler()
        watcher = self._watcher(sampler)

        self.clock.advance(1)
        sampler.value = "noticed"
        self.assertTrue(watcher.poll())

        snapshot = watcher.snapshot()      # what the push is about to send
        sampler.value = "written during the push"
        watcher.mark_pushed(snapshot)

        self.clock.advance(1)
        self.assertTrue(watcher.poll(),
                        "the mid-push write was swallowed instead of pushed")

    def test_re_sampling_after_the_push_is_what_lost_the_write(self):
        """Guards the regression: this is the old behaviour, and it drops it."""
        sampler = _FakeSampler()
        watcher = self._watcher(sampler)

        self.clock.advance(1)
        sampler.value = "noticed"
        self.assertTrue(watcher.poll())
        sampler.value = "written during the push"
        watcher.mark_pushed()              # no snapshot: adopts the new state

        self.clock.advance(1)
        self.assertFalse(watcher.poll())


class IncrementalMirrorTests(LocalRemoteTests):
    """The mirror copies only what changed, but never misses a change.

    Before the incremental mirror, every push re-read and re-copied the whole
    /opt/data tree into the workdir. On a 512 MB Free instance that is RAM
    and CPU the gateway does not get, once per push.
    """

    def _data_dir(self):
        data_dir = self.root / "data"
        (data_dir / "memories").mkdir(parents=True)
        (data_dir / "memories" / "note.md").write_text("remembered")
        (data_dir / "config.yaml").write_text("model: x\n")
        return data_dir

    def _mirror_file(self, config):
        workdir = self.storage.ensure_clone(config)
        return workdir / "data" / "memories" / "note.md"

    def test_an_unchanged_file_is_not_recopied_on_the_next_sync(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()

        storage.sync_once(data_dir, config)
        mirror = self._mirror_file(config)
        first_mtime = mirror.stat().st_mtime_ns

        storage.sync_once(data_dir, config)

        self.assertIsNotNone(storage.head_commit(storage.ensure_clone(config), config))
        self.assertEqual(
            mirror.stat().st_mtime_ns, first_mtime,
            "an unchanged file was re-copied into the worktree")

    def test_a_changed_file_is_still_pushed(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()
        storage.sync_once(data_dir, config)

        (data_dir / "memories" / "note.md").write_text("updated memory")
        storage.sync_once(data_dir, config)

        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertTrue(storage.restore(fresh, self.make_config(workdir=self.root / "w2")))
        self.assertEqual((fresh / "memories" / "note.md").read_text(),
                         "updated memory")

    def test_a_mode_only_change_is_still_mirrored(self):
        """chmod changes ctime, not mtime -- the signature carries the mode."""
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()
        storage.sync_once(data_dir, config)

        os.chmod(data_dir / "config.yaml", 0o600)
        storage.sync_once(data_dir, config)

        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertTrue(storage.restore(fresh, self.make_config(workdir=self.root / "w2")))
        self.assertEqual(
            (fresh / "config.yaml").stat().st_mode & 0o777, 0o600,
            "a permission-only change was lost by the incremental mirror")

    def test_a_corrupt_inventory_falls_back_to_a_full_copy(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()
        storage.sync_once(data_dir, config)

        workdir = storage.ensure_clone(config)
        (workdir / storage.INVENTORY_NAME).write_text("{not json", encoding="utf-8")
        (data_dir / "memories" / "note.md").write_text("after the corruption")
        storage.sync_once(data_dir, config)

        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertTrue(storage.restore(fresh, self.make_config(workdir=self.root / "w2")))
        self.assertEqual((fresh / "memories" / "note.md").read_text(),
                         "after the corruption")

    def test_the_mirror_inventory_is_never_committed(self):
        storage = self.storage
        data_dir = self._data_dir()
        storage.sync_once(data_dir, self.make_config())
        storage.sync_once(data_dir, self.make_config())

        tree = self.remote_tree()
        self.assertNotIn(storage.INVENTORY_NAME, tree)
        self.assertNotIn(f"data/{storage.INVENTORY_NAME}", tree)

    def test_deleting_a_local_file_still_removes_it_after_it_was_skipped(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()
        storage.sync_once(data_dir, config)

        (data_dir / "memories" / "note.md").unlink()
        storage.sync_once(data_dir, config)

        self.assertNotIn("data/memories/note.md", self.remote_tree())
        workdir = storage.ensure_clone(config)
        self.assertFalse((workdir / "data" / "memories").exists(),
                         "the emptied directory was left behind")

    def test_switching_to_encrypt_mode_moves_the_dotenv_off_the_branch(self):
        storage = self.storage
        data_dir = self._data_dir()
        (data_dir / ".env").write_text("HERMES_API_KEY=super-secret\n")
        config = self.make_config()
        storage.sync_once(data_dir, config)
        self.assertIn("data/.env", self.remote_tree())

        saved_age = storage.age_encrypt
        storage.age_encrypt = lambda payload, cfg: b"SEALED(" + str(len(payload)).encode() + b")"
        try:
            encrypted = self.make_config(env_mode="encrypt",
                                         age_recipient="age1test")
            storage.sync_once(data_dir, encrypted)
        finally:
            storage.age_encrypt = saved_age

        tree = self.remote_tree()
        self.assertIn("data/.env.enc", tree)
        self.assertNotIn("data/.env", tree,
                         "the plaintext dotenv survived a switch to encrypt")


class FinalFlushTests(LocalRemoteTests):
    """The shutdown flush: one last push for anything not yet on the branch."""

    def _data_dir(self, contents="remembered"):
        data_dir = self.root / "data"
        (data_dir / "memories").mkdir(parents=True)
        (data_dir / "memories" / "note.md").write_text(contents)
        return data_dir

    def _watcher(self, data_dir, pending):
        storage = self.storage
        clock = _FakeClock()
        watcher = storage.ChangeWatcher(
            data_dir, self.make_config(), clock=clock, sampler=_FakeSampler())
        if pending:
            watcher.pending_since = clock()
        return watcher

    def test_a_pending_change_is_pushed_by_the_flush(self):
        storage = self.storage
        data_dir = self._data_dir("written seconds before the stop")
        watcher = self._watcher(data_dir, pending=True)

        self.assertTrue(storage.final_flush(data_dir, self.make_config(), watcher))
        self.assertIn("data/memories/note.md", self.remote_tree())

    def test_a_clean_tree_is_not_pushed_by_the_flush(self):
        storage = self.storage
        data_dir = self._data_dir()
        watcher = self._watcher(data_dir, pending=False)

        attempted = {"calls": 0}
        real_sync_once = storage.sync_once

        def counting(*args, **kwargs):
            attempted["calls"] += 1
            return real_sync_once(*args, **kwargs)

        storage.sync_once = counting
        try:
            self.assertFalse(
                storage.final_flush(data_dir, self.make_config(), watcher))
        finally:
            storage.sync_once = real_sync_once

        self.assertEqual(attempted["calls"], 0,
                         "a flush with nothing pending still touched the remote")
        self.assertEqual(self.remote_tree(), set())

    def test_without_a_watcher_the_flush_runs_but_commits_nothing_new(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config()
        storage.sync_once(data_dir, config)
        first = subprocess.run(
            ["git", f"--git-dir={self.remote}", "rev-parse", "state"],
            capture_output=True, text=True).stdout.strip()

        self.assertTrue(storage.final_flush(data_dir, config, None))
        second = subprocess.run(
            ["git", f"--git-dir={self.remote}", "rev-parse", "state"],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(first, second,
                         "the flush committed an unchanged tree")

    def test_a_failing_flush_is_reported_not_raised(self):
        storage = self.storage
        data_dir = self._data_dir()
        watcher = self._watcher(data_dir, pending=True)

        real_sync_once = storage.sync_once

        def broken(*args, **kwargs):
            raise RuntimeError("remote is down")

        storage.sync_once = broken
        try:
            self.assertFalse(
                storage.final_flush(data_dir, self.make_config(), watcher))
        finally:
            storage.sync_once = real_sync_once


class DaemonResilienceTests(LocalRemoteTests):
    """The daemon process must survive anything one bad tick can throw at it.

    Nothing restarts the daemon until the next deploy, so a single uncaught
    exception used to end state sync silently for the rest of the instance's
    life -- the other half of "github files are not synced".
    """

    def _data_dir(self):
        data_dir = self.root / "data"
        (data_dir / "memories").mkdir(parents=True)
        (data_dir / "memories" / "note.md").write_text("built locally")
        (data_dir / "config.yaml").write_text("model: x\n")
        return data_dir

    def _run_daemon_briefly(self, data_dir, config, until_remote_has):
        storage = self.storage
        stop = threading.Event()
        thread = threading.Thread(
            target=storage.run_daemon, args=(data_dir, config, stop), daemon=True)
        thread.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline and until_remote_has not in self.remote_tree():
                time.sleep(0.5)
        finally:
            stop.set()
            thread.join(timeout=30)
        return thread

    def test_an_unexpected_push_error_does_not_end_the_daemon(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config(interval=1, watch_seconds=1,
                                  debounce_seconds=0, min_push_interval=0,
                                  retry_seconds=1, push_retry_seconds=0)

        real_sync_once = storage.sync_once
        calls = {"n": 0}

        def flaky(data_dir_, config_, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("unexpected sync failure")
            return real_sync_once(data_dir_, config_, **kwargs)

        storage.sync_once = flaky
        try:
            thread = self._run_daemon_briefly(data_dir, config,
                                              "data/memories/note.md")
        finally:
            storage.sync_once = real_sync_once

        self.assertFalse(thread.is_alive(),
                         "the daemon died on an unexpected push error")
        self.assertIn("data/memories/note.md", self.remote_tree(),
                      "the daemon never retried after the unexpected error")
        self.assertGreaterEqual(calls["n"], 2, "the failed push was not retried")

    def test_an_unexpected_watcher_error_does_not_end_the_daemon(self):
        storage = self.storage
        data_dir = self._data_dir()
        config = self.make_config(interval=1, watch_seconds=1,
                                  debounce_seconds=0, min_push_interval=0,
                                  retry_seconds=1, push_retry_seconds=0)

        real_fingerprint = storage.state_fingerprint
        calls = {"n": 0}

        def flaky_fingerprint(data_dir_):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("tree vanished mid-scan")
            return real_fingerprint(data_dir_)

        storage.state_fingerprint = flaky_fingerprint
        try:
            thread = self._run_daemon_briefly(data_dir, config,
                                              "data/memories/note.md")
        finally:
            storage.state_fingerprint = real_fingerprint

        self.assertFalse(thread.is_alive(),
                         "the daemon died on an unexpected watcher error")
        self.assertIn("data/memories/note.md", self.remote_tree())


class OomSurvivalTests(unittest.TestCase):
    """The sync worker asks the OOM killer to prefer it over the gateway."""

    def setUp(self):
        self.storage = load_storage()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_daemon_marks_itself_preferred_oom_target(self):
        storage = self.storage
        written = {}

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("/proc/self/oom_score_adj"):
                class Handle:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return False

                    def write(self_inner, text):
                        written["value"] = text.strip()
                return Handle()
            return real_open(path, *args, **kwargs)

        storage.open = fake_open
        storage.tune_for_oom_survival()
        # 1000 = "kill me first": a push spike then reclaims this
        # auto-restarted worker instead of the gateway holding the session.
        self.assertEqual(written.get("value"), "1000")

    def test_score_can_be_relaxed_and_is_clamped(self):
        storage = self.storage
        values = []

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("/proc/self/oom_score_adj"):
                class Handle:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return False

                    def write(self_inner, text):
                        values.append(text.strip())
                return Handle()
            return real_open(path, *args, **kwargs)

        storage.open = fake_open
        os.environ["GIT_STATE_OOM_SCORE_ADJ"] = "500"
        storage.tune_for_oom_survival()
        os.environ["GIT_STATE_OOM_SCORE_ADJ"] = "99999"
        storage.tune_for_oom_survival()
        self.assertEqual(values, ["500", "1000"])


if __name__ == "__main__":
    unittest.main()
