"""Rebuild/check the standalone Docker context from current runtime sources."""
import argparse
import base64
import gzip
import io
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parents[1]
MARKER='\n__EMBEDDED_CONTEXT_BEGIN__\n'
FILES=('Dockerfile','requirements-render.lock','.sops.yaml')
TREES=('scripts','env','skills','dashboard-plugins')


def sources(root):
    names=set(FILES)
    for tree in TREES:
        for path in (root/tree).rglob('*'):
            if path.is_file() and not path.is_symlink():
                relative=path.relative_to(root)
                if '__pycache__' not in relative.parts and path.suffix not in ('.pyc','.tmp'):
                    names.add(relative.as_posix())
    return sorted(names)


def render(root=ROOT):
    archive=io.BytesIO()
    with gzip.GzipFile(fileobj=archive,mode='wb',filename='',mtime=0,compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as bundle:
            for name in sources(root):
                path=root/name; data=path.read_bytes()
                info=tarfile.TarInfo(name);info.size=len(data)
                info.mode=0o755 if path.stat().st_mode & 0o111 else 0o644
                bundle.addfile(info,io.BytesIO(data))
    encoded=base64.b64encode(archive.getvalue()).decode()
    header=(root/'run-local.sh').read_text().split(MARKER,1)[0]
    return header+MARKER+'\n'.join(encoded[i:i+76] for i in range(0,len(encoded),76))+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true')
    args=parser.parse_args();target=ROOT/'run-local.sh';content=render()
    if args.check:
        if target.read_text()!=content:raise SystemExit('Standalone context is stale; run python scripts/rebuild-standalone.py')
        print('Standalone context matches current runtime sources')
    else:
        target.write_text(content);print('Updated standalone context')


if __name__=='__main__':main()
