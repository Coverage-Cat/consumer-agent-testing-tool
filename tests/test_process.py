import asyncio
import sys

import pytest

from consumer_agent_testing_tool.process import communicate_bounded


async def test_large_stdout_and_stderr_are_drained_with_explicit_loss():
    process = await asyncio.create_subprocess_exec(sys.executable, "-c",
        "import os; os.write(1,b'a'*2000000); os.write(2,b'b'*2000000)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    result = await communicate_bounded(process, limit=4096)
    assert result["exit_status"] == 0
    assert result["stdout"] == "a" * 4096
    assert result["stderr"] == "b" * 4096
    assert result["output_capture"]["stdout_bytes"] == 2000000
    assert result["output_capture"]["stdout_truncated"]


async def test_cancellation_terminates_the_subprocess():
    process = await asyncio.create_subprocess_exec(sys.executable, "-c",
        "import time; time.sleep(100)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    task = asyncio.create_task(communicate_bounded(process))
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
