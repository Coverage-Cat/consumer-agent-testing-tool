"""Synthetic schema/negative tests only; these never qualify an external service."""
import copy
import json
from datetime import timedelta

import pytest
from test_calibration_observations import NOW, packet

from consumer_agent_testing_tool.calibration import evaluate
from consumer_agent_testing_tool.qualification import REQUIREMENTS, Reference, References
from consumer_agent_testing_tool.release import audit, source_digest
from consumer_agent_testing_tool.serialization import digest, file_digest


def write(root, name, document):
    path = root / name
    path.write_text(json.dumps(document))
    return {"path": name, "sha256": file_digest(path)}


def model(provider="bedrock", mode="api_key"):
    if provider == "local-glimmer":
        return {
            "provider": provider, "api": "chat-completions", "model_id": "local-glimmer",
            "base_url": "http://127.0.0.1:8080/v1", "runtime_pid": 123,
            "runtime_build": "synthetic-contract-fixture", "memory_budget_mb": 24000,
            "artifacts": {name: {"path": f"models/{name}", "sha256": "b" * 64}
                          for name in ("weights", "runtime")},
        }
    return {
        "provider": provider, "api": "converse", "model_id": "fixture-model",
        "region": "us-east-1", "auth": {
            "mode": mode,
            **({"env": "AWS_BEARER_TOKEN_BEDROCK"} if mode == "api_key" else {}),
        },
    }


def live_document(root, gate):
    selected = model("local-glimmer") if gate == "local-glimmer-offline" else model(
        mode="aws_credentials" if gate == "bedrock-iam" else "api_key")
    configuration = {"platform": "synthetic-contract-platform",
                     "execution": {"backend": "local"}, "model": selected}
    if gate == "local-glimmer-offline":
        configuration["offline"] = True
    if gate == "browser-isolation":
        configuration["browser_runtime"] = {
            "image": "sha256:" + "b" * 64, "kernel": "fixture-kernel",
            "architecture": "arm64", "chromium": "fixture-build", "boundary": "fixture-boundary",
        }
    if gate == "cloud-worker-lifecycle":
        configuration["execution"]["backend"] = "worker"
        configuration["execution"]["provider"] = "maritime"
        configuration["cloud"] = {
            "provider": "maritime", "api_url": "https://cloud.example",
            "namespace": "fixture-tests", "template_id": "fixture-template",
        }
    lock = {
        "harness": "fixture", "platform": configuration["platform"],
        "execution": configuration["execution"], "model": selected,
        "dependencies": [{"name": "fixture", "version": "1"}],
        "source_hashes": {"fixture.py": "a" * 64},
        "dependency_lock_sha256": "c" * 64,
    }
    artifact = write(root, "observations.json", {"purpose": "synthetic validator test"})
    qualification = {
        "schema_version": 1, "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        "scopes": [{
            "id": "fixture-scope", "configuration": write(root, "configuration.json", configuration),
            "run_lock": write(root, "run-lock.json", lock),
            "checks": [{"id": name, "status": "passed",
                        "basis": "live" if live else "test-report", "artifacts": [artifact]}
                       for name, live in sorted(REQUIREMENTS[gate].items())],
        }],
    }
    return qualification


def check(root, gate, qualification, *, kind=None, now=NOW):
    # Claiming live in this isolated fixture tests the operator-attestation
    # contract, not authenticity. Nothing is written to the workspace ledger.
    basis = "authorized-observation" if gate == "muse-calibration" else "live"
    kind = kind or ("muse-observed" if gate == "muse-calibration" else "live")
    evidence = {"gate": gate, "basis": basis, "status": "passed", "evidence_kind": kind,
                "checks": [{"id": "qualification-review", "status": "passed"}],
                "source_sha256": source_digest(root), "qualification": qualification}
    artifact = write(root, "receipt.json", evidence)
    write(root, "ledger.json", {"gates": {gate: {
        "status": "passed", "basis": basis, "artifact": artifact["path"],
        "sha256": artifact["sha256"], "expires_at": (NOW + timedelta(hours=2)).isoformat(),
    }}})
    return audit(root, "ledger.json", now)["gates"][gate]["status"]


@pytest.mark.parametrize("gate", [*REQUIREMENTS, "muse-calibration"])
@pytest.mark.parametrize("kind", ["synthetic", "live"])
def test_summary_only_reports_cannot_qualify_external_gates(tmp_path, gate, kind):
    assert check(tmp_path, gate, None, kind=kind) == "unverified"


@pytest.mark.parametrize("gate", REQUIREMENTS)
def test_complete_live_receipt_contract_and_changed_artifact(tmp_path, gate):
    qualification = live_document(tmp_path, gate)
    assert check(tmp_path, gate, qualification) == "passed"
    write(tmp_path, "observations.json", {"different": True})
    assert check(tmp_path, gate, qualification) == "unverified"


@pytest.mark.parametrize("gate", REQUIREMENTS)
def test_each_required_live_check_must_be_present(tmp_path, gate):
    qualification = live_document(tmp_path, gate)
    checks = qualification["scopes"][0]["checks"]
    for index in range(len(checks)):
        changed = copy.deepcopy(qualification)
        changed["scopes"][0]["checks"].pop(index)
        assert check(tmp_path, gate, changed) == "unverified", checks[index]["id"]


@pytest.mark.parametrize("gate", REQUIREMENTS)
def test_deterministic_evidence_cannot_replace_required_live_observations(tmp_path, gate):
    qualification = live_document(tmp_path, gate)
    for row in qualification["scopes"][0]["checks"]:
        if REQUIREMENTS[gate][row["id"]]:
            row["basis"] = "test-report"
    assert check(tmp_path, gate, qualification) == "unverified"


@pytest.mark.parametrize("change", [
    "duplicate-check", "unknown-check", "failed-check", "missing-artifacts", "empty-artifact",
    "missing-file", "escaping-path", "absolute-path", "bad-hash", "duplicate-scope",
    "empty-scopes", "missing-version", "bool-version", "future-date", "no-timezone",
    "wrong-platform", "wrong-model", "wrong-backend", "missing-run-lock",
    "empty-scope-id", "invalid-dependencies", "invalid-source-hashes", "invalid-dependency-lock",
])
def test_invalid_live_qualification_stays_unverified(tmp_path, change):
    qualification = live_document(tmp_path, "bedrock-bearer")
    scope = qualification["scopes"][0]
    row = scope["checks"][0]
    if change == "duplicate-check":
        scope["checks"].append(copy.deepcopy(row))
    elif change == "unknown-check":
        row["id"] = "text-completion-only"
    elif change == "failed-check":
        row["status"] = "failed"
    elif change == "missing-artifacts":
        row["artifacts"] = []
    elif change == "empty-artifact":
        (tmp_path / "empty.log").write_text("")
        row["artifacts"] = [{"path": "empty.log", "sha256": file_digest(tmp_path / "empty.log")}]
    elif change == "missing-file":
        row["artifacts"][0]["path"] = "missing.log"
    elif change == "escaping-path":
        row["artifacts"][0]["path"] = "../outside.log"
    elif change == "absolute-path":
        row["artifacts"][0]["path"] = str(tmp_path / "observations.json")
    elif change == "bad-hash":
        row["artifacts"][0]["sha256"] = "a" * 64
    elif change == "duplicate-scope":
        qualification["scopes"].append(copy.deepcopy(scope))
    elif change == "empty-scopes":
        qualification["scopes"] = []
    elif change == "empty-scope-id":
        scope["id"] = " "
    elif change == "missing-version":
        qualification.pop("schema_version")
    elif change == "bool-version":
        qualification["schema_version"] = True
    elif change == "future-date":
        qualification["observed_at"] = (NOW + timedelta(minutes=1)).isoformat()
    elif change == "no-timezone":
        qualification["observed_at"] = "2026-10-12T10:00:00"
    elif change == "missing-run-lock":
        scope.pop("run_lock")
    else:
        lock = json.loads((tmp_path / "run-lock.json").read_text())
        if change == "wrong-platform":
            lock["platform"] = "another-platform"
        elif change == "wrong-model":
            lock["model"]["model_id"] = "another-model"
        elif change == "invalid-dependencies":
            lock["dependencies"] = "unobserved"
        elif change == "invalid-source-hashes":
            lock["source_hashes"] = {"fixture.py": "unknown"}
        elif change == "invalid-dependency-lock":
            lock["dependency_lock_sha256"] = None
        else:
            lock["execution"]["backend"] = "worker"
        scope["run_lock"] = write(tmp_path, "run-lock.json", lock)
    assert check(tmp_path, "bedrock-bearer", qualification) == "unverified"


@pytest.mark.parametrize("gate,change", [
    ("bedrock-bearer", "iam"), ("bedrock-iam", "bearer"),
    ("bedrock-bearer", "loopback"), ("bedrock-bearer", "placeholder"),
    ("local-glimmer-offline", "online"), ("local-glimmer-offline", "artifact"),
    ("local-glimmer-offline", "budget"),
    ("browser-isolation", "image"), ("browser-isolation", "runtime"),
    ("cloud-worker-lifecycle", "local-provider"), ("cloud-worker-lifecycle", "loopback"),
    ("cloud-worker-lifecycle", "placeholder"),
])
def test_configuration_must_match_qualified_capability(tmp_path, gate, change):
    qualification = live_document(tmp_path, gate)
    scope = qualification["scopes"][0]
    configuration = json.loads((tmp_path / "configuration.json").read_text())
    if change == "iam":
        configuration["model"] = model(mode="aws_credentials")
    elif change == "bearer":
        configuration["model"] = model()
    elif change == "loopback":
        if gate == "cloud-worker-lifecycle":
            configuration["cloud"]["api_url"] = "http://127.0.0.1:8080"
        else:
            configuration["model"]["base_url"] = "http://127.0.0.1:8080"
    elif change == "placeholder":
        if gate == "cloud-worker-lifecycle":
            configuration["cloud"]["template_id"] = "<replace-with-template>"
        else:
            configuration["model"]["model_id"] = "<replace-with-model>"
    elif change == "online":
        configuration["offline"] = False
    elif change == "artifact":
        configuration["model"]["artifacts"].pop("weights")
    elif change == "budget":
        configuration["model"].pop("memory_budget_mb")
    elif change == "image":
        configuration["browser_runtime"]["image"] = "latest"
    elif change == "runtime":
        configuration.pop("browser_runtime")
    else:
        configuration["cloud"]["provider"] = "command"
    scope["configuration"] = write(tmp_path, "configuration.json", configuration)
    assert check(tmp_path, gate, qualification) == "unverified"


def test_every_platform_scope_requires_complete_observations(tmp_path):
    qualification = live_document(tmp_path, "browser-isolation")
    second = copy.deepcopy(qualification["scopes"][0])
    second["id"] = "another-platform"
    qualification["scopes"].append(second)
    assert check(tmp_path, "browser-isolation", qualification) == "passed"
    second["checks"].pop()
    assert check(tmp_path, "browser-isolation", qualification) == "unverified"


def test_one_path_cannot_identify_different_versions_during_audit(tmp_path):
    references = References(tmp_path)
    first = write(tmp_path, "observation.json", {"version": 1})
    references.check(Reference.model_validate(first))
    second = write(tmp_path, "observation.json", {"version": 2})
    with pytest.raises(ValueError, match="different evidence versions"):
        references.check(Reference.model_validate(second))


def muse_document(root, *, kind="muse-observed"):
    cohort, pair = packet(root, kind=kind)
    profile = {"profile": "synthetic-schema-test"}
    cohort["profile_sha256"] = pair["profile_sha256"] = digest(profile)
    heldout = write(root, "pair.json", pair)
    cohort["observations"][0]["artifact_sha256"] = heldout["sha256"]
    calibration = copy.deepcopy(pair)
    calibration["scenario_id"] = "calibration"
    reference = write(root, "calibration.json", calibration)
    cohort["observations"].append({
        "scenario_id": "calibration", "trial_id": "one", "split": "calibration",
        "artifact": reference["path"], "artifact_sha256": reference["sha256"],
    })
    cohort_ref = write(root, "cohort.json", cohort)
    result = evaluate(root, "cohort.json", profile_sha256=digest(profile),
                      surface_revision="fixture-v1", now=NOW)
    return {
        "schema_version": 1, "observed_at": cohort["observed_at"], "cohort": cohort_ref,
        "profile": write(root, "profile.json", profile), "surface_revision": "fixture-v1",
        "study_plan": write(root, "plan.json", {"purpose": "synthetic operator-review fixture"}),
        "comparison": write(root, "comparison.json", result),
    }


def test_muse_gate_recomputes_comparison_and_rechecks_underlying_sources(tmp_path):
    qualification = muse_document(tmp_path)
    assert check(tmp_path, "muse-calibration", qualification) == "passed"
    (tmp_path / "transcript.txt").write_text("changed underlying observation")
    assert check(tmp_path, "muse-calibration", qualification) == "unverified"


@pytest.mark.parametrize("change", [
    "synthetic", "expired", "profile", "revision", "comparison", "comparison-bool",
    "no-calibration", "missing-dimension", "date", "no-plan", "unauthorized",
])
def test_muse_gate_rejects_ineligible_or_substituted_observations(tmp_path, change):
    qualification = muse_document(tmp_path, kind="synthetic" if change == "synthetic" else "muse-observed")
    now = NOW
    if change == "expired":
        # Keep the outer receipt current while expiring the actual cohort.
        cohort = json.loads((tmp_path / "cohort.json").read_text())
        cohort["expires_at"] = NOW.isoformat()
        qualification["cohort"] = write(tmp_path, "cohort.json", cohort)
    elif change == "profile":
        qualification["profile"] = write(tmp_path, "profile.json", {"different": True})
    elif change == "revision":
        qualification["surface_revision"] = "changed"
    elif change.startswith("comparison"):
        saved = json.loads((tmp_path / "comparison.json").read_text())
        saved["usable_for_comparison"] = 1 if change == "comparison-bool" else False
        qualification["comparison"] = write(tmp_path, "comparison.json", saved)
    elif change in ("no-calibration", "missing-dimension", "unauthorized"):
        cohort = json.loads((tmp_path / "cohort.json").read_text())
        if change == "no-calibration":
            cohort["observations"] = cohort["observations"][:1]
        elif change == "unauthorized":
            cohort["authorized"] = False
        else:
            pair = json.loads((tmp_path / "pair.json").read_text())
            pair["muse"]["interventions"] = None
            reference = write(tmp_path, "pair.json", pair)
            cohort["observations"][0]["artifact_sha256"] = reference["sha256"]
            cohort["primary_dimensions"] = ["task_outcome"]
        qualification["cohort"] = write(tmp_path, "cohort.json", cohort)
        if change != "unauthorized":
            result = evaluate(tmp_path, "cohort.json", profile_sha256=digest({"profile": "synthetic-schema-test"}),
                              surface_revision="fixture-v1", now=NOW)
            qualification["comparison"] = write(tmp_path, "comparison.json", result)
    elif change == "date":
        qualification["observed_at"] = (NOW - timedelta(minutes=1)).isoformat()
    elif change == "no-plan":
        qualification.pop("study_plan")
    assert check(tmp_path, "muse-calibration", qualification, now=now) == "unverified"
