"""Host-only, network-denied upstream API validator processes."""
import asyncio
import json
import os
import platform
import shutil
import signal
import sys
from importlib.resources import files
from pathlib import Path

from .errors import LabError, Unsupported
from .offline import kernel_denies_default_egress, policy_for
from .protocol import child_environment
from .serialization import file_digest, loads
from .surface import inside

NODE_PINS = {"@stoplight/http-spec": "7.1.0", "@stoplight/prism-http": "5.16.0",
             "@stoplight/spectral-core": "1.24.0", "@stoplight/spectral-rulesets": "1.23.0",
             "postman-collection": "0.0.0-catt.1"}
MAX_BYTES = 4 * 1024 * 1024


class ValidatorWorker:
    """Sequential private JSON RPC; timeout/corruption closes the whole worker."""
    def __init__(self, kind, evidence, timeout):
        if kind not in ("node", "python", "fastmcp"):
            raise ValueError("Unknown API worker kind")
        self.kind, self.evidence, self.timeout = kind, evidence, timeout
        self.process, self.sequence = None, 0

    async def start(self):
        if platform.system() != "Darwin":
            raise Unsupported("Network-denied API validator workers currently require macOS Seatbelt")
        if kernel_denies_default_egress():
            raise Unsupported("API validator workers must launch before the host's network sandbox; "
                              "macOS does not support nested Seatbelt profiles")
        root = Path.cwd()
        if self.kind == "node":
            tooling = inside(root, "tooling/api")
            for name, expected in NODE_PINS.items():
                path = inside(root, tooling / "node_modules" / name / "package.json")
                if not path.is_file() or loads(path.read_text()).get("version") != expected:
                    raise Unsupported("Install the pinned optional tooling/api dependencies")
            node = shutil.which("node")
            if not node:
                raise Unsupported("Node is required for optional API validators")
            command = [node, str(files("consumer_agent_testing_tool").joinpath("data/api_tools.cjs")), str(tooling)]
            self.evidence.manifest["run_lock"]["api_node_lock_sha256"] = file_digest(tooling / "package-lock.json")
        elif self.kind == "python":
            command = [sys.executable, "-m", "consumer_agent_testing_tool.openapi_worker"]
        else:
            command = [sys.executable, "-m", "consumer_agent_testing_tool.generated_worker"]
        temp = inside(root, self.evidence.path / "private" / ("api-" + self.kind))
        temp.mkdir(parents=True, mode=0o700, exist_ok=True)
        env = child_environment({"HOME": str(temp), "TMPDIR": str(temp),
            "XDG_CACHE_HOME": str(temp), "XDG_CONFIG_HOME": str(temp),
            "HYPOTHESIS_STORAGE_DIRECTORY": str(temp / "hypothesis")})
        if self.kind == "fastmcp":
            empty = temp / "empty.env"
            empty.write_text("")
            empty.chmod(0o600)
            env.update(FASTMCP_ENV_FILE=str(empty), FASTMCP_HOME=str(temp / "fastmcp"),
                       FASTMCP_TELEMETRY_MODE="off", FASTMCP_CHECK_FOR_UPDATES="off",
                       FASTMCP_SHOW_SERVER_BANNER="false", PYTHONNOUSERSITE="1")
        self.process = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec", "-p", policy_for([]), *command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=temp if self.kind == "fastmcp" else root,
            limit=MAX_BYTES, start_new_session=True)

    async def request(self, command, **arguments):
        if self.process is None or self.process.returncode is not None:
            raise LabError("API validator worker is unavailable")
        self.sequence += 1
        data = json.dumps({"id": self.sequence, "method": command, "arguments": arguments},
                          allow_nan=False).encode() + b"\n"
        if len(data) > MAX_BYTES:
            raise Unsupported("API validator request exceeds 4 MiB")
        try:
            async with asyncio.timeout(self.timeout):
                self.process.stdin.write(data)
                await self.process.stdin.drain()
                raw = await self.process.stdout.readline()
                if not raw.endswith(b"\n") or len(raw) > MAX_BYTES:
                    raise LabError("Incomplete or oversized API validator response")
                response = loads(raw.decode(), json_only=True)
                if not isinstance(response, dict) or type(response.get("id")) is not int \
                        or response.get("id") != self.sequence:
                    raise LabError("API validator response identity mismatch")
                if set(response) == {"id", "error", "unsupported"}:
                    if response["unsupported"]:
                        raise Unsupported(response["error"])
                    raise LabError(response["error"], code=1, category="tool/API")
                if set(response) != {"id", "result"}:
                    raise LabError("Invalid API validator response")
                return response["result"]
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await self.process.wait()
