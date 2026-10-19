"""Pinned container setup and explicit selection for the existing worker coordinator."""
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import zipfile
from contextvars import ContextVar
from pathlib import Path

from .errors import LabError
from .serialization import digest, file_digest, load
from .surface import atomic_json, inside
from .worker_container_bootstrap import (
    FORMAT,
    MAX_BYTES,
    Docker,
    inspect_bundle,
    sha,
    validate_image,
    verify_tar,
)

# Installed only by the fixed coordinator, after checking its pinned deployment receipts.
worker_docker_connections = ContextVar("worker_docker_connections", default=())


def host_identity(root, config=None):
    if config and config.provider == "maritime":
        raise LabError("Maritime does not accept local or SSH container deployment receipts")
    if config and config.provider == "ssh":
        return digest({"transport": "ssh", "target": config.ssh_target,
                       "identity": file_digest(inside(root, config.ssh_identity_file)),
                       "known_hosts": file_digest(inside(root, config.ssh_known_hosts_file))})
    return digest({"transport": "local-command-reference"})


def validate_receipt(receipt):
    required = {"format", "status", "bundle_sha256", "image", "daemon", "requested_socket",
                "environment", "host_identity"}
    if set(receipt) != required or receipt["format"] != FORMAT or receipt["status"] != "installed":
        raise LabError("Invalid container deployment receipt")
    validate_image(receipt["image"])
    if any(not re.fullmatch(r"[a-f0-9]{64}", str(receipt[name]))
           for name in ("bundle_sha256", "host_identity")):
        raise LabError("Invalid container deployment digest")
    daemon = receipt["daemon"]
    if set(daemon) != {"id", "version", "os", "arch"} or any(
            not isinstance(value, str) or not value or "\0" in value for value in daemon.values()) \
            or (daemon["os"], daemon["arch"]) != (receipt["image"]["os"], receipt["image"]["arch"]):
        raise LabError("Invalid container daemon receipt")
    environment = receipt["environment"]
    if set(environment) != {"DOCKER_HOST", "DOCKER_CONFIG"} \
            or any(not isinstance(value, str) or "\0" in value for value in environment.values()) \
            or not environment["DOCKER_HOST"].startswith("unix:///") \
            or not Path(environment["DOCKER_CONFIG"]).is_absolute() \
            or Path(environment["DOCKER_CONFIG"]).name != "docker-config" \
            or not isinstance(receipt["requested_socket"], str) \
            or not Path(receipt["requested_socket"]).is_absolute():
        raise LabError("Invalid container connection receipt")


def pack(root, image, socket, output):
    root = Path(root).resolve()
    target = inside(root, output)
    if target.exists() or target.is_symlink():
        raise LabError("Container bundle output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".container-pack-", dir=target.parent) as temporary:
        work = Path(temporary)
        # This is an explicitly selected privileged control endpoint, never an agent file.
        docker = Docker(root / socket, work)
        selected = docker.inspect(image)
        if image != selected["id"]:
            raise LabError("Container packaging requires an immutable image ID, not a tag")
        archive = work / "image.tar"
        platform = "/".join(filter(None, (selected["os"], selected["arch"], selected["variant"])))
        docker.call("image", "save", "--platform", platform, "--output", str(archive), image)
        if not 0 < archive.stat().st_size <= MAX_BYTES:
            raise LabError("Container archive exceeds 2 GiB")
        verify_tar(archive, selected)
        lock = {"format": FORMAT, "image": selected, "tar_sha256": sha(archive),
                "tar_bytes": archive.stat().st_size}
        staged = work / "container.zip"
        with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED) as package:
            package.writestr(zipfile.ZipInfo("container-lock.json"), json.dumps(lock))
            with package.open(zipfile.ZipInfo("image.tar"), "w") as output_stream, archive.open("rb") as incoming:
                shutil.copyfileobj(incoming, output_stream, 1024**2)
        staged.chmod(0o600)
        checksum = sha(staged)
        inspect_bundle(staged, checksum)
        if docker.inspect(image) != selected:
            raise LabError("Selected image changed during export")
        # An exclusive destination avoids replacing an existing reviewed archive.
        os.link(staged, target)
    return {"archive": str(target), "sha256": checksum, "bytes": target.stat().st_size, "image": selected}


async def deploy(root, archive, expected, socket, directory, output, config=None):
    root = Path(root).resolve()
    archive, output = inside(root, archive), inside(root, output)
    if output.exists() or output.is_symlink():
        raise LabError("Container deployment receipt already exists")
    lock = inspect_bundle(archive, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".container-setup-", dir=output.parent))
    # Validate the full graph before the setup transport can import anything.
    try:
        inspect_bundle(archive, expected, work / "image.tar")
    finally:
        (work / "image.tar").unlink(missing_ok=True)
    source = (Path(__file__).parent / "worker_container_bootstrap.py").read_text()
    if config:
        if config.provider != "ssh":
            raise LabError("Container setup accepts an explicit SSH host or local setup")
        from .workers import ssh_command
        directory = str(Path(config.ssh_worker_root) / "containers")
        if not Path(socket).is_absolute() or ".." in Path(socket).parts or "\0" in socket:
            raise LabError("SSH container setup needs an explicit absolute Docker socket")
        request = {"directory": directory, "socket": socket, "sha256": expected}
        command = ssh_command(root, config, shlex.join([
            config.ssh_python, "-I", "-c", source, json.dumps(request)]))
    else:
        directory = str(inside(root, directory))
        request = {"directory": directory, "socket": str((root / socket).resolve(strict=True)), "sha256": expected}
        command = [getattr(sys, "_base_executable", sys.executable), "-I", "-c", source, json.dumps(request)]
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ}
    env.update(HOME=str(work), TMPDIR=str(work), XDG_CONFIG_HOME=str(work), XDG_CACHE_HOME=str(work))
    child = None
    with archive.open("rb") as incoming, (work / "reply.json").open("wb") as reply, \
            (work / "setup-error.log").open("wb") as error:
        try:
            child = await asyncio.create_subprocess_exec(
                *command, stdin=incoming, stdout=reply, stderr=error, cwd=root, env=env, start_new_session=True)
            await asyncio.wait_for(child.wait(), timeout=300)
            if child.returncode:
                raise LabError("Container setup failed; inspect the private setup-error.log")
        finally:
            if child and child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await child.wait()
    result_path = work / "reply.json"
    if result_path.stat().st_size > 1024**2:
        raise LabError("Container deployment response exceeds limit")
    result = load(result_path)
    if result.get("format") != FORMAT or result.get("status") != "installed" \
            or result.get("bundle_sha256") != expected or result.get("image") != lock["image"] \
            or result.get("requested_socket") != request["socket"] \
            or set(result.get("environment", {})) != {"DOCKER_HOST", "DOCKER_CONFIG"} \
            or not result["environment"]["DOCKER_HOST"].startswith("unix:///") \
            or result["environment"]["DOCKER_CONFIG"] != str(Path(directory) / "docker-config"):
        raise LabError("Worker did not confirm the selected container deployment")
    result["host_identity"] = host_identity(root, config)
    validate_receipt(result)
    atomic_json(work / "receipt.json", result)
    os.link(work / "receipt.json", output)
    return {"receipt": str(output), "sha256": file_digest(output), **result}


def select(root, placement, gateway, execution):
    receipts = {}
    for role, reference in placement.container_images.items():
        path = inside(root, reference.receipt)
        if file_digest(path) != reference.sha256:
            raise LabError("Container deployment receipt changed")
        receipt = load(path)
        validate_receipt(receipt)
        if receipt["host_identity"] != host_identity(root, gateway):
            raise LabError("Container deployment belongs to a different worker host")
        receipts[role] = receipt
    if not receipts:
        return {}
    environments = [row["environment"] for row in receipts.values()]
    if any(value != environments[0] for value in environments[1:]):
        raise LabError("Selected container images must use one Docker connection")
    environment = environments[0]
    if set(environment) != {"DOCKER_HOST", "DOCKER_CONFIG"}:
        raise LabError("Invalid container deployment environment")
    if any(name in placement.environment and placement.environment[name] != value
           for name, value in environment.items()):
        raise LabError("Container deployment conflicts with the worker Docker environment")
    placement.environment.update(environment)
    if "sandbox" in receipts:
        selected = receipts["sandbox"]["image"]["id"]
        if execution.sandbox_image not in {"consumer-agent-testing-tool-sandbox:0.1.0", selected}:
            raise LabError("Container deployment conflicts with the selected sandbox image")
        execution.sandbox_image = selected
    if "browser" in receipts:
        from .config import BrowserWorkerSettings
        row = receipts["browser"]
        if execution.browser_worker and execution.browser_worker.image != row["image"]["id"]:
            raise LabError("Container deployment conflicts with the selected browser image")
        values = execution.browser_worker.model_dump() if execution.browser_worker else {}
        values.update(image=row["image"]["id"], docker_socket=environment["DOCKER_HOST"].removeprefix("unix://"),
                      docker_config=environment["DOCKER_CONFIG"])
        execution.browser_worker = BrowserWorkerSettings.model_validate(values)
    return receipts


def verify_connections(root, receipts, configuration, environment=None):
    if {role: digest(value) for role, value in receipts.items()} != configuration.get("container_images", {}):
        raise LabError("Worker container selection differs from its execution lock")
    connections = []
    for receipt in receipts.values():
        validate_receipt(receipt)
        if not isinstance(environment, dict) or environment != configuration.get("runtime_environment") or any(
                environment.get(name) != value for name, value in receipt["environment"].items()):
            raise LabError("Actual worker Docker environment differs from its deployment")
        selected_environment = receipt["environment"]
        if set(selected_environment) != {"DOCKER_HOST", "DOCKER_CONFIG"} \
                or not selected_environment["DOCKER_HOST"].startswith("unix:///"):
            raise LabError("Container deployment requires an explicit Unix Docker connection")
        config = Path(selected_environment["DOCKER_CONFIG"])
        if config.name != "docker-config" or not config.is_absolute():
            raise LabError("Invalid deployed Docker configuration")
        validate_image(receipt["image"])
        docker = Docker(selected_environment["DOCKER_HOST"].removeprefix("unix://"), config.parent, require_existing=True)
        if docker.daemon() != receipt["daemon"] or docker.inspect(receipt["image"]["id"]) != receipt["image"]:
            raise LabError("Installed Docker daemon or selected image changed")
        connections.append({"socket": docker.socket, "config": str(docker.config), "image": receipt["image"]["id"]})
    return tuple(connections)
