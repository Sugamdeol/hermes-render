from __future__ import annotations

import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "free-storage.py"


def load_storage():
    spec = importlib.util.spec_from_file_location("free_storage", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FreeStorageTests(unittest.TestCase):
    def test_archive_includes_state_but_excludes_volatile_paths(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / ".env").write_text("BYNARA_API_KEY=not-for-backups")
            (data_dir / "config.yaml").write_text("model:\n  default: qwen-3.8-max-free\n")
            (data_dir / "logs").mkdir()
            (data_dir / "logs" / "gateway.log").write_text("not needed in backup")
            archive_bytes = io.BytesIO()

            storage._create_archive(data_dir, archive_bytes)

            archive_bytes.seek(0)
            with tarfile.open(fileobj=archive_bytes, mode="r:gz") as archive:
                names = {member.name.removeprefix("./") for member in archive.getmembers()}
            # Durable state is archived...
            self.assertIn(".env", names)
            self.assertIn("config.yaml", names)
            # ...but logs are not. They are the bulk of the churn and are
            # worthless in a restored instance, so archiving them turned every
            # log line into a full-tree re-upload.
            self.assertNotIn("logs", names)
            self.assertNotIn("logs/gateway.log", names)

    def test_storage_is_disabled_without_gofile_token(self):
        storage = load_storage()
        old = os.environ.pop("GOFILE_API_TOKEN", None)
        old_alias = os.environ.pop("GOFILE_TOKEN", None)
        try:
            self.assertIsNone(storage.StorageConfig.from_env())
        finally:
            if old is not None:
                os.environ["GOFILE_API_TOKEN"] = old
            if old_alias is not None:
                os.environ["GOFILE_TOKEN"] = old_alias

    def test_state_fingerprint_tracks_durable_state_and_ignores_logs(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            config_file = data_dir / "config.yaml"
            env_file = data_dir / ".env"
            log_dir = data_dir / "logs"
            log_dir.mkdir()
            config_file.write_text("model: test\n")
            env_file.write_text("SECRET=one\n")
            log_file = log_dir / "gateway.log"
            log_file.write_text("first\n")

            first = storage._state_fingerprint(data_dir)
            # A log write must NOT count as a change: it is excluded from the
            # archive, so uploading because of it would send nothing new.
            log_file.write_text("second and different\n")
            self.assertEqual(first, storage._state_fingerprint(data_dir))

            config_file.write_text("model: changed and longer\n")
            self.assertNotEqual(first, storage._state_fingerprint(data_dir))

    def test_content_hash_catches_same_size_edits_metadata_misses(self):
        """A rotated key is the realistic case: same byte count, possibly the
        same mtime tick. The metadata fingerprint can miss that; the content
        hash is what makes it safe, which is why sync_once falls through to it
        periodically instead of trusting metadata forever."""
        storage = load_storage()
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            env_file = data_dir / ".env"
            env_file.write_text("SECRET=one\n")

            before = storage._content_hash(data_dir)
            env_file.write_text("SECRET=two\n")  # identical length

            self.assertNotEqual(before, storage._content_hash(data_dir))

    def test_content_hash_ignores_excluded_paths(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: test\n")
            (data_dir / "logs").mkdir()
            log = data_dir / "logs" / "gateway.log"
            log.write_text("line one\n")

            before = storage._content_hash(data_dir)
            log.write_text("a completely different and much longer log line\n")

            self.assertEqual(before, storage._content_hash(data_dir))

    def test_unchanged_state_skips_archive_creation(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("model: test\n")
            config = storage.StorageConfig(
                token="token",
                folder_id="folder",
                folder_name="hermes-render-state",
                prefix="hermes-state-",
                interval=300,
                max_archive_bytes=1024,
                user_agent="agent",
                language="en-US",
                wt_salt="salt",
            )
            config.last_fingerprint = storage._state_fingerprint(data_dir)
            original = storage._create_archive

            def unexpected_archive(*_args, **_kwargs):
                raise AssertionError("unchanged state was recompressed")

            storage._create_archive = unexpected_archive
            try:
                self.assertTrue(storage.sync_once(data_dir, config))
            finally:
                storage._create_archive = original

    def test_state_folder_is_created_when_folder_id_is_not_set(self):
        storage = load_storage()
        config = storage.StorageConfig(
            token="token",
            folder_id="",
            folder_name="hermes-render-state",
            prefix="hermes-state-",
            interval=60,
            max_archive_bytes=1024,
            user_agent="agent",
            language="en-US",
            wt_salt="salt",
        )
        calls = []
        responses = {
            "/accounts/getid": {"id": "account"},
            "/accounts/account": {"rootFolder": "root"},
            "/contents/root": {"type": "folder", "children": {}},
            "/contents/createFolder": {"id": "state-folder"},
        }
        original = storage._json_request

        def fake_request(_config, _method, path, **_kwargs):
            calls.append(path)
            return responses[path]

        storage._json_request = fake_request
        try:
            self.assertEqual(storage._ensure_folder(config), "state-folder")
            self.assertEqual(config.folder_id, "state-folder")
            self.assertIn("/contents/createFolder", calls)
        finally:
            storage._json_request = original


if __name__ == "__main__":
    unittest.main()


def _config(storage, **overrides):
    kwargs = dict(
        token="t", folder_id="f", folder_name="n", prefix="hermes-state-",
        interval=300, max_archive_bytes=10 ** 9, user_agent="u",
        language="en-US", wt_salt="s",
    )
    kwargs.update(overrides)
    return storage.StorageConfig(**kwargs)


class BandwidthTests(unittest.TestCase):
    """The sync used to upload the whole tree whenever any mtime moved, which
    is what made it expensive. These pin the three brakes."""

    def setUp(self):
        self.storage = load_storage()

    def _prepared(self, data_dir, config, *, uploaded_at):
        (data_dir / "config.yaml").write_text("model: a\n")
        config.last_fingerprint = self.storage._state_fingerprint(data_dir, config.excludes)
        config.last_content_hash = self.storage._content_hash(data_dir, config.excludes)
        config.last_upload_at = uploaded_at
        config.last_content_check = uploaded_at

    def test_upload_is_rate_limited_after_a_recent_upload(self):
        config = _config(self.storage, min_upload_interval=1800)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._prepared(data_dir, config, uploaded_at=1000.0)
            (data_dir / "config.yaml").write_text("model: changed materially\n")
            self.storage._create_archive = lambda *a, **k: self.fail("uploaded too soon")

            # 60s later: real change, but inside the minimum interval.
            self.assertTrue(self.storage.sync_once(data_dir, config, now=1060.0))

    def test_force_bypasses_the_rate_limit_so_shutdown_always_flushes(self):
        config = _config(self.storage, min_upload_interval=1800)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._prepared(data_dir, config, uploaded_at=1000.0)
            (data_dir / "config.yaml").write_text("model: changed\n")
            attempted = []
            self.storage._create_archive = lambda *a, **k: attempted.append(1)

            with self.assertRaises(Exception):
                self.storage.sync_once(data_dir, config, force=True, now=1060.0)
            self.assertTrue(attempted)

    def test_monthly_budget_stops_uploads(self):
        import datetime as dt
        config = _config(self.storage, min_upload_interval=0, monthly_budget_bytes=10 * 1024 * 1024)
        when = 99999.0
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._prepared(data_dir, config, uploaded_at=0.0)
            (data_dir / "config.yaml").write_text("model: changed\n")
            # Bucketed by the same clock sync_once uses, not wall-clock now.
            self.storage.record_usage(
                data_dir, 11 * 1024 * 1024,
                dt.datetime.fromtimestamp(when, dt.timezone.utc),
            )
            self.storage._create_archive = lambda *a, **k: self.fail("uploaded over budget")

            self.assertFalse(self.storage.sync_once(data_dir, config, now=when))

    def test_usage_resets_on_a_new_month(self):
        import datetime as dt
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            july = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
            august = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
            self.storage.record_usage(data_dir, 5 * 1024 * 1024, july)

            self.assertEqual(self.storage.read_usage(data_dir, july)[1], 5 * 1024 * 1024)
            self.assertEqual(self.storage.read_usage(data_dir, august)[1], 0)

    def test_standby_never_uploads(self):
        """The whole point of the lease: a standby's /opt/data is stale, so
        uploading it would destroy the active instance's state."""
        config = _config(self.storage, failover=True, min_upload_interval=0)
        config.role = self.storage.ROLE_STANDBY
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.yaml").write_text("stale\n")
            self.storage._create_archive = lambda *a, **k: self.fail("standby uploaded")

            self.assertTrue(self.storage.sync_once(data_dir, config, force=True))


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()

    def test_lease_filename_round_trips(self):
        name = self.storage.lease_filename(100, "My Laptop.local")
        self.assertEqual(name, "hermes-lease-100-My-Laptop-local.json")
        self.assertEqual(self.storage.parse_lease_name(name), (100, "My-Laptop-local"))

    def test_ignores_unrelated_filenames(self):
        self.assertIsNone(self.storage.parse_lease_name("hermes-state-20260101T000000Z-ab.tar.gz"))
        self.assertIsNone(self.storage.parse_lease_name("hermes-lease-bad.json"))

    def test_render_goes_standby_while_the_laptop_lease_is_fresh(self):
        role = self.storage.decide_role(
            [(100, "laptop", 990.0)], instance_id="render", priority=50, now=1000.0, ttl=600
        )
        self.assertEqual(role, self.storage.ROLE_STANDBY)

    def test_render_resumes_once_the_laptop_lease_goes_stale(self):
        role = self.storage.decide_role(
            [(100, "laptop", 100.0)], instance_id="render", priority=50, now=1000.0, ttl=600
        )
        self.assertEqual(role, self.storage.ROLE_ACTIVE)

    def test_laptop_outranks_render_and_stays_active(self):
        role = self.storage.decide_role(
            [(50, "render", 999.0)], instance_id="laptop", priority=100, now=1000.0, ttl=600
        )
        self.assertEqual(role, self.storage.ROLE_ACTIVE)

    def test_a_lone_instance_is_active(self):
        role = self.storage.decide_role(
            [], instance_id="render", priority=50, now=1000.0, ttl=600
        )
        self.assertEqual(role, self.storage.ROLE_ACTIVE)

    def test_own_lease_is_ignored(self):
        role = self.storage.decide_role(
            [(50, "render", 999.0)], instance_id="render", priority=50, now=1000.0, ttl=600
        )
        self.assertEqual(role, self.storage.ROLE_ACTIVE)

    def test_equal_priority_instances_do_not_both_go_quiet(self):
        peers = [(50, "alpha", 999.0), (50, "beta", 999.0)]
        roles = {
            name: self.storage.decide_role(
                peers, instance_id=name, priority=50, now=1000.0, ttl=600
            )
            for name in ("alpha", "beta")
        }
        self.assertEqual(list(roles.values()).count(self.storage.ROLE_ACTIVE), 1)
