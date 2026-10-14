import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FastRestoreTests(unittest.TestCase):
    def setUp(self):
        self.storage = load('fast_storage', 'scripts/git-storage.py')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.home.mkdir()
        (self.home / 'session.json').write_text('saved chat')
        self.config = self.storage.GitConfig(repo='owner/repo', token='test', workdir=Path(self.tmp.name) / 'clone')
        self.storage._write_sync_base(self.config, 'a'*40, self.storage.state_fingerprint(self.home), {})

    def test_reuses_only_matching_remote_and_local_state(self):
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full'}):
            self.assertTrue(self.storage.local_restore_is_current(self.home, self.config, 'a'*40))
            self.assertFalse(self.storage.local_restore_is_current(self.home, self.config, 'b'*40))
            (self.home / 'session.json').write_text('newer unsaved chat')
            self.assertFalse(self.storage.local_restore_is_current(self.home, self.config, 'a'*40))

    def test_render_and_missing_checkpoint_use_full_restore(self):
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'render-lite'}):
            self.assertFalse(self.storage.local_restore_is_current(self.home, self.config, 'a'*40))
        self.storage._sync_base_path(self.config).unlink()
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full'}):
            self.assertFalse(self.storage.local_restore_is_current(self.home, self.config, 'a'*40))

    def test_database_hash_mismatch_prevents_reuse_even_if_metadata_matches(self):
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full'}), patch.object(self.storage, '_sqlite_fingerprints', return_value={'state.db':'changed'}):
            self.assertFalse(self.storage.local_restore_is_current(self.home, self.config, 'a'*40))

    def test_verified_reuse_still_fences_writer_without_materializing(self):
        work = self.config.workdir
        work.mkdir()
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full', 'GIT_STATE_FENCE_ON_RESTORE':'1'}), patch.object(self.storage, 'ensure_clone', return_value=work), patch.object(self.storage, 'head_commit', return_value='a'*40), patch.object(self.storage, 'remote_has_data', return_value=True), patch.object(self.storage, 'ensure_safe_to_push'), patch.object(self.storage, 'run_git'), patch.object(self.storage, 'push_branch') as fence, patch.object(self.storage, 'materialize') as materialize:
            self.assertTrue(self.storage.restore(self.home, self.config))
            fence.assert_called_once()
            materialize.assert_not_called()


class FastDependencyTests(unittest.TestCase):
    def setUp(self):
        self.deps = load('fast_deps', 'scripts/runtime-dependencies.py')

    def test_batch_apt_probe_keeps_missing_packages_on_nonzero_exit(self):
        result = subprocess.CompletedProcess([], 1, 'curl\tinstall ok installed\nlibc6:amd64\tinstall ok installed\n', '')
        with patch.object(self.deps, 'command', return_value=result) as command:
            self.assertEqual(self.deps.missing_apt(['curl', 'libc6', 'missing', 'linux-kernel']), ['missing'])
            command.assert_called_once()

    def test_browser_relaunch_skips_installer_but_fresh_runtime_installs(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / '.runtime-dependencies.json').write_text(json.dumps({'version':1, 'browsers':['chromium']}))
            real_path = Path
            def redirect(value):
                return real_path(td) / 'browser-markers' if value == '/tmp/hermes-browser-ready' else real_path(value)
            result = subprocess.CompletedProcess([], 0, '{}', '')
            with patch.dict(os.environ, {'HERMES_RUNTIME_DEPS_AUTORESTORE':'0'}), patch.object(self.deps, 'Path', side_effect=redirect), patch.object(self.deps, 'command', return_value=result) as command, patch.object(self.deps, 'browser_ready', return_value=True):
                self.assertEqual(self.deps.restore(home), [])
                installs = [c for c in command.call_args_list if 'playwright' in c.args[0]]
                self.assertEqual(len(installs), 1)
                command.reset_mock()
                self.assertEqual(self.deps.restore(home), [])
                self.assertFalse(any('playwright' in c.args[0] for c in command.call_args_list))
                self.assertTrue(json.loads((home / '.runtime-dependencies-status.json').read_text())['ready'])

    def test_background_default_can_be_disabled(self):
        launcher = load('fast_launcher', 'run-colab.py')
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'cache_directory', return_value=Path(td)), patch.dict(os.environ, {'HERMES_RUNTIME_DEPS_BACKGROUND':'0'}):
            env = launcher.runtime_env('test-token', 'test-key')
        self.assertEqual(env['HERMES_RUNTIME_DEPS_BACKGROUND'], '0')
        self.assertIn('HERMES_RUNTIME_DEPS_BACKGROUND', env['HERMES_ENV_OVERRIDE_KEYS'])

    def test_gateway_continues_while_background_dependency_install_runs(self):
        source = (ROOT / 'scripts/bootstrap.sh').read_text()
        start = source.index('# Session restore is complete;')
        end = source.index('\nfi', start) + len('\nfi')
        block = source[start:end].replace('/opt/hermes/.venv/bin/python /opt/render-tools/runtime-dependencies.py restore "${DATA_DIR}"', 'sleep 0.2')
        block = block.replace('$(id -u)', '0')
        env = dict(os.environ, HERMES_RUNTIME_PROFILE='colab-full', HERMES_RUNTIME_DEPS_BACKGROUND='1', HERMES_RUNTIME_DEPS_AUTORESTORE='1', HERMES_COLAB_SYSTEM_PYTHON='python', DATA_DIR='/tmp')
        result = subprocess.run(['sh', '-c', block + '\necho gateway-started\nwait'], env=env, capture_output=True, text=True, check=True)
        self.assertLess(result.stdout.index('gateway-started'), result.stdout.index('Saved apps and libraries are ready'))


if __name__ == '__main__':
    unittest.main()
