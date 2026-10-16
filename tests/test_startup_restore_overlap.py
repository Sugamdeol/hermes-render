import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('overlap_storage',ROOT/'scripts/git-storage.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class FullStartupPreparation(unittest.TestCase):
    def test_existing_state_requires_complete_successful_restore(self):
        with patch.object(m,'probe_state',return_value=m.REMOTE_HAS_STATE),patch.object(m,'restore',return_value=True) as restore:
            self.assertTrue(m.prepare_startup_state(Path('/fixture'),object()))
            restore.assert_called_once()
        with patch.object(m,'probe_state',return_value=m.REMOTE_HAS_STATE),patch.object(m,'restore',return_value=False):
            with self.assertRaises(m.GitStateError):m.prepare_startup_state(Path('/fixture'),object())

    def test_unknown_state_is_not_empty_and_never_seeded(self):
        with patch.object(m,'probe_state',return_value=m.REMOTE_UNKNOWN),patch.object(m,'restore') as restore,patch.object(m,'seed') as seed:
            with self.assertRaises(m.GitStateError):m.prepare_startup_state(Path('/fixture'),object())
            restore.assert_not_called();seed.assert_not_called()

    def test_empty_repository_does_not_restore_or_seed_in_parallel(self):
        with patch.object(m,'probe_state',return_value=m.REMOTE_EMPTY),patch.object(m,'restore') as restore,patch.object(m,'seed') as seed:
            self.assertTrue(m.prepare_startup_state(Path('/fixture'),object()))
            restore.assert_not_called();seed.assert_not_called()

    def test_bootstrap_rechecks_revision_without_repeating_finished_restore(self):
        import json, os
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);work=root/'work';source=work/'data';source.mkdir(parents=True)
            (source/'memory.txt').write_text('all saved data')
            (work/m.MANIFEST_NAME).write_text(json.dumps({}))
            home=root/'home';config=m.GitConfig('owner/repo','fixture',workdir=work)
            with patch.dict(os.environ,{'HERMES_RUNTIME_PROFILE':'colab-full','GIT_STATE_FENCE_ON_RESTORE':'0'}),patch.object(m,'probe_state',return_value=m.REMOTE_HAS_STATE),patch.object(m,'ensure_clone',return_value=work),patch.object(m,'head_commit',return_value='a'*40),patch.object(m,'materialize',wraps=m.materialize) as materialize:
                self.assertTrue(m.prepare_startup_state(home,config))
                next_config=m.GitConfig('owner/repo','fixture',workdir=work)
                self.assertTrue(m.restore(home,next_config))
                self.assertEqual(materialize.call_count,1)
                (source/'memory.txt').write_text('new remote revision')
                with patch.object(m,'head_commit',return_value='b'*40):
                    newer=m.GitConfig('owner/repo','fixture',workdir=work)
                    self.assertTrue(m.restore(home,newer))
                self.assertEqual(materialize.call_count,2)
            self.assertEqual((home/'memory.txt').read_text(),'new remote revision')
