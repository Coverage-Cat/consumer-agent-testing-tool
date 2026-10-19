"""Developer-API trials that differ only in their trial identity share one locked configuration (I6)."""
# ruff: noqa: F811 — imported pytest fixtures
import copy
from uuid import uuid4

from inspect_ai.model import GenerateConfig, Model
from test_api_scenario import Scripted, call, done
from test_mapped_scenario import buy, inventory, spec_for  # noqa: F401

from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, interface_profile
from consumer_agent_testing_tool.decision import (
    DecisionContract,
    configuration_cohorts,
    evaluate_contract,
)
from consumer_agent_testing_tool.evidence import Evidence, run_lock
from consumer_agent_testing_tool.replay import differences


async def mapped_trial(store, root, spec, lock):
    profile = interface_profile("api-tools-surrogate-v1")
    evidence = Evidence(root, lane="local behavioral", label="surrogate behavior", profile=profile,
                        lock=copy.deepcopy(lock), scenario_id=spec["id"])
    api = Scripted([call("get_price"), buy, done()])
    checks = await run_api_scenario(spec, Config(targets={"inventory": store.target}), None, evidence,
                                    model=Model(api, GenerateConfig(max_retries=0)))
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks) == (0, "completed"), checks
    return evidence


async def test_identical_mapped_trials_have_compatible_locks_and_a_contract_can_pass(inventory, tmp_path):
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    # One CLI invocation's lock; built once so concurrent source edits cannot differ between trials.
    lock = run_lock(profile=interface_profile("api-tools-surrogate-v1"), model={"id": "scripted"},
                    target=inventory.target.model_dump(mode="json"), scenario=spec)
    lock["execution"] = {"backend": "local"}
    first = await mapped_trial(inventory, tmp_path / "one", spec, lock)
    second = await mapped_trial(inventory, tmp_path / "two", spec, lock)
    assert first.run_id != second.run_id
    # The adapter protocol still carries the trial identity; the configuration lock does not.
    for evidence in (first, second):
        identity = evidence.manifest["run_lock"]["api_control"]["identity"]
        assert "run_id" not in identity and identity["task_id"] == spec["id"]
        assert evidence.manifest["api_control_trial_identity"] == {"run_id": evidence.run_id}
    assert differences(first.manifest["run_lock"], second.manifest["run_lock"]) == []
    cohorts, compatibility = configuration_cohorts([first.manifest, second.manifest], [])
    assert compatibility["status"] == "matched" and len(cohorts) == 1, compatibility
    # Thirty independent trial identities over these two real mapped locks.
    trials = []
    for index in range(30):
        record = copy.deepcopy((first, second)[index % 2].manifest)
        record["run_id"] = f"r-{uuid4().hex}"
        trials.append(record)
    decision = DecisionContract(primary_scenarios=[spec["id"]], trials_per_scenario=30,
                                success_threshold=.8).model_dump() | {"sha256": "fixture"}
    result = evaluate_contract(decision, trials)
    assert result["per_scenario"][spec["id"]]["lock_compatibility"]["status"] == "matched"
    assert result["behavioral_decision"] == "passed", result["per_scenario"]
