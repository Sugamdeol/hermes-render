import json
import io
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('colab_update', ROOT / 'scripts/colab-update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.health = patch.object(updater.urllib.request, 'urlopen',
            side_effect=lambda *a, **kw: io.BytesIO(json.dumps(self.status).encode()))
        self.status = {'active_agents': 0, 'gateway_busy': False, 'active_sessions': 4}
        self.health.start()
        self.addCleanup(self.health.stop)

    def test_idle_recent_chats_do_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            updater.idle(Mock(), Path(td))

    def test_running_or_unknown_work_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            for status in ({'active_agents': 1}, {'active_agents': 0, 'gateway_busy': True},
                           {}, {'active_agents': '0'}, {'active_agents': False}):
                self.status = status
                with self.assertRaises(RuntimeError):
                    updater.idle(Mock(), Path(td))

    def test_annotated_release_resolves_to_commit(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, 'a'*40+'\trefs/tags/v1\n'+'b'*40+'\trefs/tags/v1^{}\n'))
        self.assertEqual(updater.resolve({'run': run}, 'v1'), 'b'*40)
        self.assertIn(updater.UPSTREAM, run.call_args.args[0])

    def test_invalid_ref_never_invokes_git(self):
        run = Mock()
        for ref in ('--upload-pack=evil', '../main', 'main;echo', '', None):
            with self.assertRaises(ValueError):
                updater.resolve({'run': run}, ref)
        run.assert_not_called()

    def test_pending_or_corrupt_journal_blocks_update(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            agent = Mock(); agent.status.return_value = {'active_sessions': 0}
            for value in ('{"x":{"status":"pending"}}', '[]', '{broken'):
                (home / '.render-recovery.json').write_text(value)
                with self.assertRaises((RuntimeError, ValueError)):
                    updater.idle(agent, home)
            (home / '.render-recovery.json').write_text('{}')
            updater.idle(agent, home)

    def test_incompatible_patches_leave_running_host_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); home = root / 'data'; home.mkdir()
            agent = Mock(); agent.status.return_value = {'active_sessions': 0}
            def run(args, **kwargs):
                if str(args[-2]).endswith('patch-colab-full.py'):
                    raise RuntimeError('patch mismatch')
            ns = {'INSTALL': root/'install', 'TOOLS': root/'tools', 'DATA': home, 'run': run}
            with patch.object(updater, 'check', return_value={'update_available': True, 'target': 'a'*40}):
                with self.assertRaisesRegex(RuntimeError, 'patch mismatch'):
                    updater.update(ns, agent)
            agent.backup.assert_not_called(); agent.stop.assert_not_called()

    def test_backup_failure_never_stops_host(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); home = root / 'data'; home.mkdir()
            agent = Mock(); agent.status.return_value = {'active_sessions': 0}
            agent.backup.side_effect = RuntimeError('backup failed')
            ns = {'INSTALL': root/'install', 'TOOLS': root/'tools', 'DATA': home, 'run': Mock()}
            with patch.object(updater, 'check', return_value={'update_available': True, 'target': 'a'*40}):
                with self.assertRaisesRegex(RuntimeError, 'backup failed'):
                    updater.update(ns, agent)
            agent.stop.assert_not_called()

    def test_failed_start_restores_previous_runtime_and_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); home = root/'data'; home.mkdir()
            install = root/'install'; install.mkdir(); (install/'old').write_text('working')
            tools = root/'tools'; tools.mkdir(); (tools/'old').write_text('working')
            agent = Mock(); agent.status.return_value = {'active_sessions': 0}
            agent.process.poll.return_value = 0
            def build(*args, **kwargs):
                install.mkdir(); (install/'candidate').touch()
            replacement = Mock()
            ns = {'INSTALL': install, 'TOOLS': tools, 'DATA': home, 'SOURCE': root,
                  'run': Mock(return_value=subprocess.CompletedProcess([], 0, 'revision')),
                  'install_runtime': build, 'cache_directory': lambda: root,
                  'StartupProgress': MagicMock(), 'launch_with_progress': Mock(side_effect=[RuntimeError('bad startup'), replacement]),
                  'HERMES_COLAB': agent}
            with patch.object(updater, 'check', return_value={'update_available': True, 'target': 'a'*40}):
                with self.assertRaisesRegex(RuntimeError, 'previous runtime restarted'):
                    updater.update(ns, agent)
            self.assertTrue((install/'old').exists())
            self.assertTrue((tools/'old').exists())
            self.assertIs(ns['HERMES_COLAB'], replacement)
            self.assertFalse((root/'selected-upstream.txt').exists())

    def test_second_backup_failure_removes_unused_tools_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'data';home.mkdir()
            install=root/'install';install.mkdir()
            tools=root/'tools';tools.mkdir();(tools/'keep').write_text('working')
            agent=Mock();agent.stop.side_effect=RuntimeError('second backup failed')
            ns={'INSTALL':install,'TOOLS':tools,'DATA':home,'SOURCE':root,
                'run':Mock(return_value=subprocess.CompletedProcess([],0,'revision'))}
            with patch.object(updater,'check',return_value={'update_available':True,'target':'a'*40}):
                with self.assertRaisesRegex(RuntimeError,'second backup failed'):updater.update(ns,agent)
            self.assertTrue(install.exists());self.assertEqual((tools/'keep').read_text(),'working')
            self.assertEqual(list(root.glob('tools-before-update-*')),[])
