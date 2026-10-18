"""Record ordinary-file coverage and explicit exceptions without following external links."""
import json
import os
from pathlib import Path
import stat

NAME='.backup-coverage.json'


def capture(home):
    home=Path(home).resolve()
    report={'version':1,'regular_files':0,'regular_bytes':0,'external_links':[],
            'special_files':[],'excluded':['.restore-quarantine']}
    def fail(error): raise error
    for directory, dirs, names in os.walk(home,followlinks=False,onerror=fail):
        root=Path(directory)
        if root==home: dirs[:]=[d for d in dirs if d!='.restore-quarantine']
        for name in dirs+names:
            path=root/name;relative=path.relative_to(home).as_posix()
            if relative==NAME:continue
            info=path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not path.resolve().is_relative_to(home):report['external_links'].append(relative)
            elif stat.S_ISREG(info.st_mode):
                report['regular_files']+=1;report['regular_bytes']+=info.st_size
            elif not stat.S_ISDIR(info.st_mode):report['special_files'].append(relative)
    for key in ['external_links','special_files']:report[key].sort()
    value=json.dumps(report,sort_keys=True,indent=2)+'\n';target=home/NAME
    if target.exists() and target.read_text()==value:return False
    temp=target.with_suffix('.tmp');temp.write_text(value);temp.chmod(0o600);temp.replace(target)
    return True
