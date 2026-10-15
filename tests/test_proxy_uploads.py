"""Exercise the generated proxy config without touching host nginx or passwords."""
import os
from pathlib import Path
import re
import runpy
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ProxyUploadTests(unittest.TestCase):
    def config(self, **env):
        with patch.dict(os.environ, {'HERMES_GATEWAY_TOKEN': 'test-only', **env}, clear=True), \
             patch('sys.argv', ['start-proxy.py']), \
             patch.object(Path, 'write_text') as write, \
             patch.object(Path, 'chmod'), patch.object(Path, 'exists', return_value=True), \
             patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'test-hash')):
            runpy.run_path(str(ROOT / 'scripts/start-proxy.py'))
        return next(call.args[0] for call in write.call_args_list if 'http {' in call.args[0])

    def test_60_mib_file_fits_stream_route_but_chat_limit_stays_small(self):
        config = self.config()
        general, stream = config.split('location = /api/files/upload-stream {')
        stream = stream.split('}', 1)[0]
        limit = int(re.search(r'client_max_body_size (\d+);', stream).group(1))
        self.assertGreater(limit, 60 * 1024 * 1024 + 4096)
        self.assertEqual(limit, 101 * 1024 * 1024)
        self.assertIn(f'client_max_body_size {26 * 1024 * 1024};', general)
        self.assertIn('proxy_request_buffering off;', stream)
        self.assertIn('proxy_http_version 1.1;', stream)
        self.assertIn('proxy_pass http://127.0.0.1:9119;', stream)
        self.assertNotIn('auth_basic off', stream)
        self.assertIn('auth_basic "Hermes";', general)
        self.assertIn('proxy_set_header Origin $hermes_origin;', stream)
        self.assertNotIn('UPLOAD_BODY_LIMIT', config)

    def test_custom_chat_limit_does_not_lower_files_limit(self):
        config = self.config(HERMES_CHAT_UPLOAD_MAX_BYTES='1024')
        self.assertIn(f'client_max_body_size {1024 + 1024 * 1024};', config)
        self.assertIn(f'client_max_body_size {101 * 1024 * 1024};', config)
