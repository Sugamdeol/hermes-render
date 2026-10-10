import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
deps=load('runtime_dependencies_test','scripts/runtime-dependencies.py')
colab=load('colab_fast_test','run-colab.py')


class DependencyTests(unittest.TestCase):
    def test_registry_requirements_are_safe_and_system_baseline_is_preserved(self):
        wanted={'requests':'2.0','torch':'2.1','new-lib':'1.0','--danger':'1','private @ https://token':'1'}
        self.assertEqual(deps.missing_python(wanted,{'Requests':'2.0','torch':'2.2'},exact=False),['new-lib==1.0'])
        self.assertEqual(deps.missing_python({'a_b':'1.0'},{'a-b':'1.0'}),[])

    def test_capture_records_packages_not_keys_or_kernel_drivers(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            (home/'.env').write_text('API_KEY=must-not-copy')
            def command(args,timeout=60):
                if args[0]=='apt-mark':out='ffmpeg\nlinux-image-x\nnvidia-driver\nlibc6\n'
                elif args[0]=='npm':out=json.dumps({'dependencies':{'tool':{'version':'2.0'},'npm':{'version':'24'}}})
                else:out=json.dumps({'requests':'2.0'})
                return subprocess.CompletedProcess(args,0,out,'')
            with patch.object(deps,'command',command),patch.dict(os.environ,{'HERMES_PYTHON':__import__('sys').executable}):
                deps.capture(home)
            text=(home/'.runtime-dependencies.json').read_text();recipe=json.loads(text)
            self.assertEqual(recipe['apt'],['ffmpeg'])
            self.assertEqual(recipe['npm'],{'tool':'2.0'})
            self.assertNotIn('must-not-copy',text)
            self.assertEqual((home/'.runtime-dependencies.json').stat().st_mode&0o777,0o600)

    def test_corrupt_recipe_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'.runtime-dependencies.json';p.write_text('{broken')
            with self.assertRaises(ValueError):deps.capture(td)
            self.assertEqual(p.read_text(),'{broken')

    def test_restore_installs_only_missing_packages_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'.runtime-dependencies.json'
            p.write_text(json.dumps({'version':1,'apt':['ffmpeg','--bad'],'python':{'agent':{'requests':'2.0','extra':'1.0'}},'npm':{'tool':'2.0'}}))
            calls=[]
            def command(args,timeout=60):
                calls.append(args)
                if args[0]=='dpkg-query':return subprocess.CompletedProcess(args,1,'','')
                if args[0]=='npm' and 'list' in args:return subprocess.CompletedProcess(args,0,'{"dependencies":{}}','')
                if args[0]=='apt-get' and 'install' in args:return subprocess.CompletedProcess(args,1,'','')
                return subprocess.CompletedProcess(args,0,'','')
            with patch.object(deps,'command',command),patch.object(deps,'python_packages',return_value={'requests':'2.0'}):
                errors=deps.restore(td)
            self.assertTrue(errors)
            self.assertFalse(json.loads((Path(td)/'.runtime-dependencies-status.json').read_text())['ready'])
            self.assertTrue(any('extra==1.0' in c for c in calls))
            self.assertFalse(any('requests==2.0' in c or '--bad' in c for c in calls))


class StartupCacheTests(unittest.TestCase):
    def test_runtime_key_is_based_on_upstream_not_application_revision(self):
        self.assertEqual(colab.runtime_key('a'*40),colab.runtime_key('a'*40))
        self.assertNotEqual(colab.runtime_key('a'*40),colab.runtime_key('b'*40))

    def test_ui_cache_roundtrip_and_checksum_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);install=root/'install';cache=root/'cache';cache.mkdir()
            for name in ('hermes_cli/web_dist/index.html','ui-tui/dist/entry.js'):
                p=install/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('public build')
            (install/'.env').write_text('must-not-cache')
            with patch.object(colab,'INSTALL',install):
                colab.save_ui_cache(cache,'key')
                with tarfile.open(cache/'key.ui.tar.gz') as archive:
                    self.assertNotIn('.env',archive.getnames())
                self.assertTrue(colab.restore_ui_cache(cache,'key'))
                (cache/'key.ui.tar.gz').write_bytes(b'bad')
                self.assertFalse(colab.restore_ui_cache(cache,'key'))

    def test_existing_core_skips_apt_pip_and_npm_on_code_update(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source';install=root/'install';tools=root/'tools';data=root/'data';cache=root/'cache'
            source.mkdir();(source/'Dockerfile').write_text('ARG HERMES_REF='+'a'*40+'\n')
            for name in ('scripts','env','skills','dashboard-plugins'):(source/name).mkdir()
            for name in ('.venv/bin/python','hermes_cli/web_dist/index.html','ui-tui/dist/entry.js','docker/entrypoint.sh'):
                p=install/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('if [ "$(id -u)" = "0" ]; then\n')
            (install/'.git').mkdir();(install/'.colab-launcher-revision').write_text('old')
            (install/'.colab-runtime-key').write_text(colab.runtime_key('a'*40))
            calls=[]
            def run(args,**kw):
                calls.append([str(a) for a in args])
                out=colab.CODE_URL if kw.get('cwd')==source else 'https://github.com/NousResearch/hermes-agent.git'
                if 'rev-parse' in args:out='b'*40
                return subprocess.CompletedProcess(args,0,out,'')
            with patch.multiple(colab,SOURCE=source,INSTALL=install,TOOLS=tools,DATA=data),patch.object(colab,'cache_directory',return_value=cache),patch.object(colab,'run',run),patch.object(colab,'install_node'),patch.object(colab.pwd,'getpwnam',return_value=type('User',(),{'pw_uid':0})()),patch.object(colab.importlib.util,'find_spec',return_value=True),patch.object(colab.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'install ok installed','')):
                colab.install()
            self.assertFalse(any(c[0] in ('apt-get','npm') or 'pip' in c for c in calls))
            self.assertTrue(any('patch-lite.py' in ' '.join(c) for c in calls))
