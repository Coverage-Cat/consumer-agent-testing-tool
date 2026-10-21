"""Fresh offline proof of the local consumer release, with no cloud or model setup."""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from distribution_support import ROOT, Commands, sha, uv_executable, write_record
from verify_distribution import (
    archive_previous,
    node_snapshot,
    stage_node,
    verification_inputs,
    verify_source_archive,
    verify_wheel,
)

from consumer_agent_testing_tool.release import source_digest
from consumer_agent_testing_tool.release_scope import NODE_GROUPS, RELEASE_SCOPE

REQUIRED = {
    "tool-versions", "export-core", "export-build", "create-core", "install-core",
    "create-build", "install-build", "install-node-protocol", "install-node-browser",
    "inventory", "build", "install-wheel", "init", "static-inspect", "static-check",
    "contract-check", "report", "export", "packaged-workflows",
}
INVENTORIES = {"notices.zip", "core-sbom.cdx.json", "sbom.cdx.json",
               "inventory-gaps.json", "browser-runtime.json", "native-runtime.json", "fixture-assets.json"}
WORKFLOWS = {
    "embedded_consumer_verified",
    "connector_lane_verified", "surrogate_targets_verified",
    "kernel_default_egress_denied", "playback_verified", "private_diagnostics_verified",
    "private_export_excluded", "report_recovery_verified", "api_continuation_verified",
    "browser_continuation_verified", "stop_recovery_verified", "curated_api_contract_verified",
    "curated_api_behavior_verified", "curated_api_continuation_verified",
    "curated_outcome_verification_verified", "pending_violation_verified",
    "invariant_history_verified", "network_decisions_verified", "configuration_decisions_verified",
    "browser_oracle_reporting_verified", "browser_measurements_verified",
    "authentication_lifecycle_verified",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-cache", action="store_true",
                        help="Explicit setup: download supported locked dependencies only")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="local-dist-", dir=ROOT / ".cache"))
    project = work / "project"
    project.mkdir()
    commands = Commands(ROOT, work, setup=args.prepare_cache)
    report = {"status": "running", "release_scope": RELEASE_SCOPE,
              "recorded_at": datetime.now(UTC).isoformat(),
              "phase": "dependency-setup" if args.prepare_cache else "offline-distribution-verification",
              "workspace": str(work.relative_to(ROOT)), "steps": commands.steps,
              "verification_inputs": verification_inputs(), "inference": "not called",
              "external_network": "dependency setup only" if args.prepare_cache else "kernel-denied external egress",
              "test_scope": "fresh core/OpenRouter + consumer Node tooling + local synthetic workflows"}
    destination = ROOT / ("dist/setup.json" if args.prepare_cache else "dist/verification.json")
    archive_previous(destination)
    try:
        uv = uv_executable(ROOT)
        if not args.prepare_cache:
            log = commands.run("tool-versions", [sys.executable, "-c",
                ("import json,platform,subprocess; print(json.dumps({'python':platform.python_version(),"
                "'platform':platform.platform(),'node':subprocess.check_output(['node','--version'],text=True).strip(),"
                "'npm':subprocess.check_output(['npm','--version'],text=True).strip()}))")])
            report["tool_versions"] = json.loads(log.read_text())
        for name in ("core", "build"):
            requirement = work / f"{name}.txt"
            project_args = ["--project", ROOT / "tooling/build-python"] if name == "build" else []
            extra = ["--extra", "openrouter"] if name == "core" else []
            commands.run(f"export-{name}", [uv, "export", *project_args, "--frozen", "--offline",
                "--no-dev", "--no-emit-project", *extra, "--output-file", requirement])
            venv = work / name
            commands.run(f"create-{name}", [uv, "venv", "--offline", "--python", sys.executable, venv])
            commands.run(f"install-{name}", [uv, "pip", "install",
                *([] if args.prepare_cache else ["--offline"]), "--require-hashes",
                "--only-binary", ":all:", "--python", venv / "bin/python", "-r", requirement])
        stage_node(project, NODE_GROUPS)
        for label, group in zip(("protocol", "browser"), NODE_GROUPS, strict=True):
            commands.run("install-node-" + label, ["npm", "ci", "--ignore-scripts",
                *([] if args.prepare_cache else ["--offline"])], cwd=project / group)
        if args.prepare_cache:
            report["status"] = "passed"
            return 0
        report["node_content"] = {"before": node_snapshot(project, work / "node-before.json", NODE_GROUPS)}
        python = work / "core/bin/python"
        commands.run("inventory", [sys.executable, ROOT / "scripts/inventory.py", "--local-consumer",
                                  "--core-python", python, "--node-root", project])
        report["inventory"] = {name: sha(ROOT / "third_party" / name) for name in sorted(INVENTORIES)}
        gaps = json.loads((ROOT / "third_party/inventory-gaps.json").read_text())
        if gaps["python_core"] or any(gaps["node"].values()):
            raise RuntimeError("Supported dependency installation has missing original project notices")
        report["source_sha256"] = source_digest(ROOT)
        distribution = work / "dist"
        commands.run("build", [uv, "build", "--offline", "--no-build-isolation",
            "--python", work / "build/bin/python", "--out-dir", distribution])
        wheels, archives = list(distribution.glob("*.whl")), list(distribution.glob("*.tar.gz"))
        if len(wheels) != 1 or len(archives) != 1:
            raise RuntimeError("Expected one wheel and one source archive")
        wheel, archive = wheels[0], archives[0]
        report["wheel_content"] = verify_wheel(wheel)
        report["source_archive_content"] = verify_source_archive(archive)
        commands.run("install-wheel", [uv, "pip", "install", "--offline", "--no-deps", "--python", python, wheel])
        executable = work / "core/bin/consumer-agent-testing-tool"
        commands.run("init", [executable, "init", "--provider", "none"], cwd=project)
        if (project / "tooling/api").exists() or (project / "calibration-examples").exists():
            raise RuntimeError("Default initialization installed excluded development tooling")
        for name, arguments in (
            ("static-inspect", ["inspect", "booking-service", "--offline"]),
            ("static-check", ["check", "booking-service", "--suite", "static", "--offline"]),
            ("contract-check", ["check", "booking-service", "--suite", "contract", "--offline"]),
        ):
            commands.run(name, [executable, *arguments], cwd=project)
        records = [p for p in (project / "runs").glob("*/manifest.json")
                   if json.loads(p.read_text())["lane"] == "contract"]
        if len(records) != 1:
            raise RuntimeError("Expected exactly one installed contract record")
        manifest = json.loads(records[0].read_text())
        if manifest["execution_status"] != "completed" or any(
            c["status"] != "passed" for c in manifest["checks"] if c.get("required", True)
        ):
            raise RuntimeError("Installed contract checks failed")
        for name in ("report", "export"):
            commands.run(name, [executable, name, records[0].parent], cwd=project)
        (project / ".cache").mkdir(exist_ok=True)
        (project / ".cache/ms-playwright").symlink_to(ROOT / ".cache/ms-playwright", target_is_directory=True)
        commands.run("packaged-workflows", [python, "-I", ROOT / "scripts/distribution_smoke.py",
                     project, work / "core", "--local-consumer"], cwd=project)
        report["packaged_workflows"] = json.loads((project / "distribution-smoke.json").read_text())
        report["node_content"]["after"] = node_snapshot(project, work / "node-after.json", NODE_GROUPS)
        if report["node_content"]["before"]["sha256"] != report["node_content"]["after"]["sha256"]:
            raise RuntimeError("Installed Node files changed during verification")
        report["node_content"]["verified"] = True
        if verification_inputs() != report["verification_inputs"]:
            raise RuntimeError("Packaging inputs changed during verification")
        if source_digest(ROOT) != report["source_sha256"]:
            raise RuntimeError("Product source changed during distribution verification")
        (ROOT / "dist").mkdir(exist_ok=True)  # A clean checkout has no dist/ yet.
        for path in (wheel, archive):
            published = ROOT / "dist" / path.name
            if published.exists() and sha(path) != sha(published):
                historical = ROOT / "dist/checkpoints" / sha(published)[:12]
                historical.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(published, historical / path.name)
            shutil.copyfile(path, published)
        report.update(status="passed", source_sha256=source_digest(ROOT),
                      wheel=wheel.name, wheel_sha256=sha(wheel), source_archive=archive.name,
                      source_archive_sha256=sha(archive), contract_run_id=manifest["run_id"])
        verify(ROOT, report)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        report.update(status="failed", error=str(error))
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if report["status"] == "running":
            report["status"] = "failed"
        write_record(work / "verification.json", report)
        write_record(destination, report)
        print(f"Distribution {report['status']}: {destination.relative_to(ROOT)}", flush=True)


def verify(root, report):
    from distribution_support import contained
    if report.get("release_scope") != RELEASE_SCOPE or report.get("status") != "passed" \
            or report.get("phase") != "offline-distribution-verification" \
            or report.get("source_sha256") != source_digest(root):
        raise ValueError("A current-source local-consumer distribution proof is required")
    steps = report["steps"]
    if {s["step"] for s in steps} != REQUIRED or len(steps) != len(REQUIRED):
        raise ValueError("Distribution stages are missing or duplicated")
    for step in steps:
        if step.get("status") != "passed" or step.get("returncode") != 0 or not step.get("network_policy") \
                or sha(contained(root, step["log"])) != step["log_sha256"]:
            raise ValueError("Distribution stage did not pass or its log changed")
    inputs = report.get("verification_inputs", {})
    if not {"scripts/local_distribution.py", "scripts/distribution_smoke.py", "scripts/inventory.py",
            "scripts/verify_distribution.py", "scripts/distribution_support.py",
            "scripts/surface_fixture.mjs",
            "pyproject.toml", "uv.lock", "tooling/package-lock.json",
            "tooling/payment-ui/package-lock.json", "tooling/build-python/uv.lock"} <= inputs.keys():
        raise ValueError("Distribution inputs are incomplete")
    for name, checksum in inputs.items():
        if sha(contained(root, name)) != checksum:
            raise ValueError("Distribution inputs changed")
    if set(report.get("inventory", {})) != INVENTORIES:
        raise ValueError("Distribution inventory is incomplete")
    for name, checksum in report["inventory"].items():
        if sha(root / "third_party" / name) != checksum:
            raise ValueError("Distribution inventory changed")
    gaps = json.loads((root / "third_party/inventory-gaps.json").read_text())
    if gaps.get("release_scope") != RELEASE_SCOPE or gaps.get("python_core") != [] \
            or set(gaps.get("node", {})) != {"node-protocol", "node-browser"} \
            or any(gaps["node"].values()):
        raise ValueError("Supported dependency notices are incomplete")
    content = report["node_content"]
    if content.get("verified") is not True or content["before"]["sha256"] != content["after"]["sha256"]:
        raise ValueError("Installed Node contents changed or are unverified")
    for reference in (content["before"], content["after"]):
        path = contained(root, reference["path"])
        if sha(path) != reference["sha256"]:
            raise ValueError("Installed Node snapshot changed")
        snapshot = json.loads(path.read_text())
        if snapshot.get("format") != "node-package-files-v1" or set(snapshot["groups"]) != set(NODE_GROUPS):
            raise ValueError("Installed Node scope is incomplete")
        for group, identity in snapshot["groups"].items():
            if not identity.get("installed") or identity["lock_sha256"] != sha(root / group / "package-lock.json"):
                raise ValueError("Installed Node dependencies differ from their lock")
    for name in ("wheel", "source_archive"):
        if Path(report[name]).name != report[name] or sha(root / "dist" / report[name]) != report[name + "_sha256"]:
            raise ValueError("Published artifact differs from the tested artifact")
    smoke = report["packaged_workflows"]
    if smoke.get("release_scope") != RELEASE_SCOPE or smoke.get("status") != "passed" \
            or not all(smoke.get(name) is True for name in WORKFLOWS):
        raise ValueError("Packaged consumer workflows are incomplete")
    from consumer_agent_testing_tool.surface import inside
    project = inside(root, Path(report.get("workspace", "")) / "project")
    verify_connector_targets(project, smoke.get("connector_targets"))
    backend = smoke.get("browser_backend", {})
    if backend.get("name") != "@playwright/mcp" or backend.get("transport") != "private-in-memory" \
            or not all(backend.get(key) for key in ("version", "playwright_version", "protocol_version")):
        raise ValueError("Packaged native browser backend is unverified")
    for lane in ("api_checks", "browser_scenario_checks"):
        if not smoke.get(lane) or any(c["status"] != "passed" for c in smoke[lane]):
            raise ValueError("Packaged consumer scenario checks failed")
        for scorer, expected in (("backend.confirmed_count", 1), ("backend.duplicate_effect_count", 0),
                                 ("backend.unauthorized_effect_count", 0)):
            if sum(c["id"] == scorer and c.get("actual") == expected and c.get("expected") == expected
                   for c in smoke[lane]) != 1:
                raise ValueError("Packaged workflow lacks independent backend assertions")
    return [{"id": step["step"], "status": "passed"} for step in steps]


def verify_connector_targets(project, rows):
    """A boolean cannot qualify target support: require four distinct sealed runs, exports and readiness."""
    from consumer_agent_testing_tool.emulation import surrogate_contract
    from consumer_agent_testing_tool.replay import playback
    from consumer_agent_testing_tool.surface import inside

    expected = {f"{target}/{projection}" for target in ("muse", "instinct") for projection in ("native", "cli")}
    if not isinstance(rows, dict) or set(rows) != expected:
        raise ValueError("Packaged surrogate targets need both native/CLI runs for Muse and Instinct")
    used = set()
    for key, row in rows.items():
        target, projection = key.split("/")
        if row.get("emulation_target") != target or row.get("projection") != projection \
                or type(row.get("confirmed_count")) is not int or row["confirmed_count"] != 1:
            raise ValueError("Packaged surrogate target or independent outcome does not match its treatment")
        for field, checksum in (("run", "result_sha256"), ("export", "export_sha256"),
                                ("readiness", "readiness_sha256")):
            try:
                if not isinstance(row.get(field), str) or Path(row[field]).is_absolute():
                    raise ValueError("Expected a project-relative run")
                path = inside(project, row[field])
                if path in used or sha(path / "result.json") != row.get(checksum):
                    raise ValueError("Missing, repeated or changed sealed result")
                used.add(path)
                result = playback(path)["result"]
                if field == "export" and (path / "private").exists():
                    raise ValueError("Export contains private evidence")
            except Exception as error:
                raise ValueError(f"Packaged surrogate {key} {field} proof is invalid: {error}") from error
            lock = result.get("run_lock") or {}
            if result.get("emulation_target") != target or lock.get("projection") != projection \
                    or result.get("status") != "completed" or result.get("reporting_status") != "completed" \
                    or lock.get("surrogate_contract") != surrogate_contract(target):
                raise ValueError(f"Packaged surrogate {key} {field} has the wrong target, contract or status")
            if field == "readiness":
                if result.get("kind") != "surrogate-readiness-check" or not result.get("checks") \
                        or any(check.get("status") in ("failed", "unsupported") for check in result["checks"]):
                    raise ValueError("Packaged surrogate readiness has no passing catalog proof")
            elif not any(check.get("id") == "one-confirmed-booking" and check.get("status") == "passed"
                         and check.get("actual") == 1 for check in result.get("checks", [])):
                raise ValueError("Packaged surrogate has no independent booking proof")
