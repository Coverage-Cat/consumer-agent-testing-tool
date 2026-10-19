import importlib
import json
import platform
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def packaging(monkeypatch):
    monkeypatch.syspath_prepend(str(Path.cwd() / "scripts"))
    return tuple(importlib.import_module(name) for name in (
        "distribution_support", "verify_distribution", "inventory"))


def test_distribution_environment_excludes_credentials_and_global_configuration(tmp_path, monkeypatch, packaging):
    support, _, _ = packaging
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "OPENROUTER_API_KEY", "PYTHONPATH",
                 "UV_INDEX_URL", "npm_config_registry", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "synthetic-inherited-setting")
    work = tmp_path / "work"
    env = support.environment(tmp_path, work)
    assert "synthetic-inherited-setting" not in env.values()
    for key in ("HOME", "TMPDIR", "UV_CACHE_DIR", "npm_config_cache",
                "npm_config_userconfig", "npm_config_globalconfig", "PLAYWRIGHT_BROWSERS_PATH"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert env["npm_config_userconfig"] != env["npm_config_globalconfig"]
    assert env["UV_PYTHON_DOWNLOADS"] == "never" and env["UV_NO_CONFIG"] == "1"


def test_distribution_paths_reject_sibling_and_symlink_escape(tmp_path, packaging):
    support, _, _ = packaging
    root = tmp_path / "allowed"
    root.mkdir()
    sibling = tmp_path / "other"
    sibling.mkdir()
    (root / "link").symlink_to(sibling, target_is_directory=True)
    for path in ("../other/file", "link/file"):
        with pytest.raises(ValueError, match="workspace"):
            support.contained(root, path)


def test_distribution_uses_uv_from_path(tmp_path, monkeypatch, packaging):
    support, _, _ = packaging
    installed = tmp_path / "tools/uv"
    monkeypatch.setattr(support.shutil, "which", lambda name: str(installed) if name == "uv" else None)
    assert support.uv_executable(tmp_path) == installed


def test_distribution_can_use_a_workspace_uv(tmp_path, monkeypatch, packaging):
    support, _, _ = packaging
    monkeypatch.setattr(support.shutil, "which", lambda name: None)
    local = tmp_path / ".venv/bin/uv"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\nexit 0\n")
    local.chmod(0o755)
    assert support.uv_executable(tmp_path) == local
    local.chmod(0o644)
    with pytest.raises(RuntimeError, match="install uv"):
        support.uv_executable(tmp_path)


def test_distribution_explains_a_missing_uv(tmp_path, monkeypatch, packaging):
    support, _, _ = packaging
    monkeypatch.setattr(support.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="install uv.*PATH"):
        support.uv_executable(tmp_path)


def test_timed_out_packaging_command_retains_failed_log_evidence(tmp_path, monkeypatch, packaging):
    support, _, _ = packaging
    commands = support.Commands(tmp_path, tmp_path / "work", setup=True)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["synthetic-command"], 1)

    monkeypatch.setattr(support.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        commands.run("timeout", ["synthetic-command"])
    assert commands.steps[0]["status"] == "failed"
    assert commands.steps[0]["log_sha256"]


@pytest.mark.parametrize("stage", ["static-inspect", "static-check", "contract-check"])
def test_offline_cli_stages_do_not_nest_seatbelt(tmp_path, monkeypatch, packaging, stage):
    support, _, _ = packaging
    commands = support.Commands(tmp_path, tmp_path / "work")
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(support.subprocess, "run", run)
    command = ["/synthetic/consumer-agent-testing-tool", "inspect", "fixture", "--offline"]
    commands.run(stage, command)
    assert calls == [command]
    assert commands.steps[0]["network_policy"] == "CLI --offline exact-port kernel policy"
    with pytest.raises(RuntimeError, match="requires.*--offline"):
        commands.run(stage, command[:-1])
    assert calls == [command]


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS packaging egress boundary")
def test_distribution_kernel_policy_denies_build_network_and_allows_only_smoke_loopback(tmp_path, packaging):
    support, _, _ = packaging
    commands = support.Commands(tmp_path, tmp_path / "work")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        address = server.getsockname()
        program = (
            "import socket; s=socket.socket(); s.settimeout(1)\n"
            f"try: s.connect({address!r})\n"
            "except PermissionError: pass\n"
            "else: raise SystemExit('Unexpected outbound connection')"
        )
        commands.run("build-probe", [sys.executable, "-c", program])
        for name in ("packaged-workflows", "packaged-worker-continuation", "packaged-worker-container"):
            commands.run(name, [sys.executable, "-c",
                f"import socket; s=socket.create_connection({address!r}, 1); s.close()"])
            connection, _ = server.accept()
            connection.close()
    assert all(step["status"] == "passed" and step["network_policy"] for step in commands.steps)


@pytest.mark.parametrize("fault", [None, "tamper", "link", "missing"])
def test_worker_wheel_cache_is_bound_to_source_lock(tmp_path, monkeypatch, packaging, fault):
    support, verification, _ = packaging
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    cache = tmp_path / ".cache/worker-python-wheels"
    cache.mkdir(parents=True)
    wheel = cache / "example-1.0-py3-none-any.whl"
    wheel.write_bytes(b"synthetic wheel bytes")
    checksum = support.sha(wheel)
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="example"\nversion="1.0"\nwheels=[{url="https://invalid.test/' +
        wheel.name + '", hash="sha256:' + checksum + '"}]\n')
    if fault == "tamper":
        wheel.write_bytes(b"changed")
    elif fault == "link":
        target = cache / "source"
        wheel.rename(target)
        wheel.symlink_to(target.name)
    elif fault == "missing":
        wheel.unlink()
    project = tmp_path / "project"
    project.mkdir()
    if fault:
        with pytest.raises(RuntimeError, match="wheel|Wheel"):
            verification.stage_python_wheels(project)
    else:
        assert verification.stage_python_wheels(project) == {wheel.name: checksum}
        assert support.sha(project / "worker-wheels" / wheel.name) == checksum


def node_project(tmp_path):
    project = tmp_path / "tooling"
    package = project / "node_modules/example"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "example", "version": "1.0.0"}))
    (project / "package-lock.json").write_text(json.dumps({"packages": {
        "": {}, "node_modules/example": {"version": "1.0.0", "integrity": "synthetic"},
        "node_modules/other-platform": {"version": "1.0.0", "optional": True},
    }}))
    return project, package


def test_node_inventory_counts_installed_components_and_validates_notice_provenance(tmp_path, monkeypatch, packaging):
    support, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, package = node_project(tmp_path)
    original = tmp_path / "third_party/supplemental/example/LICENSE"
    original.parent.mkdir(parents=True)
    original.write_text("Synthetic test notice")
    record = tmp_path / "third_party/supplemental-notices.json"
    record.write_text(json.dumps({"entries": [{"name": "example", "version": "1.0.0",
        "path": str(original.relative_to(tmp_path)), "sha256": support.sha(original),
        "source_url": "https://example.invalid/pinned/notice", "basis": "synthetic fixture"}]}))
    components, gaps, absent = inventory.node_inventory(project, tmp_path / "stage", "node-test")
    assert [row["name"] for row in components] == ["example"]
    assert not gaps and absent == ["node_modules/other-platform"]
    notice = json.loads(components[0]["properties"][1]["value"])[0]
    assert notice["basis"] == "synthetic fixture"
    original.write_text("Tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        inventory.node_inventory(project, tmp_path / "stage", "node-test")
    (package / "package.json").write_text(json.dumps({"name": "example", "version": "2.0.0"}))
    with pytest.raises(ValueError, match="version"):
        inventory.node_inventory(project, tmp_path / "stage", "node-test")


def test_inventory_keeps_prefixed_original_license_files(tmp_path, monkeypatch, packaging):
    support, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, package = node_project(tmp_path)
    notice = package / "MIT-LICENSE.txt"
    notice.write_text("Synthetic original permission notice")
    components, gaps, _ = inventory.node_inventory(project, tmp_path / "stage", "node-test")
    assert not gaps
    copied = json.loads(components[0]["properties"][1]["value"])[0]
    assert copied["sha256"] == support.sha(notice)
    assert (tmp_path / "stage" / copied["path"].removeprefix("third_party/")).read_bytes() == notice.read_bytes()


def artifact_notice(tmp_path, package, support):
    project_notice = package / "LICENSE"
    project_notice.write_text("Synthetic project copyright and permission notice")
    payload = package / "font.ttf"
    payload.write_bytes(b"synthetic font payload")
    notice = tmp_path / "third_party/supplemental/example/FONT-LICENSE.txt"
    notice.parent.mkdir(parents=True)
    notice.write_text("Synthetic original font copyright and permission notice")
    entry = {"name": "example", "version": "1.0.0",
             "path": str(notice.relative_to(tmp_path)), "sha256": support.sha(notice),
             "source_url": "https://example.invalid/pinned/FONT-LICENSE.txt",
             "basis": "Synthetic exact-payload fixture",
             "artifacts": {"font.ttf": support.sha(payload)}}
    manifest = tmp_path / "third_party/supplemental-notices.json"
    manifest.write_text(json.dumps({"entries": [entry]}))
    return project_notice, payload, notice, entry, manifest


@pytest.mark.parametrize("ecosystem", ["node", "python3.13", "python3.14"])
def test_supplemental_payload_notice_survives_existing_project_license(
        tmp_path, monkeypatch, packaging, ecosystem):
    support, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    if ecosystem == "node":
        project, package = node_project(tmp_path)
    else:
        prefix = tmp_path / "venv"
        package = prefix / "lib" / ecosystem / "site-packages"
        package.mkdir(parents=True)
    original, payload, notice, entry, _ = artifact_notice(tmp_path, package, support)
    stage = tmp_path / "stage"
    if ecosystem == "node":
        components, gaps, _ = inventory.node_inventory(project, stage, "node-test")
    else:
        # Identical bindings work across interpreter-specific site-packages paths.
        observed = {"prefix": str(prefix), "packages": [{
            "name": "example", "version": "1.0.0", "root": str(package), "license": "synthetic",
            "files": [str(original), str(payload)]}]}
        monkeypatch.setattr(inventory.subprocess, "check_output",
                            lambda *args, **kwargs: json.dumps(observed))
        _, components, gaps = inventory.python_inventory(
            tmp_path / "synthetic-python", stage, "python-test",
            [{"name": "example", "version": "1.0.0", "wheels": []}])
    assert not gaps
    copied = json.loads(next(p["value"] for p in components[0]["properties"]
                             if p["name"] == "consumer-agent-testing-tool:notice-files"))
    assert {row["sha256"] for row in copied} == {support.sha(original), support.sha(notice)}
    added = next(row for row in copied if row["sha256"] == support.sha(notice))
    assert added["installed_artifacts"] == entry["artifacts"]
    assert (stage / added["path"].removeprefix("third_party/")).read_bytes() == notice.read_bytes()


@pytest.mark.parametrize("alias", [False, True])
def test_python_notice_root_cannot_escape_selected_interpreter(
        tmp_path, monkeypatch, packaging, alias):
    _, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    prefix = tmp_path / "venv"
    prefix.mkdir()
    elsewhere = tmp_path / "different-installation"
    elsewhere.mkdir()
    package_root = elsewhere
    if alias:
        package_root = prefix / "linked-site-packages"
        package_root.symlink_to(elsewhere, target_is_directory=True)
    observed = {"prefix": str(prefix), "packages": [{
        "name": "example", "version": "1", "root": str(package_root), "license": "synthetic",
        "files": []}]}
    monkeypatch.setattr(inventory.subprocess, "check_output",
                        lambda *args, **kwargs: json.dumps(observed))
    with pytest.raises(ValueError, match="must stay inside"):
        inventory.python_inventory(tmp_path / "synthetic-python", tmp_path / "stage",
                                   "python-test", [{"name": "example", "version": "1"}])


@pytest.mark.parametrize("fault", [
    "changed", "missing", "escape", "alias", "empty", "bad-hash", "noncanonical", "null",
])
def test_supplemental_artifact_notice_requires_valid_installed_binding(
        tmp_path, monkeypatch, packaging, fault):
    support, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, package = node_project(tmp_path)
    _, payload, _, entry, manifest = artifact_notice(tmp_path, package, support)
    if fault == "changed":
        payload.write_bytes(b"changed without a package version change")
    elif fault == "missing":
        payload.unlink()
    elif fault in ("escape", "alias"):
        outside = package.parent / "not-in-package.ttf"
        outside.write_bytes(payload.read_bytes())
        entry["artifacts"] = {
            ("../not-in-package.ttf" if fault == "escape" else "font.ttf"): support.sha(outside)}
        if fault == "alias":
            payload.unlink()
            payload.symlink_to(outside)
    elif fault == "empty":
        entry["artifacts"] = {}
    elif fault == "bad-hash":
        entry["artifacts"]["font.ttf"] = "unknown"
    elif fault == "noncanonical":
        entry["artifacts"] = {"./font.ttf": support.sha(payload)}
    elif fault == "null":
        entry["artifacts"] = None
    manifest.write_text(json.dumps({"entries": [entry]}))
    with pytest.raises(ValueError, match="artifact|payload|link"):
        inventory.node_inventory(project, tmp_path / "stage", "node-test")


def test_installed_content_changes_without_a_version_change(tmp_path, monkeypatch, packaging):
    _, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, package = node_project(tmp_path)
    script = package / "index.js"
    script.write_text("module.exports = 1;")
    before = inventory.node_content_identity(project)
    script.write_text("module.exports = 2;")
    after = inventory.node_content_identity(project)
    assert before != after
    assert before["lock_sha256"] == after["lock_sha256"]
    first, second = (record["installed"]["node_modules/example"] for record in (before, after))
    assert first["package.json"] == second["package.json"]
    assert first["index.js"]["sha256"] != second["index.js"]["sha256"]
    script.chmod(0o700)
    changed_mode = inventory.node_content_identity(project)["installed"]["node_modules/example"]
    assert second["index.js"]["sha256"] == changed_mode["index.js"]["sha256"]
    assert second["index.js"]["mode"] != changed_mode["index.js"]["mode"]


def test_partial_optional_package_is_not_treated_as_absent(tmp_path, monkeypatch, packaging):
    _, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, _ = node_project(tmp_path)
    partial = project / "node_modules/other-platform"
    partial.mkdir()
    (partial / "index.js").write_text("module.exports = 'incomplete installation';")
    with pytest.raises(ValueError, match="missing a regular manifest"):
        inventory.node_content_identity(project)


def test_manifest_identity_cannot_race_the_recorded_bytes(tmp_path, monkeypatch, packaging):
    _, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, _ = node_project(tmp_path)
    original = inventory.node_package_files

    def change(source):
        result = original(source)
        (source / "package.json").write_text('{"name":"example","version":"2.0.0"}')
        return result

    monkeypatch.setattr(inventory, "node_package_files", change)
    with pytest.raises(ValueError, match="manifest changed"):
        inventory.node_content_identity(project)

@pytest.mark.parametrize("location", ["../other", "node_modules/../other", "node_modules/example/elsewhere",
                                    "node_modules/example/"])
def test_inventory_rejects_noncanonical_package_locations(tmp_path, monkeypatch, packaging, location):
    _, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, _ = node_project(tmp_path)
    (project / "package-lock.json").write_text(json.dumps({"packages": {location: {"version": "1.0.0"}}}))
    with pytest.raises(ValueError, match="canonical npm"):
        inventory.node_content_identity(project)


@pytest.mark.parametrize("name", ["package.json", "LICENSE", "subdirectory"])
def test_inventory_rejects_package_links_before_copying(tmp_path, monkeypatch, packaging, name):
    _, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, package = node_project(tmp_path)
    original = tmp_path / "private-fixture"
    if name == "subdirectory":
        original.mkdir()
        (original / "LICENSE").write_text("Synthetic private value")
    else:
        original.write_text('{"name":"example","version":"1.0.0"}')
    path = package / name
    if path.exists():
        path.unlink()
    path.symlink_to(original, target_is_directory=name == "subdirectory")
    with pytest.raises(ValueError, match="link"):
        inventory.node_inventory(project, tmp_path / "stage", "node-test")
    assert not (tmp_path / "stage").exists()


def test_referenced_notice_is_bound_to_the_installed_readme(tmp_path, monkeypatch, packaging):
    support, _, inventory = packaging
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    project, package = node_project(tmp_path)
    readme = package / "README.md"
    readme.write_text("Original test reference: https://example.invalid/author-license")
    notice = tmp_path / "third_party/supplemental/example/author-notice.txt"
    notice.parent.mkdir(parents=True)
    notice.write_text("Original synthetic author notice")
    manifest = notice.parents[2] / "supplemental-notices.json"
    manifest.write_text(json.dumps({"entries": [{"name": "example", "version": "1.0.0",
        "path": str(notice.relative_to(tmp_path)), "sha256": support.sha(notice),
        "source_url": "https://example.invalid/author-license", "basis": "synthetic referenced source",
        "checked_at": "2026-10-12", "reference": {"path": "README.md", "sha256": support.sha(readme),
                                                "url": "https://example.invalid/author-license"}}]}))
    components, gaps, _ = inventory.node_inventory(project, tmp_path / "stage", "node-test")
    assert not gaps
    item = json.loads(components[0]["properties"][1]["value"])[0]
    assert item["referenced_by"]["sha256"] == support.sha(readme)
    readme.write_text("Reference changed but package version unchanged")
    with pytest.raises(ValueError, match="reference"):
        inventory.node_inventory(project, tmp_path / "second", "node-test")


def test_wheel_verification_rejects_stale_notices_missing_source_and_unexpected_data(tmp_path, monkeypatch, packaging):
    _, verifier, _ = packaging
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    source = tmp_path / "src/consumer_agent_testing_tool"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("# synthetic source")
    (tmp_path / "uv.lock").write_text("synthetic lock")
    (tmp_path / "third_party").mkdir()
    (tmp_path / "third_party/notices.zip").write_bytes(b"synthetic notices")
    contents = {"consumer_agent_testing_tool/__init__.py": "# synthetic source",
        "consumer_agent_testing_tool/data/locks/uv.lock": "synthetic lock",
        "consumer_agent_testing_tool/data/third_party/notices.zip": "synthetic notices"}
    wheel = tmp_path / "synthetic.whl"

    def make(entries):
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)

    make(contents)
    assert verifier.verify_wheel(wheel)["checked_source_files"] == 3
    make({**contents, "consumer_agent_testing_tool/data/third_party/notices.zip": "old notices"})
    with pytest.raises(RuntimeError, match="current source"):
        verifier.verify_wheel(wheel)
    make({**contents, "customer-data.txt": "synthetic private test value"})
    with pytest.raises(RuntimeError, match="unexpected"):
        verifier.verify_wheel(wheel)
    (source / "new.py").write_text("# new source")
    make(contents)
    with pytest.raises(KeyError):
        verifier.verify_wheel(wheel)


def test_initialization_installs_consumer_manifests_and_mapped_api_without_overwriting(tmp_path):
    from consumer_agent_testing_tool.cli import initialize, parser
    from consumer_agent_testing_tool.scenario import read_scenario
    path = tmp_path / "consumer-agent-testing-tool.yaml"
    custom = tmp_path / "tooling/payment-ui/package.json"
    custom.parent.mkdir(parents=True)
    custom.write_text('{"custom":true}')
    initialize(parser().parse_args(["init", "--config", str(path), "--provider", "none"]))
    assert custom.read_text() == '{"custom":true}'
    assert (custom.parent / "package-lock.json").is_file()
    assert not (tmp_path / "tooling/api").exists()
    assert not (tmp_path / "openapi").exists()
    assert read_scenario(tmp_path / "api-scenarios/stale_total.yaml", milestone=2)["surface"] == "api-tools"
    assert read_scenario(tmp_path / "browser-scenarios/stale_total.yaml", milestone=2)["surface"] == "browser"


def test_distribution_attestation_rejects_missing_proof_and_artifact_changes(tmp_path, packaging):
    import copy

    from consumer_agent_testing_tool.release_scope import RELEASE_SCOPE

    support, _, _ = packaging
    recorder = importlib.import_module("local_distribution")
    steps = []
    for name in recorder.REQUIRED:
        path = tmp_path / f"{name}.log"
        path.write_text("Synthetic stage log")
        steps.append({"step": name, "status": "passed", "returncode": 0,
                      "network_policy": "synthetic policy fixture", "log": path.name,
                      "log_sha256": support.sha(path)})
    inputs = {}
    for name in ("scripts/local_distribution.py", "scripts/verify_distribution.py",
                 "scripts/distribution_support.py", "scripts/distribution_smoke.py",
                 "scripts/surface_fixture.mjs", "scripts/inventory.py", "pyproject.toml", "uv.lock",
                 "tooling/package-lock.json", "tooling/payment-ui/package-lock.json",
                 "tooling/build-python/uv.lock"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic pinned input")
        inputs[name] = support.sha(path)
    (tmp_path / "third_party").mkdir()
    inventories = {}
    for name in recorder.INVENTORIES:
        path = tmp_path / "third_party" / name
        path.write_text(json.dumps({"release_scope": RELEASE_SCOPE, "python_core": [],
                                   "node": {"node-protocol": [], "node-browser": []}}))
        inventories[name] = support.sha(path)
    (tmp_path / "dist").mkdir()
    artifact = tmp_path / "dist/fixture.whl"
    artifact.write_bytes(b"Synthetic artifact")
    snapshot = tmp_path / "installed-node.json"
    snapshot.write_text(json.dumps({"format": "node-package-files-v1", "groups": {
        group: {"lock_sha256": support.sha(tmp_path / group / "package-lock.json"),
                "installed": {"node_modules/example": {"package.json": {"sha256": "synthetic"}}}}
        for group in recorder.NODE_GROUPS}}))
    identity = {"path": snapshot.name, "sha256": support.sha(snapshot)}
    checks = [{"id": name, "status": "passed", "expected": count, "actual": count}
              for name, count in (("backend.confirmed_count", 1), ("backend.duplicate_effect_count", 0),
                                  ("backend.unauthorized_effect_count", 0))]
    smoke = {**dict.fromkeys(recorder.WORKFLOWS, True), "release_scope": RELEASE_SCOPE,
             "status": "passed", "api_checks": checks, "browser_scenario_checks": checks,
             "browser_backend": {"name": "@playwright/mcp", "transport": "private-in-memory",
                                 "version": "fixture", "playwright_version": "fixture",
                                 "protocol_version": "fixture"}}
    report = {"release_scope": RELEASE_SCOPE, "status": "passed",
              "phase": "offline-distribution-verification", "source_sha256": recorder.source_digest(tmp_path),
              "steps": steps, "verification_inputs": inputs, "inventory": inventories,
              "wheel": artifact.name, "wheel_sha256": support.sha(artifact),
              "source_archive": artifact.name, "source_archive_sha256": support.sha(artifact),
              "node_content": {"before": identity, "after": identity, "verified": True},
              "packaged_workflows": smoke}
    assert len(recorder.verify(tmp_path, report)) == 19
    for key in recorder.WORKFLOWS:
        with pytest.raises(ValueError, match="workflows"):
            recorder.verify(tmp_path, {**report, "packaged_workflows": {**smoke, key: False}})
    for key in ("inventory", "verification_inputs"):
        with pytest.raises(ValueError, match="incomplete"):
            recorder.verify(tmp_path, {**report, key: {}})
    with pytest.raises(ValueError, match="stages"):
        recorder.verify(tmp_path, {**report, "steps": steps[:-1]})
    with pytest.raises(ValueError, match="scope|current-source"):
        recorder.verify(tmp_path, {**report, "release_scope": "legacy"})
    with pytest.raises(ValueError, match="contents"):
        recorder.verify(tmp_path, {**report, "node_content": {**report["node_content"], "verified": False}})
    broken = copy.deepcopy(report)
    broken["packaged_workflows"]["api_checks"][0]["actual"] = 0
    with pytest.raises(ValueError, match="backend assertions"):
        recorder.verify(tmp_path, broken)
    # Even a rehashed inventory cannot make a missing original notice acceptable.
    gaps = tmp_path / "third_party/inventory-gaps.json"
    original = gaps.read_bytes()
    data = json.loads(original)
    data["python_core"] = [{"name": "synthetic-missing-notice"}]
    gaps.write_text(json.dumps(data))
    changed = copy.deepcopy(report)
    changed["inventory"][gaps.name] = support.sha(gaps)
    with pytest.raises(ValueError, match="notices"):
        recorder.verify(tmp_path, changed)
    gaps.write_bytes(original)
    artifact.write_bytes(b"Changed artifact")
    with pytest.raises(ValueError, match="artifact"):
        recorder.verify(tmp_path, report)



def test_dependency_plan_counts_separate_core_and_build_versions(tmp_path, packaging):
    _, verifier, _ = packaging
    requirements, locks = [], []
    for version, size in (("1.0", 100), ("2.0", 200)):
        requirements.append(tmp_path / f"requirements-{version}.txt")
        requirements[-1].write_text(f"example=={version} \\\n --hash=sha256:synthetic\n")
        locks.append(tmp_path / f"lock-{version}.toml")
        locks[-1].write_text(
            f'[[package]]\nname="example"\nversion="{version}"\n'
            f'wheels=[{{url="https://example.invalid/example-{version}-py3-none-any.whl",size={size}}}]\n')
    plan = verifier.download_plan(requirements, locks)
    assert plan["packages"] == 2 and plan["maximum_wheel_bytes"] == 300
