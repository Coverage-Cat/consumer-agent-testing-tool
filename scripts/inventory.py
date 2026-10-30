"""Inventory installed components and original notices, without interpreting licenses."""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from distribution_support import ROOT, contained, environment, sha, write_record

NOTICE = ("license", "licence", "notice", "copying", "copyright")
PROBE = """
import importlib.metadata as m, json, platform, sys
print(json.dumps({'python': platform.python_version(), 'platform': platform.platform(),
 'prefix': sys.prefix, 'packages': [
 {'name': d.metadata['Name'], 'version': d.version,
  'root': str(d.locate_file('').absolute()),
  'license': d.metadata.get('License-Expression') or d.metadata.get('License') or 'unknown',
  'files': [str(d.locate_file(p).absolute()) for p in d.files or []]}
 for d in m.distributions() if d.metadata['Name'].lower() != 'consumer-agent-testing-tool']}))
"""


def normalized(name):
    return re.sub(r"[-_.]+", "-", name.lower())


def replace_file(source, destination):
    """Copy beside ``destination`` and rename over it, never writing through the existing path:
    older evidence hard-links notices.zip, and an in-place copy would rewrite every link."""
    destination = Path(destination)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def is_notice(path):
    name = Path(path).name.lower()
    return name.startswith(NOTICE) or bool(re.search(r"[-_.](?:licen[cs]e|notice)(?:[.-]|$)", name))


def supplemental_notices(name, version, stage, group, source_root=None):
    references = []
    manifest = ROOT / "third_party/supplemental-notices.json"
    if not manifest.is_file():
        return references
    for entry in json.loads(manifest.read_text())["entries"]:
        if (entry["name"], entry["version"]) != (name, version):
            continue
        original = contained(ROOT, entry["path"])
        if sha(original) != entry["sha256"]:
            raise ValueError(f"Supplemental notice hash mismatch: {name}")
        artifacts = entry.get("artifacts")
        if "artifacts" in entry:
            if source_root is None or not isinstance(artifacts, dict) or not artifacts:
                raise ValueError("Supplemental artifact notices require installed payload bindings")
            for relative, expected in artifacts.items():
                if not isinstance(relative, str) or not relative \
                        or PurePosixPath(relative).is_absolute() or "\\" in relative \
                        or ".." in PurePosixPath(relative).parts \
                        or str(PurePosixPath(relative)) != relative \
                        or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError("Invalid supplemental artifact binding")
                payload = contained(source_root, source_root / relative)
                if not payload.is_file() or sha(payload) != expected:
                    raise ValueError(f"Supplemental artifact hash mismatch: {name}/{relative}")
        reference = notice_copy(original, stage, group, f"{name.replace('/', '__')}-{version}")
        reference.update(source_url=entry["source_url"], basis=entry["basis"])
        if artifacts is not None:
            reference["installed_artifacts"] = dict(artifacts)
        if entry.get("reference"):
            declared = entry["reference"]
            relative = Path(declared["path"])
            if source_root is None or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("A referenced notice requires a package-local source document")
            document = contained(source_root, source_root / relative)
            if sha(document) != declared["sha256"] or declared["url"] not in document.read_text():
                raise ValueError(f"Notice reference does not match the installed package: {name}")
            reference["referenced_by"] = {
                **notice_copy(document, stage, group, f"{name.replace('/', '__')}-{version}"),
                "installed_path": str(relative), "url": declared["url"],
            }
            reference["checked_at"] = entry["checked_at"]
        references.append(reference)
    return references


def notice_copy(source, stage, group, component):
    source = contained(ROOT, source)
    key = sha(source)
    relative = Path("notices") / group / component / (key[:12] + "-" + source.name)
    destination = contained(stage, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {"path": str(Path("third_party") / relative), "sha256": key}


def bom(name, components, **properties):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "metadata": {"component": {"type": "application", "name": name, "version": version},
                         "properties": [{"name": "consumer-agent-testing-tool:" + k, "value": str(v)}
                                        for k, v in properties.items()]},
            "components": components}


def python_inventory(python, stage, group, locks):
    result = json.loads(subprocess.check_output(
        [str(python), "-I", "-c", PROBE], env=environment(ROOT, stage / "probe"),
        timeout=30, text=True))
    contained(ROOT, result["prefix"])
    components, gaps = [], []
    for package in sorted(result["packages"], key=lambda item: item["name"].lower()):
        name, version = normalized(package["name"]), package["version"]
        package_root = contained(Path(result["prefix"]), package["root"])
        locked = next((p for p in locks if normalized(p["name"]) == name and p["version"] == version), None)
        notices = [notice_copy(path, stage, group, f"{name}-{version}")
                   for path in package["files"] if is_notice(path)
                   and contained(ROOT, path).is_file()]
        notices.extend(supplemental_notices(
            name, version, stage, group, source_root=package_root))
        upstream_boms = [notice_copy(path, stage, group, f"{name}-{version}")
                         for path in package["files"] if "sboms" in Path(path).parts
                         and Path(path).suffix == ".json" and contained(ROOT, path).is_file()]
        if not locked:
            gaps.append({"name": name, "reason": "installed version absent from supplied locks"})
        if not notices:
            gaps.append({"name": name, "reason": "no original installed notice located"})
        components.append({"type": "library", "name": package["name"], "version": version,
            "bom-ref": f"{group}:pypi:{name}@{version}", "purl": f"pkg:pypi/{name}@{version}",
            "properties": [
                {"name": "consumer-agent-testing-tool:notice-files", "value": json.dumps(notices)},
                {"name": "consumer-agent-testing-tool:declared-license", "value": package["license"]},
                {"name": "consumer-agent-testing-tool:locked-wheel-hashes", "value": json.dumps(
                    [row["hash"] for row in locked.get("wheels", [])] if locked else [])},
                {"name": "consumer-agent-testing-tool:scope", "value": group},
                {"name": "consumer-agent-testing-tool:upstream-sboms", "value": json.dumps(upstream_boms)},
            ]})
    return result, components, gaps


def node_package_files(source):
    """Capture installed bytes/modes, excluding separately inventoried nested packages."""
    result = {}
    for directory, folders, names in os.walk(source, followlinks=False):
        folders[:] = sorted(name for name in folders if name != "node_modules")
        for name in [*folders, *sorted(names)]:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("Installed package contains a link or special file")
            if stat.S_ISREG(info.st_mode):
                result[path.relative_to(source).as_posix()] = {
                    "sha256": sha(path), "bytes": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
                after = path.lstat()
                if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode) != \
                        (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
                    raise ValueError("Installed package changed during inventory")
    return result


def installed_node_packages(project):
    """Read only canonical npm lock locations; missing optional packages stay explicit."""
    project = contained(ROOT, project)
    lock = json.loads((project / "package-lock.json").read_text())
    installed, absent = {}, []
    for location, package in lock["packages"].items():
        if not location:
            continue
        parts = PurePosixPath(location).parts
        if not re.fullmatch(r"node_modules/(?:@[^/@]+/)?[^/@]+(?:/node_modules/(?:@[^/@]+/)?[^/@]+)*", location) \
                or "\\" in location or \
                any(part in (".", "..") for part in location.split("/")) or PurePosixPath(location).is_absolute():
            raise ValueError("Installed Node package location is not a canonical npm path")
        for length in range(1, len(parts) + 1):
            if project.joinpath(*parts[:length]).is_symlink():
                raise ValueError("Installed Node package location contains a link")
        source = contained(ROOT, project / location)
        if (source / "package.json").is_symlink():
            raise ValueError("Installed Node manifest is a link")
        if not (source / "package.json").is_file():
            # Other-platform optional packages are locked, but not installed components.
            if package.get("optional") and not source.exists():
                absent.append(location)
                continue
            raise ValueError(f"Locked Node package is missing a regular manifest: {location}")
        recorded_files = node_package_files(source)
        manifest = (source / "package.json").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != recorded_files["package.json"]["sha256"]:
            raise ValueError("Installed Node manifest changed during inventory")
        actual = json.loads(manifest)
        if actual["version"] != package["version"]:
            raise ValueError(f"Installed Node version differs from lock: {location}")
        expected_name = package.get("name", location.rsplit("node_modules/", 1)[-1])
        if actual["name"] != expected_name:
            raise ValueError(f"Installed Node name differs from lock: {location}")
        installed[location] = {"source": source, "package": package, "actual": actual,
                               "files": recorded_files}
    return installed, absent


def node_content_identity(project):
    """Identity of locked installed files, not an assertion about publisher authenticity."""
    installed, absent = installed_node_packages(project)
    return {"lock_sha256": sha(project / "package-lock.json"),
            "installed": {name: row["files"] for name, row in installed.items()}, "absent_optional": absent}


def node_inventory(project, stage, group, native=None):
    installed, absent = installed_node_packages(project)
    components, gaps = [], []
    for location, row in installed.items():
        source, actual, package = row["source"], row["actual"], row["package"]
        name, version = actual["name"], actual["version"]
        notices = []
        for path in source.rglob("*"):
            if "node_modules" in path.relative_to(source).parts or not is_notice(path):
                continue
            path = contained(ROOT, path)
            if path.is_file():
                notices.append(notice_copy(path, stage, group, f"{name.replace('/', '__')}-{version}"))
        notices.extend(supplemental_notices(name, version, stage, group, source_root=source))
        if not notices:
            gaps.append({"name": name, "version": version, "reason": "no original installed notice located"})
        components.append({"type": "library", "name": name, "version": version,
            "bom-ref": f"{group}:{location}", "purl": f"pkg:npm/{name}@{version}",
            "properties": [
                {"name": "consumer-agent-testing-tool:integrity", "value": package.get("integrity", "unknown")},
                {"name": "consumer-agent-testing-tool:notice-files", "value": json.dumps(notices)},
                {"name": "consumer-agent-testing-tool:declared-license", "value": json.dumps(
                    actual.get("license", package.get("license", "unknown")))},
                {"name": "consumer-agent-testing-tool:scope", "value": group},
                {"name": "consumer-agent-testing-tool:installed-files", "value": json.dumps(row["files"], sort_keys=True)},
            ]})
        if native is not None:
            native.add(group, f"{group}:{location}", {
                name: (source / name, info["sha256"]) for name, info in row["files"].items()},
                package={"name": name, "version": version})
    if native is not None:
        native.observed_groups.add(group)
    return components, gaps, absent


def browser_inventory(project, stage, native=None):
    cache = contained(ROOT, ROOT / ".cache/ms-playwright")
    registry = contained(ROOT, project / "tooling/payment-ui/node_modules/playwright-core/browsers.json")
    files, notices = {}, []
    if cache.exists():
        for path in sorted(cache.rglob("*")):
            if ".links" in path.parts:
                continue  # Installer bookkeeping is not a runtime artifact.
            path = contained(ROOT, path)
            if path.is_file():
                files[str(path.relative_to(cache))] = sha(path)
                if path.name.lower().startswith(NOTICE):
                    notices.append(notice_copy(path, stage, "browser-runtime", "chromium"))
    if native is not None:
        native.add("browser-runtime", "browser-runtime:workspace-playwright", {
            name: (cache / name, checksum) for name, checksum in files.items()})
    return {
        "scope": "Existing workspace browser runtime used by tests; browser binaries are not shipped in the wheel",
        "registry_sha256": sha(registry) if registry.is_file() else None,
        "registry": json.loads(registry.read_text()) if registry.is_file() else None,
        "file_hashes": files, "original_notices": notices,
        "font_files": [name for name in files if Path(name).suffix.lower() in (".ttf", ".otf", ".woff", ".woff2")],
        "system_fonts": "not inventoried; system files outside workspace were not inspected",
        "download_archive_sha256": None,
    }


def main():
    from native_inventory import NativeInventory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-python", type=Path)
    parser.add_argument("--api-python", type=Path)
    parser.add_argument("--local-consumer", action="store_true",
                        help="Inventory only the supported core/OpenRouter and consumer Node dependencies")
    parser.add_argument("--node-root", type=Path, default=ROOT,
                        help="Workspace-local project with the actual installed optional tooling")
    args = parser.parse_args()
    project = contained(ROOT, args.node_root)
    stage = Path(tempfile.mkdtemp(prefix="i-", dir=ROOT / ".cache"))
    native = NativeInventory(ROOT)
    locks = []
    for path in (ROOT / "uv.lock", ROOT / "tooling/build-python/uv.lock"):
        if path.exists():
            locks.extend(tomllib.loads(path.read_text())["package"])
    development, python_gaps = [], []
    if not args.local_consumer:
        _, development, python_gaps = python_inventory(sys.executable, stage, "development-python", locks)
    api, api_gaps = [], [] if args.local_consumer else [{"reason": "fresh optional API installation not supplied"}]
    if args.api_python:
        api_installation, api, api_gaps = python_inventory(
            contained(ROOT, args.api_python.parent) / args.api_python.name, stage, "optional-api-python", locks)
        native.python("optional-api-python", api_installation, api)
    optional, node_gaps, missing_optional = [], {}, {}
    for group, path in (("node-protocol", "tooling"), ("node-browser", "tooling/payment-ui"),
                        ("node-api", "tooling/api")):
        if args.local_consumer and group == "node-api":
            continue
        components, gaps, absent = node_inventory(project / path, stage, group, native)
        optional.extend(components)
        node_gaps[group], missing_optional[group] = gaps, absent
    write_record(stage / "sbom.cdx.json", bom("consumer-agent-testing-tool-development-and-optional-tooling",
                                            development + api + optional))
    if args.core_python:
        core, components, core_gaps = python_inventory(
            contained(ROOT, args.core_python.parent) / args.core_python.name, stage, "core-python", locks)
        native.python("core-python", core, components)
        # The release verifier records the observed host in verification.json/tool_versions.
        # Host patch versions are not component identity and must not churn the bundled notices.
        write_record(stage / "core-sbom.cdx.json", bom("consumer-agent-testing-tool-core-installation", components,
            scope="Fresh core dependency installation; optional tooling and runtimes inventoried separately",
            environment_evidence="Release verification.json: tool_versions",
            dependency_lock_sha256=sha(ROOT / "uv.lock")))
    else:
        core_gaps = [{"reason": "fresh core installation not supplied"}]
    browser = browser_inventory(project, stage, native)
    write_record(stage / "browser-runtime.json", browser)
    write_record(stage / "native-runtime.json", native.result())
    write_record(stage / "fixture-assets.json", {
        "scope": "Packaged synthetic fixtures and scenarios; no customer data or model artifacts",
        "files": {str(path.relative_to(ROOT / "src/consumer_agent_testing_tool")): sha(path)
                  for directory in ("fixtures", "scenarios", "scenarios_api", "scenarios_browser", "openapi")
                  for path in (ROOT / "src/consumer_agent_testing_tool/data" / directory).rglob("*") if path.is_file()}})
    gaps = {"scope": "Installed components only; original notices copied without a licensing opinion",
        "python_development": python_gaps, "python_core": core_gaps, "python_api": api_gaps, "node": node_gaps,
        "optional_locked_but_not_installed": missing_optional,
        "separate_artifacts": {"model_weights": "not installed/unverified",
            "tokenizer": "not installed/unverified", "quantization": "not installed/unverified",
            "chat_template": "not installed/unverified",
            "browser": "browser-runtime.json; local runtime is not distributed",
            "native_subcomponents": "Project notices do not establish a complete SBOM for libraries "
                                    "statically embedded in native Node or browser binaries",
            "container": "Separate historic container/inventory.json; not reverified here",
            "service_terms": "not assessed by an open-source component inventory"}}
    if args.local_consumer:
        from consumer_agent_testing_tool.release_scope import RELEASE_SCOPE
        gaps["release_scope"] = RELEASE_SCOPE
        gaps["excluded"] = ["development-python", "optional-api-python", "node-api",
                            "model weights", "host OS", "worker images"]
    write_record(stage / "inventory-gaps.json", gaps)
    # Archive only current generated documents/notices plus the separately identified OS inventory.
    archive_path = stage / "notices.zip"
    sources = [(path, str(path.relative_to(stage))) for path in stage.rglob("*")
               if path.is_file() and "probe" not in path.relative_to(stage).parts]
    container = ROOT / "third_party/container"
    if container.exists() and not args.local_consumer:
        sources.extend((contained(ROOT, path), str(path.relative_to(ROOT / "third_party")))
                       for path in container.rglob("*") if path.is_file())
    supplemental = ROOT / "third_party/supplemental"
    if supplemental.exists():
        sources.extend((contained(ROOT, path), str(path.relative_to(ROOT / "third_party")))
                       for path in supplemental.rglob("*") if path.is_file())
        sources.append((ROOT / "third_party/supplemental-notices.json", "supplemental-notices.json"))
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in sorted(sources, key=lambda item: item[1]):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    output = ROOT / "third_party"
    output.mkdir(exist_ok=True)
    # Move the previous generated notice tree to the private workspace cache, preserving history.
    if (output / "notices").exists():
        shutil.move(output / "notices", stage / "previous-notices")
    shutil.move(stage / "notices", output / "notices")
    for path in stage.glob("*.json"):
        shutil.copyfile(path, output / path.name)
    replace_file(archive_path, output / "notices.zip")
    print(f"Inventoried {len(development)} development Python and {len(optional)} installed Node components")
    print(f"Original-notice gaps: core={len(core_gaps)}, development={len(python_gaps)}, "
          f"Node={sum(len(value) for value in node_gaps.values())}")


if __name__ == "__main__":
    main()
