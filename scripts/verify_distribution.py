"""Explicit dependency setup or fresh offline distribution verification inside this workspace."""
import json
import shutil
import tarfile
import tomllib
import zipfile
from pathlib import Path

from distribution_support import ROOT, sha, write_record
from inventory import node_content_identity


def node_snapshot(project, output, groups=("tooling", "tooling/payment-ui", "tooling/api")):
    write_record(output, {"format": "node-package-files-v1", "groups": {
        group: node_content_identity(project / group)
        for group in groups}})
    return {"path": str(output.relative_to(ROOT)), "sha256": sha(output)}


def stage_node(project, groups=("tooling", "tooling/payment-ui", "tooling/api")):
    for group in groups:
        directory = project / group
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(ROOT / group / name, directory / name)
        if (ROOT / group / ".npmrc").is_file():
            shutil.copyfile(ROOT / group / ".npmrc", directory / ".npmrc")
        if (ROOT / group / "stubs").is_dir():
            shutil.copytree(ROOT / group / "stubs", directory / "stubs", dirs_exist_ok=True)


def stage_python_wheels(project):
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename
    allowed = {Path(wheel["url"]).name: wheel["hash"].removeprefix("sha256:")
               for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
               for wheel in package.get("wheels", [])}
    tags = set(sys_tags())
    selected = {}
    destination = project / "worker-wheels"
    destination.mkdir()
    for path in sorted((ROOT / ".cache/worker-python-wheels").glob("*.whl")):
        if path.name not in allowed or not parse_wheel_filename(path.name)[3] & tags:
            continue
        if path.is_symlink() or sha(path) != allowed[path.name]:
            raise RuntimeError("Cached worker wheel differs from the source dependency lock")
        shutil.copyfile(path, destination / path.name)
        selected[path.name] = allowed[path.name]
    if not selected:
        raise RuntimeError("Worker wheel cache missing; run the explicit --prepare-cache setup first")
    return selected


def verify_wheel(wheel):
    expected = {f"consumer_agent_testing_tool/{path.relative_to(ROOT / 'src/consumer_agent_testing_tool')}": path
                for path in (ROOT / "src/consumer_agent_testing_tool").rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    expected["consumer_agent_testing_tool/data/locks/uv.lock"] = ROOT / "uv.lock"
    expected.update({f"consumer_agent_testing_tool/data/third_party/{path.relative_to(ROOT / 'third_party')}": path
                     for path in (ROOT / "third_party").rglob("*") if path.is_file()})
    with zipfile.ZipFile(wheel) as package:
        for name, source in expected.items():
            if package.read(name) != source.read_bytes():
                raise RuntimeError(f"Wheel is missing current source content: {name}")
        if any("node_modules/" in name or "/.cache/" in name or name.endswith(".private.json")
               for name in package.namelist()):
            raise RuntimeError("Wheel includes workspace runtime or private state")
        unexpected = [name for name in package.namelist() if name not in expected
                      and not (name.split("/")[0].startswith("consumer_agent_testing_tool-")
                               and name.split("/")[0].endswith(".dist-info"))]
        if unexpected:
            raise RuntimeError(f"Wheel includes unexpected content: {unexpected[0]}")
    return {"checked_source_files": len(expected), "wheel_sha256": sha(wheel)}


def verify_source_archive(archive):
    required = ["pyproject.toml", "uv.lock", "tooling/build-python/uv.lock",
                "tooling/payment-ui/package-lock.json", "tooling/api/package-lock.json",
                "scripts/distribution_smoke.py", "scripts/api_distribution_smoke.py",
                "scripts/container_distribution_smoke.py",
                "src/consumer_agent_testing_tool/data/fixtures/reservations.html",
                "src/consumer_agent_testing_tool/data/scenarios_api/stale_total.yaml",
                "src/consumer_agent_testing_tool/data/scenarios_browser/stale_total.yaml"]
    with tarfile.open(archive) as package:
        members = package.getmembers()
        roots = {Path(m.name).parts[0] for m in members}
        if len(roots) != 1 or any(Path(m.name).is_absolute() or ".." in Path(m.name).parts
                                  or m.issym() or m.islnk() for m in members):
            raise RuntimeError("Source archive contains escaping or linked members")
        prefix = next(iter(roots))
        for name in required:
            content = package.extractfile(prefix + "/" + name)
            if content is None or content.read() != (ROOT / name).read_bytes():
                raise RuntimeError(f"Source archive omitted or changed {name}")
    return {"checked_paths": required, "sha256": sha(archive)}


def archive_previous(destination):
    if not destination.exists():
        return
    previous = json.loads(destination.read_text())
    if previous.get("status") == "passed" and previous.get("wheel_sha256"):
        identity = previous["wheel_sha256"]
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid previous publication digest")
        directory = ROOT / "dist/checkpoints" / identity[:12]
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(destination, directory / f"verification-{sha(destination)[:12]}.json")


def verification_inputs():
    paths = list((ROOT / "scripts").glob("*.py")) + list((ROOT / "scripts").glob("*.mjs"))
    paths += [ROOT / name for name in ("pyproject.toml", "uv.lock", "tooling/package-lock.json",
        "tooling/payment-ui/package-lock.json", "tooling/api/package-lock.json", "tooling/build-python/uv.lock",
        "third_party/supplemental-notices.json")]
    return {str(path.relative_to(ROOT)): sha(path) for path in paths if path.is_file()}


def download_plan(requirements, locks):
    from packaging.requirements import Requirement
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    tags, wanted, downloads = set(sys_tags()), set(), {}
    for path in requirements:
        for line in path.read_text().replace("\\\n", "").splitlines():
            line = line.strip()
            if not line.strip() or line.startswith("#"):
                continue
            requirement = Requirement(line.split("--hash=")[0].strip())
            if not requirement.marker or requirement.marker.evaluate():
                specifiers = list(requirement.specifier)
                if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
                    raise RuntimeError("Download plans require exact locked versions")
                wanted.add((canonicalize_name(requirement.name), specifiers[0].version))
    for path in locks:
        for package in tomllib.loads(path.read_text())["package"]:
            name = canonicalize_name(package["name"])
            identity = (name, package["version"])
            if identity not in wanted:
                continue
            compatible = [wheel for wheel in package.get("wheels", [])
                          if parse_wheel_filename(Path(wheel["url"]).name)[3] & tags]
            if not compatible:
                raise RuntimeError(f"No locked compatible wheel for {name}; source builds are disabled")
            downloads[identity] = max(wheel["size"] for wheel in compatible)
    if set(downloads) != set(wanted):
        raise RuntimeError("Dependency size plan is incomplete")
    return {"packages": len(downloads), "maximum_wheel_bytes": sum(downloads.values()),
            "cache_hits": "not assumed", "scope": "compatible Python core/build/optional API wheels"}


def main():
    from local_distribution import main as local_main
    return local_main()


if __name__ == "__main__":
    from local_distribution import main as local_main
    raise SystemExit(local_main())
