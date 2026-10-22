"""A report from another source/run cannot renew deterministic release gates."""
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest

from consumer_agent_testing_tool.release import audit, default_ledger, results_ledger, source_digest
from consumer_agent_testing_tool.serialization import file_digest, load

NOW = datetime(2026, 10, 13, tzinfo=UTC)
REQUIRED = {"transactional-consent": {"test_effect"}, "replay-diff": {"test_replay"}}


@pytest.fixture
def recorder(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/record_release_tests.py"
    spec = importlib.util.spec_from_file_location("gate_recorder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REQUIRED", REQUIRED)
    return module


def write(path, value):
    path.write_text(json.dumps(value))


def fixture(root):
    """Synthetic proof chain for the recorder, never a real release attestation."""
    source = root / "src/consumer_agent_testing_tool/subject.py"
    source.parent.mkdir(parents=True)
    source.write_text("# synthetic implementation\n")
    destination = root / "release/evidence"
    destination.mkdir(parents=True)
    report = destination / "regression-test.xml"
    suite = ElementTree.Element("testsuite", tests="3")
    checks = []
    for name in ("test_effect", "test_replay", "test_analysis"):
        ElementTree.SubElement(suite, "testcase", classname="tests.test_synthetic", name=name)
        checks.append({"id": f"tests.test_synthetic::{name}", "status": "passed"})
    ElementTree.ElementTree(suite).write(report)
    log = report.with_suffix(".log")
    log.write_text("synthetic 3 passed\n")
    run = report.with_suffix(".run.json")
    write(run, {"schema_version": 1, "status": "passed", "returncode": 0,
                "source_before": source_digest(root), "source_after": source_digest(root),
                "junit": str(report.relative_to(root)), "junit_sha256": file_digest(report),
                "log": str(log.relative_to(root)), "log_sha256": file_digest(log)})
    regression = destination / "regression.json"
    write(regression, {"gate": "regression", "basis": "test-report", "status": "passed",
                       "source_sha256": source_digest(root), "junit": str(report.relative_to(root)),
                       "junit_sha256": file_digest(report), "run_record_sha256": file_digest(run),
                       "collected_cases": 3, "checks": checks})
    ledger = root / "release/release-gates.json"
    write(ledger, {"gates": {"regression": {
        "status": "passed", "basis": "test-report", "artifact": str(regression.relative_to(root)),
        "sha256": file_digest(regression), "expires_at": (NOW + timedelta(days=1)).isoformat()},
        # A gate recorder must leave separately attested live gates alone.
        "bedrock-iam": {"status": "unverified", "reason": "no live evidence"}}})
    return report


def resign_regression(root):
    ledger = root / "release/release-gates.json"
    document = load(ledger)
    document["gates"]["regression"]["sha256"] = file_digest(root / "release/evidence/regression.json")
    write(ledger, document)


def test_release_recorder_uses_matching_current_complete_regression(tmp_path, recorder):
    report = fixture(tmp_path)
    ledger = tmp_path / "release/release-gates.json"
    previous = load(ledger)
    results = recorder.record(tmp_path, report, now=NOW)
    assert set(results) == set(REQUIRED)
    recorded = load(ledger)
    for name in ("regression", "bedrock-iam"):
        assert recorded["gates"][name] == previous["gates"][name]
    for name, artifact in results.items():
        assert artifact["status"] == "passed"
        assert artifact["source_sha256"] == source_digest(tmp_path)
        assert artifact["run_record_sha256"] == file_digest(report.with_suffix(".run.json"))
        assert artifact["regression_sha256"] == previous["gates"]["regression"]["sha256"]
        assert file_digest(tmp_path / recorded["gates"][name]["artifact"]) == recorded["gates"][name]["sha256"]
    result = audit(tmp_path, ledger, NOW)
    assert all(result["gates"][name]["status"] == "passed" for name in REQUIRED)
    assert not result["release_qualified"]
    # No required live gate is graduated by deterministic tests.
    assert "bedrock-iam" not in result["gates"]
    assert "bedrock-iam" in result["excluded_capabilities"]


def test_release_recorder_writes_the_ledger_format_of_the_other_recorders(tmp_path, recorder):
    recorder.record(tmp_path, fixture(tmp_path), now=NOW)
    written = [tmp_path / "release/release-gates.json",
               *(tmp_path / "release/evidence" / f"{gate}.json" for gate in REQUIRED)]
    for path in written:  # As record_distribution and record_regression write them: indent 2, final newline.
        text = path.read_text()
        assert text == json.dumps(json.loads(text), indent=2) + "\n", path


@pytest.mark.parametrize("fault", [
    "source", "missing-run", "failed-run", "old-source", "changed-junit", "changed-log",
    "no-regression", "expired-regression", "changed-regression", "other-junit",
    "other-run", "incomplete-collection", "duplicate-collection", "wrong-count",
    "failed-check",
])
def test_release_recorder_rejects_unbound_evidence_without_writing(tmp_path, recorder, fault):
    report = fixture(tmp_path)
    run_path = report.with_suffix(".run.json")
    regression_path = tmp_path / "release/evidence/regression.json"
    ledger_path = tmp_path / "release/release-gates.json"
    if fault == "source":
        (tmp_path / "src/consumer_agent_testing_tool/subject.py").write_text("# changed\n")
    elif fault == "missing-run":
        run_path.unlink()
    elif fault in ("failed-run", "old-source"):
        run = load(run_path)
        run.update({"status": "failed", "returncode": 1} if fault == "failed-run"
                   else {"source_before": "old-source", "source_after": "old-source"})
        write(run_path, run)
    elif fault == "changed-junit":
        report.write_text(report.read_text().replace("test_effect", "different_test"))
    elif fault == "changed-log":
        report.with_suffix(".log").write_text("changed")
    elif fault in ("no-regression", "expired-regression"):
        ledger = load(ledger_path)
        if fault == "no-regression":
            ledger["gates"].pop("regression")
        else:
            ledger["gates"]["regression"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
        write(ledger_path, ledger)
    else:
        regression = load(regression_path)
        if fault == "changed-regression":
            regression["checks"][0]["status"] = "failed"
        elif fault == "other-junit":
            regression["junit"] = "some-other-report.xml"
        elif fault == "other-run":
            regression["run_record_sha256"] = "0" * 64
        elif fault == "incomplete-collection":
            regression["checks"].pop()
        elif fault == "duplicate-collection":
            regression["checks"].append(regression["checks"][0])
        elif fault == "wrong-count":
            regression["collected_cases"] = 2
        else:
            regression["checks"][0]["status"] = "failed"
        write(regression_path, regression)
        if fault != "changed-regression":
            resign_regression(tmp_path)
    for gate in REQUIRED:
        write(tmp_path / f"release/evidence/{gate}.json", {"prior": "leave untouched"})
    before = {str(path): path.read_bytes() for path in (tmp_path / "release").rglob("*") if path.is_file()}
    with pytest.raises((ValueError, OSError)):
        recorder.record(tmp_path, report, now=NOW)
    assert {str(path): path.read_bytes() for path in (tmp_path / "release").rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("fault", ["source", "report", "ledger"])
def test_release_recorder_rechecks_inputs_before_publishing(tmp_path, recorder, monkeypatch, fault):
    report = fixture(tmp_path)
    ledger_path = tmp_path / "release/release-gates.json"
    original = recorder.verified_report
    changed = None

    def interrupted(*args):
        nonlocal changed
        result = original(*args)
        path = {"source": tmp_path / "src/consumer_agent_testing_tool/subject.py",
                "report": report, "ledger": ledger_path}[fault]
        path.write_text(path.read_text() + "\n")
        changed = ledger_path.read_bytes()
        return result

    monkeypatch.setattr(recorder, "verified_report", interrupted)
    with pytest.raises(ValueError, match="changed while preparing"):
        recorder.record(tmp_path, report, now=NOW)
    assert ledger_path.read_bytes() == changed
    assert all(not (tmp_path / f"release/evidence/{gate}.json").exists() for gate in REQUIRED)


def test_release_recorder_missing_gate_cases_cannot_pass(tmp_path, recorder, monkeypatch):
    report = fixture(tmp_path)
    monkeypatch.setattr(recorder, "REQUIRED", {**REQUIRED, "replay-diff": {"test_missing"}})
    results = recorder.record(tmp_path, report, now=NOW)
    assert results["replay-diff"]["status"] == "failed"
    ledger = load(tmp_path / "release/release-gates.json")
    assert ledger["gates"]["replay-diff"]["status"] == "failed"


def test_surrogate_gate_requires_exact_target_projection_and_module(recorder):
    required = {"tests.test_surrogate_parity::test_payment[instinct-cli]"}
    for module, name in (("tests.test_surrogate_parity", "test_payment[muse-cli]"),
                         ("tests.test_surrogate_parity", "test_payment[instinct-native]"),
                         ("tests.test_elsewhere", "test_payment[instinct-cli]"),
                         ("tests.test_surrogate_parity", "test_payment")):
        case = ElementTree.Element("testcase", classname=module, name=name)
        assert recorder.required_case(case, required) is None
    exact = ElementTree.Element("testcase", classname="tests.test_surrogate_parity",
                                name="test_payment[instinct-cli]")
    assert recorder.required_case(exact, required) == next(iter(required))


def test_surrogate_gate_cannot_pass_from_unparameterized_family_coverage(tmp_path, recorder, monkeypatch):
    report = fixture(tmp_path)
    required = {"tests.test_synthetic::test_effect[muse-native]",
                "tests.test_synthetic::test_effect[instinct-native]"}
    monkeypatch.setattr(recorder, "REQUIRED", {"surrogate-parity": required})
    result = recorder.record(tmp_path, report, now=NOW)["surrogate-parity"]
    assert result["status"] == "failed"
    assert set(result["missing_cases"]) == required


def test_recorded_results_start_from_the_committed_policy_and_the_policy_alone_qualifies_nothing(tmp_path):
    policy = tmp_path / "docs/release-gates.json"
    policy.parent.mkdir()
    write(policy, {"gates": {"regression": {"status": "unverified", "reason": "Recorded by the release workflow"}}})
    assert default_ledger(tmp_path) == "docs/release-gates.json"
    result = audit(tmp_path, default_ledger(tmp_path), NOW)
    assert not result["release_qualified"]
    assert result["gates"]["regression"]["detail"] == "Recorded by the release workflow"
    ledger = results_ledger(tmp_path)
    assert ledger == tmp_path / "release/release-gates.json"
    assert ledger.read_bytes() == policy.read_bytes()
    assert default_ledger(tmp_path) == "release/release-gates.json"
    write(ledger, {"gates": {}})
    assert results_ledger(tmp_path).read_text() == '{"gates": {}}'  # an existing results ledger is kept
