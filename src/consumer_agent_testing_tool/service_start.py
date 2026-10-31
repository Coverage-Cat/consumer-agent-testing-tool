"""connector-run --start COMMAND: the tool starts the service under test, waits until it answers HTTP, runs
the trials, and stops it (SIGTERM to its process group, SIGKILL after STOP_SECONDS), on success, failure,
Ctrl-C and SIGTERM alike.

COMMAND is split with shlex (no shell); "{port}" in it, and in --ready-url, is a free loopback port, which the
scenario also sees as ${PORT} unless --set PORT=... is given; the service's PORT environment variable is the
scenario's PORT either way.
Its stdout and stderr go to a plain log, <output>/service-<id>.log, beside the sealed runs and outside them:
the service's own output is not redacted evidence, so it is never sealed or exported."""
import asyncio
import os
import shlex
import signal
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import LabError
from .evidence import REDACTED, Redactor, credential_key

STOP_SECONDS = 5.0
POLL_SECONDS = 0.2
TAIL_LINES, TAIL_CHARACTERS = 20, 2000


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def redacted_command(argv) -> list[str]:
    """The command as the run lock records it: environment secrets, credential query parameters, and the value
    of any credential-named option (--api-key X, --token=X) replaced."""
    redactor, shown, hide = Redactor.environment(), [], False
    for token in argv:
        name, equals, _ = token.partition("=")
        if hide:
            shown.append(REDACTED)
        elif token.startswith("-") and equals and credential_key(name.lstrip("-")):
            shown.append(f"{name}={REDACTED}")
        else:
            shown.append(redactor.text(token))
        hide = token.startswith("-") and not equals and credential_key(token.lstrip("-"))
    return shown


def log_tail(path) -> str:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()[-TAIL_LINES:]
    except OSError:
        return "(no log)"
    text = "\n".join(lines)[-TAIL_CHARACTERS:]
    return text if text.strip() else "(the log is empty)"


@dataclass
class Service:
    process: subprocess.Popen
    argv: list[str]
    port: int
    log: Path
    ready_url: str

    def record(self) -> dict:
        """What the run lock and the summary record: never the raw command."""
        return {"started_by": "consumer-agent-testing-tool --start", "command": redacted_command(self.argv),
                "port": self.port, "ready_url": Redactor.environment().text(self.ready_url), "log": self.log.name}

    def stop(self) -> int | None:
        """SIGTERM to the process group, SIGKILL after STOP_SECONDS (also any child that outlived the leader).
        Synchronous: it runs from finally blocks, also once the event loop is shutting down."""
        if self.process.poll() is None:
            _signal_group(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        _signal_group(self.process.pid, signal.SIGKILL)
        try:
            return self.process.wait(STOP_SECONDS)
        except subprocess.TimeoutExpired:
            return None

    async def astop(self):
        """Poll without an executor queue; cancellation cannot abandon the owned process group."""
        async def stop():
            if self.process.poll() is None:
                _signal_group(self.process.pid, signal.SIGTERM)
                deadline = asyncio.get_running_loop().time() + STOP_SECONDS
                while self.process.poll() is None and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
            _signal_group(self.process.pid, signal.SIGKILL)
            deadline = asyncio.get_running_loop().time() + STOP_SECONDS
            while self.process.poll() is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            return self.process.poll()

        cleanup = asyncio.create_task(stop())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A suite cancels work, then waits for this bounded cleanup before recording its result.
                continue
        return cleanup.result()


def _signal_group(pid, number):
    try:
        os.killpg(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def start_argv(command, port) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise LabError(f"--start: the command does not parse ({error})") from None
    if not argv:
        raise LabError("--start needs a command")
    return [token.replace("{port}", str(port)) for token in argv]


async def wait_ready(service, timeout) -> None:
    """Ready once any HTTP response arrives (a 4xx or 405 on GET of an MCP endpoint counts); the body is never
    read, so an event stream cannot hold the probe open."""
    import httpx
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(2.0, connect=1.0),
                                 follow_redirects=False) as client:
        while True:
            if (code := service.process.poll()) is not None:
                raise LabError(f"The --start service exited with code {code} before it was ready at "
                               f"{service.ready_url}; log {service.log}, last lines:\n{log_tail(service.log)}")
            try:
                async with client.stream("GET", service.ready_url):
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise LabError(f"The --start service did not answer {service.ready_url} within {timeout:g} s "
                               f"(--ready-timeout); it was stopped; log {service.log}, last lines:\n"
                               f"{log_tail(service.log)}")
            await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def started(command, *, port, output, ready_url, timeout, environment, announce=None,
                  handle_signals=True, async_cleanup=False):
    """The running service, stopped on every way out (an error, a cancellation, Ctrl-C, SIGTERM); announce(service)
    runs once it is started, before it is ready, so a summary of a failed start still names it."""
    argv = start_argv(command, port)
    output.mkdir(parents=True, exist_ok=True)
    log = output / f"service-{uuid.uuid4().hex}.log"
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        try:
            # Its own session and process group: the terminal's Ctrl-C reaches the tool, which stops the group.
            # Synchronous Popen: stop() must work from finally blocks once the event loop is shutting down.
            process = subprocess.Popen(  # noqa: ASYNC220
                argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, **environment})
        except OSError as error:
            raise LabError(f"--start could not run {argv[0]!r}: {error.strerror or error}") from None
    service = Service(process, argv, port, log, ready_url.replace("{port}", str(port)))
    loop, task, terminated = asyncio.get_running_loop(), asyncio.current_task(), []
    if announce is not None:
        announce(service)

    def terminate():
        # SIGTERM to the tool (a CI job cancelled): the service starts stopping now, and the run is cancelled
        # as Ctrl-C cancels it, so no further trial reaches it; the command then exits 3 (interrupted).
        terminated.append(True)
        _signal_group(process.pid, signal.SIGTERM)
        task.cancel()
    handled = False
    if handle_signals:
        try:
            loop.add_signal_handler(signal.SIGTERM, terminate)
            handled = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    try:
        try:
            await wait_ready(service, timeout)
            yield service
        except asyncio.CancelledError:
            if not terminated:
                raise
        if terminated:
            raise LabError(f"Stopped by SIGTERM; the --start service was stopped (log {service.log})", code=3,
                           category="interrupted")
    finally:
        try:
            if async_cleanup:
                await service.astop()
            else:
                service.stop()
        finally:
            if handled:
                loop.remove_signal_handler(signal.SIGTERM)
