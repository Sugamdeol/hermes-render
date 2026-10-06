import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
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

    def test_encrypted_sqlite_snapshot_roundtrip_handles_multiple_chunks(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STORAGE_ENCRYPTION_KEY": "test-only-key"}):
            root = Path(tmp)
            data, work, restored = root / "local", root / "work", root / "restored"
            for p in (data, work, restored):
                p.mkdir()
            config = storage.GitConfig(repo="owner/repo", token="test", env_mode="encrypt",
                                       workdir=work)
            db_path = data / "state.db"
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body BLOB)")
            conn.execute("INSERT INTO messages(body) VALUES (zeroblob(2300000))")
            conn.commit()
            storage.build_worktree(data, work, config)

            cipher_path = work / "data/state.db.enc"
            self.assertTrue(cipher_path.exists())
            self.assertFalse((work / "data/state.db").exists())
            self.assertFalse(Path(str(work / "data/state.db") + "-wal").exists())
            with cipher_path.open("rb") as handle:
                self.assertEqual(handle.readline(), storage._COMPRESSED_STREAM_PREFIX)
            self.assertLess(cipher_path.stat().st_size, 230000,
                            "compress sparse database pages before encryption")
            first_ciphertext = cipher_path.read_bytes()
            storage._write_sync_base(config, "checkpoint", "fingerprint",
                                     config._sqlite_hashes)
            storage.build_worktree(data, work, config)
            self.assertEqual((work / "data/state.db.enc").read_bytes(), first_ciphertext,
                             "unchanged SQLite content should reuse ciphertext and avoid needless commits")
            storage.materialize(work, restored, config)
            restored_conn = sqlite3.connect(restored / "state.db")
            try:
                length = restored_conn.execute("SELECT length(body) FROM messages").fetchone()[0]
                self.assertEqual(length, 2300000)
            finally:
                restored_conn.close()
                conn.close()
            storage._write_sync_base(config, "checkpoint", "fingerprint",
                                     storage._sqlite_fingerprints(restored))
            storage.build_worktree(restored, work, config)
            self.assertEqual((work / "data/state.db.enc").read_bytes(), first_ciphertext,
                             "restore followed by sync should not churn encrypted database commits")

    def test_legacy_and_compressed_streams_restore_random_data(self):
        config = storage.GitConfig(repo="owner/repo", token="test", env_mode="encrypt")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STORAGE_ENCRYPTION_KEY": "test-only-key"}):
            root = Path(tmp)
            source, sealed, opened = root / "source", root / "sealed", root / "opened"
            payload = os.urandom(2 * storage._STREAM_CHUNK_BYTES + 37)
            source.write_bytes(payload)
            for compress in (False, True):
                storage.encrypt_stream_file(source, sealed, config, compress=compress)
                storage.decrypt_stream_file(sealed, opened, config)
                self.assertEqual(opened.read_bytes(), payload)
                sealed.write_bytes(sealed.read_bytes()[:-1])
                opened.unlink()
                with self.assertRaises(ValueError):
                    storage.decrypt_stream_file(sealed, opened, config)
                self.assertFalse(opened.exists())

    def test_streamed_sqlite_ciphertext_tampering_fails_closed(self):
        config = storage.GitConfig(repo="owner/repo", token="test", env_mode="encrypt")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STORAGE_ENCRYPTION_KEY": "test-only-key"}):
            root = Path(tmp)
            source, sealed, opened = root / "source", root / "sealed", root / "opened"
            source.write_bytes(b"sensitive database page" * 70000)
            storage.encrypt_stream_file(source, sealed, config)
            ciphertext = bytearray(sealed.read_bytes())
            ciphertext[-20] ^= 1
            sealed.write_bytes(ciphertext)
            with self.assertRaises(Exception):
                storage.decrypt_stream_file(sealed, opened, config)
            self.assertFalse(opened.exists())

    def test_age_identity_temp_file_is_removed_after_decrypt(self):
        config = storage.GitConfig(repo="owner/repo", token="test")
        created = []

        def fake_run(args, **kwargs):
            identity = Path(args[args.index("-i") + 1])
            created.append(identity)
            self.assertTrue(identity.exists())
            self.assertEqual(identity.read_text().strip(), "AGE-SECRET-KEY-test")
            return subprocess.CompletedProcess(args, 0, b"opened", b"")

        with patch.dict(os.environ, {"SOPS_AGE_KEY": "AGE-SECRET-KEY-test"}), \
             patch.object(storage.shutil, "which", return_value="/usr/bin/age"), \
             patch.object(storage.subprocess, "run", side_effect=fake_run):
            self.assertEqual(storage.age_decrypt(b"age1ciphertext", config), b"opened")
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())
