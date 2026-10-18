import hashlib, importlib.util, json, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'scripts/original-files-tab'
def load_patch():
    spec=importlib.util.spec_from_file_location('files_patch',ROOT/'scripts/patch-files-analysis.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class OriginalFilesTests(unittest.TestCase):
    def test_byte_exact_assets_and_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'hermes_cli').mkdir();(root/'hermes_cli/web_server.py').write_text('app.include_router(_files_routes.router)')
            helper=load_patch();self.assertTrue(helper.patch(root));self.assertFalse(helper.patch(root))
            for name,digest in json.loads((ASSETS/'manifest.json').read_text())['files'].items():
                self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest)
    def test_unknown_router_refused_before_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'hermes_cli').mkdir();server=root/'hermes_cli/web_server.py';server.write_text('unknown=True')
            with self.assertRaises(RuntimeError):load_patch().patch(root)
            self.assertEqual(server.read_text(),'unknown=True');self.assertFalse((root/'web').exists())
