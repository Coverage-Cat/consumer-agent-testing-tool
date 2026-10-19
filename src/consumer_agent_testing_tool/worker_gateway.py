"""Local-process reference for the cloud gateway protocol, not an isolation boundary."""
import base64
import fcntl
import hashlib
import io
import json
import math
import os
import platform
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def control(job):
    directory = job.parent / ".control"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (job.name + ".lock")).open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield stream.fileno()


class WorkerRunning(ValueError):
    """The kernel still reports a live owner of this job."""


def lease(job, name="active.lock"):
    descriptor = os.open(job / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def guard_source():
    source = globals().get("WORKER_GUARD_SOURCE")
    return source if source is not None else Path(__file__).with_name("worker_guard.py").read_text()


def runtime_identity():
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "P_PID")):
        raise ValueError("Worker supervision requires POSIX waitid/WNOWAIT support")
    return {"kind": "posix-process-group-lease-v1", "platform": sys.platform,
            "python": sys.version.split()[0],
            "guard_sha256": hashlib.sha256(guard_source().encode()).hexdigest(),
            "completion_grace_seconds": .1, "isolation": "none"}


def failed_launch(job, reason):
    for name in ("stdout.log", "stderr.log"):
        (job / name).touch()
    temporary = job / "exit.tmp"
    with temporary.open("w") as stream:
        json.dump({"trial_id": job.name, "exit_code": 2, "reason": reason,
                   "scope": "reference-process-group"}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(job / "exit.json")


def require_stopped(job):
    """A marker/elapsed deadline alone cannot establish that a guard has stopped."""
    if not (job / "running").exists() and not (job / "dispatched").exists():
        return
    descriptors = []
    try:
        try:
            for name in ("active.lock", "supervisor.lock"):
                descriptors.append(lease(job, name))
        except BlockingIOError as error:
            raise WorkerRunning("Worker is still running; terminal evidence is unavailable") from error
        if (job / "dispatched").exists() and not (job / "started").exists() \
                and not (job / "exit.json").exists():
            # The startup owner is gone, and no guard was ever launched. The
            # admission marker prevents a future start using this identity.
            failed_launch(job, "dispatch_lost")
        if not (job / "exit.json").is_file():
            raise ValueError("Worker termination is unconfirmed; retain it for investigation")
        outcome = json.loads((job / "exit.json").read_text())
        if outcome.get("trial_id") != job.name or type(outcome.get("exit_code")) is not int:
            raise ValueError("Worker terminal receipt is invalid")
        (job / "running").unlink(missing_ok=True)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def run_job(job, coordinator_environment=None, *, supervisor_fd=None):
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "P_PID")):
        raise ValueError("Worker supervision requires POSIX waitid/WNOWAIT support")
    code = guard_source()
    private = json.dumps(coordinator_environment or {}).encode()
    if len(private) > 16 * 1024:
        raise ValueError("Worker coordinator credential input exceeds 16 KiB")
    descriptor = alive_read = alive_write = finished_read = finished_write = None
    supervisor = supervisor_fd
    child = None
    try:
        with control(job):
            if (job.parent / ".control" / (job.name + ".retired")).exists():
                raise ValueError("Retired worker identity cannot execute again")
            if (job / "started").exists() or (supervisor_fd is None and (job / "dispatched").exists()):
                require_stopped(job)
                # Repeat RPCs retrieve the original result, even after expiry.
                return {"trial_id": job.name, **json.loads((job / "exit.json").read_text())}
            if (job / "cancelled").exists():
                raise ValueError("Cancelled worker cannot start")
            state = json.loads((job / "state.json").read_text())
            if time.time() >= state["expires_at"]:
                raise ValueError("Worker TTL expired")
            lock = json.loads((job / "bundle-lock.json").read_text())
            if coordinator_environment and (
                    lock.get("command") != ["consumer-agent-testing-tool-worker-run"]
                    or lock.get("coordinator", {}).get("format") != "consumer-agent-testing-tool-worker-run-v1"):
                raise ValueError("Credentials are only accepted by the pinned Consumer Agent Testing Tool coordinator")
            if supervisor is None:
                supervisor = lease(job, "supervisor.lock")
            descriptor = lease(job)
            alive_read, alive_write = os.pipe()
            finished_read, finished_write = os.pipe()
            (job / "started").touch()
            (job / "running").touch()
            env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
            child = subprocess.Popen([sys.executable, "-I", "-c", code, str(job),
                str(alive_read), str(descriptor), str(finished_write)], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                start_new_session=True, pass_fds=(alive_read, descriptor, finished_write))
            child.stdin.write(private)
            child.stdin.close()
            os.close(alive_read)
            alive_read = None
            os.close(descriptor)
            descriptor = None
            os.close(finished_write)
            finished_write = None
        # WNOWAIT preserves ownership of the group-leader PID until the fallback
        # kill is sent. Popen.poll()/wait() here would introduce PID-reuse risk.
        while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if select.select([finished_read], [], [], .025)[0] and os.read(finished_read, 1):
                break
    finally:
        try:
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    child.wait()
                if not (job / "exit.json").exists():
                    (job / "exit.json").write_text(json.dumps({
                        "trial_id": job.name, "exit_code": child.returncode,
                        "reason": "guard_lost", "scope": "reference-process-group"}))
        finally:
            if supervisor_fd is not None and child is None and not (job / "exit.json").exists():
                # A cancelled/expired/invalid asynchronous launch never began a
                # guard. Record that failure while the supervisor lease is held.
                failed_launch(job, "start_failed")
            for fd in (descriptor, supervisor, alive_read, alive_write, finished_read, finished_write):
                if fd is not None:
                    os.close(fd)
    require_stopped(job)
    return {"trial_id": job.name, **json.loads((job / "exit.json").read_text())}


def observe_job(job):
    """Read one admitted execution; never launch or infer success from expiry."""
    if (job.parent / ".control" / (job.name + ".retired")).exists():
        raise ValueError("Retired worker identity cannot execute again")
    if not (job / "state.json").is_file():
        raise ValueError("Worker is unavailable")
    if not (job / "started").exists() and not (job / "dispatched").exists():
        return {"trial_id": job.name, "run_state": "not_started"}
    try:
        require_stopped(job)
    except WorkerRunning:
        return {"trial_id": job.name, "run_state": "running"}
    return {**json.loads((job / "exit.json").read_text()), "trial_id": job.name,
            "run_state": "completed"}


def polling_job(job, mode, coordinator_environment=None):
    if mode not in ("start", "poll"):
        raise ValueError("Unknown worker run mode")
    if mode == "poll" and coordinator_environment:
        raise ValueError("Observation requests cannot supply coordinator credentials")
    with control(job) as control_fd:
        observed = observe_job(job)
        if mode == "poll" or observed["run_state"] != "not_started":
            return observed
        if threading.active_count() != 1:
            raise ValueError("Reference async dispatch requires a dedicated single-thread gateway process")
        if (job / "cancelled").exists():
            raise ValueError("Cancelled worker cannot start")
        if time.time() >= json.loads((job / "state.json").read_text())["expires_at"]:
            raise ValueError("Worker TTL expired")
        if len(json.dumps(coordinator_environment or {}).encode()) > 16 * 1024:
            raise ValueError("Worker coordinator credential input exceeds 16 KiB")
        if not (job / "bundle-lock.json").is_file():
            raise ValueError("Worker must be seeded before execution")
        supervisor = lease(job, "supervisor.lock")
        try:
            (job / "dispatched").touch()
            # The gateway is a dedicated, single-thread POSIX process. Forking
            # retains the same pinned code even for the source-only SSH layout.
            # The child owns supervision independently of this short RPC.
            pid = os.fork()
            if pid == 0:
                os.close(control_fd)  # Do not unlock the parent's shared lock.
                try:
                    os.setsid()
                    with open(os.devnull, "rb+") as stream:
                        for fd in (0, 1, 2):
                            os.dup2(stream.fileno(), fd)
                    (job / "supervisor.json").write_text(json.dumps({
                        "trial_id": job.name, "pid": os.getpid(), "mode": "poll-v1"}))
                    run_job(job, coordinator_environment, supervisor_fd=supervisor)
                finally:
                    os._exit(0)
        except BaseException:
            # Fork failed, so no process could have started. Admission remains
            # consumed; observation reports failure rather than launching again.
            failed_launch(job, "dispatch_failed")
            raise
        finally:
            os.close(supervisor)
    return {"trial_id": job.name, "run_state": "running"}


def cancel_job(job):
    with control(job):
        if job.exists():
            (job / "cancelled").touch()
    deadline = time.monotonic() + 10
    while True:
        try:
            with control(job):
                if job.exists():
                    require_stopped(job)
            return {"trial_id": job.name, "cancelled": True}
        except WorkerRunning:
            # Startup needs this same lock to observe cancellation and retire
            # its supervisor lease. Never hold the lock while waiting for it.
            if time.monotonic() >= deadline:
                raise
            time.sleep(.025)


def export_evidence(job, identifier, message=None):
    """Collect bounded terminal diagnostics, including after TTL or failed startup."""
    files, issues = {}, []
    if not job.exists() or job.is_symlink():
        return {"trial_id": identifier, "files": files, "complete": False,
                "issues": ["Worker directory is unavailable"]}
    require_stopped(job)
    snapshot = job.parent / ".control" / (identifier + ".export")
    if message and "archive_sha256" in message:
        metadata = json.loads((snapshot / "index.json").read_text())
        archive = snapshot / "runs.zip"
        offset = message.get("offset")
        if message["archive_sha256"] != metadata["sha256"] \
                or type(offset) is not int or not 0 <= offset < metadata["bytes"] \
                or offset % (128 * 1024):
            raise ValueError("Invalid worker archive chunk request")
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["sha256"]:
                raise ValueError("Worker run export changed during transfer")
            stream.seek(offset)
            data = stream.read(128 * 1024)
        return {"trial_id": identifier, "archive_sha256": metadata["sha256"],
                "offset": offset, "data_base64": base64.b64encode(data).decode()}
    paths = [job / name for name in ("stdout.log", "stderr.log", "exit.json")]
    remaining = 512 * 1024
    for item in paths:
        name = str(item.relative_to(job))
        try:
            # Nonblocking/no-follow avoids FIFOs and links made by the evaluated job.
            fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Not a regular file")
                limit = min(256 * 1024, remaining)
                content = stream.read(limit + 1)
                if len(content) > limit:
                    issues.append(f"Evidence truncated: {name}")
                content = content[:limit]
                remaining -= len(content)
                try:
                    files[name] = content.decode("utf-8")
                except UnicodeDecodeError:
                    files[name] = content.decode("utf-8", errors="replace")
                    issues.append(f"Invalid UTF-8 replaced: {name}")
        except (OSError, ValueError):
            issues.append(f"Evidence unavailable or unsafe: {name}")
    result = {"trial_id": identifier, "files": files, "issues": issues}
    if (job / "runs").exists() or (job / "runs").is_symlink():
        try:
            if not snapshot.exists():
                env = dict(os.environ)
                for index, key in enumerate((message or {}).get("redaction_env", [])):
                    if key in env:
                        env[f"SURFACE_WORKER_EXPORT_SECRET_{index}"] = env[key]
                for index, value in enumerate((message or {}).get("redaction_values", [])):
                    if not isinstance(value, str):
                        raise TypeError("Invalid export redaction input")
                    env[f"SURFACE_WORKER_EXPORT_SECRET_VALUE_{index}"] = value
                completed = subprocess.run(
                    [sys.executable, "-I", "-m", "consumer_agent_testing_tool.worker_export", str(job), str(snapshot)],
                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=60, check=False)
                if completed.returncode:
                    raise ValueError("Run export builder failed")
            result["run_archive"] = json.loads((snapshot / "index.json").read_text())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            issues.append("Complete redacted run export unavailable; inspect the installed harness and source evidence")
    result["complete"] = not issues
    return result


def handle(root, message):
    identifier = message["trial_id"]
    if not re.fullmatch(r"worker-[0-9a-f]{32}", identifier):
        raise ValueError("Invalid trial identity")
    root = root.resolve()
    job = root / identifier
    if message["operation"] == "run":
        if "mode" in message:
            return polling_job(job, message["mode"], message.get("coordinator_environment"))
        return run_job(job, message.get("coordinator_environment"))
    if message["operation"] == "cancel":
        return cancel_job(job)
    with control(job):
        return handle_control(root, job, identifier, message)


def handle_control(root, job, identifier, message):
    operation = message["operation"]
    if operation == "provision":
        if (root / ".control" / (identifier + ".retired")).exists():
            raise ValueError("Retired worker identity cannot be provisioned again")
        job.mkdir(parents=True, exist_ok=True)
        if not (job / "state.json").exists():
            expiry = message["expires_at"]
            remaining = expiry - time.time() if type(expiry) in (int, float) else float("nan")
            if not math.isfinite(remaining) or not 0 < remaining <= 3600:
                raise ValueError("Worker expiry must be a finite future deadline within one hour")
            (job / "state.json").write_text(json.dumps({
                "expires_at": expiry, "deadline_monotonic": time.monotonic() + remaining}))
        return {"trial_id": identifier}
    if operation == "destroy":
        if job.exists():
            require_stopped(job)
            (root / ".control" / (identifier + ".retired")).touch()
            shutil.rmtree(job)
        snapshot = root / ".control" / (identifier + ".export")
        if snapshot.exists():
            shutil.rmtree(snapshot)
        for temporary in snapshot.parent.glob(snapshot.name + ".tmp-*"):
            shutil.rmtree(temporary)
        checkpoints = root / ".control" / (identifier + ".checkpoints")
        if checkpoints.exists():
            shutil.rmtree(checkpoints)
        for temporary in checkpoints.parent.glob(checkpoints.name + ".tmp-*"):
            shutil.rmtree(temporary)
        return {"trial_id": identifier, "destroyed": True}
    # Expiry forbids further execution, not collection of completed diagnostics.
    if operation == "export":
        return export_evidence(job, identifier, message)
    if operation == "checkpoint" and message.get("mode") in ("list", "read"):
        from consumer_agent_testing_tool.worker_checkpoint import export_chunk, prepare
        require_stopped(job)
        snapshot = root / ".control" / (identifier + ".checkpoints")
        if message["mode"] == "read":
            return export_chunk(snapshot, message)
        checkpoints = prepare(job, snapshot)
        return {"trial_id": identifier, "complete": True, "checkpoints": checkpoints}
    state = json.loads((job / "state.json").read_text())
    if time.time() >= state["expires_at"]:
        raise ValueError("Worker TTL expired")
    if operation == "ready":
        result = {"ready": True, "trial_id": identifier, "execution": "local-process-reference",
                  "runtime": runtime_identity(), "run_protocol": "poll-v1"}
        if message.get("require_harness"):
            command = [sys.executable, "-I", "-m", "consumer_agent_testing_tool.worker_run", "--identity"]
            if provider := message.get("model_provider"):
                command.append(provider)
            probe = subprocess.run(command, capture_output=True, check=False, timeout=30)
            if probe.returncode:
                raise ValueError("Pinned Consumer Agent Testing Tool worker installation is unavailable")
            result["harness"] = json.loads(probe.stdout)
            result["runtime_protocol"] = "runtime-chunks-v1"
            result["runtime_platform"] = {"system": platform.system(), "machine": platform.machine()}
            result["checkpoint_protocol"] = "checkpoint-chunks-v1"
        return result
    if operation == "runtime":
        from consumer_agent_testing_tool.worker_assets import receive
        return receive(job, message)
    if operation == "checkpoint":
        from consumer_agent_testing_tool.worker_checkpoint import receive
        return receive(job, message)
    if operation == "seed":
        if any((job / name).exists() for name in ("started", "dispatched", "cancelled")):
            raise ValueError("Started or cancelled worker cannot be reseeded")
        if "bundle_base64" in message:
            package = base64.b64decode(message["bundle_base64"], validate=True)
        else:
            package = Path(message["bundle"]).resolve().read_bytes()
        if hashlib.sha256(package).hexdigest() != message["sha256"]:
            raise ValueError("Bundle integrity failed")
        contents = {}
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            lock = json.loads(archive.read("bundle-lock.json"))
            for name, expected in lock["files"].items():
                if Path(name).parts[0] in {
                        "state.json", "bundle-lock.json", "active.lock", "supervisor.lock", "guard.json",
                        "running", "started", "cancelled", "exit.json", "stdout.log", "stderr.log",
                        "dispatched", "supervisor.json",
                        ".runtime", ".runtime-upload", ".runtime-stage",
                        ".checkpoint-upload", ".checkpoint-stage", ".checkpoint-receipt.json",
                        "input.zip", "seed-attempted", "bundle-lock.tmp", "exit.tmp", "guard.tmp"}:
                    raise ValueError("Worker bundle member conflicts with private lifecycle state")
                destination = (job / name).resolve()
                if not destination.is_relative_to(job) or sum(i.file_size for i in archive.infolist()) > 64*1024*1024:
                    raise ValueError("Unsafe or oversized worker archive")
                content = archive.read(name)
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError("Bundle member integrity failed")
                contents[destination] = content
        if (job / "bundle-lock.json").exists():
            if json.loads((job / "bundle-lock.json").read_text()).get("bundle_sha256") != message["sha256"]:
                raise ValueError("Worker is already seeded with another pinned bundle")
            return {"seeded": True, "trial_id": identifier}
        if (job / "seed-attempted").exists():
            raise ValueError("Prior seed commit is unconfirmed; use a new trial")
        (job / "seed-attempted").touch()
        for destination, content in contents.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        lock["bundle_sha256"] = message["sha256"]
        temporary = job / "bundle-lock.tmp"
        with temporary.open("w") as stream:
            json.dump(lock, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(job / "bundle-lock.json")
        return {"seeded": True, "trial_id": identifier}
    raise ValueError("Unknown lifecycle operation")


if __name__ == "__main__":
    try:
        print(json.dumps(handle(Path(sys.argv[1]), json.load(sys.stdin))))
    except Exception as error:  # noqa: BLE001 — gateway protocol error response
        print(json.dumps({"error": str(error)}))
