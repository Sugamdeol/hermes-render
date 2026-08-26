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

    def test_storage_is_disabled_without_all_credentials(self):
        storage = load_storage()
        names = ("GOFILE_API_TOKEN", "GOFILE_FOLDER_ID")
        old = {name: os.environ.pop(name, None) for name in names}
        try:
            self.assertIsNone(storage.StorageConfig.from_env())
        finally:
            for name, value in old.items():
                if value is not None:
                    __import__("os").environ[name] = value


if __name__ == "__main__":
    unittest.main()
