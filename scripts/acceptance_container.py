#!/usr/bin/env python3
"""Disposable full-image acceptance, isolated from real Gitwire data and OAuth."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import urllib.request
import yaml
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]
PROJECT='gitwire-acceptance-deploy'
PORT=10242
BASE=ROOT/'data'/'acceptance-container'
SPEC=BASE/'compose.json'
ENV=BASE/'runtime.env'


def run(*args, capture=False):
    return subprocess.run(args,cwd=ROOT,check=True,text=True,
        stdout=subprocess.PIPE if capture else None,stderr=subprocess.PIPE if capture else None)


def prepare():
    registry=yaml.safe_load(Path('/Users/czm/.config/local-deploy/ports.yaml').read_text())
    assert registry['projects']['gitwire']['ports']['container-acceptance']['port']==PORT
    owned=run('docker','ps','--filter',f'label=com.docker.compose.project={PROJECT}',
              '--filter',f'publish={PORT}','--format','{{.Names}}',capture=True).stdout.strip()
    listeners=subprocess.run(['lsof',f'-tiTCP:{PORT}','-sTCP:LISTEN'],text=True,capture_output=True).stdout.strip()
    if listeners and not owned:
        raise SystemExit(f'Port {PORT} belongs to another process; refusing to replace it')
    BASE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not ENV.exists():
        with ENV.open('x') as file:
            ENV.chmod(0o600)
            file.write('GITWIRE_DB_PASSWORD='+secrets.token_hex(32)+'\nENCRYPTION_KEY='+Fernet.generate_key().decode()+
                       f'\nPUBLIC_URL=http://127.0.0.1:{PORT}\n')
    config=yaml.safe_load((ROOT/'docker-compose.yml').read_text())
    config.pop('x-application',None);config['name']=PROJECT
    for service in config['services'].values():
        service.pop('build',None)
        if 'env_file' in service: service['env_file']=str(ENV)
    config['services']['api']['ports']=[f'127.0.0.1:{PORT}:8000']
    SPEC.write_text(json.dumps(config,indent=2));SPEC.chmod(0o600)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['up','status','down'])
    args=parser.parse_args()
    if args.action=='up': prepare()
    if not SPEC.exists() or not ENV.exists(): raise SystemExit('No acceptance stack has been created')
    command=['docker','compose','--env-file',str(ENV),'-p',PROJECT,'-f',str(SPEC)]
    if args.action=='up':
        run(*command,'up','-d','--wait','--wait-timeout','180')
    elif args.action=='down':
        # This command owns only the disposable acceptance project's resources.
        run(*command,'down','--volumes')
        return
    run(*command,'ps','--all')
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/health',timeout=5) as response:
        print('container_health',json.load(response))


if __name__=='__main__': main()
