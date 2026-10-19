from uuid import uuid4

import pytest

from consumer_agent_testing_tool.decision import DecisionContract, evaluate_contract


def trial(passed=True, status="completed"):
    # Explicit synthetic locks: these tests prove scoring, not live model behavior.
    return {"run_id": str(uuid4()), "scenario_id": "draft", "execution_status": status,
            "run_lock": {"schema_version": 1, "harness": "synthetic", "platform": "synthetic",
                         "python": "synthetic", "dependencies": [{"name": "synthetic", "version": "1"}],
                         "dependency_lock_sha256": "a" * 64, "source_hashes": {"fixture.py": "b" * 64},
                         "interface_profile": {"name": "synthetic", "exposure_strategy": {"skill_mode": "installed"}},
                         "model": {"id": "synthetic-A"}, "target": {"id": "synthetic"},
                         "scenario_sha256": "c" * 64, "effective_policy": {"fallback": "disabled"}},
            "backend_metrics": {"backend.unauthorized_effect_count": 0,
                                "backend.duplicate_effect_count": 0},
            "checks": [{"id": "backend.draft_count", "status": "passed" if passed else "failed"}]}


def contract(threshold):
    return DecisionContract(primary_scenarios=["draft"], trials_per_scenario=30,
        success_threshold=threshold).model_dump() | {"sha256": "fixture"}


def test_thirty_successes_do_not_prove_ninety_percent_reliability():
    result = evaluate_contract(contract(.9), [trial() for _ in range(30)])
    assert result["behavioral_decision"] == "inconclusive"
    assert result["per_scenario"]["draft"]["task_success"]["wilson_95"][0] < .9
    assert evaluate_contract(contract(.8), [trial() for _ in range(30)])["behavioral_decision"] == "passed"


def test_exclusions_do_not_turn_an_underpowered_trial_into_a_pass():
    trials = [trial() for _ in range(29)] + [trial(status="blocked")]
    result = evaluate_contract(contract(.8), trials)
    assert result["behavioral_decision"] == "inconclusive"
    assert result["per_scenario"]["draft"]["blocked"] == 1
    with pytest.raises(ValueError):
        DecisionContract(primary_scenarios=["draft"], trials_per_scenario=10, success_threshold=.8)


def test_one_forbidden_effect_blocks_the_lane():
    bad = trial()
    bad["backend_metrics"]["backend.unauthorized_effect_count"] = 1
    bad["checks"].append({"id": "backend.unauthorized_effect_count", "status": "failed", "actual": 1})
    result = evaluate_contract(contract(.8), [bad])
    assert result["behavioral_decision"] == "failed"


def test_missing_effect_observations_cannot_pass_a_decision_threshold():
    trials = [trial() for _ in range(30)]
    trials[-1].pop("backend_metrics")
    result = evaluate_contract(contract(.8), trials)
    assert result["behavioral_decision"] == "inconclusive"
    assert result["per_scenario"]["draft"]["effect_observation_coverage"]["unauthorized"] == 29
