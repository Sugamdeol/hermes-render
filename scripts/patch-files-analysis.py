"""Portable reconstruction of the Files Search/Analysis recorded in the private backup."""
import argparse
from pathlib import Path
import subprocess
import sys


def patch(root):
    root = Path(root)
    assets = Path(__file__).parent
    server = root / 'hermes_cli/web_server.py'
    page = root / 'web/src/pages/FilesPage.tsx'
    text, jsx = server.read_text(), page.read_text()
    anchor = 'app.include_router(_files_routes.router)'
    addition = '\nfrom hermes_cli.web_routers import fs_analysis as _fs_analysis_routes\napp.include_router(_fs_analysis_routes.router)'
    if addition not in text:
        if text.count(anchor) != 1:
            raise RuntimeError('Files router registration changed; refusing an unverified patch')
        text = text.replace(anchor, anchor + addition, 1)
    imp = 'import FilesStorageTools from "@/components/FilesStorageTools";\n'
    old = '<PluginSlot name="files:top" />'
    new = old + '\n      <FilesStorageTools path={activePath} onNavigate={setCurrentPath} />'
    if new not in jsx:
        if jsx.count(old) != 1 or 'const activePath =' not in jsx:
            raise RuntimeError('Files page integration changed; refusing an unverified patch')
        jsx = imp + jsx.replace(old, new, 1)
    router = (assets / 'files-analysis-router.py').read_text()
    compile(router, 'fs_analysis.py', 'exec')
    compile(text, str(server), 'exec')
    outputs = {server: text, page: jsx,
               root / 'hermes_cli/web_routers/fs_analysis.py': router,
               root / 'web/src/components/FilesStorageTools.tsx': (assets / 'FilesStorageTools.tsx').read_text()}
    changed = False
    for path, content in outputs.items():
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            changed = True
    return changed


BUILD = '''
from pathlib import Path
from hermes_cli.source_build import source_build_env, prepare_source_dependencies, build_source_web
from hermes_cli.runtime_state import _lock
from hermes_cli.main_web_build import _web_ui_build_needed
root = Path.cwd()
with (root / '.web_ui_build.lock').open('ab') as lock:
    _lock(lock.fileno(), wait=True)
    env = source_build_env(explicit=True)
    prepare_source_dependencies(root, ('ui-tui', 'web'), env=env, explicit=True)
    if _web_ui_build_needed(root / 'web'):
        build_source_web(root, env=env)
print('Files Search and Analysis assets ready')
'''

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    patch(args.root)
    if args.build:
        subprocess.run([str(args.root / '.venv/bin/python'), '-u', '-c', BUILD], cwd=args.root, check=True)
