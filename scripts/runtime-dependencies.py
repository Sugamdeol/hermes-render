"""Save package recipes, not installed binaries or credentials, in private state."""
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import sys
import tempfile
import time
import urllib.request

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]{0,150}$')
VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+!-]{0,100}$')
NPM_NAME = re.compile(r'^(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}$')
SCAN = '''import importlib.metadata as m,json
out={}
for d in m.distributions():
 try:
  direct=json.loads(d.read_text('direct_url.json') or '{}')
  if direct.get('dir_info',{}).get('editable'):continue
  out[d.metadata['Name']]=d.version
 except (ValueError,TypeError,KeyError):pass
print(json.dumps(out))
'''


def command(args, timeout=60):
    return subprocess.run(args,capture_output=True,text=True,timeout=timeout)


def python_packages(python):
    result=command([python,'-c',SCAN])
    if result.returncode:raise RuntimeError('Package scan failed')
    return {n:v for n,v in json.loads(result.stdout).items() if isinstance(n,str) and
            isinstance(v,str) and NAME.fullmatch(n) and VERSION.fullmatch(v)}


def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.dependency-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o600);os.replace(temp,path)
    finally:Path(temp).unlink(missing_ok=True)


def read(path):
    if not path.exists():return {}
    if path.stat().st_size > 2*1024*1024:raise RuntimeError('Dependency recipe exceeds size limit')
    data=json.loads(path.read_text())
    if not isinstance(data,dict):raise RuntimeError('Invalid dependency recipe')
    return data


def _capture(home):
    home=Path(home);path=home/'.runtime-dependencies.json'
    previous=read(path)
    result={'version':1,'python':dict(previous.get('python',{})),'apt':previous.get('apt',[]),'npm':previous.get('npm',{}),
            'browsers':previous.get('browsers',[])}
    targets={'agent':os.environ.get('HERMES_PYTHON','/opt/hermes/.venv/bin/python'),
             'system':os.environ.get('HERMES_COLAB_SYSTEM_PYTHON',sys.executable)}
    for kind,python in targets.items():
        if Path(python).exists():
            result['python'][kind]={**previous.get('python',{}).get(kind,{}),**python_packages(python)}
    manual=command(['apt-mark','showmanual'])
    if manual.returncode==0:
        # Colab supplies kernels, CUDA, GPU drivers and base libraries itself.
        packages=[n for n in manual.stdout.splitlines() if NAME.fullmatch(n) and
                  not n.startswith(('linux-','cuda-','nvidia-'))]
        result['apt']=sorted(set(result['apt'])|set(packages))
    try:
        npm=command(['npm','list','--global','--depth=0','--json'])
        for name,row in json.loads(npm.stdout or '{}').get('dependencies',{}).items():
            version=row.get('version')
            if name not in ('npm','corepack') and NPM_NAME.fullmatch(name) and isinstance(version,str) and VERSION.fullmatch(version):
                result['npm'][name]=version
    except (OSError,ValueError):pass
    for base in (home/'home/.cache/ms-playwright',Path.home()/'.cache/ms-playwright'):
        if base.is_dir():
            for browser in ('chromium','firefox','webkit'):
                if any(base.glob(browser+'-*')):result['browsers'].append(browser)
    result['browsers']=sorted(set(result['browsers']))
    if result!=previous:atomic(path,result)
    return result


def capture(home):
    import fcntl
    with open('/tmp/hermes-dependency-inventory.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _capture(home)


def missing_python(desired,current,exact=True):
    present={re.sub(r'[-_.]+','-',n).lower():v for n,v in current.items()}
    return [n+'=='+v for n,v in desired.items() if isinstance(n,str) and isinstance(v,str)
            and NAME.fullmatch(n) and VERSION.fullmatch(v) and
            (re.sub(r'[-_.]+','-',n).lower() not in present or
             (exact and present[re.sub(r'[-_.]+','-',n).lower()]!=v))]


def restore(home):
    home=Path(home);recipe=read(home/'.runtime-dependencies.json')
    if os.environ.get('HERMES_RUNTIME_DEPS_AUTORESTORE') == '1':
        # Chrome is a Colab startup requirement, even for older saved recipes.
        recipe = dict(recipe or {'version':1})
        recipe['apt'] = sorted(set(recipe.get('apt',[])) | {'google-chrome-stable'})
    if not recipe:return []
    if recipe.get('version')!=1:raise RuntimeError('Unsupported dependency recipe version')
    errors=[]
    def install(args):
        try:
            result=command(args,timeout=900)
            if result.returncode:errors.append('Installer failed: '+args[0])
        except (OSError,subprocess.TimeoutExpired):errors.append('Installer unavailable or timed out: '+args[0])
    missing=[]
    for n in recipe.get('apt',[]):
        if not isinstance(n,str) or not NAME.fullmatch(n) or n.startswith(('linux-','cuda-','nvidia-')):continue
        result=command(['dpkg-query','-W','-f=${Status}',n])
        if result.returncode or result.stdout.strip()!='install ok installed':missing.append(n)
    if 'google-chrome-stable' in missing:
        # Chrome's downloaded .deb is not necessarily registered in apt sources.
        try:
            with tempfile.TemporaryDirectory() as temporary:
                deb = Path(temporary) / 'google-chrome-stable.deb'
                urllib.request.urlretrieve('https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb', deb)
                install(['apt-get','update','-qq'])
                install(['apt-get','install','-y','--no-install-recommends',str(deb)])
            missing.remove('google-chrome-stable')
        except OSError:
            errors.append('Chrome download failed')
            missing.remove('google-chrome-stable')
    if missing:
        install(['apt-get','update','-qq'])
        install(['apt-get','install','-y','--no-install-recommends',*missing])
    targets={'agent':os.environ.get('HERMES_PYTHON','/opt/hermes/.venv/bin/python'),
             'system':os.environ.get('HERMES_COLAB_SYSTEM_PYTHON',sys.executable)}
    for kind,desired in recipe.get('python',{}).items():
        if kind not in targets or not isinstance(desired,dict):continue
        python=targets[kind]
        try:requirements=missing_python(desired,python_packages(python),exact=kind=='agent' and os.environ.get('HERMES_RUNTIME_PROFILE') != 'colab-full')
        except (OSError,RuntimeError):errors.append('Python environment unavailable: '+kind);continue
        if requirements:
            # Colab's Python/GPU baseline is left alone; restore its missing extras.
            env_python=os.environ.get('HERMES_COLAB_SYSTEM_PYTHON',sys.executable)
            uv = shutil.which('uv')
            if uv:
                install([uv,'pip','install','--python',python,*requirements])
            else:
                probe = command([env_python,'-m','uv','--version'])
                if probe.returncode == 0:
                    install([env_python,'-m','uv','pip','install','--python',python,*requirements])
                else:
                    install([python,'-m','pip','install',*requirements])
    try:
        current=json.loads(command(['npm','list','-g','--depth=0','--json']).stdout or '{}').get('dependencies',{})
        packages=[n+'@'+v for n,v in recipe.get('npm',{}).items() if isinstance(n,str) and isinstance(v,str)
                  and NPM_NAME.fullmatch(n) and VERSION.fullmatch(v) and current.get(n,{}).get('version')!=v]
        if packages:install(['npm','install','-g','--',*packages])
    except (OSError,ValueError):errors.append('Global npm inventory unavailable')
    for browser in recipe.get('browsers',[]):
        if browser in ('chromium','firefox','webkit'):
            install([targets['agent'],'-m','playwright','install','--with-deps',browser])
    atomic(home/'.runtime-dependencies-status.json',{'ready':not errors,'errors':errors,'updated_at':int(time.time())})
    return errors


if __name__=='__main__':
    action,home=sys.argv[1:3]
    if action=='capture':capture(home)
    elif action=='restore':
        errors=restore(home)
        for error in errors:print('[dependencies]',error,flush=True)
        raise SystemExit(bool(errors))
