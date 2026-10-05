import asyncio
import importlib.util
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.request = Path(self.tmp.name) / 'checkpoint.json'
        self.env = patch.dict(os.environ, {
            'HERMES_RECOVERY_REQUIRE_REMOTE': '1',
            'GIT_STATE_REPO': 'owner/private', 'GIT_STATE_TOKEN': 'test-only',
            'HERMES_RECOVERY_CHECKPOINT_FILE': str(self.request),
            'HERMES_RECOVERY_CHECKPOINT_TIMEOUT_SECONDS': '2',
        })
        self.env.start()
        self.recovery = runpy.run_path(str(ROOT / 'scripts/task-recovery.py'))
        spec = importlib.util.spec_from_file_location('checkpoint_storage', ROOT / 'scripts/git-storage.py')
        self.storage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.storage)
        self.config = self.storage.GitConfig(repo='owner/private', token='test-only', env_mode='encrypt')

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_agent_waits_for_remote_upload_acknowledgement(self):
        self.request.with_suffix('.ack').write_text(json.dumps({'nonce': 'old', 'ok': True}))
        task = asyncio.create_task(self.recovery['checkpoint']())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        with patch.object(self.storage, 'sync_once', return_value=True) as upload:
            self.assertTrue(self.storage.recovery_checkpoint(Path(self.tmp.name), self.config))
            upload.assert_called_once()
            self.assertFalse(self.storage.recovery_checkpoint(Path(self.tmp.name), self.config))
        await task

    async def test_failed_upload_does_not_allow_agent_execution(self):
        task = asyncio.create_task(self.recovery['checkpoint']())
        await asyncio.sleep(0.05)
        with patch.object(self.storage, 'sync_once', side_effect=self.storage.GitStateError('network unavailable')):
            self.storage.recovery_checkpoint(Path(self.tmp.name), self.config)
        with self.assertRaisesRegex(RuntimeError, 'remains queued'):
            await task

    async def test_dead_storage_thread_times_out_instead_of_false_success(self):
        with patch.dict(os.environ, {'HERMES_RECOVERY_CHECKPOINT_TIMEOUT_SECONDS': '0.05'}):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                await self.recovery['checkpoint']()

    async def test_optional_backup_failure_allows_local_recovery(self):
        with patch.dict(os.environ, {'HERMES_RECOVERY_REQUIRE_REMOTE': '0'}):
            task = asyncio.create_task(self.recovery['checkpoint']())
            await asyncio.sleep(0.05)
            with patch.object(self.storage, 'sync_once', side_effect=self.storage.GitStateError('remote advanced')):
                self.storage.recovery_checkpoint(Path(self.tmp.name), self.config)
            self.assertFalse(await task)

    async def test_optional_backup_timeout_allows_chat(self):
        with patch.dict(os.environ, {'HERMES_RECOVERY_REQUIRE_REMOTE': '0', 'HERMES_RECOVERY_CHECKPOINT_TIMEOUT_SECONDS': '0.05'}):
            self.assertFalse(await self.recovery['checkpoint']())

    async def test_no_remote_storage_does_not_wait(self):
        with patch.dict(os.environ, {'GIT_STATE_REPO': ''}):
            await self.recovery['checkpoint']()
        self.assertFalse(self.request.exists())

    async def test_omit_mode_never_acknowledges_recoverable_backup(self):
        self.config.env_mode = 'omit'
        self.request.write_text(json.dumps({'nonce': 'test'}))
        with patch.object(self.storage, 'sync_once') as upload:
            self.storage.recovery_checkpoint(Path(self.tmp.name), self.config)
            upload.assert_not_called()
        self.assertFalse(json.loads(self.request.with_suffix('.ack').read_text())['ok'])

    async def test_root_storage_ack_is_owned_by_unprivileged_gateway(self):
        self.request.write_text(json.dumps({'nonce': 'unprivileged'}))
        with patch.object(self.storage, 'sync_once', return_value=True), \
             patch.object(os, 'geteuid', return_value=0), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=10000, st_gid=10000)), \
             patch.object(os, 'fchown') as transfer:
            self.storage.recovery_checkpoint(Path(self.tmp.name), self.config)
        self.assertEqual(transfer.call_args.args[1:], (10000, 10000))
        self.assertEqual(self.request.with_suffix('.ack').stat().st_mode & 0o777, 0o600)
