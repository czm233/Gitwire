"""Real subprocess cancellation, output bounds and secret-safe Git errors."""
import asyncio
import os
import subprocess
import sys
import pytest
from app.processes import run_bounded, ProcessLimitError
from app.vault import git, VaultError


async def test_process_cancellation_kills_descendants(tmp_path):
    pidfile = tmp_path / 'child.pid'
    child = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    parent = ('import subprocess,sys,time;from pathlib import Path;'
              f'p=subprocess.Popen([sys.executable,"-c",{child!r}]);'
              f'Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(60)')
    task = asyncio.create_task(run_bounded(sys.executable, '-c', parent, cwd=tmp_path, env={}, timeout=30))
    async with asyncio.timeout(5):
        while not pidfile.exists():
            await asyncio.sleep(.02)
    pid = int(pidfile.read_text())
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = subprocess.run(['ps','-p',str(pid),'-o','stat='],text=True,capture_output=True).stdout.strip()
        assert not state or state.startswith('Z')  # A zombie is terminated, awaiting OS reaping.
    finally:
        # Test owns this exact child only; never leave it behind on assertion failures.
        try: os.kill(pid, 9)
        except ProcessLookupError: pass


async def test_process_timeout_and_output_limit(tmp_path):
    with pytest.raises(TimeoutError):
        await run_bounded(sys.executable,'-c','import time;time.sleep(60)',cwd=tmp_path,env={},timeout=.05)
    with pytest.raises(ProcessLimitError):
        await run_bounded(sys.executable,'-c','import sys;sys.stdout.write("x"*100000)',cwd=tmp_path,env={},output_limit=1000)


async def test_git_failure_does_not_disclose_arguments(tmp_path):
    with pytest.raises(VaultError) as error:
        await git(tmp_path,'-c','http.extraheader=Authorization: Bearer TEST_SECRET_MARKER','not-a-real-git-command')
    assert 'TEST_SECRET_MARKER' not in str(error.value)
    assert 'Authorization' not in str(error.value)
