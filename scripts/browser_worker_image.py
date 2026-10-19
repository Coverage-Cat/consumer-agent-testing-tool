"""Explicit pinned-image setup using only the dedicated workspace VM."""
import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import urllib.request
import uuid
from pathlib import Path

from prepare_browser_vm import PROFILE, ROOT, WORK, environment

BASE = "mcr.microsoft.com/playwright@sha256:a0f4498920a5dbac63196d9140ed738ef00470f27e2e74029abd8850b7bd5717"
ASSETS = ROOT / "src/consumer_agent_testing_tool/data/browser_worker"


def archive_path(filename):
    if not isinstance(filename, str) or filename != Path(filename).name \
            or not filename.endswith(".deb"):
        raise RuntimeError("Invalid package archive filename")
    path = ROOT / ".cache/browser-debs" / filename
    if not path.resolve().is_relative_to(ROOT / ".cache/browser-debs"):
        raise RuntimeError("Package archive escapes the workspace cache")
    return path


def fetch_dependencies():
    """Setup from the committed lock; does not resolve or change package versions."""
    lock = json.loads((ASSETS / "dependencies.json").read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for row in lock["packages"]:
        destination = archive_path(row["file"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == row["sha256"]:
            continue
        temporary = destination.with_suffix(".partial-" + uuid.uuid4().hex)
        try:
            with opener.open(row["url"], timeout=60) as source, temporary.open("xb") as output:
                total = 0
                while chunk := source.read(65536):
                    total += len(chunk)
                    if total > row["bytes"]:
                        raise RuntimeError("Dependency download exceeded its locked size")
                    output.write(chunk)
            if total != row["bytes"] or hashlib.sha256(temporary.read_bytes()).hexdigest() != row["sha256"]:
                raise RuntimeError("Dependency download does not match its locked hash")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    print("Verified", len(lock["packages"]), "locked firewall archives")


def docker(*arguments, timeout=900, capture=False):
    env = environment()
    socket = WORK / "colima" / PROFILE / "docker.sock"
    if not socket.is_socket():
        raise SystemExit("Start the owned workspace VM first")
    env["DOCKER_HOST"] = "unix://" + str(socket)
    result = subprocess.run(["docker", "--config", str(WORK / "docker"),
                           "--host", env["DOCKER_HOST"], *arguments],
                          cwd=ROOT, env=env, timeout=timeout, check=False,
                          capture_output=capture, text=capture)
    if capture:
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()
    return result.returncode


def resolve_dependencies():
    """Maintainer setup: resolve signed Ubuntu packages once, then lock exact bytes."""
    name = "surface-browser-deps-" + uuid.uuid4().hex[:10]
    cache = ROOT / ".cache/browser-debs"
    cache.mkdir(parents=True, exist_ok=True)
    docker("run", "-d", "--name", name, "--pull=never", "--network=bridge",
           "--memory=512m", "--cpus=1", "--pids-limit=128",
           "--security-opt=no-new-privileges", "--entrypoint=/bin/sleep",
           BASE, "infinity", capture=True)
    try:
        code = docker("exec", name, "/bin/sh", "-c",
            "apt-get update -qq && "
            "apt-get --print-uris --yes --download-only install iptables > /tmp/package-uris && "
            "apt-get --yes --download-only install iptables")
        if code:
            raise RuntimeError("Package resolution failed")
        uris = docker("exec", name, "cat", "/tmp/package-uris", capture=True)
        sources = {}
        for line in uris.splitlines():
            if line.startswith("'"):
                url, filename, size, checksum = shlex.split(line)
                sources[filename] = {"url": url, "bytes": int(size), "apt_checksum": checksum}
        if docker("cp", name + ":/var/cache/apt/archives/.", str(cache)):
            raise RuntimeError("Cannot export dependency archives")
        packages = []
        for filename, source in sources.items():
            if "/" in filename or not filename.endswith(".deb"):
                raise RuntimeError("Unexpected package archive name")
            path = cache / filename
            if not path.is_file() or path.stat().st_size != source["bytes"]:
                raise RuntimeError("Dependency archive does not match the resolver")
            fields = docker("exec", name, "dpkg-deb", "--show",
                            "--showformat=${Package}\\n${Version}\\n${Architecture}",
                            "/var/cache/apt/archives/" + filename, capture=True).splitlines()
            package, version, architecture = fields
            packages.append({"file": filename, "package": package, "version": version,
                             "architecture": architecture, **source,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        if not packages:
            raise RuntimeError("No firewall dependency packages were resolved")
        destination = ROOT / "src/consumer_agent_testing_tool/data/browser_worker/dependencies.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"schema_version": 1, "base_image": BASE,
            "platform": "linux/arm64", "playwright_version": "1.63.0",
            "packages": packages}, indent=2) + "\n")
        print("Locked", len(packages), "Ubuntu package archives in", destination.relative_to(ROOT))
    finally:
        if docker("rm", "-f", name):
            raise RuntimeError("Dependency setup container cleanup was not confirmed")


def build():
    """Build offline from the pinned image, local SDK and hash-locked archives."""
    lock = json.loads((ASSETS / "dependencies.json").read_text())
    if lock["base_image"] != BASE or lock["platform"] != "linux/arm64":
        raise RuntimeError("Unsupported browser dependency lock")
    context = WORK / ("build-" + uuid.uuid4().hex[:10])
    context.mkdir()
    shutil.copyfile(ASSETS / "Dockerfile", context / "Dockerfile")
    shutil.copytree(ASSETS, context / "worker")
    (context / "debs").mkdir()
    for row in lock["packages"]:
        archive = archive_path(row["file"])
        if hashlib.sha256(archive.read_bytes()).hexdigest() != row["sha256"]:
            raise RuntimeError("Missing or modified firewall package archive")
        shutil.copyfile(archive, context / "debs" / row["file"])
    for name in ("playwright", "playwright-core"):
        sdk = ROOT / "tooling/payment-ui/node_modules" / name
        if not sdk.resolve().is_relative_to(ROOT) or any(
                not file.resolve().is_relative_to(ROOT) for file in sdk.rglob("*")):
            raise RuntimeError("SDK copy would access a path outside the workspace")
        if json.loads((sdk / "package.json").read_text())["version"] != lock["playwright_version"]:
            raise RuntimeError("Worker SDK version does not match the browser image")
        shutil.copytree(sdk, context / "node_modules" / name)
    iid = context / "image-id"
    code = docker("build", "--network=none", "--pull=false", "--platform=linux/arm64",
                  "--iidfile", str(iid), str(context))
    if code:
        raise RuntimeError("Offline browser worker image build failed")
    image = iid.read_text().strip()
    inputs = {str(file.relative_to(context)): hashlib.sha256(file.read_bytes()).hexdigest()
              for file in sorted(context.rglob("*")) if file.is_file() and file != iid}
    (WORK / "worker-image.json").write_text(json.dumps({
        "schema_version": 1, "image": image, "base_image": BASE,
        "platform": "linux/arm64", "inputs": inputs,
        "qualification": "experimental; browser bypass matrix is not yet qualified",
    }, indent=2) + "\n")
    print("Built offline browser worker", image)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=[
        "info", "pull", "probe", "resolve-deps", "fetch-deps", "build", "build-shell-fixture"])
    args = parser.parse_args()
    if args.action == "info":
        code = docker("info", "--format", "{{json .ServerVersion}}", timeout=30)
    elif args.action == "pull":
        print(json.dumps({"image": BASE, "platform": "linux/arm64",
                          "compressed_layer_bytes": 936671625}), flush=True)
        code = docker("pull", "--platform=linux/arm64", BASE)
    elif args.action == "resolve-deps":
        resolve_dependencies()
        code = 0
    elif args.action == "fetch-deps":
        fetch_dependencies()
        code = 0
    elif args.action == "build":
        build()
        code = 0
    elif args.action == "build-shell-fixture":
        # Setup only. The Dockerfile pins the base image and package versions;
        # this uses the same owned VM without changing any global Docker context.
        code = docker("build", "--pull=false", "-f", "containers/Dockerfile",
                      "-t", "consumer-agent-testing-tool-sandbox:0.1.0", str(ROOT))
    else:
        code = docker("run", "--rm", "--pull=never", "--network=none", "--ipc=private",
                      "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                      "--memory=256m", "--pids-limit=64", "--entrypoint=/bin/sh", BASE, "-c",
                      "node --version; for tool in iptables ip6tables setpriv capsh; do command -v \"$tool\" || true; done; "
                      "dpkg-query -W iptables util-linux libcap2-bin 2>/dev/null; "
                      "ls /ms-playwright")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
