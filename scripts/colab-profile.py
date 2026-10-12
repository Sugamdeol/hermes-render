"""One-time migration of Render resource defaults for native Colab."""
import json
import os
from pathlib import Path
import sys
import yaml

# Only exact adapter defaults are reset. Models, providers and user changes survive.
LIMITS = {
 'agent': {'max_turns':30,'api_max_retries':1,'gateway_timeout':900},
 'delegation': {'max_iterations':20,'max_concurrent_children':1,'max_spawn_depth':1,'orchestrator_enabled':False},
 'auxiliary.session_search': {'max_concurrency':1},
 'compression': {'threshold':0.40,'target_ratio':0.15,'protect_last_n':12,'hygiene_hard_message_limit':250},
 'goals': {'max_turns':8}, 'code_execution': {'timeout':180,'max_tool_calls':30},
 'browser': {'inactivity_timeout':60,'command_timeout':20},
 'tool_output': {'max_bytes':30000,'max_lines':1000,'max_line_length':1500},
 '': {'file_read_max_chars':50000},
}


def migrate(config):
    for section, fields in LIMITS.items():
        target=config
        for part in section.split('.') if section else []:
            target=target.get(part,{}) if isinstance(target,dict) else {}
        if isinstance(target,dict):
            for key,value in fields.items():
                if target.get(key)==value:
                    target.pop(key,None)  # native runtime supplies current upstream defaults
    return config


def apply(home, install):
    home=Path(home);install=Path(install)
    marker=home/'.colab-full-profile-v1'
    path=home/'config.yaml'
    if not marker.exists():
        config=yaml.safe_load(path.read_text()) if path.exists() else {}
        if not isinstance(config,dict):raise ValueError('Invalid saved config')
        migrate(config)
        skills=config.setdefault('skills',{})
        if isinstance(skills,dict):
            dirs=skills.setdefault('external_dirs',[])
            for entry in ('/opt/render-tools/skills-local',str(install/'skills')):
                if isinstance(dirs,list) and entry not in dirs:dirs.append(entry)
        temporary=path.with_suffix('.colab.tmp')
        temporary.write_text(yaml.safe_dump(config,sort_keys=False))
        temporary.replace(path)
        marker.touch()
    # Use Hermes' own per-install update channel, not an external pin-update service.
    from hermes_cli.update_channel import install_id, _write_channel_record
    _write_channel_record(install_id(install), str(install), 'main', config_path=path)
    # Keep the user-selected password in encrypted private state as well as this process.
    env_path=home/'.env'
    import importlib.util
    spec=importlib.util.spec_from_file_location('colab_seed',Path(__file__).with_name('seed-env.py'))
    seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
    password=os.environ.get('HERMES_GATEWAY_TOKEN')
    if password:
        text,_=seed.merge(seed.read_text(env_path),[('HERMES_GATEWAY_TOKEN',password)],force=True)
        if not seed.write_env(env_path,text):raise RuntimeError('Could not save dashboard password')
        runtime=home/'.render-runtime-env.json'
        if runtime.exists():
            data=json.loads(runtime.read_text())
            data['variables']['HERMES_GATEWAY_TOKEN']=password
            temporary=runtime.with_suffix('.tmp');temporary.write_text(json.dumps(data));temporary.chmod(0o600);temporary.replace(runtime)


if __name__=='__main__':apply(*sys.argv[1:3])
