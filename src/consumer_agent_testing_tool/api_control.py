"""Private, bounded lifecycle/oracle connection for developer API trials.

The evaluated model never receives this connection or its operations. A service
adapter must hold an exclusive test-state lease until release/disconnect.
"""
import asyncio
import copy
import json
import os
import stat
from contextvars import ContextVar
from pathlib import Path

from .consent import binding, effective_arguments
from .consent import digest as consent_digest
from .errors import LabError, Unsupported
from .scenario import SCORERS
from .serialization import digest, loads
from .socket_paths import socket_address
from .surface import inside

PROTOCOL = "consumer-agent-testing-tool-api-control-v1"
MAX_BYTES = 4 * 1024 * 1024
BACKEND_SCORERS = {name: kind for name, kind in SCORERS.items() if name.startswith("backend.")}
# Per-trial identity stays in the protocol, the manifest and events. A run lock
# compares configurations across trials, so it must not contain these.
TRIAL_IDENTITY = ("run_id",)
# Set only by the fixed coordinator after validating its pinned invocation.
# This grants access to selected preconfigured worker resources, not to model tools.
worker_control_sockets = ContextVar("worker_control_sockets", default=())


def control_path(target):
    path = Path(target.api_control.socket).absolute()
    external = str(path) in worker_control_sockets.get()
    if not external:
        inside(Path.cwd(), path)
    # Resolve only after rejecting linked components; an owned socket is still
    # not an OS sandbox against a hostile process running as the same user.
    for item in (path, *path.parents):
        if not external and item == Path.cwd().parent:
            break
        if item.is_symlink():
            raise LabError("API control paths cannot contain symlinks")
    try:
        info = path.stat()
    except OSError as error:
        raise LabError("API control socket is unavailable") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LabError("API control requires an owner-only Unix socket in this workspace")
    return path


class ApiControlClient:
    def __init__(self, target, mapping, scenario, evidence, session):
        self.target, self.mapping, self.scenario = target, mapping, scenario
        self.evidence, self.session = evidence, session
        self.reader = self.writer = None
        self.sequence, self.acquired, self.broken = 0, False, False
        self.retained_lease = None
        self.proposals = {}
        self.identity = {
            "protocol": PROTOCOL, "run_id": evidence.run_id, "task_id": scenario["id"],
            "target": scenario["target"], "base_url": target.base_url,
            "source_revision": target.source_revision, "mapping_sha256": digest(mapping.model_dump(
                mode="json", by_alias=True)), "principal": target.api_control.principal,
        }

    async def connect(self, *, retain=False):
        if retain:
            from .api_lease import RetainedApiLease
            self.retained_lease = RetainedApiLease(self)
            # Reject mismatched/missing private state before touching the service.
            if self.session.resuming:
                self.session.remaining_seconds()
                self.retained_lease.load()
        path = control_path(self.target)
        self.evidence.redactor.secrets.append(str(path))
        async with asyncio.timeout(min(self.target.timeout_seconds, self.session.remaining_seconds())):
            self.reader, self.writer = await asyncio.open_unix_connection(socket_address(path), limit=MAX_BYTES)
        metadata = await self.rpc("describe")
        expected = {key: self.identity[key] for key in (
            "protocol", "target", "base_url", "source_revision", "mapping_sha256", "principal")}
        fields = {"descriptor", "environment", "capabilities", "events", "fixtures", "personas"}
        if not isinstance(metadata, dict) or set(metadata) != fields or metadata.get("environment") != "test" \
                or metadata.get("descriptor") != expected:
            raise LabError("Private API adapter identity/test environment does not match the target")
        if any(not isinstance(metadata[key], list) or any(not isinstance(item, str) or not item
               for item in metadata[key]) for key in ("capabilities", "events", "fixtures", "personas")):
            raise LabError("Private API adapter capabilities must be explicit string lists")
        required = {"acquire", "snapshot", "release"}
        if any(op.effect in ("commit", "cancel") for op in self.mapping.operations.values()):
            required.update(("prepare", "grant"))
        if not required <= set(metadata.get("capabilities", [])):
            raise Unsupported("Private API adapter lacks required lifecycle/consent capabilities")
        for event in self.scenario.get("events", []):
            if event["action"] != "interrupt" and (
                    "event" not in metadata.get("capabilities", [])
                    or event["action"] not in metadata.get("events", [])):
                raise Unsupported("Private API adapter does not support a requested scenario event")
        initial = self.scenario["initial_state"]
        if initial["fixture"] not in metadata.get("fixtures", []) \
                or initial["persona"] not in metadata.get("personas", []):
            raise Unsupported("Private API adapter does not support the requested seed/persona")
        self.evidence.manifest["run_lock"]["api_control"] = {
            "identity": {key: value for key, value in self.identity.items() if key not in TRIAL_IDENTITY},
            "metadata": metadata,
            "scope": "trusted developer adapter; exclusive test-state lease; no OS isolation proof"}
        self.evidence.manifest["api_control_trial_identity"] = {key: self.identity[key] for key in TRIAL_IDENTITY}
        self.evidence.event("api_control_described", {**metadata, "trial_identity": {
            key: self.identity[key] for key in TRIAL_IDENTITY}},
            provenance="private developer control channel; not model context")
        if retain:
            return await self.retained_lease.open(metadata)
        result = await self.rpc("acquire", initial_state=initial)
        # Do not retry acquire on a missing/malformed response: reset may have run.
        if not isinstance(result, dict) or result.get("reset_completed") is not True \
                or result.get("clock") != initial["clock"] or not isinstance(result.get("user_profile"), dict):
            raise LabError("Private API adapter did not confirm the requested reset")
        self.acquired = True
        self.evidence.event("fixture_reset", {"clock": result["clock"], "reset_completed": True},
                           provenance="private API adapter; exclusive lease acquired")
        return result["user_profile"]

    async def rpc(self, command, *, cleanup=False, **args):
        if self.broken or self.writer is None:
            raise LabError("Private API control connection is unavailable")
        self.sequence += 1
        request = {"id": self.sequence, "identity": self.identity, "operation": command, "args": args}
        data = json.dumps(request, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_BYTES:
            raise LabError("Private API control request exceeds byte budget")
        timeout = min(5 if cleanup else self.session.remaining_seconds(), self.target.timeout_seconds)
        try:
            async with asyncio.timeout(timeout):
                self.writer.write(data)
                await self.writer.drain()
                raw = await self.reader.readline()
                if not raw.endswith(b"\n") or len(raw) > MAX_BYTES:
                    raise LabError("Incomplete or oversized private API control response")
                response = loads(raw.decode("utf-8"), json_only=True)
                if not isinstance(response, dict) or set(response) != {"id", "identity", "result"} \
                        or type(response["id"]) is not int or response["id"] != self.sequence \
                        or response["identity"] != self.identity:
                    raise LabError("Private API control response identity mismatch")
                return response["result"]
        except (OSError, TimeoutError, ValueError, UnicodeError) as error:
            self.broken = True
            raise LabError("Private API control transport failed: " + type(error).__name__) from error
        except BaseException:
            # Never reconnect and reset automatically after an uncertain command.
            self.broken = True
            raise

    async def close(self, *, suspend=False):
        if self.writer:
            try:
                if self.acquired and not self.broken:
                    if suspend:
                        if self.retained_lease is None:
                            raise LabError("This API lease does not support suspension")
                        await self.retained_lease.suspend()
                        return
                    result = await self.rpc("release", cleanup=True)
                    if result != {"released": True}:
                        raise LabError("Private API adapter did not confirm lease release")
                    self.acquired = False
                    if self.retained_lease:
                        self.retained_lease.released()
                    self.evidence.event("api_lease_released", result, provenance="private developer control channel")
            finally:
                self.writer.close()
                async with asyncio.timeout(5):
                    await self.writer.wait_closed()

    async def prepare(self, checkpoint, operation, arguments):
        result = await self.rpc("prepare", proposal_id=checkpoint,
                                operation=operation.operation, arguments=arguments)
        envelope = result.get("envelope") if isinstance(result, dict) and set(result) == {"envelope"} else None
        if not isinstance(envelope, dict):
            raise LabError("Private API adapter returned no consent envelope")
        expected = {
            "run_id": self.identity["run_id"], "task_id": self.identity["task_id"],
            "principal": self.identity["principal"], "target": self.identity["target"],
            "destination": self.target.base_url, "operation": operation.operation,
            "effect": operation.effect, "credential_ref": self.target.auth.env or "none",
        }
        if any(envelope.get(key) != value for key, value in expected.items()):
            raise LabError("Private API consent envelope does not match the proposed operation")
        # Never change omissions/defaults without disclosure: the grant binds the
        # effective arguments, so a material default must be explicit.
        if effective_arguments(operation.input_schema, arguments) != arguments:
            raise Unsupported("Transactional arguments must explicitly include material schema defaults")
        bound = binding(envelope, operation.input_schema, arguments)
        self.proposals[checkpoint] = copy.deepcopy({
            "envelope": envelope, "schema": operation.input_schema, "arguments": arguments,
            "binding_sha256": consent_digest(bound)})
        return envelope

    async def control(self, operation, **args):
        if operation == "event":
            return await self.rpc(operation, **args)
        if operation != "grant" or args["proposal_id"] not in self.proposals:
            raise LabError("Private API grant has no frozen proposal")
        proposal = self.proposals[args["proposal_id"]]
        if args["arguments"] != proposal["arguments"] \
                or args["principal"] != proposal["envelope"]["principal"] \
                or args["target_operation"] != proposal["envelope"]["operation"]:
            raise LabError("Private API grant differs from the frozen proposal")
        result = await self.rpc("grant", **args, **{key: proposal[key] for key in ("envelope", "schema")})
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"] \
                or result.get("binding_sha256") != proposal["binding_sha256"]:
            raise LabError("Private API adapter did not confirm the exact grant binding")
        self.evidence.redactor.secrets.append(result["id"])
        return result

    async def snapshot(self):
        result = await self.rpc("snapshot", cleanup=True)
        if not isinstance(result, dict) or set(result) != {"metrics", "pending", "provenance"} \
                or not isinstance(result["metrics"], dict) or not isinstance(result["pending"], list) \
                or not all(isinstance(item, str) and item for item in result["pending"]) \
                or not isinstance(result["provenance"], str) or not result["provenance"]:
            raise LabError("Invalid independent API oracle record")
        for name, value in result["metrics"].items():
            if name not in BACKEND_SCORERS or type(value) is not BACKEND_SCORERS[name] \
                    or type(value) is int and value < 0:
                raise LabError("API oracle returned an unknown metric or invalid typed value")
        return result
