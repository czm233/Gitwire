#!/usr/bin/env python3
"""Back up Gitwire's own PostgreSQL and verify by restoring a disposable database."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import uuid
from acceptance import ROOT, registered

COMMAND = ['docker', 'compose', '--env-file', '.env.infra', '-f', 'compose.local.yaml',
           'exec', '-T', 'postgres']


def run(*args, **kwargs):
    result = subprocess.run(COMMAND + list(args), cwd=ROOT, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        raise RuntimeError('PostgreSQL 备份或验证失败；原数据库未被恢复操作覆盖')
    return result


def backup():
    registered()
    directory = ROOT / 'data' / 'backups'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = directory / f'gitwire-{stamp}-{uuid.uuid4().hex[:8]}.dump'
    with path.open('xb') as output:
        path.chmod(0o600)
        run('pg_dump', '-U', 'gitwire', '-d', 'gitwire', '-Fc', stdout=output)
    return path


def verify(path):
    registered()
    database = 'gitwire_restore_test_' + uuid.uuid4().hex
    run('createdb', '-U', 'gitwire', database, stdout=subprocess.PIPE)
    try:
        with path.open('rb') as source:
            run('pg_restore', '-U', 'gitwire', '--dbname', database, '--exit-on-error', stdin=source, stdout=subprocess.PIPE)
        query = "SELECT json_build_object('accounts',(SELECT count(*) FROM h_account),'candidates',(SELECT count(*) FROM h_candidate),'jobs',(SELECT count(*) FROM h_job),'revision',(SELECT version_num FROM alembic_version));"
        result = run('psql', '-U', 'gitwire', '-d', database, '-Atc', query, stdout=subprocess.PIPE)
        counts = json.loads(result.stdout)
        report = path.with_suffix('.verified.json')
        report.write_text(json.dumps({'restored': True, 'checked_at': datetime.now(timezone.utc).isoformat(), **counts}, indent=2))
        report.chmod(0o600)
        return counts
    finally:
        run('dropdb', '-U', 'gitwire', database, stdout=subprocess.PIPE)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true', help='在独立临时数据库中执行恢复演练')
    args = parser.parse_args()
    path = backup()
    print('Backup saved:', path)
    if args.verify:
        print('Verified restore:', verify(path))
