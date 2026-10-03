import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("storage_encryption", Path(__file__).parents[1] / "scripts/git-storage.py")
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class EncryptedRestoreTests(unittest.TestCase):
    def test_roundtrip_and_unchanged_ciphertext(self):
        config = storage.GitConfig(repo="owner/repo", token="test", env_mode="encrypt")
        with patch.dict(os.environ, {"STORAGE_ENCRYPTION_KEY": "test-only-key"}):
            sealed = storage.age_encrypt(b"provider_secret=123", config)
            self.assertNotIn(b"provider_secret", sealed)
            self.assertEqual(storage.age_decrypt(sealed, config), b"provider_secret=123")
            self.assertEqual(storage.age_encrypt(b"provider_secret=123", config), sealed)
        with patch.dict(os.environ, {"STORAGE_ENCRYPTION_KEY": "wrong"}):
            with self.assertRaises(RuntimeError):
                storage.age_decrypt(sealed, config)

    def test_all_settings_restore_and_archives_survive(self):
        config = storage.GitConfig(repo="owner/repo", token="test", env_mode="encrypt")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STORAGE_ENCRYPTION_KEY": "test-only-key"}):
            root = Path(tmp)
            data, work, restored = root / "local", root / "work", root / "restored"
            for p in (data, work, restored):
                p.mkdir()
            (work / "archives").mkdir()
            (work / "archives/nanobot.enc").write_bytes(b"old encrypted archive")
            for name in storage.SENSITIVE_FILES:
                (data / name).write_text("a-private-value")
            (data / "memories").mkdir()
            (data / "memories/MEMORY.md").write_text("remember this")
            storage.build_worktree(data, work, config)
            for name in storage.SENSITIVE_FILES:
                self.assertFalse((work / "data" / name).exists())
                self.assertTrue((work / "data" / (name + ".enc")).exists())
            storage.materialize(work, restored, config)
            self.assertEqual((restored / "config.yaml").read_text(), "a-private-value")
            self.assertEqual((restored / "memories/MEMORY.md").read_text(), "remember this")
            self.assertTrue((work / "archives/nanobot.enc").exists())

    def test_workspace_copy_does_not_buffer_files(self):
        config = storage.GitConfig(repo="owner/repo", token="test", env_mode="omit")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, work, restored = root / "local", root / "work", root / "restored"
            for directory in (data, work, restored):
                directory.mkdir()
            artifact = data / "artifact.bin"
            with artifact.open("wb") as handle:
                handle.write(b"workspace payload")
                handle.truncate(16 * 1024 * 1024)
            # Ordinary files must never use whole-file reads in either direction.
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded file read")):
                storage.build_worktree(data, work, config)
                storage.materialize(work, restored, config)
            self.assertEqual((restored / "artifact.bin").stat().st_size, artifact.stat().st_size)
            with (restored / "artifact.bin").open("rb") as handle:
                self.assertEqual(handle.read(17), b"workspace payload")
