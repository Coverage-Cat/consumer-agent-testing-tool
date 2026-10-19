"""Private, opt-in transfer of existing Inspect scenario checkpoints.

These archives are never public evidence. The reference gateway must hold its
control lock and prove the job stopped before export. No archive member is
executed; the installed coordinator retains ownership of continuation.
"""
import base64
import fcntl
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .errors import LabError
from .serialization import digest, file_digest, load, loads
from .surface import atomic_json
from .worker_export import CHUNK_BYTES, MAX_BYTES, MAX_ENTRIES, bounded_files, run_files

FORMAT = "consumer-agent-testing-tool-worker-checkpoint-v1"
PROTOCOL = "checkpoint-chunks-v1"
RUN_ID = r"r-[a-f0-9]{32}"
WORKER_ID = r"worker-[a-f0-9]{32}"
SHA = r"[a-f0-9]{64}"
RESERVED = {".checkpoint-upload", ".checkpoint-stage", ".checkpoint-receipt.json"}


def read_record(path):
    """Reject special files before reading private transfer control records."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise LabError("Invalid private checkpoint control record")
            content = stream.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise LabError("Oversized private checkpoint control record")
        value = loads(content.decode(), json_only=True)
        if not isinstance(value, dict):
            raise LabError("Private checkpoint control record must be an object")
        return value
    except (OSError, UnicodeError) as error:
        raise LabError("Cannot read private checkpoint control record") from error


def verify_archive(path, metadata):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != metadata["bytes"] \
            or file_digest(path) != metadata["sha256"]:
        raise LabError("Private checkpoint archive integrity failed")


def validate_metadata(metadata):
    if not isinstance(metadata, dict) or set(metadata) != {
            "format", "source_trial", "run_id", "sha256", "bytes", "files"} \
            or metadata["format"] != FORMAT \
            or not re.fullmatch(WORKER_ID, str(metadata["source_trial"])) \
            or not re.fullmatch(RUN_ID, str(metadata["run_id"])) \
            or not re.fullmatch(SHA, str(metadata["sha256"])) \
            or type(metadata["bytes"]) is not int or not 0 < metadata["bytes"] <= MAX_BYTES:
        raise LabError("Invalid private checkpoint identity or size")
    rows = metadata["files"]
    if not isinstance(rows, dict) or not 0 < len(rows) <= MAX_ENTRIES:
        raise LabError("Invalid private checkpoint inventory")
    total, folded = 0, set()
    for name, row in rows.items():
        if not isinstance(name, str) or len(name.encode()) > 512 \
                or "\\" in name or "\0" in name or PurePosixPath(name).as_posix() != name \
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts \
                or not name.startswith(metadata["run_id"] + "/") \
                or name.casefold() in folded:
            raise LabError("Unsafe private checkpoint path")
        folded.add(name.casefold())
        if not isinstance(row, dict) or set(row) != {"bytes", "sha256"} \
                or type(row["bytes"]) is not int or row["bytes"] < 0 \
                or not re.fullmatch(SHA, str(row["sha256"])):
            raise LabError("Invalid private checkpoint file identity")
        total += row["bytes"]
    if total > MAX_BYTES or any(parent.as_posix().casefold() in folded
                               for name in rows for parent in PurePosixPath(name).parents):
        raise LabError("Oversized or conflicting private checkpoint inventory")


def verify_tree(root, metadata):
    validate_metadata(metadata)
    if set(bounded_files(root)) != set(metadata["files"]):
        raise LabError("Private checkpoint inventory changed")
    for name, row in metadata["files"].items():
        path = root / name
        if path.stat().st_size != row["bytes"] or file_digest(path) != row["sha256"]:
            raise LabError("Private checkpoint contents changed")
    run = root / metadata["run_id"]
    manifest = load(run / "manifest.json")
    if manifest.get("run_id") != run.name or manifest.get("resumable") is not True \
            or not (run / "private/trial.private.json").is_file():
        raise LabError("Private archive lacks its original retained scenario")
    run_files(run)  # Verify referenced public artifacts and all browser segments too.


def archive_run(run, destination, source_trial):
    names = bounded_files(run)
    metadata = {"format": FORMAT, "source_trial": source_trial, "run_id": run.name,
                "files": {f"{run.name}/{name}": {
                    "sha256": file_digest(run / name), "bytes": (run / name).stat().st_size}
                    for name in names}}
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as archive:
        for name in names:
            info = zipfile.ZipInfo(f"{run.name}/{name}")
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            with (run / name).open("rb") as source, archive.open(info, "w") as target:
                shutil.copyfileobj(source, target, CHUNK_BYTES)
    destination.chmod(0o600)
    metadata.update(sha256=file_digest(destination), bytes=destination.stat().st_size)
    validate_metadata(metadata)
    return metadata


def unpack(archive_path, destination, metadata):
    validate_metadata(metadata)
    verify_archive(archive_path, metadata)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) != len(metadata["files"]) \
                or {item.filename for item in members} != set(metadata["files"]):
            raise LabError("Private checkpoint archive inventory mismatch")
        destination.mkdir(mode=0o700)
        for item in members:
            if item.file_size != metadata["files"][item.filename]["bytes"] \
                    or item.compress_type != zipfile.ZIP_STORED or item.flag_bits & 1 \
                    or not stat.S_ISREG(item.external_attr >> 16):
                raise LabError("Unsafe private checkpoint archive member")
            path = destination / item.filename
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(item) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target, CHUNK_BYTES)
                target.flush()
                os.fsync(target.fileno())
            path.chmod(0o600)
    verify_tree(destination, metadata)


def retention_requested(job):
    document = read_record(job / "invocation.worker.json")
    lock = read_record(job / "bundle-lock.json")
    if document.get("retain_continuation") is not True or lock.get("coordinator") != {
            "format": "consumer-agent-testing-tool-worker-run-v1", "invocation_sha256": digest(document)}:
        raise LabError("Private checkpoint collection was not explicitly requested")
    return document


def prepare(job, destination):
    retention_requested(job)
    if destination.exists():
        return read_record(destination / "index.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=destination.name + ".tmp-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "snapshot"
        staged.mkdir(mode=0o700)
        checkpoints, total = {}, 0
        runs = job / "runs"
        if runs.exists() or runs.is_symlink():
            bounded_files(runs)
            for run in sorted(runs.iterdir()):
                if not (run / "private/trial.private.json").exists():
                    continue
                if not re.fullmatch(RUN_ID, run.name):
                    raise LabError("Unrecognized private checkpoint run identity")
                metadata = archive_run(run, staged / f"{run.name}.zip", job.name)
                # Validate the actual transport bytes before publishing a snapshot.
                with tempfile.TemporaryDirectory(dir=staged) as check:
                    unpack(staged / f"{run.name}.zip", Path(check) / "run", metadata)
                checkpoints[run.name] = metadata
                total += metadata["bytes"]
                if total > MAX_BYTES:
                    raise LabError("Private checkpoint collection exceeds transfer limit")
        atomic_json(staged / "index.json", checkpoints)
        staged.rename(destination)
    return checkpoints


def export_chunk(snapshot, message):
    metadata = read_record(snapshot / "index.json").get(message.get("run_id"))
    validate_metadata(metadata)
    offset = message.get("offset")
    if message.get("sha256") != metadata["sha256"] or type(offset) is not int \
            or not 0 <= offset < metadata["bytes"] or offset % CHUNK_BYTES:
        raise LabError("Invalid private checkpoint chunk request")
    archive = snapshot / f"{metadata['run_id']}.zip"
    verify_archive(archive, metadata)
    with archive.open("rb") as stream:
        stream.seek(offset)
        content = stream.read(CHUNK_BYTES)
    return {"trial_id": metadata["source_trial"], "run_id": metadata["run_id"],
            "sha256": metadata["sha256"], "offset": offset,
            "data_base64": base64.b64encode(content).decode()}


def verify_received(job, metadata):
    receipt = job / ".checkpoint-receipt.json"
    if read_record(receipt) != metadata:
        raise LabError("Restored checkpoint receipt differs from the pinned invocation")
    verify_tree(job / "runs", metadata)


def receive(job, message):
    if any((job / name).exists() for name in ("started", "dispatched", "cancelled")):
        raise LabError("Started or cancelled worker cannot restore a checkpoint")
    metadata = read_record(job / "bundle-lock.json").get("checkpoint")
    validate_metadata(metadata)
    if message.get("sha256") != metadata["sha256"]:
        raise LabError("Checkpoint upload differs from the pinned invocation")
    upload = job / ".checkpoint-upload"
    stage = job / ".checkpoint-stage"
    if any((job / name).is_symlink() for name in (*RESERVED, "runs")):
        raise LabError("Linked checkpoint staging paths are forbidden")
    if upload.exists() and not stat.S_ISREG(upload.lstat().st_mode):
        raise LabError("Invalid private checkpoint upload file")
    if message.get("mode") == "commit":
        if (job / ".checkpoint-receipt.json").exists():
            verify_received(job, metadata)
        else:
            if (job / "runs").exists():
                # Recover a commit whose process exited after the atomic rename
                # but before its receipt. Only the exact pinned tree can qualify.
                verify_tree(job / "runs", metadata)
            else:
                if upload.stat().st_size != metadata["bytes"]:
                    raise LabError("Incomplete private checkpoint transfer")
                if stage.exists():
                    shutil.rmtree(stage)
                unpack(upload, stage, metadata)
                stage.rename(job / "runs")
            atomic_json(job / ".checkpoint-receipt.json", metadata)
            upload.unlink(missing_ok=True)
        return {"trial_id": job.name, "sha256": metadata["sha256"], "installed": True}
    if message.get("mode") != "chunk":
        raise LabError("Unknown checkpoint restore operation")
    if (job / ".checkpoint-receipt.json").exists():
        raise LabError("Private checkpoint was already installed")
    offset = message.get("offset")
    if type(offset) is not int or not 0 <= offset < metadata["bytes"] or offset % CHUNK_BYTES:
        raise LabError("Invalid private checkpoint upload offset")
    encoded = message.get("data_base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK_BYTES + 2) // 3):
        raise LabError("Invalid private checkpoint upload size")
    content = base64.b64decode(encoded, validate=True)
    if len(content) != min(CHUNK_BYTES, metadata["bytes"] - offset):
        raise LabError("Private checkpoint chunk size mismatch")
    with upload.open("r+b" if upload.exists() else "x+b") as stream:
        upload.chmod(0o600)
        size = stream.seek(0, os.SEEK_END)
        if size == offset:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        elif size >= offset + len(content):
            stream.seek(offset)
            if stream.read(len(content)) != content:
                raise LabError("Conflicting private checkpoint retry")
        else:
            raise LabError("Private checkpoint chunks must be contiguous")
    return {"trial_id": job.name, "sha256": metadata["sha256"], "offset": offset,
            "accepted_bytes": len(content), "installed": False}


def verify_capsule(capsule, metadata):
    if capsule.is_symlink() or any(path.is_symlink() for path in capsule.rglob("*")):
        raise LabError("Linked private checkpoint capsules are forbidden")
    if read_record(capsule / "receipt.json") != metadata:
        raise LabError("Private checkpoint capsule receipt changed")
    archive = capsule / "checkpoint.zip"
    verify_archive(archive, metadata)
    verify_tree(capsule / "run", metadata)


class CapsuleOwner:
    """Serialize continuation admission; a lost start reply cannot reuse a snapshot."""
    def __init__(self, capsule, *, root):
        self.path = Path(capsule)
        self.registry_owner = None
        if self.path.is_symlink() or (self.path / "claim.lock").is_symlink():
            raise LabError("Linked private checkpoint ownership paths are forbidden")
        self.owner = os.fdopen(os.open(self.path / "claim.lock",
                                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "w")
        try:
            if not stat.S_ISREG(os.fstat(self.owner.fileno()).st_mode):
                raise LabError("Invalid private checkpoint ownership lock")
            try:
                fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LabError("Private checkpoint already has an active continuation owner") from error
            self.metadata = read_record(self.path / "receipt.json")
            validate_metadata(self.metadata)
            verify_capsule(self.path, self.metadata)
            journal = read_record(self.path.parent.parent / "worker.json")
            if self.path.name != self.metadata["run_id"] \
                    or self.path.parent.parent.name != self.metadata["source_trial"] \
                    or journal["trial_id"] != self.metadata["source_trial"] \
                    or journal.get("cleanup") != "confirmed" \
                    or journal.get("continuation", {}).get("checkpoints", {}).get(
                        self.metadata["run_id"]) != digest(self.metadata):
                raise LabError("Private checkpoint is not bound to its collecting worker")
            # The admission record belongs to the workspace, so restoring an
            # earlier copy of a capsule cannot reset its consumed authority.
            registry = Path(root).resolve() / ".consumer-agent-testing-tool-worker-claims"
            if registry.is_symlink():
                raise LabError("Linked continuation admission storage is forbidden")
            registry.mkdir(mode=0o700, exist_ok=True)
            registry.chmod(0o700)
            key = digest(self.metadata)
            self.claim = registry / f"{key}.json"
            lock = registry / f"{key}.lock"
            self.registry_owner = os.fdopen(os.open(
                lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "r+")
            if not stat.S_ISREG(os.fstat(self.registry_owner.fileno()).st_mode):
                raise LabError("Invalid continuation admission lock")
            try:
                fcntl.flock(self.registry_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LabError("Private checkpoint already has an active continuation owner") from error
            if self.claim.exists() or self.claim.is_symlink() or (self.path / "claim.json").exists():
                raise LabError("Checkpoint was already admitted; use the subsequent worker's checkpoint")
            self.run = self.path / "run" / self.metadata["run_id"]
            state = load(self.run / "private/trial.private.json")
            if state.get("status") == "completed" or state.get("resume_blocked_reason"):
                raise LabError("This retained trial cannot be continued")
            if type(state.get("deadline")) not in (int, float) or not math.isfinite(state["deadline"]) \
                    or state["deadline"] <= time.time():
                raise LabError("Original scenario deadline has expired", code=3, category="interaction")
        except BaseException:
            self.close()
            raise

    def consume(self, trial_id):
        if not re.fullmatch(WORKER_ID, trial_id):
            raise LabError("Invalid continuation admission identity")
        if self.claim.exists() or self.claim.is_symlink() or (self.path / "claim.json").exists():
            raise LabError("Checkpoint continuation admission was already consumed")
        receipt = {
            "trial_id": trial_id, "checkpoint_sha256": self.metadata["sha256"],
            "status": "admitted-or-uncertain"}
        # Even a process exit during this write leaves the authority consumed.
        # Never infer permission to retry from a partial admission receipt.
        with os.fdopen(os.open(self.claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_json(self.path / "claim.json", receipt)

    def validate_selection(self, scenario, target, model, profile, configuration, browser_worker):
        from .scenario_session import reopen_evidence, verify_execution
        evidence = reopen_evidence(self.run, profile)
        verify_execution(evidence, configuration)
        if evidence.manifest["run_lock"].get("decision_contract"):
            raise LabError("A preregistered fixed-sample trial cannot be resumed")
        if target.kind == "api":
            from .mapped_scenario import continuation_contract
        elif scenario["surface"] == "browser":
            from functools import partial

            from .browser_scenario import continuation_contract
            continuation_contract = partial(continuation_contract, worker=browser_worker)
        else:
            from .api_scenario import continuation_contract
        contract = continuation_contract(target, model, evidence)
        state = load(self.run / "private/trial.private.json")
        if state.get("contract_sha256") != digest(contract) \
                or state.get("scenario_sha256") != digest(scenario):
            raise LabError("Scenario, model, target or profile changed; cannot resume the worker trial")

    def close(self):
        self.owner.close()
        if self.registry_owner:
            self.registry_owner.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
