#!/usr/bin/env python3
"""Generate deployment-only secrets once; never display or overwrite them."""
import argparse
from pathlib import Path
import secrets
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]


def initialize(path):
    template=(ROOT/'.env.production.example').read_text()
    replacements={'GITWIRE_DB_PASSWORD':secrets.token_hex(32),
                  'ENCRYPTION_KEY':Fernet.generate_key().decode(),
                  'MAIL_FEEDBACK_SECRET':secrets.token_hex(32)}
    lines=[key+'='+replacements[key] if (key:=line.partition('=')[0]) in replacements else line
           for line in template.splitlines()]
    with path.open('x') as file:
        path.chmod(0o600)
        file.write('\n'.join(lines)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'.env.production')
    args=parser.parse_args()
    try: initialize(args.output)
    except FileExistsError: raise SystemExit('Configuration already exists; no credentials changed')
    print('Created private deployment configuration. Fill domain, GitHub App, model and mail settings before deployment.')
