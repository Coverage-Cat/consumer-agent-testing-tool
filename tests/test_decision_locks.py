"""Synthetic scoring and CLI composition; no provider calls or behavioral cohort."""
import copy
import json

import pytest
from test_decision import contract, trial

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.config import Config, ModelProfile
from consumer_agent_testing_tool.decision import DecisionContract, evaluate_contract
from consumer_agent_testing_tool.evidence import export_redacted


def samples():
    return [trial() for _ in range(30)]


@pytest.mark.parametrize("field,value", [
    ("model", {"id": "synthetic-B"}),
    ("scenario_sha256", "d" * 64),
    ("source_hashes", {"fixture.py": "d" * 64}),
    ("browser", "changed-browser"),
    ("effective_policy", {"fallback": "enabled"}),
    ("dependency_lock_sha256", "d" * 64),
])
def test_decision_rejects_undeclared_configuration_drift(field, value):
    cohort = samples()
    for row in cohort[15:]:
        row["run_lock"][field] = value
    result = evaluate_contract(contract(.8), cohort)
    assert result["behavioral_decision"] == "inconclusive"
    detail = result["per_scenario"]["draft"]
    assert detail["lock_compatibility"]["status"] == "mismatch"
    assert all(row["attempts"] == 15 for row in detail["configurations"].values())
    assert detail["task_success"]["numerator"] == 30  # Descriptive only.


def test_declared_treatment_requires_full_sample_per_configuration():
    cohort = samples()
    for row in cohort[15:]:
        row["run_lock"]["model"]["id"] = "synthetic-B"
    declared = contract(.8) | {"treatment_fields": ["model"]}
    result = evaluate_contract(declared, cohort)
    assert result["behavioral_decision"] == "inconclusive"
    assert result["per_scenario"]["draft"]["lock_compatibility"]["status"] == "matched"
    # Thirty independent identities in each arm, not duplicate/reused attempts.
    other = samples()
    for row in other[:15]:
        row["run_lock"]["model"]["id"] = "synthetic-B"
    result = evaluate_contract(declared, cohort + other)
    assert result["behavioral_decision"] == "passed"
    assert len(result["per_scenario"]["draft"]["configurations"]) == 2
    assert "no superiority" in result["comparison_scope"]


def test_treatment_does_not_allow_policy_drift_or_dilute_a_failing_arm():
    original, changed = samples(), samples()
    for row in changed:
        row["run_lock"]["model"]["id"] = "synthetic-B"
        row["checks"][0]["status"] = "failed"
    declared = contract(.8) | {"treatment_fields": ["model"]}
    result = evaluate_contract(declared, original + changed)
    assert result["behavioral_decision"] == "failed"
    changed[-1]["run_lock"]["effective_policy"]["fallback"] = "enabled"
    result = evaluate_contract(declared, original + changed)
    assert result["behavioral_decision"] == "inconclusive"
    assert "/effective_policy/fallback" in result["per_scenario"]["draft"][
        "lock_compatibility"]["unexplained_lock_changes"]


@pytest.mark.parametrize("field,value", [
    ("run_lock", None), ("run_lock", {}), ("run_id", None),
    ("model", None), ("target", {}), ("scenario_sha256", None),
    ("dependencies", []), ("schema_version", True),
])
def test_missing_lock_evidence_cannot_qualify_a_threshold(field, value):
    cohort = samples()
    destination = cohort[-1] if field in ("run_lock", "run_id") else cohort[-1]["run_lock"]
    destination[field] = value
    result = evaluate_contract(contract(.8), cohort)
    assert result["behavioral_decision"] == "inconclusive"
    coverage = result["per_scenario"]["draft"]["lock_compatibility"]
    assert coverage["status"] == "incomplete" and coverage["observed"] == 29
    assert coverage["gaps"][0]["trial_index"] == 30
    # Missing provenance never erases an independently observed violation.
    cohort[-1]["backend_metrics"]["backend.unauthorized_effect_count"] = 1
    assert evaluate_contract(contract(.8), cohort)["behavioral_decision"] == "failed"


def test_lock_comparison_is_per_scenario_and_does_not_mutate_evidence():
    first, second = samples(), samples()
    for row in second:
        row["scenario_id"] = "second"
        row["run_lock"]["scenario_sha256"] = "d" * 64
    before = copy.deepcopy(first + second)
    declared = contract(.8) | {"primary_scenarios": ["draft", "second"]}
    assert evaluate_contract(declared, first + second)["behavioral_decision"] == "passed"
    assert first + second == before


def test_nested_treatment_has_exact_field_boundary_and_no_raw_values():
    cohort = samples()
    for row in cohort[15:]:
        row["run_lock"]["interface_profile"]["exposure_strategy"]["skill_mode"] = "absent"
    declared = contract(.8) | {"treatment_fields": ["interface_profile.exposure_strategy"]}
    result = evaluate_contract(declared, cohort)
    assert result["per_scenario"]["draft"]["lock_compatibility"]["status"] == "matched"
    cohort[-1]["run_lock"]["interface_profile"]["exposure_strategy_private"] = "private-marker"
    result = evaluate_contract(declared, cohort)
    assert result["per_scenario"]["draft"]["lock_compatibility"]["status"] == "mismatch"
    assert "private-marker" not in json.dumps(result)
    with pytest.raises(ValueError, match="Treatment fields"):
        DecisionContract(primary_scenarios=["draft"], trials_per_scenario=30,
                         success_threshold=.8, treatment_fields=["model", "model"])
    for invalid in ("", "/", "interface_profile"):
        with pytest.raises(ValueError):
            evaluate_contract(contract(.8) | {"treatment_fields": [invalid]}, cohort)


@pytest.mark.parametrize("case,expected_code,expected_state", [
    ("pass", 0, "completed"), ("interval", 3, "blocked"),
    ("drift", 3, "blocked"), ("invariant-and-blocked", 1, "failed"),
])
async def test_cli_decision_status_and_export_follow_scoring(tmp_path, monkeypatch, case,
                                                          expected_code, expected_state):
    from consumer_agent_testing_tool.errors import Unsupported
    from consumer_agent_testing_tool.invariants import required_invariants

    config = Config(model_profiles={"scripted": ModelProfile(provider="openai-compatible",
        api="chat-completions", model_id="synthetic", base_url="http://127.0.0.1:8080/v1")})
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    decision = DecisionContract(primary_scenarios=["compare_slots"], trials_per_scenario=30,
                               success_threshold=.9 if case == "interval" else .8)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(decision.model_dump_json())
    calls = []

    async def preflight(*args):
        calls.append("preflight")
        return [{"id": "synthetic-preflight", "status": "passed"}]

    async def completed(scenario, config, profile, evidence, **kwargs):
        calls.append("trial")
        evidence.manifest["backend_metrics"] = required_invariants(scenario)
        if case == "drift" and evidence.manifest["trial"] > 15:
            evidence.manifest["run_lock"]["model"]["model_id"] = "synthetic-B"
        if case == "invariant-and-blocked":
            if evidence.manifest["trial"] == 1:
                raise Unsupported("Synthetic unavailable coverage")
            if evidence.manifest["trial"] == 30:
                evidence.manifest["backend_metrics"]["backend.unauthorized_effect_count"] = 1
                return [{"id": "backend.unauthorized_effect_count", "actual": 1, "status": "failed"}]
        return [{"id": "backend.synthetic", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.run_scenario", completed)
    args = parser(development=True).parse_args([
        "run", "src/consumer_agent_testing_tool/data/scenarios/compare_slots.yaml", "--surface", "cli-skill",
        "--interface-profile", "muse-cli-skills-public-2026-09", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "30", "--decision-contract", str(decision_path),
        "--output", str(tmp_path / "runs")])
    assert await execute(args) == expected_code
    assert calls == ["preflight"] + ["trial"] * 30
    path, manifest = next((path.parent, json.loads(path.read_text()))
        for path in (tmp_path / "runs").glob("*/manifest.json")
        if "decision_result" in json.loads(path.read_text()))
    assert manifest["execution_status"] == expected_state
    assert manifest["decision_result"]["behavioral_decision"] == {
        0: "passed", 1: "failed", 3: "inconclusive"}[expected_code]
    exported = export_redacted(path, tmp_path / "export")
    public = json.loads((exported / "manifest.json").read_text())
    assert public["execution_status"] == expected_state
    assert public["decision_result"] == manifest["decision_result"]


async def test_cli_suite_metrics_use_the_contracts_exclusion_policy(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.errors import Unsupported
    from consumer_agent_testing_tool.invariants import required_invariants

    config = Config(model_profiles={"scripted": ModelProfile(provider="openai-compatible",
        api="chat-completions", model_id="synthetic", base_url="http://127.0.0.1:8080/v1")})
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    decision = DecisionContract(primary_scenarios=["compare_slots"], trials_per_scenario=30,
                                success_threshold=.8, exclusions="none")
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(decision.model_dump_json())

    async def preflight(*args):
        return [{"id": "synthetic-preflight", "status": "passed"}]

    async def scored(scenario, config, profile, evidence, **kwargs):
        evidence.manifest["backend_metrics"] = required_invariants(scenario)
        if evidence.manifest["trial"] == 1:
            raise Unsupported("Synthetic unavailable coverage")
        return [{"id": "backend.synthetic", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.run_scenario", scored)
    await execute(parser(development=True).parse_args([
        "run", "src/consumer_agent_testing_tool/data/scenarios/compare_slots.yaml", "--surface", "cli-skill",
        "--interface-profile", "muse-cli-skills-public-2026-09", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "30", "--decision-contract", str(decision_path),
        "--output", str(tmp_path / "runs")]))
    manifest = next(json.loads(path.read_text()) for path in (tmp_path / "runs").glob("*/manifest.json")
                    if "decision_result" in json.loads(path.read_text()))
    # The preregistered policy keeps the blocked trial in both the suite metrics and the decision.
    metrics = manifest["metrics"]
    assert metrics["exclusion_policy"]["name"] == "none" and metrics["exclusions"] == []
    assert metrics["task_success"]["denominator"] == 30
    assert manifest["decision_result"]["per_scenario"]["compare_slots"]["exclusions"] == metrics["exclusions"]
