"""Workspace-local browser sidecar and Inspect driver for surface workflows."""
import asyncio
import json
import os
from importlib.resources import files
from pathlib import Path

from .errors import LabError
from .surface import Paused, ViewReply, inside

HOST_LINE_LIMIT = 32 * 1024 * 1024
# Host failures the service under test or its app caused; everything else is integration (exit 2).
OUTCOME_CATEGORIES = {"tool/API", "interaction"}


def host_error(message):
    category = message.get("category", "integration-failure")
    if category == "capability-unavailable":
        return Paused(message["error"], category="unsupported")
    return LabError(message["error"], code=1 if category in OUTCOME_CATEGORIES else 2, category=category)


class BrowserHost:
    def __init__(self, root, tooling="tooling/payment-ui"):
        self.root = Path(root).resolve()
        runtime = os.environ.get("CONSUMER_AGENT_TESTING_TOOL_RUNTIME")
        self.runtime = inside(self.root, runtime) if runtime else None
        if self.runtime and tooling == "tooling/payment-ui":
            tooling = str(self.runtime / tooling)
        self.tooling = inside(self.root, tooling)
        for module in ("playwright", "esbuild", "@modelcontextprotocol/client",
                       "@modelcontextprotocol/ext-apps"):
            if not (self.tooling / "node_modules" / module).is_dir():
                raise LabError(f"Missing workspace host dependency {module}; install the optional Node tooling")
        self.pending, self.serial, self.process, self.reader = {}, 0, None, None
        self.callback = None
        self.credential = None  # A linking.Credential for the MCP endpoint, set by the coordinator.
        self.tasks = set()

    async def start(self):
        from .worker_container import worker_docker_connections
        script = files("consumer_agent_testing_tool").joinpath("data/surface_host/host.mjs")
        temp = self.root / ".cache/surface-host"
        temp.mkdir(parents=True, exist_ok=True)
        # Inference credentials remain in Python. The browser host gets only explicit inputs.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SYSTEMROOT", "WINDIR"}}
        # The child runs in self.root. A relative path keeps Chromium's Unix socket
        # address short even when the checkout path is long; files stay in this workspace.
        env.update(TMPDIR=str(temp.relative_to(self.root)), PLAYWRIGHT_BROWSERS_PATH=str(
            self.runtime / "browsers" if self.runtime else self.root / ".cache/ms-playwright"),
            HOME=str(temp / "home"),
            XDG_CACHE_HOME=str(temp / "cache"), XDG_CONFIG_HOME=str(temp / "config"))
        if connections := worker_docker_connections.get():
            env["CONSUMER_AGENT_TESTING_TOOL_WORKER_DOCKER"] = json.dumps(connections)
        if self.runtime:
            entrypoints = json.loads((self.runtime / "runtime-lock.json").read_text())["entrypoints"]
            if executable := entrypoints.get("chromium"):
                env["CONSUMER_AGENT_TESTING_TOOL_BROWSER_EXECUTABLE"] = str(inside(self.runtime, executable))
        for name in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
            Path(env[name]).mkdir(exist_ok=True)
        # One JSON line carries a whole page snapshot or tool result; asyncio's 64 KiB
        # default line limit would kill the session on ordinary large pages.
        self.process = await asyncio.create_subprocess_exec(
            str(self.runtime / "node") if self.runtime and (self.runtime / "node").is_file() else "node",
            str(script), str(self.tooling), str(self.root),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=self.root, env=env, limit=HOST_LINE_LIMIT)
        self.reader = asyncio.create_task(self.read())

    async def connect(self, scenario):
        from .browser_execution import validate_worker
        validate_worker(scenario.browser_worker, self.root)
        await self.start()
        arguments = scenario.model_dump()
        if self.credential is not None:
            # The applied endpoint and headers cross only this private channel, never evidence.
            url, headers = self.credential.apply(scenario.endpoint, {})
            arguments["transport"] = {"url": url, "headers": headers}
        return await self.request("connect", arguments)

    async def read(self):
        failure = None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if message.get("event") == "observation":
                    self.observe(message["value"])
                elif message.get("event") == "callback":
                    task = asyncio.create_task(self.handle_callback(message))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                else:
                    future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if message.get("error"):
                            future.set_exception(host_error(message))
                        else:
                            future.set_result(message.get("result"))
        except Exception as error:  # noqa: BLE001 — a broken channel fails pending requests with its cause
            failure = error
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        LabError(str(failure), code=failure.code, category=failure.category)
                        if isinstance(failure, LabError) else LabError(
                            "Host transport closed unexpectedly" + (
                                f": {type(failure).__name__}" if failure else ""),
                            category="integration-failure"))

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def handle_callback(self, message):
        try:
            reply = {"result": await self.callback(message["name"], message["arguments"])}
            if callable(completes := getattr(self, "completes", None)):
                # A polling read is answered but does not complete a step that submits a write.
                reply["completion"] = completes(message["name"])
        except ViewReply as outcome:
            # A rejected tool result reaches the view unchanged (isError included); `abort`
            # ends the UI step now rather than after its callback timeout.
            reply = {"abort": outcome.stop}
            if outcome.result is not None:
                reply.update(result=outcome.result, rejected=True)
            else:
                reply["error"] = str(outcome)
        except Paused as error:
            reply = {"error": str(error), "abort": True}
        except Exception as error:  # noqa: BLE001 — return callback failures over the private channel
            reply = {"error": str(error)}
        try:
            await self.send({"callback_id": message["id"], **reply})
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass  # The host already exited; its pending requests fail with the transport error.

    async def request(self, method, arguments):
        if self.reader and self.reader.done():
            # Nothing can deliver a reply; fail now instead of waiting out the timeout.
            raise LabError("Host transport closed unexpectedly", category="integration-failure")
        self.serial += 1
        identifier = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:  # A failed send must not leave a future that nobody will ever read.
            await self.send({"id": identifier, "method": method, "arguments": arguments})
            return await asyncio.wait_for(future, 130)
        finally:
            self.pending.pop(identifier, None)

    async def call(self, name, arguments):
        return await self.request("call", {"name": name, "arguments": arguments})

    async def render(self, ui, source, path):
        return await self.request("render", {"ui": ui, "source": source, "run": str(path)})

    async def close(self):
        failure = None
        try:
            # A dead reader can never deliver the reply: terminate without waiting for one.
            if self.process and self.process.returncode is None and self.reader and not self.reader.done():
                try:
                    await asyncio.wait_for(self.request("close", {}), 5)
                except TimeoutError:
                    failure = LabError("Host did not acknowledge close within 5 s; it was terminated",
                                       category="integration-failure")
                except LabError as error:
                    failure = error
                except (OSError, RuntimeError) as error:  # The channel broke before the host could close.
                    failure = LabError(f"Host channel failed during close ({type(error).__name__}); "
                                       "it was terminated", category="integration-failure")
        finally:
            # Whatever happened above, including cancellation, the host and its browser stop here.
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            if self.reader and not self.reader.done():
                try:  # Its stdout is closed now; a reader still blocked after that is abandoned.
                    await asyncio.wait_for(self.reader, 5)
                except TimeoutError:
                    pass
            for task in list(self.tasks):
                task.cancel()
        if failure:
            raise failure



async def run_from_cli(args):
    from .surface import SurfaceRun, read_surface
    root = Path.cwd().resolve()
    scenario = read_surface(inside(root, args.scenario))
    from .release_scope import require_supported
    development = getattr(args, "development_extensions", False)
    require_supported(experimental=development, worker=scenario.browser_worker)
    secrets = json.loads(inside(root, args.secrets_file).read_text()) if args.secrets_file else {}
    replies = json.loads(inside(root, args.replies).read_text()) if args.replies else {}
    planner, model_lock = None, None
    if args.driver == "agent":
        from .config import read_config
        from .surface_agent import InspectSurfaceDriver
        config = read_config(inside(root, args.config))
        profile = args.model_profile or config.defaults.model_profile
        if not profile or profile not in config.model_profiles:
            raise LabError("Agent runs require a configured model profile")
        require_supported(experimental=development, config=config, model=config.model_profiles[profile])
        planner = InspectSurfaceDriver(model_profile=config.model_profiles[profile])
        model_lock = config.model_profiles[profile].model_dump(mode="json")
    def acquire(path):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise LabError("Run is active, or crashed with a lock. Verify it stopped before removing .active") from None
        os.close(descriptor)

    # Acquire before reading a resumed checkpoint, so no stale snapshot can win a race.
    lock = inside(root, args.resume) / ".active" if args.resume else None
    if lock:
        acquire(lock)
    try:
        run = SurfaceRun(root, scenario, BrowserHost(root, args.tooling), secrets=secrets,
                         replies=replies, resume=args.resume, planner=planner, model_lock=model_lock)
        if getattr(args, "expected_lock", None) is not None:
            run.require_run_lock(args.expected_lock)
        if lock is None:
            lock = run.path / ".active"
            acquire(lock)
        result = await run.run()
        print(run.summary(), flush=True)
        print(f"Surface report: {run.path / 'report.html'}", flush=True)
        return result
    finally:
        if lock:
            lock.unlink(missing_ok=True)
