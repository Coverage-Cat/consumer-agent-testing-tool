"""Explicit Maritime transport for the existing trusted worker coordinator.

Provider credentials stay here. Requests containing coordinator credentials use
private volume files, never command arguments or the agent's chat/inbox.
This adapter does not provision an inference model or install a remote harness.
"""
import asyncio
import base64
import fcntl
import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from .errors import LabError
from .serialization import digest, file_digest
from .surface import Strict, atomic_json, inside

MAX_RESPONSE = 4 * 1024 * 1024
MAX_REQUEST = 48 * 1024 * 1024
OPERATIONS = {"provision", "ready", "seed", "run", "export", "cancel", "destroy",
              "runtime", "checkpoint"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ExecutionNotStarted(LabError):
    """The two documented provider errors that explicitly deny exec admission."""


class MaritimeConfig(Strict):
    api_url: str = "https://api.maritime.sh"
    api_key_env: str = Field(default="MARITIME_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    namespace: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    template_id: str = Field(min_length=1, max_length=128)
    python: str = "/opt/consumer-agent-testing-tool/bin/python"
    cleanup: Literal["external-reaper-required"] = "external-reaper-required"

    @model_validator(mode="after")
    def explicit_endpoint(self):
        endpoint = urlsplit(self.api_url)
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username \
                or endpoint.password or endpoint.query or endpoint.fragment \
                or endpoint.path not in ("", "/"):
            raise ValueError("Maritime requires an explicit HTTPS origin without credentials")
        if not self.python.startswith("/") or ".." in PurePosixPath(self.python).parts \
                or "\\" in self.python or any(ord(char) < 32 for char in self.python):
            raise ValueError("Maritime Python must be an absolute in-container executable path")
        self.api_url = self.api_url.rstrip("/")
        return self


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LabError("Maritime returned an invalid resource identifier")
    return value


def volume_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") \
            or ".." in PurePosixPath(value).parts or "\\" in value \
            or any(ord(char) < 32 for char in value):
        raise LabError("Maritime returned an invalid volume root")
    return str(PurePosixPath(value))


@contextmanager
def state_lock(directory, trial):
    """A separate reaper must not race the coordinator's resource admission."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / (trial + ".lock"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LabError("Maritime trial is busy; retry reconciliation later") from error
        yield
    finally:
        os.close(descriptor)


class MaritimeGateway:
    def __init__(self, root, config, *, transport=None):
        self.root, self.config, self.settings = Path(root).resolve(), config, config.maritime
        self.transport = transport  # Injected local contract transport; never selected from a run file.
        self.directory = inside(self.root, Path(".cache/maritime") / digest(config.model_dump()))

    async def request(self, client, method, path, *, accepted=(200,), raw=False, **kwargs):
        # No redirects, ambient proxy settings, retries, or response-body diagnostics.
        # Errors may echo uploaded private data and must not enter public journals.
        async with client.stream(method, path, **kwargs) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE:
                    raise LabError("Maritime response exceeds 4 MiB")
            if response.status_code not in accepted:
                try:
                    error = json.loads(content)
                    detail = error.get("detail") if isinstance(error, dict) else None
                except (ValueError, UnicodeError):
                    detail = None
                if method == "POST" and path.endswith("/exec") and isinstance(detail, dict) \
                        and detail.get("execution_started") is False \
                        and (response.status_code, detail.get("code")) in {
                            (409, "vm_not_ready"), (503, "guest_command_unavailable")}:
                    raise ExecutionNotStarted("Maritime execution was not admitted")
                raise LabError(f"Maritime {method} request failed with HTTP {response.status_code}")
            if raw or response.status_code in (204, 404):
                return response.status_code, bytes(content)
            try:
                return response.status_code, json.loads(content)
            except (ValueError, UnicodeError) as error:
                raise LabError("Maritime returned invalid JSON") from error

    def save(self, path, state):
        atomic_json(path, state)
        # Preserve resource identity across a coordinator crash after the HTTP send.
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def resolve(self, client, state):
        status, body = await self.request(client, "GET", state["end_user_path"] + "/agents",
                                          accepted=(200, 404))
        if status == 404:
            return None
        agents = body.get("agents") if isinstance(body, dict) else body
        if not isinstance(agents, list) or len(agents) > 1:
            raise LabError("Maritime trial has an ambiguous agent collection")
        if not agents:
            return None
        if not isinstance(agents[0], dict):
            raise LabError("Maritime returned an invalid agent")
        agent = identifier(agents[0].get("id"))
        if state.get("agent_id") not in (None, agent):
            raise LabError("Maritime trial agent identity changed")
        return agent

    async def execution(self, client, agent, command):
        """Submit once, then retrieve that execution; a lost POST is uncertain."""
        base = f"/api/agents/{agent}/exec"
        while True:
            try:
                _, result = await self.request(client, "POST", base, accepted=(200, 202),
                    json={"command": command, "detached": True,
                          "timeout": self.config.call_timeout_seconds})
                break
            except ExecutionNotStarted:
                await asyncio.sleep(self.config.poll_interval_seconds)
        if not isinstance(result, dict):
            raise LabError("Maritime returned an invalid execution")
        execution = identifier(result.get("executionId"))
        while result.get("status") == "running":
            await asyncio.sleep(self.config.poll_interval_seconds)
            _, result = await self.request(client, "GET", base + "/" + execution)
            if not isinstance(result, dict) or result.get("executionId") != execution:
                raise LabError("Maritime execution identity changed during polling")
        if result.get("status") != "completed" or type(result.get("exitCode")) is not int \
                or result["exitCode"] != 0:
            raise LabError("Maritime bridge execution did not complete successfully")
        # stdout/stderr can be truncated, so never use them as the worker protocol.

    async def bridge(self, client, state, operation, payload):
        agent = state["agent_id"]
        base = f"/api/agents/{agent}"
        _, listing = await self.request(client, "GET", base + "/files/list")
        if not isinstance(listing, dict):
            raise LabError("Maritime returned an invalid volume listing")
        volume = volume_path(listing.get("root"))
        if state.get("volume") not in (None, volume):
            raise LabError("Maritime volume root changed")
        state["volume"] = volume
        directory = str(PurePosixPath(volume) / (".consumer-agent-testing-tool-" + payload["trial_id"]))
        request_id = uuid.uuid4().hex
        input_path = directory + "/" + request_id + ".input.json"
        output_path = directory + "/" + request_id + ".output.json"
        # Prepare the private directory before sending any coordinator secrets.
        prepare = (
            "import os,sys; from pathlib import Path; p=Path(sys.argv[1]); "
            "os.umask(0o077); p.mkdir(mode=0o700,exist_ok=True); "
            "assert not p.is_symlink() and p.stat().st_mode & 0o077 == 0"
        )
        await self.execution(client, agent, [self.settings.python, "-I", "-c", prepare, directory])
        message = {**payload, "operation": operation}
        if operation == "seed":
            package = inside(self.root, message.pop("bundle"))
            if package.stat().st_size > 32 * 1024 * 1024 or file_digest(package) != message["sha256"]:
                raise LabError("Maritime seed bundle exceeds its limit or changed before upload")
            message["bundle_base64"] = base64.b64encode(package.read_bytes()).decode()
        content = json.dumps(message).encode()
        if len(content) > MAX_REQUEST:
            raise LabError("Maritime bridge input exceeds 48 MiB")
        _, uploaded = await self.request(client, "POST", base + "/files/upload", accepted=(200, 201),
            data={"dest_dir": directory},
            files={"file": (request_id + ".input.json", content, "application/json")})
        if not isinstance(uploaded, dict) or uploaded.get("path") != input_path:
            raise LabError("Maritime upload did not confirm the exact private destination")
        # The stdin-based reference gateway is reused verbatim, including its
        # process guard. No new model loop, shell tool or scoring path is added.
        source = Path(__file__).with_name("worker_gateway.py").read_text()
        guard = Path(__file__).with_name("worker_guard.py").read_text()
        code = (
            "import os,json,sys; from pathlib import Path\n"
            "os.umask(0o077)\n"
            "os.environ.clear(); os.environ['PATH']=os.defpath\n"
            "source=Path(sys.argv[1]); destination=Path(sys.argv[2])\n"
            "message=json.loads(source.read_bytes()); source.unlink()\n"
            "namespace={'__name__':'consumer_agent_testing_tool_maritime_bridge',"
            f"'WORKER_GUARD_SOURCE':{guard!r}}}\n"
            f"exec({source!r},namespace)\n"
            "try: result=namespace['handle'](Path(sys.argv[3]),message)\n"
            "except Exception: result={'error':'Worker operation failed; private diagnostic review required'}\n"
            "temporary=destination.with_suffix('.tmp')\n"
            "with temporary.open('x') as output:\n"
            " json.dump(result,output); output.flush(); os.fsync(output.fileno())\n"
            "temporary.replace(destination)\n"
        )
        await self.execution(client, agent, [self.settings.python, "-I", "-c", code,
            input_path, output_path, directory + "/jobs"])
        _, data = await self.request(client, "GET", base + "/files/download",
                                    params={"path": output_path}, raw=True)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise LabError("Maritime worker returned invalid JSON") from error
        if not isinstance(result, dict) or result.get("error") \
                or result.get("trial_id") != payload["trial_id"]:
            raise LabError("Maritime worker rejected the operation or returned a different trial")
        # Read-before-delete; failed cleanup remains a failed operation. Unknown
        # exec results are retained until whole-agent deletion by the reaper.
        await self.request(client, "DELETE", base + "/files/delete",
                           params={"path": output_path}, accepted=(200, 204, 404))
        return result

    async def destroy(self, client, state, path):
        # Do not launch another remote command: cancel/export have already been
        # attempted and journaled. Provider deletion also covers a half-created job.
        state["phase"] = "deleting"
        self.save(path, state)
        if not state.get("create_requested") and not state.get("agent_id"):
            state["phase"] = "destroyed"
            self.save(path, state)
            return {"trial_id": state["trial_id"], "destroyed": True,
                    "resource_deletion": "no_creation_requested", "provider": "maritime"}
        agent = state.get("agent_id") or await self.resolve(client, state)
        if agent is None and state.get("create_requested"):
            raise LabError("Maritime creation is uncertain; retain the trial for reconciliation")
        if agent:
            state["agent_id"] = agent
            self.save(path, state)
            resource = f"/api/agents/{agent}"
            await self.request(client, "DELETE", resource, accepted=(204, 404))
            status, _ = await self.request(client, "GET", resource, accepted=(200, 404))
            if status != 404:
                raise LabError("Maritime has not confirmed agent deletion")
        await self.request(client, "DELETE", state["end_user_path"], accepted=(204, 404))
        status, _ = await self.request(client, "GET", state["end_user_path"], accepted=(200, 404))
        if status != 404:
            raise LabError("Maritime has not confirmed end-user cleanup")
        state["phase"] = "destroyed"
        self.save(path, state)
        return {"trial_id": state["trial_id"], "destroyed": True,
                "resource_deletion": "provider-confirmed", "provider": "maritime"}

    async def call(self, operation, payload):
        trial = payload.get("trial_id")
        expiry = payload.get("expires_at")
        if operation not in OPERATIONS or not isinstance(trial, str) \
                or not re.fullmatch(r"worker-[0-9a-f]{32}", trial) \
                or type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise LabError("Invalid Maritime lifecycle operation, identity, or deadline")
        if operation == "run" and payload.get("mode") not in ("start", "poll"):
            raise LabError("Maritime requires polling execution")
        credentials = payload.get("coordinator_environment", {})
        control_values = {os.environ[name] for name in self.config.credential_env
                          if os.environ.get(name)}
        if operation == "run" and (set(credentials) & set(self.config.credential_env)
                or set(credentials.values()) & control_values):
            raise LabError("Cloud provisioning credentials cannot enter the worker environment")
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise LabError("The explicitly selected Maritime API key is unavailable")
        path = self.directory / (trial + ".json")
        with state_lock(self.directory, trial):
            if path.is_symlink():
                raise LabError("Linked Maritime state is forbidden")
            state = json.loads(path.read_text()) if path.exists() else None
            if state is None:
                if operation != "provision":
                    raise LabError("Maritime trial has no durable provision intent")
                if not 0 < expiry - time.time() <= self.config.ttl_seconds:
                    raise LabError("Maritime deadline must be future and within the configured TTL")
                external = self.settings.namespace + "-" + trial
                state = {"trial_id": trial, "expires_at": expiry, "phase": "intent",
                         "end_user_path": "/api/v1/end-users/" + external}
                self.save(path, state)  # Always precedes the first provider request.
            if state["trial_id"] != trial or state["expires_at"] != expiry:
                raise LabError("Maritime trial identity or original deadline changed")
            if state["phase"] == "destroyed":
                if operation == "destroy":
                    return {"trial_id": trial, "destroyed": True}
                raise LabError("Retired Maritime trial cannot be reused")
            if (operation in {"provision", "ready", "seed", "runtime"}
                    or operation == "run" and payload.get("mode") == "start"
                    or operation == "checkpoint" and payload.get("mode") in ("chunk", "commit")) \
                    and (state["phase"] == "deleting" or time.time() >= expiry):
                raise LabError("Maritime trial is deleting or its execution deadline expired")
            try:
                async with asyncio.timeout(self.config.call_timeout_seconds), httpx.AsyncClient(
                    base_url=self.settings.api_url, headers={"Authorization": "Bearer " + key},
                    timeout=self.config.call_timeout_seconds, follow_redirects=False, trust_env=False,
                    transport=self.transport,
                ) as client:
                    if operation == "destroy":
                        return await self.destroy(client, state, path)
                    if operation == "provision" and state.get("agent_id") is None:
                        agent = await self.resolve(client, state)
                        if agent is None:
                            # End-user agent creation is idempotent by external ID.
                            # Normal agent creation and exec POST are not.
                            state["create_requested"] = True
                            self.save(path, state)
                            _, created = await self.request(client, "POST",
                                state["end_user_path"] + "/agents", accepted=(200, 201),
                                json={"templateId": self.settings.template_id,
                                      "name": self.settings.namespace + "-" + trial})
                            if not isinstance(created, dict):
                                raise LabError("Maritime returned an invalid provision result")
                            agent = identifier(created.get("id"))
                        elif not state.get("create_requested"):
                            raise LabError("Maritime trial identity already exists; refusing adoption")
                        state.update(agent_id=agent, phase="provisioned")
                        self.save(path, state)
                    if not state.get("agent_id"):
                        if not state.get("create_requested"):
                            raise LabError("Maritime agent was not created by this trial")
                        agent = await self.resolve(client, state)
                        if agent is None:
                            raise LabError("Maritime agent is unavailable; it will not be recreated")
                        state["agent_id"] = agent
                        self.save(path, state)
                    result = await self.bridge(client, state, operation, payload)
                    self.save(path, state)
                    if operation == "ready":
                        result["cloud"] = {"provider": "maritime", "agent_id": state["agent_id"],
                            "template_id": self.settings.template_id,
                            "resource_cleanup": self.settings.cleanup,
                            "resource_ttl_verified": False}
                    return result
            except httpx.TimeoutException as error:
                raise TimeoutError("Maritime request timed out; submission may have succeeded") from error
            except httpx.TransportError as error:
                raise OSError("Maritime transport failed; submission may have succeeded") from error
