#!/usr/bin/env python3
"""Gitwire local acceptance lifecycle; only manage sessions and ports owned by this repo."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time
import urllib.request
import fcntl

ROOT = Path(__file__).resolve().parents[1]
PORTS = {'backend': 10240, 'frontend': 10241}
SERVICES = {'backend': 'uv run gitwire serve',
            'frontend': 'npm --prefix frontend run dev -- --host 127.0.0.1 --strictPort',
            'worker': 'uv run gitwire worker', 'scheduler': 'uv run gitwire scheduler'}
DRAIN_ENGINE = None


def run(*args, check=True, capture=False):
    return subprocess.run(args, cwd=ROOT, check=check, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)


def registered():
    import yaml
    registry = yaml.safe_load(Path('/Users/czm/.config/local-deploy/ports.yaml').read_text())
    block = registry['projects']['gitwire']['assigned_block']
    if str(block) != '10240-10249':
        raise SystemExit('Gitwire registered port block has changed; update acceptance settings first')


def owners(port):
    ids = run('lsof', '-tiTCP:' + str(port), '-sTCP:LISTEN', check=False, capture=True).stdout.split()
    for pid in set(ids):
        cwd = run('lsof', '-a', '-p', pid, '-d', 'cwd', '-Fn', check=False, capture=True).stdout
        if not any(line == 'n' + str(ROOT) or line == 'n' + str(ROOT / 'frontend') for line in cwd.splitlines()):
            raise SystemExit(f'Port {port} belongs to a different workspace (PID {pid}); refusing to stop or reuse it')
    return ids


def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:10240/api/health', timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def session_exists(name):
    return run('tmux', 'has-session', '-t', 'gitwire-' + name, check=False, capture=True).returncode == 0


def session_owned(name):
    value = run('tmux', 'display-message', '-p', '-t', 'gitwire-' + name, '#{pane_current_path}', capture=True).stdout.strip()
    if Path(value).resolve() not in {ROOT, ROOT / 'frontend'}:
        raise SystemExit(f'gitwire-{name} is from another workspace; refusing to manage it')


def drain_hosted():
    global DRAIN_ENGINE
    from app.config import Settings
    from app.hosted.store import engine_for
    from app.hosted.maintenance import drain
    DRAIN_ENGINE = engine_for(Settings())
    if session_exists('scheduler'):
        session_owned('scheduler')
        run('tmux', 'send-keys', '-t', 'gitwire-scheduler', 'C-c')
    print('Draining queue; running tasks will finish before service restart.', flush=True)
    deadline = time.monotonic() + 2100
    previous = None
    while True:
        running = drain(DRAIN_ENGINE)
        if running != previous:
            print('Active tasks remaining:', running, flush=True)
            previous = running
        if not running:
            return
        if time.monotonic() >= deadline:
            raise SystemExit('Tasks did not finish within the maintenance window; services were not forcibly stopped')
        time.sleep(1)


def status():
    for name, port in PORTS.items():
        print(name, port, 'listening' if owners(port) else 'stopped')
    for name in SERVICES:
        if session_exists(name):
            session_owned(name)
            print('session', 'gitwire-' + name, 'running')
    print('health', health())
    run('docker', 'compose', '--env-file', '.env.infra', '-f', 'compose.local.yaml', 'ps')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['up', 'status', 'down', 'restart'])
    args = parser.parse_args()
    registered()
    for port in PORTS.values():
        owners(port)
    if args.action == 'status':
        return status()
    if args.action in {'down', 'restart'}:
        state = health()
        if state and state.get('mode') == 'hosted':
            drain_hosted()
        if state and state.get('mode') != 'hosted':
            with urllib.request.urlopen('http://127.0.0.1:10240/api/runs?status=running', timeout=5) as response:
                if json.load(response).get('total', 0):
                    raise SystemExit('Legacy analysis is running; wait before restarting')
        for name in reversed(SERVICES):
            if session_exists(name):
                session_owned(name)
                run('tmux', 'send-keys', '-t', 'gitwire-' + name, 'C-c')
        for _ in range(20):
            if not any(session_exists(n) for n in SERVICES):
                break
            time.sleep(.25)
        if any(session_exists(n) for n in SERVICES):
            raise SystemExit('Some processes are still shutting down; inspect status before retrying')
        if args.action == 'down':
            return status()
    run('docker', 'compose', '--env-file', '.env.infra', '-f', 'compose.local.yaml', 'up', '-d', '--wait')
    run('uv', 'run', 'alembic', 'upgrade', 'head')
    if DRAIN_ENGINE is None:
        # Recover an interrupted earlier lifecycle command before starting work.
        from app.config import Settings
        from app.hosted.store import engine_for
        from app.hosted.maintenance import resume
        engine = engine_for(Settings())
        resume(engine)
        engine.dispose()
    for name, command in SERVICES.items():
        if session_exists(name):
            session_owned(name)
            continue
        if name in PORTS and owners(PORTS[name]):
            raise SystemExit(f'{name} already running outside managed session; inspect before starting')
        run('tmux', 'new-session', '-d', '-s', 'gitwire-' + name, '-c', str(ROOT), command)
    for _ in range(40):
        if health():
            break
        time.sleep(.25)
    if not health():
        raise SystemExit('Backend did not become healthy; inspect gitwire-backend tmux session')
    status()

if __name__ == '__main__':
    (ROOT / 'data').mkdir(exist_ok=True)
    with (ROOT / 'data' / 'acceptance.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another Gitwire acceptance lifecycle command is running')
        try:
            main()
        finally:
            if DRAIN_ENGINE:
                from app.hosted.maintenance import resume
                resume(DRAIN_ENGINE)
                DRAIN_ENGINE.dispose()
                if session_exists('worker') and health() and not session_exists('scheduler'):
                    session_owned('worker')
                    run('tmux', 'new-session', '-d', '-s', 'gitwire-scheduler', '-c', str(ROOT), SERVICES['scheduler'])
