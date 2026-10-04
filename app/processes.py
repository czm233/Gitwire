"""Bounded subprocesses: cancellation owns the complete process group."""
import asyncio
import contextlib
import os
import signal


class ProcessLimitError(RuntimeError):
    def __init__(self, message):
        self.safe_message = message
        super().__init__(message)


async def stop_process_group(proc):
    # The group can outlive its leader (e.g. git-remote-https owns pipe handles).
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except asyncio.TimeoutError:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def run_bounded(executable, *args, cwd, env, timeout=120, output_limit=8 * 1024 * 1024):
    proc = await asyncio.create_subprocess_exec(executable, *args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    used = 0
    async def read(stream):
        nonlocal used
        chunks = []
        while chunk := await stream.read(65536):
            used += len(chunk)
            if used > output_limit:
                raise ProcessLimitError('Git 输出超过采集上限')
            chunks.append(chunk)
        return b''.join(chunks)
    readers = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    try:
        async with asyncio.timeout(timeout):
            out, err = await asyncio.gather(*readers)
            await proc.wait()
        return proc.returncode, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
    except BaseException:
        for reader in readers:
            reader.cancel()
        await stop_process_group(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        raise
