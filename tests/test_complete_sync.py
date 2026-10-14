import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_git_storage import load_storage, make_config


class CompleteSyncTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'home'
        self.data.mkdir()
        self.work = self.root / 'repo'
        self.work.mkdir()
        self.config = make_config(self.storage, workdir=self.work)

    def write(self, name, value='saved'):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_colab_roundtrip_keeps_project_and_installed_tool_files_and_deletions(self):
        names = ('workspace/logs/report.log', 'workspace/tmp/draft.tmp',
                 'workspace/node_modules/custom/index.js', 'workspace/.git/config',
                 'workspace/.venv/important', 'tools/store/node/lib/node_modules/npm/npm-cli.js',
                 '.cache/ms-playwright/chromium/chrome', 'desktop/interrupted_turns.json')
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full'}):
            for name in names:
                self.write(name)
            self.write('logs/gateway.log')
            self.write('.cache/pip/wheel')
            manifest = self.storage.build_worktree(self.data, self.work, self.config)
            self.assertEqual(manifest['files'], len(names))
            restored = self.root / 'restored'
            self.storage.materialize(self.work, restored, self.config)
            for name in names:
                self.assertEqual((restored/name).read_text(), 'saved')
            (self.data/names[0]).unlink()
            self.storage.build_worktree(self.data, self.work, self.config)
            self.assertFalse((self.work/'data'/names[0]).exists())

    def test_absolute_links_inside_home_are_restored_as_portable_relative_links(self):
        self.write('tools/bin/node')
        (self.data/'node').symlink_to(self.data/'tools/bin/node')
        self.storage.build_worktree(self.data, self.work, self.config)
        restored = self.root/'restored'
        self.storage.materialize(self.work, restored, self.config)
        self.assertTrue((restored/'node').is_symlink())
        self.assertEqual((restored/'node').read_text(), 'saved')
        self.assertFalse(Path(os.readlink(restored/'node')).is_absolute())

    def test_copy_failure_retains_previous_complete_snapshot_and_metadata(self):
        self.write('file', 'previous')
        self.storage.build_worktree(self.data, self.work, self.config)
        manifest = (self.work/'MANIFEST.json').read_bytes()
        signatures = (self.work/'hermes-source-signatures.json').read_bytes()
        self.write('file', 'new content')
        with patch.object(self.storage.shutil, 'copy2', side_effect=PermissionError('unreadable')):
            with self.assertRaisesRegex(self.storage.GitStateError, 'retaining the previous backup'):
                self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual((self.work/'data/file').read_text(), 'previous')
        self.assertEqual((self.work/'MANIFEST.json').read_bytes(), manifest)
        self.assertEqual((self.work/'hermes-source-signatures.json').read_bytes(), signatures)
        self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual((self.work/'data/file').read_text(), 'new content')

    def test_unreadable_directory_aborts_instead_of_turning_contents_into_deletions(self):
        def failed_walk(root, **kwargs):
            kwargs['onerror'](PermissionError('blocked directory'))
            yield root, [], []
        with patch.object(self.storage.os, 'walk', failed_walk):
            with self.assertRaisesRegex(self.storage.GitStateError, 'Cannot scan'):
                self.storage.plan_tree(self.data, self.storage.DEFAULT_EXCLUDES)

    def test_decryption_failure_invalidates_restore_checkpoint(self):
        (self.work/'data').mkdir()
        (self.work/'data/config.yaml.enc').write_bytes(b'unopenable')
        self.storage._write_sync_base(self.config, 'a'*40, 'old')
        with patch.object(self.storage, 'ensure_clone', return_value=self.work), patch.object(self.storage, 'head_commit', return_value='b'*40), patch.object(self.storage, 'age_decrypt', return_value=None):
            with self.assertRaisesRegex(self.storage.GitStateError, 'restore is incomplete'):
                self.storage.restore(self.data, self.config)
        self.assertIsNone(self.storage._read_sync_base(self.config))

    def test_same_vm_restart_preserves_unsynced_edits_additions_and_deletions(self):
        self.write('session', 'old')
        self.write('removed')
        self.storage._write_sync_base(self.config, 'a'*40, self.storage.state_fingerprint(self.data), {'state.db':'previous-remote-hash'}, data_dir=self.data)
        self.write('session', 'new progress')
        self.write('new-file')
        (self.data/'removed').unlink()
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full'}), patch.object(self.storage, 'ensure_clone', return_value=self.work), patch.object(self.storage, 'head_commit', return_value='a'*40), patch.object(self.storage, 'materialize') as materialize:
            self.assertTrue(self.storage.restore(self.data, self.config))
            materialize.assert_not_called()
        self.assertEqual(self.storage._read_sync_base(self.config)['sqlite_hashes'], {'state.db':'previous-remote-hash'})
        self.assertEqual((self.data/'session').read_text(), 'new progress')
        self.assertTrue((self.data/'new-file').exists())
        self.assertFalse((self.data/'removed').exists())

    def test_metadata_only_symlinks_and_empty_directories_are_pushed(self):
        storage = self.storage
        storage.run_git(['init', '-q'], cwd=self.work, config=self.config)
        self.write('one')
        self.write('two')
        (self.data/'link').symlink_to('one')
        storage.build_worktree(self.data, self.work, self.config)
        storage.stage_snapshot(self.work, self.config)
        storage.run_git(['commit', '-q', '-m', 'initial'], cwd=self.work, config=self.config)
        sha = storage.head_commit(self.work, self.config)
        storage._write_sync_base(self.config, sha, storage.state_fingerprint(self.data), data_dir=self.data)
        (self.data/'link').unlink()
        (self.data/'link').symlink_to('two')
        (self.data/'empty').mkdir()
        with patch.object(storage, 'ensure_safe_to_push'), patch.object(storage, 'ensure_clone', return_value=self.work), patch.object(storage, 'push_branch') as push:
            self.assertTrue(storage.sync_once(self.data, self.config))
            push.assert_called_once()
        result = storage.run_git(['show', 'HEAD:MANIFEST.json'], cwd=self.work, config=self.config)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest['symlinks']['link'], 'two')
        self.assertIn('empty', manifest['empty_dirs'])

    def test_failure_after_large_file_split_keeps_old_chunks_restorable(self):
        storage = self.storage
        with patch.object(storage, 'GIT_BLOB_CHUNK_BYTES', 16):
            self.write('large', 'A'*80)
            storage.build_worktree(self.data, self.work, self.config)
            self.write('large', 'B'*80)
            with patch.object(storage, 'split_large_blobs', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    storage.build_worktree(self.data, self.work, self.config)
            restored = self.root/'restored'
            storage.materialize(self.work, restored, self.config)
            self.assertEqual((restored/'large').read_text(), 'A'*80)

    def test_colab_git_uses_parallel_packing_without_render_memory_caps(self):
        import subprocess
        for profile, threads, memory in [('colab-full', '4', '128m'), ('render-lite', '1', '4m')]:
            with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':profile}), patch.object(self.storage.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                self.storage.run_git(['status'], config=self.config)
                args = run.call_args.args[0]
                self.assertIn('pack.threads='+threads, args)
                self.assertIn('pack.windowMemory='+memory, args)

    def test_other_vm_or_new_remote_requires_full_restore(self):
        self.write('session')
        self.storage._write_sync_base(self.config, 'a'*40, 'different', data_dir=self.data)
        for boot, sha in [('another-vm', 'a'*40), (self.storage.runtime_boot_id(), 'b'*40)]:
            with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full'}), patch.object(self.storage, 'runtime_boot_id', return_value=boot), patch.object(self.storage, 'ensure_clone', return_value=self.work), patch.object(self.storage, 'head_commit', return_value=sha), patch.object(self.storage, 'materialize', return_value=1) as materialize:
                self.assertTrue(self.storage.restore(self.data, self.config))
                materialize.assert_called_once()


if __name__ == '__main__':
    unittest.main()
