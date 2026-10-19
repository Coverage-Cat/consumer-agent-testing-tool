"""Bound subprocess output while draining both pipes, including on cancellation."""
import asyncio


async def communicate_bounded(process, *, input=None, timeout=30, limit=1024 * 1024):
    async def read(stream):
        retained, total = bytearray(), 0
        while chunk := await stream.read(65536):
            total += len(chunk)
            retained.extend(chunk[:max(0, limit - len(retained))])
        return bytes(retained), total

    async def write():
        if process.stdin is not None:
            try:
                if input:
                    process.stdin.write(input)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

    tasks = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr)),
             asyncio.create_task(write())]
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
            stdout, stderr, _ = await asyncio.gather(*tasks)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return {
        "stdout": stdout[0].decode(errors="replace"),
        "stderr": stderr[0].decode(errors="replace"),
        "exit_status": process.returncode,
        "output_capture": {
            "limit_bytes_per_stream": limit,
            "stdout_bytes": stdout[1], "stderr_bytes": stderr[1],
            "stdout_truncated": stdout[1] > limit, "stderr_truncated": stderr[1] > limit,
        },
    }
