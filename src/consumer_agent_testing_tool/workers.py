"""Provider-independent temporary worker lifecycle over an explicit JSON command gateway."""
import asyncio
import base64
import json
import os
import shlex
import signal
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .errors import LabError
from .evidence import Redactor
from .serialization import digest, file_digest, load
from .surface import Strict, atomic_json, inside
from .worker_export import CHUNK_BYTES, unpack, validate_metadata, verify_saved
from .worker_maritime import MaritimeConfig


class WorkerConfig(Strict):
    schema_version: Literal[1] = 1
    provider: str
    gateway: list[str] = Field(default_factory=list)
    ssh_target: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.@-]+$")
    ssh_identity_file: str | None = None
    ssh_known_hosts_file: str | None = None
    ssh_worker_root: str = "/tmp/consumer-agent-testing-tool-workers"
    ssh_python: str = "python3"
    ttl_seconds: int = Field(default=300, ge=1, le=3600)
    call_timeout_seconds: int = Field(default=60, ge=1, le=300)
    run_mode: Literal["auto", "blocking", "polling"] = "auto"
    poll_interval_seconds: float = Field(default=1.0, ge=.05, le=60)
    credential_env: list[str] = Field(default_factory=list)
    expected_harness_sha256: str | None = None
    maritime: MaritimeConfig | None = None

    @model_validator(mode="after")
    def selected_gateway(self):
        if self.provider == "maritime":
            if self.maritime is None or self.gateway or self.ssh_target or self.run_mode == "blocking":
                raise ValueError("Maritime requires its own configuration and polling execution")
            if self.maritime.api_key_env not in self.credential_env:
                raise ValueError("Maritime API key reference must be in credential_env")
        elif self.maritime is not None:
            raise ValueError("Maritime settings require the maritime provider")
        elif self.provider == "ssh":
            if not self.ssh_target or self.ssh_target.startswith("-") or not self.ssh_identity_file \
                    or not self.ssh_known_hosts_file:
                raise ValueError("SSH workers require a target and workspace identity/known-hosts files")
            if not self.ssh_worker_root.startswith("/tmp/") or ".." in Path(self.ssh_worker_root).parts:
                raise ValueError("SSH worker root must be an explicitly selected /tmp directory")
            if self.ssh_python != "python3" and (
                    not Path(self.ssh_python).is_absolute() or ".." in Path(self.ssh_python).parts):
                raise ValueError("SSH Python must be python3 or an explicit absolute path")
        elif not self.gateway:
            raise ValueError("Select an explicit worker command gateway")
        return self


def selected_gateway(root, config):
    if config.provider == "maritime":
        from .worker_maritime import MaritimeGateway
        return MaritimeGateway(root, config)
    return SSHGateway(root, config) if config.provider == "ssh" else CommandGateway(root, config)


def bundle(root, manifest, destination):
    root = Path(root).resolve()
    declared = load(inside(root, manifest))
    paths = declared["files"]
    if len(set(paths)) != len(paths) or not paths:
        raise LabError("Worker bundle files must be distinct and explicit")
    locked = {}
    for name in paths:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise LabError("Worker bundle paths must be relative")
        if any(word in name.lower() for word in ("secret", ".env", "checkpoint.private", "credentials")):
            raise LabError("Private files cannot be worker bundle inputs")
        locked[name] = file_digest(inside(root, name))
    result = {"schema_version": 1, "files": locked,
              "command": declared["command"], "environment": "scoped by worker gateway"}
    if "coordinator" in declared:
        result["coordinator"] = declared["coordinator"]
    if "runtime" in declared:
        result["runtime"] = declared["runtime"]
    if "checkpoint" in declared:
        result["checkpoint"] = declared["checkpoint"]
    target = inside(root, destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bundle-lock.json", json.dumps(result))
        for name in paths:
            archive.write(inside(root, name), name)
    return {"bundle": str(target), "sha256": file_digest(target), "lock": result}


class CommandGateway:
    def __init__(self, root, config):
        self.root, self.config = Path(root).resolve(), config

    async def call(self, operation, payload):
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
        for key in self.config.credential_env:
            if key not in os.environ:
                raise LabError(f"Missing explicit worker credential environment variable: {key}")
            env[key] = os.environ[key]
        child = await asyncio.create_subprocess_exec(
            *self.config.gateway, cwd=self.root, env=env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _error = await asyncio.wait_for(child.communicate(
                json.dumps({"operation": operation, **payload}).encode()),
                self.config.call_timeout_seconds)
            if child.returncode:
                raise LabError(f"Worker gateway {operation} failed with exit {child.returncode}")
            if len(out) > 4 * 1024 * 1024:
                raise LabError("Worker gateway response exceeds 4 MiB")
            result = json.loads(out)
            if result.get("error"):
                raise LabError(f"Worker gateway rejected {operation}: {result['error']}")
            return result
        finally:
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGKILL)
                await child.wait()


def ssh_command(root, config, command):
    identity = inside(root, config.ssh_identity_file)
    known_hosts = inside(root, config.ssh_known_hosts_file)
    if not identity.is_file() or not known_hosts.is_file():
        raise LabError("SSH identity and known-hosts files must be available in the workspace")
    return ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", f"GlobalKnownHostsFile={known_hosts}", "-i", str(identity), config.ssh_target, command]


class SSHGateway(CommandGateway):
    """Temporary workers on an explicitly selected existing cloud/remote Linux host."""
    async def call(self, operation, payload):
        code = "WORKER_GUARD_SOURCE = " + repr(
            (Path(__file__).parent / "worker_guard.py").read_text()) + "\n"
        code += (Path(__file__).parent / "worker_gateway.py").read_text()
        command = shlex.join([self.config.ssh_python, "-I", "-c", code, self.config.ssh_worker_root])
        gateway = ssh_command(self.root, self.config, command)
        # SSH sends only an explicitly pinned archive, not coordinator filesystem paths.
        payload = dict(payload)
        if operation == "seed":
            package = inside(self.root, payload.pop("bundle"))
            payload["bundle_base64"] = base64.b64encode(package.read_bytes()).decode()
        config = self.config.model_copy(update={"gateway": gateway})
        return await CommandGateway(self.root, config).call(operation, payload)


def export_complete(result):
    """Require an explicit collection acknowledgement, not merely a successful RPC."""
    if not isinstance(result.get("files"), dict) or not all(
            isinstance(name, str) and isinstance(text, str) for name, text in result["files"].items()):
        raise LabError("Worker export must contain a text file map")
    if type(result.get("complete")) is not bool or not isinstance(result.get("issues"), list) \
            or not all(isinstance(issue, str) for issue in result["issues"]):
        raise LabError("Worker export must explicitly report completeness and collection issues")
    if result["complete"] and result["issues"]:
        raise LabError("Worker export claims completion despite collection issues")
    return result["complete"]


class WorkerJournal:
    """Persist replies and collected evidence before the next destructive operation."""
    def __init__(self, config, gateway, path, record):
        self.config, self.gateway, self.path, self.record = config, gateway, path, record
        self.redactor = Redactor(os.environ.get(key) for key in config.credential_env)
        self.export_secrets = [os.environ[key] for key in record.get("coordinator_credential_env", [])
                               if key in os.environ]
        self.redactor.add(self.export_secrets)

    def save(self):
        atomic_json(self.path / "worker.json", self.redactor.clean(self.record))

    async def collect_runs(self, metadata):
        """Transfer only the worker's redacted snapshot, then verify every link locally."""
        validate_metadata(metadata)
        destination = self.path / "evidence"
        if destination.exists():
            verify_saved(destination, metadata)
            return
        with tempfile.TemporaryDirectory(prefix=".worker-transfer-", dir=self.path) as temporary:
            archive = Path(temporary) / "runs.zip"
            with archive.open("xb") as stream:
                for offset in range(0, metadata["bytes"], CHUNK_BYTES):
                    reply = await self.gateway.call("export", {
                        "trial_id": self.path.name, "expires_at": self.record["expires_at"],
                        "archive_sha256": metadata["sha256"], "offset": offset})
                    if not isinstance(reply, dict) or reply.get("trial_id") != self.path.name \
                            or reply.get("archive_sha256") != metadata["sha256"] \
                            or type(reply.get("offset")) is not int or reply["offset"] != offset:
                        raise LabError("Worker run chunk identity or offset mismatch")
                    content = base64.b64decode(reply["data_base64"], validate=True)
                    if len(content) != min(CHUNK_BYTES, metadata["bytes"] - offset):
                        raise LabError("Worker run chunk size mismatch")
                    stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            staged = Path(temporary) / "evidence"
            unpack(archive, staged, metadata)
            staged.rename(destination)

    async def operation(self, name, **payload):
        event = {"operation": name, "status": "requested"}
        if "mode" in payload:
            event["mode"] = payload["mode"]
        self.record["events"].append(event)
        self.save()
        try:
            result = await self.gateway.call(name, {"trial_id": self.path.name,
                "expires_at": self.record["expires_at"], **payload})
            if not isinstance(result, dict) or result.get("trial_id") != self.path.name:
                raise LabError("Worker result belongs to another trial")
            if name == "ready" and "runtime" in result:
                self.record["runtime"] = result["runtime"]
            if name == "ready" and "cloud" in result:
                self.record["cloud"] = result["cloud"]
            if name == "run":
                if payload.get("mode") in ("start", "poll"):
                    state = result.get("run_state")
                    if state not in ("running", "completed", "not_started") \
                            or state != "completed" and "exit_code" in result:
                        raise LabError("Worker returned an invalid polling execution state")
                    self.record["execution_state"] = state
                    if state == "running":
                        self.record["status"] = "running"
                    if state == "not_started":
                        raise LabError("Worker did not admit the original invocation; observation cannot restart it")
                else:
                    state = "completed"
                if state == "completed" and type(result.get("exit_code")) is not int:
                    raise LabError("Worker did not report an integer execution exit code")
                if state == "completed":
                    self.record["execution"] = result
            if name == "export":
                complete = export_complete(result)
                # Hash the redacted representation that will actually be stored.
                self.record["artifacts"] = self.redactor.clean(result)
                self.record["artifacts_sha256"] = digest(self.record["artifacts"])
                # Preserve diagnostics even if a later chunk or linked artifact fails.
                self.record["collection"] = "pending"
                self.save()
                if "run_archive" in result:
                    await self.collect_runs(result["run_archive"])
                self.record["collection"] = "complete" if complete else "incomplete"
            field = {"ready": "ready", "seed": "seeded", "cancel": "cancelled",
                     "destroy": "destroyed"}.get(name)
            if field and result.get(field) is not True:
                raise LabError(f"Worker did not confirm {name}")
        except (Exception, asyncio.CancelledError) as error:
            event.update(status="failed", detail=str(error) or "Operation cancelled")
            self.save()
            raise
        event["status"] = "completed"
        self.save()
        return result

    async def cancel(self):
        try:
            await self.operation("cancel")
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — still collect and clean up
            self.record["cancel_error"] = str(error) or "Cancellation interrupted"
            self.save()

    async def collect(self):
        previous = self.record.get("artifacts", {})
        # A destroy reply may have been lost after this exact export was persisted.
        if self.record.get("collection") == "complete" \
                and previous.get("trial_id") == self.path.name \
                and self.record.get("artifacts_sha256") == digest(previous):
            try:
                if export_complete(previous):
                    if "run_archive" in previous:
                        verify_saved(self.path / "evidence", previous["run_archive"])
                    return
            except LabError:
                pass
        try:
            available = {digest(value) for value in self.export_secrets}
            if set(self.record.get("coordinator_redaction_sha256", [])) - available:
                raise LabError("Original coordinator redaction values are unavailable; export was not requested")
            await self.operation("export", redaction_env=self.config.credential_env,
                                 redaction_values=self.export_secrets)
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — failed collection is evidence
            self.record.update(collection="failed", collection_error=str(error) or "Export interrupted")
        if self.record.get("collection") != "complete":
            self.record["status"] = "failed"
        self.save()

    async def collect_continuation(self):
        """Collect explicitly retained private state independently of public evidence."""
        if not self.record.get("retain_continuation"):
            return
        from .worker_checkpoint import MAX_BYTES, MAX_ENTRIES, read_record, verify_capsule
        from .worker_checkpoint import unpack as unpack_checkpoint
        from .worker_checkpoint import validate_metadata as validate_checkpoint

        state = self.record.setdefault("continuation", {"collection": "pending", "checkpoints": {}})
        private = self.path / "private"
        try:
            if private.is_symlink():
                raise LabError("Linked private checkpoint storage is forbidden")
            private.mkdir(mode=0o700, exist_ok=True)
            private.chmod(0o700)
            if state["collection"] == "complete":
                for run_id, expected in state["checkpoints"].items():
                    capsule = private / run_id
                    metadata = read_record(capsule / "receipt.json")
                    if digest(metadata) != expected:
                        raise LabError("Collected private checkpoint receipt changed")
                    verify_capsule(capsule, metadata)
                return
            reply = await self.operation("checkpoint", mode="list")
            checkpoints = reply.get("checkpoints")
            if reply.get("complete") is not True or not isinstance(checkpoints, dict) \
                    or len(checkpoints) > 4096:
                raise LabError("Worker did not confirm complete private checkpoint collection")
            for metadata in checkpoints.values():
                validate_checkpoint(metadata)
            if sum(row["bytes"] for row in checkpoints.values()) > MAX_BYTES \
                    or sum(len(row["files"]) for row in checkpoints.values()) > MAX_ENTRIES:
                raise LabError("Private checkpoint collection exceeds transfer limits")
            for run_id, metadata in checkpoints.items():
                validate_checkpoint(metadata)
                if metadata["source_trial"] != self.path.name or metadata["run_id"] != run_id:
                    raise LabError("Private checkpoint belongs to another worker or run")
                capsule = private / run_id
                if capsule.exists():
                    verify_capsule(capsule, metadata)
                else:
                    with tempfile.TemporaryDirectory(prefix=".checkpoint-", dir=private) as temporary:
                        staged = Path(temporary) / "capsule"
                        staged.mkdir(mode=0o700)
                        archive = staged / "checkpoint.zip"
                        with archive.open("xb") as stream:
                            archive.chmod(0o600)
                            for offset in range(0, metadata["bytes"], CHUNK_BYTES):
                                chunk = await self.operation("checkpoint", mode="read", run_id=run_id,
                                    sha256=metadata["sha256"], offset=offset)
                                if chunk.get("run_id") != run_id or chunk.get("sha256") != metadata["sha256"] \
                                        or type(chunk.get("offset")) is not int or chunk["offset"] != offset:
                                    raise LabError("Private checkpoint chunk identity mismatch")
                                content = base64.b64decode(chunk["data_base64"], validate=True)
                                if len(content) != min(CHUNK_BYTES, metadata["bytes"] - offset):
                                    raise LabError("Private checkpoint chunk size mismatch")
                                stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                        unpack_checkpoint(archive, staged / "run", metadata)
                        atomic_json(staged / "receipt.json", metadata)
                        (staged / "claim.lock").touch(mode=0o600)
                        staged.rename(capsule)
                state["checkpoints"][run_id] = digest(metadata)
                self.save()  # Preserve each complete capsule before a later transfer can fail.
            if set(state["checkpoints"]) != set(checkpoints):
                raise LabError("Private checkpoint collection changed between attempts")
            state["collection"] = "complete"
            state.pop("error", None)
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — still clean up the worker
            state.update(collection="failed", error=str(error) or "Private checkpoint collection interrupted")
            self.record["status"] = "failed"
        self.save()

    async def destroy(self):
        try:
            await self.operation("destroy")
            self.record["cleanup"] = "confirmed"
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — retain cleanup failures
            self.record.update(cleanup="failed", cleanup_error=str(error) or "Destruction interrupted",
                               status="failed")
        self.save()


class WorkerLifecycle(WorkerJournal):
    def __init__(self, root, config, gateway=None, *, output="runs"):
        self.root = Path(root).resolve()
        path = inside(self.root, Path(output) / f"worker-{uuid.uuid4().hex}")
        path.mkdir(parents=True)
        record = {"trial_id": path.name, "provider": config.provider, "events": [],
                  "gateway_sha256": digest(config.model_dump()),
                  "expires_at": time.time() + config.ttl_seconds,
                  "status": "pending", "cleanup": "pending", "collection": "pending"}
        super().__init__(config, gateway or selected_gateway(root, config), path, record)

    async def execute_job(self, ready, coordinator_environment, *, before_start=None):
        protocol = ready.get("run_protocol")
        polling = self.config.run_mode != "blocking" and protocol == "poll-v1"
        if self.config.run_mode == "polling" and not polling \
                or self.config.run_mode == "auto" and protocol not in (None, "poll-v1"):
            raise LabError("Worker does not support the selected polling protocol")
        self.record["run_mode"] = "polling" if polling else "blocking"
        self.save()
        credentials = {"coordinator_environment": coordinator_environment} if coordinator_environment else {}
        if before_start:
            before_start()  # Admission is consumed before the first, possibly lost, start request.
        if not polling:
            return await self.operation("run", **credentials)
        result = None
        try:
            result = await self.operation("run", mode="start", **credentials)
        except (TimeoutError, OSError):
            # An uncertain start is never sent twice. Observe the original
            # identity within the existing invocation deadline instead.
            pass
        while not result or result["run_state"] != "completed":
            await asyncio.sleep(self.config.poll_interval_seconds)
            try:
                result = await self.operation("run", mode="poll")
            except (TimeoutError, OSError):
                continue
        return result

    async def deploy_runtime(self, path, metadata, ready):
        from .worker_assets import CHUNK, PROTOCOL, sha
        if ready.get("runtime_protocol") != PROTOCOL or ready.get("runtime_platform") != metadata["platform"]:
            raise LabError("Worker lacks the runtime transfer protocol or has a different platform")
        if await asyncio.to_thread(sha, path) != metadata["sha256"]:
            raise LabError("Runtime archive changed before transfer")
        with Path(path).open("rb") as stream:  # noqa: ASYNC230 — bounded local chunk reads
            offset = 0
            while content := stream.read(CHUNK):
                payload = {"mode": "chunk", "sha256": metadata["sha256"], "offset": offset,
                           "data_base64": base64.b64encode(content).decode()}
                # A lost response retries identical pinned bytes at the same offset,
                # never job admission. The worker validates duplicates.
                while True:
                    try:
                        reply = await self.operation("runtime", **payload)
                        break
                    except (TimeoutError, OSError):
                        await asyncio.sleep(self.config.poll_interval_seconds)
                if reply.get("sha256") != metadata["sha256"] or reply.get("offset") != offset \
                        or reply.get("accepted_bytes") != len(content) or reply.get("installed") is not False:
                    raise LabError("Runtime chunk acknowledgement differs from request")
                offset += len(content)
        while True:
            try:
                reply = await self.operation("runtime", mode="commit", sha256=metadata["sha256"])
                break
            except (TimeoutError, OSError):
                await asyncio.sleep(self.config.poll_interval_seconds)
        if reply.get("sha256") != metadata["sha256"] or reply.get("installed") is not True:
            raise LabError("Worker did not confirm runtime installation")
        self.record["runtime_assets"] = metadata
        self.save()

    async def restore_checkpoint(self, owner):
        metadata = owner.metadata
        archive = owner.path / "checkpoint.zip"
        if file_digest(archive) != metadata["sha256"]:
            raise LabError("Private checkpoint archive changed before transfer")
        with archive.open("rb") as stream:
            offset = 0
            while content := stream.read(CHUNK_BYTES):
                payload = {"mode": "chunk", "sha256": metadata["sha256"], "offset": offset,
                           "data_base64": base64.b64encode(content).decode()}
                while True:
                    try:
                        reply = await self.operation("checkpoint", **payload)
                        break
                    except (TimeoutError, OSError):
                        await asyncio.sleep(self.config.poll_interval_seconds)
                if reply.get("sha256") != metadata["sha256"] or reply.get("offset") != offset \
                        or reply.get("accepted_bytes") != len(content) or reply.get("installed") is not False:
                    raise LabError("Private checkpoint chunk acknowledgement differs from request")
                offset += len(content)
        while True:
            try:
                reply = await self.operation("checkpoint", mode="commit", sha256=metadata["sha256"])
                break
            except (TimeoutError, OSError):
                await asyncio.sleep(self.config.poll_interval_seconds)
        if reply.get("sha256") != metadata["sha256"] or reply.get("installed") is not True:
            raise LabError("Worker did not confirm checkpoint restoration")

    async def run(self, bundle_path, *, expected_sha256=None, coordinator_environment=None,
                  harness=None, model_provider=None, runtime_path=None, runtime=None,
                  retain_continuation=False, checkpoint_owner=None):
        package = inside(self.root, bundle_path)
        expected = expected_sha256 or self.config.expected_harness_sha256
        if expected_sha256 and self.config.expected_harness_sha256 \
                and expected_sha256 != self.config.expected_harness_sha256:
            raise LabError("Generated worker bundle differs from the explicitly configured pin")
        if not expected or file_digest(package) != expected:
            raise LabError("Worker bundle differs from pinned harness digest")
        self.record["bundle_sha256"] = expected
        if retain_continuation:
            self.record["retain_continuation"] = True
        self.record["coordinator_credential_env"] = sorted(coordinator_environment or {})
        self.record["coordinator_redaction_sha256"] = sorted(
            {digest(value) for value in (coordinator_environment or {}).values()})
        self.export_secrets = list((coordinator_environment or {}).values())
        self.redactor.add((coordinator_environment or {}).values())
        self.save()
        try:
            async with asyncio.timeout(self.config.ttl_seconds):
                # The gateway must make provisioning idempotent by trial identity.
                await self.operation("provision")
                ready = await self.operation("ready", **(
                    {"model_provider": model_provider, "require_harness": True} if harness else {}))
                if harness and ready.get("harness") != harness:
                    raise LabError("Worker installed harness differs from the pinned implementation")
                if retain_continuation:
                    from .worker_checkpoint import PROTOCOL
                    if ready.get("checkpoint_protocol") != PROTOCOL:
                        raise LabError("Worker does not support private checkpoint transfer")
                await self.operation("seed", bundle=str(package),
                                     sha256=expected)
                if runtime is not None:
                    await self.deploy_runtime(runtime_path, runtime, ready)
                if checkpoint_owner:
                    await self.restore_checkpoint(checkpoint_owner)
                execution = await self.execute_job(ready, coordinator_environment,
                    before_start=(lambda: checkpoint_owner.consume(self.path.name)) if checkpoint_owner else None)
                self.record["status"] = "completed" if execution["exit_code"] == 0 else "failed"
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — always attempt cleanup
            self.record.update(status="failed", detail=str(error) or "Worker cancelled or TTL expired")
            await self.cancel()
        # Read-only collection remains available after execution expiry. Never delete
        # until either the export or an explicit collection failure is stored locally.
        await self.collect()
        await self.collect_continuation()
        await self.destroy()
        return self.redactor.clean(self.record)


async def reap(root, config, gateway=None, now=None, *, output="runs"):
    """Retry destruction after a coordinator crash; intended for a periodic control-plane job."""
    gateway = gateway or selected_gateway(root, config)
    now = time.time() if now is None else now
    results, failures = [], []
    for path in inside(Path(root).resolve(), output).glob("worker-*/worker.json"):
        record = load(path)
        if record["provider"] != config.provider \
                or record.get("gateway_sha256") != digest(config.model_dump()) \
                or record["cleanup"] == "confirmed" \
                or record["expires_at"] > now:
            continue
        if record["trial_id"] != path.parent.name:
            raise LabError("Worker journal trial identity does not match its directory")
        journal = WorkerJournal(config, gateway, path.parent, record)
        await journal.cancel()
        await journal.collect()
        await journal.collect_continuation()
        await journal.destroy()
        if record["cleanup"] == "confirmed":
            # Cleanup is not successful execution, nor proof that evidence survived.
            record["status"] = "expired-cleaned" if record["collection"] == "complete" \
                and record.get("continuation", {}).get("collection", "complete") == "complete" else "failed"
            journal.save()
            results.append(record["trial_id"])
        if record["cleanup"] != "confirmed" or record["collection"] != "complete" \
                or record.get("continuation", {}).get("collection", "complete") != "complete":
            failures.append(record["trial_id"])
    if failures:
        raise LabError(f"Worker recovery incomplete for {len(failures)} trial(s); inspect worker.json journals")
    return results
