"""Keep workspace aliases stable when restoring InstaBox data in a notebook."""
from pathlib import Path
import os


def aliases(home):
    home = Path(home).resolve()
    return {'/storage': home, '/mnt/hermes-storage': home,
            '/mnt/hermes-shared': home/'shared-storage',
            '/data/hermes/state': home, '/data/workspace': home/'workspace',
            '/data/home': home/'home'}


def apply(home, filesystem=Path('/')):
    if os.environ.get('HERMES_NOTEBOOK_HOST') not in ('colab', 'kaggle'): return False
    mapping = aliases(home)
    for name, target in mapping.items():
        path = filesystem/name.lstrip('/')
        if path.is_symlink():
            if path.resolve() != target.resolve(): raise RuntimeError('Conflicting workspace alias: '+name)
        elif path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise RuntimeError('Workspace alias contains existing data: '+name)
    for name, target in mapping.items():
        target.mkdir(parents=True, exist_ok=True)
        path=filesystem/name.lstrip('/');path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink(): continue
        if path.exists(): path.rmdir()
        path.symlink_to(target, target_is_directory=True)
    # Expose second-disk-only files under the pooled path too. Links stay inside
    # the backed-up home. Conflicting existing files retain their original copy.
    secondary=Path(home)/'shared-storage'
    if secondary.is_dir():
        for directory, dirs, files in os.walk(secondary, followlinks=False):
            source=Path(directory);relative=source.relative_to(secondary)
            destination=Path(home)/relative
            dirs[:]=[name for name in dirs if not (source/name).is_symlink()]
            if destination.is_symlink() or not destination.is_dir():
                dirs[:]=[];continue
            for name in dirs+files:
                target=destination/name
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(os.path.relpath(source/name,target.parent),target_is_directory=(source/name).is_dir())
    return True
