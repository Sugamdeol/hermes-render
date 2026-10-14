import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class MissingCommitTests(unittest.TestCase):
    def test_fresh_checkout_fetch_has_its_own_noninteractive_environment(self):
        spec = importlib.util.spec_from_file_location('missing_commit_launcher', ROOT/'run-colab.py')
        launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); install=root/'install'; source=root/'source'
            (install/'.git').mkdir(parents=True); source.mkdir()
            (source/'colab-upstream.txt').write_text('a'*40)
            result=subprocess.CompletedProcess([],0,'https://github.com/NousResearch/hermes-agent.git','')
            with patch.object(launcher,'INSTALL',install), patch.object(launcher,'SOURCE',source), patch.object(launcher,'cache_directory',return_value=root/'cache'), patch.object(launcher,'runtime_key',return_value='key'), patch.object(launcher.subprocess,'run',return_value=subprocess.CompletedProcess([],1)), patch.object(launcher,'run',return_value=result) as run, patch.object(launcher,'install_node',side_effect=RuntimeError('node reached')):
                with self.assertRaisesRegex(RuntimeError,'node reached'):
                    launcher.install_runtime('revision')
            fetches=[call for call in run.call_args_list if 'fetch' in call.args[0]]
            self.assertEqual(len(fetches),1)
            self.assertEqual(fetches[0].kwargs['env']['GIT_TERMINAL_PROMPT'],'0')
            self.assertEqual(fetches[0].kwargs['cwd'],install)
