"""Complete public run exports using the existing evidence/redaction builders."""
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

from .errors import LabError
from .evidence import export_redacted
from .replay import evidence_path, export_public_run, playback
from .serialization import file_digest, load

FORMAT = "consumer-agent-testing-tool-worker-runs-v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 4096
CHUNK_BYTES = 128 * 1024


def identity():
    return {name: file_digest(Path(__file__).with_name(name)) for name in (
        "worker_export.py", "evidence.py", "replay.py", "browser_segments.py")}


def bounded_files(root):
    """Check file types and bounds before any evidence builder reads the tree."""
    pending, names, total, entries = [root], [], 0, 0
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or not directory.is_dir():
            raise LabError("Unsafe worker evidence directory")
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise LabError("Worker run evidence entry limit exceeded")
                mode = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(mode.st_mode):
                    pending.append(Path(child.path))
                elif stat.S_ISREG(mode.st_mode):
                    total += mode.st_size
                    if total > MAX_BYTES:
                        raise LabError("Worker run evidence size limit exceeded")
                    names.append(Path(child.path).relative_to(root).as_posix())
                else:
                    raise LabError("Unsafe worker evidence entry")
    return sorted(names)


def run_files(root):
    """Verify linked run evidence, preserving recorded execution/reporting outcomes."""
    from .browser_segments import references, verify

    if evidence_path(root, "result.json").is_file():
        recorded = playback(root)
        names = set(load(root / "evidence.sha256.json")) | {"evidence.sha256.json"}
        result = recorded["result"]
        agents = set(result.get("agent_runs", []))
        if result.get("agent_evidence"):
            agents.add(result["agent_evidence"])
        for name in agents:
            names.update(f"{name}/{child}" for child in run_files(evidence_path(root, name)))
        return names
    manifest = load(evidence_path(root, "manifest.json"))
    if not isinstance(manifest.get("run_lock"), dict) or not manifest.get("run_id") \
            or not manifest.get("lane") or not manifest.get("evidence_label"):
        raise LabError("Worker run manifest lacks its lock or fidelity identity")
    names = {"manifest.json", "events.jsonl"}
    if evidence_path(root, "report.html").is_file():
        names.add("report.html")
    elif manifest.get("reporting_status") == "completed":
        raise LabError("Worker run report is missing")

    def reference(row):
        if not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256"))):
            raise LabError("Invalid worker linked artifact digest")
        name = row["path"]
        path = evidence_path(root, name)
        if not path.is_file() or file_digest(path) != row["sha256"]:
            raise LabError("Worker run linked artifact digest mismatch")
        names.add(name)

    if notices := manifest.get("dependency_notices"):
        if notices["path"] != f'artifacts/{notices["sha256"]}.zip':
            raise LabError("Invalid worker dependency notice path")
        reference(notices)
    logs = manifest.get("inspect_log_artifacts", [])
    if manifest.get("inspect_logs", []) != [item["path"] for item in logs]:
        raise LabError("Worker Inspect log references are incomplete")
    for item in logs:
        reference(item)
    events = evidence_path(root, "events.jsonl")
    for index, line in enumerate(events.read_text().splitlines(), 1):
        event = json.loads(line)
        if event.get("run_id") != manifest["run_id"] \
                or event.get("event_id") != f'{manifest["run_id"]}:{index}':
            raise LabError("Worker run event identity or ordering is invalid")
        for item in event["artifacts"]:
            if item["path"] != f'artifacts/{item["sha256"]}.json':
                raise LabError("Invalid worker event artifact path")
            reference(item)
    for segment in references(manifest):
        child = verify(root, segment)
        names.update(f'{segment["path"]}/{name}' for name in run_files(child))
    return names


def prepare(job, destination):
    """Atomically publish a redacted snapshot; private traces never enter the archive."""
    runs = job / "runs"
    bounded_files(runs)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=destination.name + ".tmp-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "snapshot"
        public = staged / "public"
        public.mkdir(parents=True, mode=0o700)
        kinds = {}
        for source in sorted(runs.iterdir()):
            if not source.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source.name):
                raise LabError("Unrecognized worker run directory")
            run_files(source)
            target = public / "runs" / source.name
            if (source / "result.json").is_file():
                export_public_run(source, target)
                kinds[f"runs/{source.name}"] = "sealed-surface"
            else:
                export_redacted(source, target)
                kinds[f"runs/{source.name}"] = "core-evidence"
            if set(bounded_files(target)) != run_files(target):
                raise LabError("Worker run export contains unlinked or missing evidence")
        inventory = {name: {"sha256": file_digest(public / name),
                            "bytes": (public / name).stat().st_size}
                     for name in bounded_files(public)}
        archive_path = staged / "runs.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in inventory:
                archive.write(public / name, name)
        if archive_path.stat().st_size > MAX_BYTES:
            raise LabError("Worker run archive size limit exceeded")
        metadata = {"format": FORMAT, "sha256": file_digest(archive_path),
                    "bytes": archive_path.stat().st_size, "files": inventory,
                    "runs": kinds, "exporter": identity()}
        (staged / "index.json").write_text(json.dumps(metadata))
        # All contents are already redacted; keep one transport copy only.
        import shutil
        shutil.rmtree(public)
        staged.rename(destination)
    return metadata


def validate_metadata(metadata):
    if not isinstance(metadata, dict) or metadata.get("format") != FORMAT \
            or metadata.get("exporter") != identity():
        raise LabError("Worker run exporter differs from the pinned local implementation")
    if type(metadata.get("bytes")) is not int or not 0 < metadata["bytes"] <= MAX_BYTES \
            or not re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("sha256"))):
        raise LabError("Invalid worker run archive identity or size")
    rows, runs = metadata.get("files"), metadata.get("runs")
    if not isinstance(rows, dict) or len(rows) > MAX_ENTRIES or not isinstance(runs, dict):
        raise LabError("Invalid worker run archive inventory")
    for name, kind in runs.items():
        if not re.fullmatch(r"runs/[A-Za-z0-9_-]{1,128}", name) \
                or kind not in ("core-evidence", "sealed-surface"):
            raise LabError("Invalid worker run root")
    total = 0
    for name, row in rows.items():
        if not isinstance(name, str) or "\\" in name or Path(name).as_posix() != name:
            raise LabError("Invalid worker run archive path")
        evidence_path(Path("."), name)
        if not any(name.startswith(run + "/") for run in runs) \
                or not isinstance(row, dict) or type(row.get("bytes")) is not int \
                or row["bytes"] < 0 or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256"))):
            raise LabError("Invalid worker run inventory entry")
        total += row["bytes"]
    if total > MAX_BYTES:
        raise LabError("Worker run inventory size limit exceeded")


def verify_saved(root, metadata):
    validate_metadata(metadata)
    if set(bounded_files(root)) != set(metadata["files"]):
        raise LabError("Saved worker run evidence inventory mismatch")
    for name, row in metadata["files"].items():
        item = evidence_path(root, name)
        if item.stat().st_size != row["bytes"] or file_digest(item) != row["sha256"]:
            raise LabError("Saved worker run evidence digest mismatch")
    linked = set()
    for name, kind in metadata["runs"].items():
        run = evidence_path(root, name)
        if (run / "result.json").is_file() != (kind == "sealed-surface"):
            raise LabError("Worker run evidence format mismatch")
        linked.update(f"{name}/{child}" for child in run_files(run))
    if linked != set(metadata["files"]):
        raise LabError("Worker run archive has missing or unlinked artifacts")


def unpack(archive_path, destination, metadata):
    validate_metadata(metadata)
    if archive_path.stat().st_size != metadata["bytes"] \
            or file_digest(archive_path) != metadata["sha256"]:
        raise LabError("Worker run archive integrity failed")
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) != len(metadata["files"]) \
                or {item.filename for item in members} != set(metadata["files"]):
            raise LabError("Worker run archive inventory mismatch")
        destination.mkdir(mode=0o700)
        for item in members:
            expected = metadata["files"][item.filename]
            if item.file_size != expected["bytes"] or item.compress_type != zipfile.ZIP_STORED \
                    or stat.S_ISLNK(item.external_attr >> 16):
                raise LabError("Invalid worker run archive member")
            path = evidence_path(destination, item.filename)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(item) as source, path.open("xb") as target:
                while content := source.read(CHUNK_BYTES):
                    target.write(content)
            path.chmod(0o600)
    verify_saved(destination, metadata)


if __name__ == "__main__":
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
