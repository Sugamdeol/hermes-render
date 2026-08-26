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
    def test_archive_excludes_env_file(self):
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
            self.assertNotIn(".env", names)
            self.assertNotIn("logs", names)
            self.assertNotIn("logs/gateway.log", names)
            self.assertIn("config.yaml", names)

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

    def test_state_fingerprint_ignores_logs_and_env(self):
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
            log_file.write_text("second and different\n")
            env_file.write_text("SECRET=two\n")
            self.assertEqual(first, storage._state_fingerprint(data_dir))

            config_file.write_text("model: changed\n")
            self.assertNotEqual(first, storage._state_fingerprint(data_dir))

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
