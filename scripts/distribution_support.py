"""Workspace-contained packaging operations; no inference or customer credentials."""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OFFLINE_CLI_STAGES = frozenset({"static-inspect", "static-check", "contract-check"})


def uv_executable(root=ROOT):
    if executable := shutil.which("uv"):
        return Path(executable).resolve()
    local = root / ".venv/bin/uv"
    if local.is_file() and os.access(local, os.X_OK):
        return local.resolve()
    raise RuntimeError("uv is not installed; install uv and put it on PATH before verifying a distribution")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained(root, path):
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Distribution path must stay inside the workspace: {path}")
    return candidate


def environment(root, work):
    """Allowlist process environment; explicit workspace-local homes, caches and temp."""
    directories = {name: work / name for name in ("home", "tmp", "config")}
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    empty_config = directories["config"] / "empty"
    empty_config.touch(exist_ok=True)
    global_config = directories["config"] / "global-empty"
    global_config.touch(exist_ok=True)
    result = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    result.update({
        "HOME": str(directories["home"]), "TMPDIR": str(directories["tmp"]),
        "XDG_CONFIG_HOME": str(directories["config"]),
        "XDG_CACHE_HOME": str(root / ".cache/distribution-cache"),
        "PYTHONNOUSERSITE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_NO_CONFIG": "1", "UV_CACHE_DIR": str(root / ".cache/uv"),
        "UV_LINK_MODE": "copy", "UV_KEYRING_PROVIDER": "disabled",
        "npm_config_cache": str(root / ".cache/npm"),
        "npm_config_userconfig": str(empty_config),
        "npm_config_globalconfig": str(global_config),
        "npm_config_update_notifier": "false", "npm_config_audit": "false",
        "npm_config_fund": "false", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "PLAYWRIGHT_BROWSERS_PATH": str(root / ".cache/ms-playwright"),
        "INSPECT_DISPLAY": "none",
    })
    return result


class Commands:
    def __init__(self, root, work, *, setup=False):
        self.root, self.work = root, work
        self.env = environment(root, work)
        self.steps = []
        self.setup = setup

    def run(self, name, arguments, *, cwd=None, timeout=240):
        log = self.work / f"{name}.log"
        started = time.monotonic()
        record = {"step": name, "arguments": [str(item) for item in arguments],
                  "cwd": str(contained(self.root, cwd or self.root).relative_to(self.root)),
                  "status": "failed", "log": str(log.relative_to(self.root))}
        self.steps.append(record)
        try:
            execution = record["arguments"]
            if not self.setup and name in OFFLINE_CLI_STAGES and "--offline" not in execution:
                raise RuntimeError(f"{name} requires the CLI's --offline network guard")
            if not self.setup and name not in OFFLINE_CLI_STAGES | {"packaged-api-tools"}:
                # Offline CLI commands install their own policy; Seatbelt cannot nest.
                # Other build steps need no network; the fixed smoke owns local fixtures.
                if platform.system() != "Darwin":
                    raise RuntimeError("Distribution egress enforcement currently requires macOS")
                policy = "(version 1)(allow default)(deny network-outbound)"
                if name in ("packaged-workflows", "packaged-worker-continuation", "packaged-worker-container"):
                    policy += '(allow network-outbound (remote ip "localhost:*"))'
                    policy += f"(allow network-outbound (subpath {json.dumps(str(self.root))}))"
                execution = ["/usr/bin/sandbox-exec", "-p", policy, *execution]
                record["network_policy"] = policy
            elif not self.setup:
                record["network_policy"] = ("Fixed API proof launch; workers deny all egress; hosts apply "
                    "exact-port kernel policy before HTTP" if name == "packaged-api-tools"
                    else "CLI --offline exact-port kernel policy")
            with log.open("w") as output:
                log.chmod(0o600)
                result = subprocess.run(execution, cwd=cwd or self.root, env=self.env,
                    stdout=output, stderr=subprocess.STDOUT, timeout=timeout, check=False)
            record["returncode"] = result.returncode
            if result.returncode:
                raise RuntimeError(f"{name} failed; inspect {record['log']}")
            record["status"] = "passed"
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            if log.exists():
                record["log_sha256"] = sha(log)
            print(f"{name}: {record['status']}", flush=True)
        return log


def write_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
