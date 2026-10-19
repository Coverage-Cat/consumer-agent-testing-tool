"""Trusted browser-worker admission, before scenario state or inference."""
import json
import os
import subprocess
from pathlib import Path

from .errors import LabError
from .surface import inside


def validate_worker(worker, root=None):
    if worker is None:
        return
    root = Path(root or Path.cwd()).resolve()
    from .worker_container import worker_docker_connections
    selection = {"socket": str(Path(worker.docker_socket).absolute()),
                 "config": str(Path(worker.docker_config).absolute()), "image": worker.image}
    approved = selection in worker_docker_connections.get()
    socket = Path(selection["socket"]) if approved else inside(root, worker.docker_socket)
    config = Path(selection["config"]) if approved else inside(root, worker.docker_config)
    if approved and (socket.resolve() != socket or config.resolve() != config):
        raise LabError("Worker Docker connection changed after admission")
    if not socket.is_socket() or not config.is_dir():
        raise LabError("Selected browser worker is unavailable; start its owned VM and configure workspace paths")
    home = root / ".cache/browser-worker-preflight"
    home.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(home), TMPDIR=str(home), DOCKER_CONFIG=str(config), DOCKER_HOST="unix://" + str(socket))
    try:
        result = subprocess.run(["docker", "--config", str(config), "--host", "unix://" + str(socket),
                                 "image", "inspect", worker.image, "--format", "{{json .}}"],
                                cwd=root, env=env, text=True, capture_output=True, timeout=15, check=True)
        image = json.loads(result.stdout)
        if image.get("Id") != worker.image or image.get("Os") != "linux" or image.get("Architecture") != "arm64":
            raise ValueError("Unexpected worker image identity or platform")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as error:
        # Docker diagnostics can include host paths/configuration; keep them out of public errors.
        raise LabError("Selected immutable Linux ARM64 browser image is unavailable; prepare the owned worker") from error


def execution_contract(worker):
    return {"backend": "container" if worker else "native",
            "worker": worker.model_dump(mode="json") if worker else None}


def validate_identity(worker, identity):
    required = {"image", "network", "chromium_sandbox", "uid", "os_isolation",
                "webrtc_loopback_probe", "runtime", "browser_version"}
    if not isinstance(identity, dict):
        raise LabError("Browser worker identity must be an object")
    runtime = identity.get("runtime", {})
    if set(identity) != required or identity["image"] != worker.image or identity["network"] != "none" \
            or identity["uid"] != 65532 or identity["chromium_sandbox"] is not True \
            or identity["webrtc_loopback_probe"] is not False \
            or identity["os_isolation"] != "qualification-pending" or not isinstance(runtime, dict) \
            or set(runtime) != {"docker_version", "docker_os", "docker_arch", "kernel"} \
            or any(not isinstance(value, str) or not value for value in runtime.values()) \
            or runtime["docker_os"] != "linux" \
            or not isinstance(identity["browser_version"], str) or not identity["browser_version"]:
        raise LabError("Browser worker identity does not match the selected policy")
