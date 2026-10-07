from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ChatUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the real composer probe')
    def test_composer_interactions(self):
        subprocess.run(['node', str(ROOT / 'tests/chat_composer_probe.cjs'),
                        str(ROOT / 'dashboard-plugins/hermes-chat-dashboard/dashboard/bundle/index.js')], check=True)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the model probe')
    def test_model_switching(self):
        subprocess.run(['node', str(ROOT / 'tests/chat_model_probe.cjs'),
                        str(ROOT / 'dashboard-plugins/hermes-chat-dashboard/dashboard/bundle/index.js')], check=True)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the stream probe')
    def test_missing_start_event(self):
        subprocess.run(['node', str(ROOT / 'tests/chat_stream_probe.cjs'),
                        str(ROOT / 'dashboard-plugins/hermes-chat-dashboard/dashboard/bundle/index.js')], check=True)
