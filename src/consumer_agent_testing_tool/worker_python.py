"""Prepare and explicitly deploy pinned wheels through the existing worker transport."""
import base64
import json
import shlex
import stat
import sys
import zipfile
from pathlib import Path

from .errors import LabError
from .serialization import load
from .surface import atomic_json, inside
from .worker_python_bootstrap import (
    FORMAT,
    MAX_BYTES,
    MAX_WHEELS,
    environment_identity,
    read_bundle,
    sha,
)
from .worker_run import harness_identity
from .workers import CommandGateway, WorkerConfig


def pack(root, manifest, output):
    root = Path(root).resolve()
    specification = load(inside(root, manifest))
    if set(specification) - {"wheels", "platform", "model_provider"} \
            or not isinstance(specification.get("wheels"), list) \
            or not 0 < len(specification["wheels"]) <= MAX_WHEELS:
        raise LabError("Declare the complete wheel list for the selected Python harness")
    paths = [inside(root, name) for name in specification["wheels"]]
    if len({path.name.casefold() for path in paths}) != len(paths) \
            or any(path.is_symlink() or not path.is_file() or path.suffix != ".whl" for path in paths):
        raise LabError("Python deployment requires distinct regular workspace wheel files")
    if sum(path.stat().st_size for path in paths) > MAX_BYTES:
        raise LabError("Python deployment bundle exceeds 256 MiB")
    provider = specification.get("model_provider")
    lock = {"format": FORMAT, "platform": specification.get("platform", environment_identity()),
            "harness": harness_identity(provider), "model_provider": provider,
            "wheels": {f"wheels/{path.name}": {"sha256": sha(path), "bytes": path.stat().st_size}
                       for path in paths}}
    target = inside(root, output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LabError("Python bundle output already exists")
    try:
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("python-lock.json", json.dumps(lock))
            for path in paths:
                info = zipfile.ZipInfo("wheels/" + path.name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, path.read_bytes())
        target.chmod(0o600)
        pin = sha(target)
        read_bundle(target, pin)
        return {"archive": str(target), "sha256": pin, "bytes": target.stat().st_size,
                "platform": lock["platform"], "harness": lock["harness"]}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


async def deploy(root, archive, expected, directory, output_config, config=None):
    root = Path(root).resolve()
    path, output = inside(root, archive), inside(root, output_config)
    if output.exists():
        raise LabError("Worker configuration output already exists")
    lock = read_bundle(path, expected)
    if lock["harness"] != harness_identity(lock["model_provider"]):
        raise LabError("Python deployment bundle does not match the current selected harness")
    source = (Path(__file__).parent / "worker_python_bootstrap.py").read_text()
    if config and config.provider == "ssh":
        # Match the same explicit host/key checks used by ordinary worker calls.
        from .workers import ssh_command
        directory = config.ssh_worker_root
        gateway = ssh_command(root, config, shlex.join(["python3", "-I", "-c", source, directory]))
    else:
        if config is not None:
            raise LabError("Only explicit SSH or local Python setup is supported")
        directory = str(inside(root, directory))
        gateway = [getattr(sys, "_base_executable", sys.executable), "-I", "-c", source, directory]
    setup = WorkerConfig(provider="python-setup", gateway=gateway, call_timeout_seconds=300)
    result = await CommandGateway(root, setup).call("python-install", {
        "archive_base64": base64.b64encode(path.read_bytes()).decode(), "sha256": expected})
    if result.get("status") != "installed" or result.get("bundle_sha256") != expected \
            or result.get("harness") != lock["harness"] \
            or result.get("python") != str(Path(directory) / f"python-{expected}" / "venv/bin/python"):
        raise LabError("Worker did not confirm the selected Python installation")
    selected = (config.model_copy(update={"ssh_python": result["python"]}) if config else
                WorkerConfig(provider="local-process-reference", gateway=[
                    result["python"], "-I", "-m", "consumer_agent_testing_tool.worker_gateway",
                    str(Path(directory) / "jobs")]))
    atomic_json(output, selected.model_dump(mode="json"))
    return {**result, "worker_config": str(output)}
