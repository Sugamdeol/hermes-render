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

    def _data_dir(self, contents="restored from gofile"):
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

        # What bootstrap.sh does on a first launch: git has nothing, GoFile
        # (here: the pre-existing directory) is the only copy.
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
                         "restored from gofile")

    def test_seed_refuses_to_overwrite_state_that_is_already_there(self):
        storage = self.storage
        storage.seed(self._data_dir("newer github state"), self.make_config())
        # A second boot that somehow still thinks it is seeding must not
        # replace what is on the branch.
        with self.assertRaises(storage.GitStateError):
            storage.seed(self._data_dir("older gofile copy"),
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

    def test_a_gofile_booted_instance_does_not_overwrite_github_state(self):
        """The guard bootstrap.sh arms when GitHub was unreachable at boot."""
        storage = self.storage
        storage.seed(self._data_dir("state already in github"), self.make_config())

        # This instance booted from GoFile, so its tree is not derived from
        # GitHub. Pushing would clobber the newer copy.
        config = self.make_config(workdir=self.root / "w2", seed_from_gofile=True)
        with self.assertRaises(storage.GitStateError):
            storage.sync_once(self._data_dir("older gofile copy"), config)

        restored = self.root / "restored"
        restored.mkdir()
        storage.restore(restored, self.make_config(workdir=self.root / "w3"))
        self.assertEqual((restored / "memories" / "note.md").read_text(),
                         "state already in github")

    def test_the_guard_lets_an_empty_branch_be_filled(self):
        """The guard must not block the very case it exists for."""
        self.assertTrue(
            self.storage.seed(self._data_dir(),
                              self.make_config(seed_from_gofile=True)))

    def test_successful_seed_disarms_the_guard(self):
        config = self.make_config(seed_from_gofile=True)
        self.storage.seed(self._data_dir(), config)
        self.assertFalse(config.seed_from_gofile)
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

    def test_fingerprint_changes_when_a_state_file_changes(self):
        storage = self.storage
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: x\n")
            before = storage.state_fingerprint(data_dir)
            (data_dir / "config.yaml").write_text("model: y\n")
            after = storage.state_fingerprint(data_dir)
            self.assertNotEqual(before, after)

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


if __name__ == "__main__":
    unittest.main()
