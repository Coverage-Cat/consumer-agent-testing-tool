"""Explicit, pinned runtime trees. No downloads, package scripts or shared cache."""
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

FORMAT = "consumer-agent-testing-tool-worker-runtime-v1"
PROTOCOL = "runtime-chunks-v1"
CHUNK = 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_FILES = 32768
MAX_LOCK = 8 * 1024 * 1024
ROLES = {"tooling", "browsers", "node"}
LOCK = "runtime-lock.json"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity():
    return {"system": platform.system(), "machine": platform.machine()}


def relative(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name \
            or str(PurePosixPath(name)) != name or PurePosixPath(name).is_absolute() \
            or any(part in (".", "..") for part in name.split("/")):
        raise ValueError("Runtime member must be a canonical relative path")
    return name


def checked_lock(value):
    if not isinstance(value, dict) or set(value) != {"format", "platform", "files", "entrypoints"} \
            or value["format"] != FORMAT or not isinstance(value["platform"], dict) \
            or set(value["platform"]) != {"system", "machine"} \
            or any(not isinstance(v, str) or not v for v in value["platform"].values()):
        raise ValueError("Invalid runtime manifest")
    records = value["files"]
    if not isinstance(records, dict) or not 0 < len(records) <= MAX_FILES:
        raise ValueError("Runtime file count exceeds limit or is empty")
    names = set()
    total = 0
    for name, item in records.items():
        relative(name)
        parts = name.split("/")
        if parts[0] not in ROLES or (parts[0] == "node") != (len(parts) == 1):
            raise ValueError("Runtime files must belong to a declared role")
        if name.casefold() in names:
            raise ValueError("Runtime paths collide")
        names.add(name.casefold())
        if isinstance(item, dict) and set(item) == {"link"}:
            relative(item["link"])
            continue
        if not isinstance(item, dict) or set(item) != {"sha256", "bytes", "mode"} \
                or not isinstance(item["sha256"], str) \
                or not re.fullmatch("[a-f0-9]{64}", item["sha256"]) \
                or type(item["bytes"]) is not int or item["bytes"] < 0 \
                or type(item["mode"]) is not int or item["mode"] not in (0o644, 0o755):
            raise ValueError("Invalid runtime file record")
        total += item["bytes"]
    if total > MAX_EXPANDED:
        raise ValueError("Expanded runtime exceeds 1 GiB")
    for name in names:
        if any("/".join(name.split("/")[:i]) in names for i in range(1, len(name.split("/")))):
            raise ValueError("Runtime file conflicts with a parent directory")
    regular = {name for name, item in records.items() if "link" not in item}
    directories = {"/".join(name.split("/")[:i]) for name in regular
                   for i in range(1, len(name.split("/")))}
    for name, item in records.items():
        if "link" in item and item["link"] not in regular | directories:
            raise ValueError("Runtime link must target a declared regular file or directory")
        if "link" in item and name.startswith(item["link"] + "/"):
            raise ValueError("Runtime directory link forms a cycle")
    entrypoints = value["entrypoints"]
    if "node" in records and records["node"].get("mode") != 0o755:
        raise ValueError("Node runtime must be executable")
    if not isinstance(entrypoints, dict) or set(entrypoints) - {"chromium"}:
        raise ValueError("Unknown runtime entrypoint")
    for name in entrypoints.values():
        relative(name)
        if not name.startswith("browsers/") or name not in records or records[name].get("mode") != 0o755:
            raise ValueError("Chromium entrypoint must be an executable in the pinned browser tree")
    return value


def inspect_archive(path, expected=None, *, destination=None, secrets=()):
    """Validate every byte before admission; optionally extract into an empty private tree."""
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_ARCHIVE:
        raise ValueError("Runtime archive exceeds 512 MiB or is empty")
    checksum = sha(path)
    if expected is not None and checksum != expected:
        raise ValueError("Runtime archive differs from its explicit pin")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES + 1 or len({m.filename for m in members}) != len(members):
            raise ValueError("Runtime archive contains duplicate or excessive entries")
        if archive.getinfo(LOCK).file_size > MAX_LOCK:
            raise ValueError("Runtime manifest exceeds limit")
        lock_bytes = archive.read(LOCK)
        lock = checked_lock(json.loads(lock_bytes))
        if set(archive.namelist()) != {LOCK, *lock["files"]}:
            raise ValueError("Runtime archive has undeclared or missing files")
        needles = [value.encode() for value in secrets if value]
        if any(needle in lock_bytes for needle in needles):
            raise ValueError("Runtime manifest contains a selected credential value")
        overlap = max([len(value) - 1 for value in needles] or [0])
        for name, item in lock["files"].items():
            info = archive.getinfo(name)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if "link" in item:
                encoded = item["link"].encode()
                if kind != stat.S_IFLNK or info.file_size != len(encoded) or archive.read(info) != encoded:
                    raise ValueError("Runtime link differs from its declared target")
                continue
            if info.file_size != item["bytes"] or kind not in (0, stat.S_IFREG) \
                    or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError("Invalid runtime archive member")
            output = None
            try:
                if destination is not None:
                    target = Path(destination) / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    output = target.open("xb")
                with archive.open(info) as source:
                    count, checksum_file, tail = 0, hashlib.sha256(), b""
                    while content := source.read(CHUNK):
                        count += len(content)
                        if count > item["bytes"]:
                            raise ValueError("Runtime member exceeds declared size")
                        sample = tail + content
                        if any(needle in sample for needle in needles):
                            raise ValueError("Runtime input contains a selected credential value")
                        tail = sample[-overlap:] if overlap else b""
                        checksum_file.update(content)
                        if output:
                            output.write(content)
                if count != item["bytes"] or checksum_file.hexdigest() != item["sha256"]:
                    raise ValueError("Runtime member integrity failed")
                if output:
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), item["mode"])
            finally:
                if output:
                    output.close()
    if destination is not None:
        for name, item in lock["files"].items():
            if "link" in item:
                target = Path(destination) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(os.path.relpath(Path(destination) / item["link"], target.parent))
        (Path(destination) / LOCK).write_bytes(lock_bytes)
    return {"format": FORMAT, "sha256": checksum, "bytes": size, "platform": lock["platform"],
            "files": len(lock["files"]), "lock_sha256": hashlib.sha256(lock_bytes).hexdigest()}


def pack(root, manifest, destination):
    """Package explicit workspace trees, including contained links and execute bits."""
    root = Path(root).resolve()
    from .surface import inside
    specification = json.loads(inside(root, manifest).read_text())
    if set(specification) - {"platform", "paths", "entrypoints"} or "platform" not in specification \
            or not isinstance(specification.get("paths"), dict) \
            or not specification["paths"]:
        raise ValueError("Declare runtime platform and explicit tooling, browsers or node paths")
    for name in specification["paths"]:
        relative(name)
        if name.split("/")[0] not in ROLES or name.startswith("node/"):
            raise ValueError("Runtime destinations must be tooling, browsers or node paths")
    target = inside(root, destination)
    if target.exists():
        raise ValueError("Runtime destination already exists")
    sources, links = {}, {}
    total = 0

    def visit(path, name, boundary, role, parents=()):
        nonlocal total
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(boundary) or not resolved.is_relative_to(root):
            raise ValueError("Runtime symlink escapes its selected workspace tree")
        if resolved == target or resolved in parents:
            raise ValueError("Runtime source contains destination or a directory cycle")
        if name in sources or name in links:
            raise ValueError("Runtime source mappings overlap")
        if len(sources) + len(links) >= MAX_FILES:
            raise ValueError("Runtime tree exceeds bounded file limit")
        if path.is_symlink():
            links[name] = {"link": role + "/" + resolved.relative_to(boundary).as_posix()}
            return
        if resolved.is_dir():
            for child in sorted(resolved.iterdir()):
                visit(child, name + "/" + child.name, boundary, role, (*parents, resolved))
        elif resolved.is_file():
            total += resolved.stat().st_size
            if total > MAX_EXPANDED or len(sources) >= MAX_FILES:
                raise ValueError("Runtime tree exceeds bounded file or byte limit")
            sources[name] = resolved
        else:
            raise ValueError("Runtime source must contain only regular files and directories")

    for role, value in specification["paths"].items():
        source = inside(root, value)
        if role == "node" and not source.is_file() or role != "node" and not source.is_dir():
            raise ValueError("Node must be a file; tooling and browsers must be directories")
        if target.is_relative_to(source):
            raise ValueError("Runtime destination cannot be inside a selected source")
        visit(source, role, source if source.is_dir() else source.parent, role)
    lock = checked_lock({"format": FORMAT, "platform": specification["platform"],
        "entrypoints": specification.get("entrypoints", {}), "files": {**links, **{
        name: {"sha256": sha(path), "bytes": path.stat().st_size,
               "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644}
        for name, path in sources.items()}}})
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".runtime-pack-", dir=target.parent) as temporary:
        archive_path = Path(temporary) / "runtime.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            member = zipfile.ZipInfo(LOCK)
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, json.dumps(lock, sort_keys=True))
            for name, source in sorted(sources.items()):
                member = zipfile.ZipInfo(name)
                member.compress_type = zipfile.ZIP_DEFLATED
                member.external_attr = (stat.S_IFREG | lock["files"][name]["mode"]) << 16
                with source.open("rb") as incoming, archive.open(member, "w") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, CHUNK)
            for name, item in sorted(links.items()):
                member = zipfile.ZipInfo(name)
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(member, item["link"])
        result = inspect_archive(archive_path)
        # Link publishes without overwriting a concurrently created destination.
        os.link(archive_path, target)
    return {"archive": str(target), **result}


def verify_installed(job, metadata):
    runtime = job / ".runtime"
    if runtime.is_symlink() or any((runtime / name).is_symlink() for name in (LOCK, "receipt.json")):
        raise ValueError("Installed runtime control files must not be links")
    if json.loads((runtime / "receipt.json").read_text()) != metadata:
        raise ValueError("Installed runtime receipt differs from invocation")
    if sha(runtime / LOCK) != metadata["lock_sha256"]:
        raise ValueError("Installed runtime manifest changed before execution")
    lock = checked_lock(json.loads((runtime / LOCK).read_text()))
    if lock["platform"] != identity() or lock["platform"] != metadata["platform"]:
        raise ValueError("Runtime platform differs from worker")
    expected = {*lock["files"], LOCK, "receipt.json"}
    actual = set()
    for path in runtime.rglob("*"):
        if path.is_symlink():
            actual.add(path.relative_to(runtime).as_posix())
        elif not (path.is_file() or path.is_dir()):
            raise ValueError("Installed runtime contains a nonregular member")
        elif path.is_file():
            actual.add(path.relative_to(runtime).as_posix())
    if expected != actual:
        raise ValueError("Installed runtime contains missing or undeclared files")
    for name, item in lock["files"].items():
        path = runtime / name
        if "link" in item:
            if not path.is_symlink() or os.readlink(path) != os.path.relpath(runtime / item["link"], path.parent):
                raise ValueError("Installed runtime link changed before execution")
        elif path.is_symlink() or path.stat().st_size != item["bytes"] \
                or stat.S_IMODE(path.stat().st_mode) != item["mode"] \
                or sha(path) != item["sha256"]:
            raise ValueError("Installed runtime changed before execution")


def receive(job, message):
    """Called under the gateway's per-job control lock, only before run admission."""
    if any((job / name).exists() for name in ("started", "dispatched", "cancelled")):
        raise ValueError("Runtime transfer is forbidden after run admission or cancellation")
    lock = json.loads((job / "bundle-lock.json").read_text())
    metadata = lock.get("runtime")
    if not lock.get("coordinator") or not metadata or metadata["platform"] != identity() \
            or message.get("sha256") != metadata["sha256"]:
        raise ValueError("Runtime transfer differs from pinned coordinator or worker platform")
    if type(metadata["bytes"]) is not int or not 0 < metadata["bytes"] <= MAX_ARCHIVE:
        raise ValueError("Invalid pinned runtime size")
    upload = job / ".runtime-upload"
    runtime = job / ".runtime"
    mode = message.get("mode")
    if mode not in ("chunk", "commit"):
        raise ValueError("Unknown runtime transfer mode")
    if runtime.exists():
        verify_installed(job, metadata)
        return {"trial_id": job.name, "sha256": metadata["sha256"], "installed": True}
    if mode == "chunk":
        offset = message.get("offset")
        encoded = message.get("data_base64")
        if type(offset) is not int or offset < 0 or offset % CHUNK \
                or not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK + 2) // 3):
            raise ValueError("Invalid runtime chunk offset or size")
        content = base64.b64decode(encoded, validate=True)
        if len(content) != min(CHUNK, metadata["bytes"] - offset) or not content:
            raise ValueError("Runtime chunk size differs from pin")
        if not upload.exists():
            upload.touch(mode=0o600)
        size = upload.stat().st_size
        with upload.open("r+b") as stream:
            if offset < size:
                stream.seek(offset)
                if stream.read(len(content)) != content:
                    raise ValueError("Retried runtime chunk differs")
            elif offset == size:
                stream.seek(offset)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            else:
                raise ValueError("Runtime chunks must be contiguous")
        return {"trial_id": job.name, "sha256": metadata["sha256"],
                "offset": offset, "accepted_bytes": len(content), "installed": False}
    if not upload.is_file() or upload.stat().st_size != metadata["bytes"]:
        raise ValueError("Runtime upload is incomplete")
    # Interrupted extraction is discarded; nothing becomes executable before rename.
    staging = job / ".runtime-stage"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    try:
        result = inspect_archive(upload, metadata["sha256"], destination=staging)
        if result != metadata:
            raise ValueError("Runtime archive metadata differs from pinned invocation")
        (staging / "receipt.json").write_text(json.dumps(metadata))
        staging.rename(runtime)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    upload.unlink()
    return {"trial_id": job.name, "sha256": metadata["sha256"], "installed": True}
