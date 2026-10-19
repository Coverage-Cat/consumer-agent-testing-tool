"""Standard-library bootstrap for an explicitly supplied offline wheel bundle.

This source can run before Consumer Agent Testing Tool is installed. Wheels are trusted operator
inputs; their hashes establish integrity, not safety of arbitrary Python code.
"""
import base64
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

FORMAT = "consumer-agent-testing-tool-worker-python-v1"
MAX_BYTES = 256 * 1024 * 1024
MAX_WHEELS = 512


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def environment_identity():
    return {"system": platform.system(), "machine": platform.machine(),
            "python": list(sys.version_info[:2])}


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate deployment manifest key")
        result[key] = value
    return result


def read_bundle(path, expected, destination=None):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) \
            or not 0 < path.stat().st_size <= MAX_BYTES or sha(path) != expected:
        raise ValueError("Python bundle integrity failed")
    with zipfile.ZipFile(path) as archive:
        lock_info = archive.getinfo("python-lock.json")
        if lock_info.file_size > 1024 * 1024:
            raise ValueError("Python bundle manifest exceeds limit")
        lock = json.loads(archive.read(lock_info), object_pairs_hook=unique)
        if set(lock) != {"format", "platform", "wheels", "harness", "model_provider"} \
                or lock["format"] != FORMAT or not isinstance(lock["wheels"], dict) \
                or not 0 < len(lock["wheels"]) <= MAX_WHEELS:
            raise ValueError("Invalid Python bundle manifest")
        names = list(archive.namelist())
        if len(names) != len(set(names)) or set(names) != {"python-lock.json", *lock["wheels"]}:
            raise ValueError("Python bundle inventory mismatch")
        if len({name.casefold() for name in lock["wheels"]}) != len(lock["wheels"]):
            raise ValueError("Python wheel paths collide")
        total, expanded, file_count = 0, 0, 0
        for name, record in lock["wheels"].items():
            if not re.fullmatch(r"wheels/[A-Za-z0-9_.+!-]+\.whl", name) \
                    or set(record) != {"sha256", "bytes"} \
                    or not re.fullmatch(r"[a-f0-9]{64}", str(record["sha256"])) \
                    or type(record["bytes"]) is not int or record["bytes"] <= 0:
                raise ValueError("Invalid Python wheel record")
            total += record["bytes"]
            info = archive.getinfo(name)
            if total > MAX_BYTES or info.file_size != record["bytes"] \
                    or info.compress_type != zipfile.ZIP_STORED \
                    or not stat.S_ISREG(info.external_attr >> 16):
                raise ValueError("Invalid or oversized Python wheel archive")
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("Python wheel integrity failed")
            with zipfile.ZipFile(io.BytesIO(content)) as wheel:
                members = wheel.infolist()
                expanded += sum(item.file_size for item in members)
                file_count += len(members)
                if file_count > 100000 or expanded > 1024**3 \
                        or len({item.filename.casefold() for item in members}) != len(members):
                    raise ValueError("Python wheel expansion exceeds limits or has colliding paths")
                for member in members:
                    name_in_wheel = member.filename.rstrip("/")
                    if "\\" in name_in_wheel or PurePosixPath(name_in_wheel).is_absolute() \
                            or ".." in PurePosixPath(name_in_wheel).parts \
                            or stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError("Unsafe Python wheel member")
            if destination:
                target = destination / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(content)
        return lock


def clean_environment(root):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    for key, directory in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache")):
        path = root / directory
        path.mkdir(mode=0o700, exist_ok=True)
        env[key] = str(path)
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONNOUSERSITE="1")
    return env


def command(arguments, env):
    result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=240, check=False)
    if result.returncode:
        # Package managers can echo arbitrary wheel metadata; retain it privately.
        (Path(env["HOME"]).parent / "setup-error.log").write_text(result.stdout + result.stderr)
        raise ValueError("Python deployment command failed; inspect private setup-error.log")
    return result.stdout


def probe(python, lock, env):
    code = ("import json; from consumer_agent_testing_tool.worker_run import harness_identity; "
            f"print(json.dumps(harness_identity({lock['model_provider']!r})))")
    measured = json.loads(command([str(python), "-I", "-c", code], env))
    if measured != lock["harness"]:
        raise ValueError("Installed Python harness differs from the selected bundle")
    command([str(python), "-I", "-m", "pip", "check"], env)
    return measured


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Python environment must be an unlinked directory")
    records = {}
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            records[name] = {"link": os.readlink(path)}
        elif path.is_file():
            records[name] = {"sha256": sha(path), "mode": stat.S_IMODE(path.stat().st_mode)}
        elif not path.is_dir():
            raise ValueError("Python deployment contains a special file")
    return records


def deployment_root(root):
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Linked Python deployment paths are forbidden")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def install(root, archive, expected):
    lock = read_bundle(archive, expected)
    if lock["platform"] != environment_identity():
        raise ValueError("Python deployment platform or interpreter version differs")
    root = deployment_root(root)
    target = root / ("python-" + expected)
    ownership = root / (target.name + ".lock")
    descriptor = os.open(ownership, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "w") as owner:
        if not stat.S_ISREG(os.fstat(owner.fileno()).st_mode):
            raise ValueError("Invalid Python deployment lock")
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = target / "deployment.json"
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not receipt.is_file() or receipt.is_symlink():
                raise ValueError("Incomplete Python deployment requires explicit cleanup")
            saved = json.loads(receipt.read_text())
            if saved.get("bundle_sha256") != expected or saved.get("files") != inventory(target / "venv"):
                raise ValueError("Python deployment receipt differs")
            probe(target / "venv/bin/python", lock, clean_environment(target))
            return {key: value for key, value in saved.items() if key != "files"}
        target.mkdir(mode=0o700)
        env = clean_environment(target)
        # Keep the final venv path stable: moving it would invalidate script shebangs.
        with tempfile.TemporaryDirectory(prefix="wheels-", dir=target) as temporary:
            staged = Path(temporary)
            read_bundle(archive, expected, staged)
            requirements = staged / "requirements.txt"
            requirements.write_text("".join(
                f"{(staged / name).as_uri()} --hash=sha256:{row['sha256']}\n"
                for name, row in lock["wheels"].items()))
            # Run venv in an isolated subprocess; ensurepip must not inherit secrets.
            command([sys.executable, "-I", "-m", "venv", str(target / "venv")], env)
            python = target / "venv/bin/python"
            command([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps",
                     "--only-binary=:all:", "--require-hashes", "-r", str(requirements)], env)
            measured = probe(python, lock, env)
        result = {"format": FORMAT, "bundle_sha256": expected, "python": str(python),
                  "platform": lock["platform"], "harness": measured, "status": "installed"}
        temporary = receipt.with_suffix(".tmp")
        with temporary.open("x") as stream:
            json.dump({**result, "files": inventory(target / "venv")}, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.rename(receipt)
        return result


def main():
    message = json.loads(sys.stdin.buffer.read(4 * ((MAX_BYTES + 2) // 3) + 4096))
    root = deployment_root(Path(sys.argv[1]))
    # The caller supplies a bounded archive through the existing private transport.
    with tempfile.TemporaryDirectory(prefix="python-upload-", dir=root) as temporary:
        archive = Path(temporary) / "bundle.zip"
        archive.write_bytes(base64.b64decode(message["archive_base64"], validate=True))
        archive.chmod(0o600)
        print(json.dumps(install(root, archive, message["sha256"])))


if __name__ == "__main__":
    main()
