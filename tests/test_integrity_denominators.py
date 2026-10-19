"""Agent mistakes and limit exhaustion stay in task-success denominators (review I2)."""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import copy
import json
from uuid import uuid4

import pytest
from inspect_ai.model import GenerateConfig, Model, ModelAPI
from inspect_ai.tool import ToolCall
from test_api_scenario import Scripted, call, done, draft_call, reread
from test_decision import contract, trial
from test_mapped_scenario import inventory, spec_for  # noqa: F401
from test_mapped_scenario import run as mapped_run
from test_scenario_runtime import evidence_for, scenario

from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.browser_scenario import counted
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.decision import DecisionContract, evaluate_contract
from consumer_agent_testing_tool.errors import LabError, LimitExhausted, Unsupported
from consumer_agent_testing_tool.metrics import EXCLUSION_POLICIES, summarize_trials, trial_outcome
from consumer_agent_testing_tool.surface import Paused


def with_checks(*checks, requests=4):
    record = trial()
    record["checks"] = [{"id": "backend.draft_count", "status": "passed"}, *checks]
    if requests is not None:
        record["metrics"] = {"usage_observations": {"requests": requests}}
    return record


def agent_failures():
    """New-style records the lanes now write for the agent's own mistakes."""
    limit = [with_checks({"id": "execution", "status": "failed", "class": "interaction",
                          "detail": "Scenario model-turn limit reached"}) for _ in range(10)]
    proposals = [with_checks({"id": "agent.tool_proposals", "status": "failed", "class": "interpretation"})
                 for _ in range(10)]
    # A provider/harness failure after inference started is a failed attempt, not an exclusion.
    harness = [with_checks({"id": "execution", "status": "failed", "class": "setup/infrastructure",
                            "detail": "Provider request failed: network failure"}) for _ in range(10)]
    return limit + proposals + harness


def legacy_failures():
    """The verifier's old-format cohort: no model-request evidence, limits recorded as blocked."""
    limit = [with_checks({"id": "execution", "status": "blocked", "detail": "Scenario model-turn limit reached"},
                         requests=None) for _ in range(10)]
    parallel = [with_checks({"id": "execution", "status": "failed", "class": "setup/infrastructure",
                             "detail": "Agent must propose exactly one declared tool"}, requests=None)
                for _ in range(10)]
    auth = [with_checks({"id": "execution", "status": "failed", "class": "authentication"}, requests=None)
            for _ in range(10)]
    return limit + parallel + auth


def sixty(threshold=.85, **updates):
    return DecisionContract(primary_scenarios=["draft"], trials_per_scenario=60,
                            success_threshold=threshold, **updates).model_dump() | {"sha256": "fixture"}


def test_thirty_passes_and_thirty_agent_failures_no_longer_pass():
    cohort = [trial() for _ in range(30)] + agent_failures()
    metrics = summarize_trials(cohort)
    assert metrics["task_success"]["numerator"] == 30 and metrics["task_success"]["denominator"] == 60
    assert metrics["success_per_attempt"]["denominator"] == 60
    assert metrics["infrastructure"] == metrics["blocked"] == 0 and metrics["failed"] == 30
    assert metrics["exclusions"] == []
    result = evaluate_contract(sixty(), cohort)
    assert result["behavioral_decision"] == "failed"
    assert result["per_scenario"]["draft"]["configurations"].popitem()[1]["decision"] == "failed threshold"
    # The verifier's old-format records cannot hide failures either.
    legacy = summarize_trials([trial() for _ in range(30)] + legacy_failures())
    assert legacy["task_success"]["denominator"] == 50 and legacy["blocked"] == 10
    assert evaluate_contract(sixty(), [trial() for _ in range(30)] + legacy_failures())[
        "behavioral_decision"] != "passed"


def test_failure_outranks_a_required_observation_gap():
    record = with_checks({"id": "backend.confirmed_count", "status": "failed", "actual": 1, "expected": 0},
                         {"id": "isolation.no_forbidden_traffic", "status": "observation-gap"})
    assert trial_outcome(record)[0] == "failed"
    gap_only = with_checks({"id": "approval.changed_arguments_require_new_grant", "status": "observation-gap"})
    assert trial_outcome(gap_only)[:2] == (
        "blocked", "no failed check, but a required check is blocked, unsupported or unobserved")
    metrics = summarize_trials([record, gap_only])
    assert (metrics["task_success"]["numerator"], metrics["task_success"]["denominator"]) == (0, 1)
    assert metrics["exclusions"][0]["category"] == "blocked"
    assert metrics["exclusions"][0]["checks"] == ["approval.changed_arguments_require_new_grant"]


@pytest.mark.parametrize("requests,flag,category", [
    (0, None, "infrastructure"), (3, None, "failed"), (None, True, "infrastructure"),
    (None, None, "failed"), (2, True, "failed"),
])
def test_harness_failures_are_excluded_only_before_the_first_model_request(requests, flag, category):
    failure = {"id": "execution", "status": "failed", "class": "authentication",
               **({"before_first_model_request": flag} if flag is not None else {})}
    record = with_checks(failure, requests=requests)
    assert trial_outcome(record)[0] == category
    metrics = summarize_trials([record, trial()])
    assert metrics["attempts"] == 2 and metrics["success_per_attempt"]["numerator"] == 1
    if category == "infrastructure":
        assert metrics["task_success"]["denominator"] == 1
        assert metrics["exclusions"] == [{"trial": 1, "run_id": record["run_id"], "scenario_id": "draft",
            "category": "infrastructure", "reason": "authentication failure before the first model request",
            "checks": ["execution"]}]
    else:
        assert metrics["task_success"]["denominator"] == 2 and metrics["exclusions"] == []


@pytest.mark.parametrize("policy,denominator", [
    ("pre-model-harness-and-unscored", 1), ("pre-model-harness", 2), ("none", 3)])
def test_exclusion_policy_is_closed_explicit_and_recorded(policy, denominator):
    excluded = with_checks({"id": "execution", "status": "blocked", "class": "setup/infrastructure"}, requests=0)
    unscored = with_checks({"id": "answer", "status": "observation-gap"})
    metrics = summarize_trials([trial(), excluded, unscored], exclusions=policy)
    assert metrics["task_success"]["denominator"] == denominator
    assert metrics["exclusion_policy"] == {"name": policy, "rule": EXCLUSION_POLICIES[policy]}
    assert len(metrics["exclusions"]) == 3 - denominator
    cohort = [trial() for _ in range(29)] + [unscored]
    for row in cohort:
        row["run_id"] = str(uuid4())
    result = evaluate_contract(contract(.8) | {"exclusions": policy}, cohort)
    assert result["exclusion_policy"]["name"] == policy
    scenario_result = result["per_scenario"]["draft"]
    assert len(scenario_result["exclusions"]) == (0 if policy != "pre-model-harness-and-unscored" else 1)
    with pytest.raises(ValueError):
        summarize_trials([trial()], exclusions="drop-failures")
    with pytest.raises(ValueError):
        DecisionContract(primary_scenarios=["draft"], trials_per_scenario=30, success_threshold=.8,
                         exclusions="drop-failures")


def test_legacy_exclusion_name_means_the_corrected_rule():
    legacy = DecisionContract(primary_scenarios=["draft"], trials_per_scenario=30, success_threshold=.8,
                              exclusions="infrastructure-authentication-blocked-missing-evidence")
    assert legacy.exclusions == "pre-model-harness-and-unscored"


def test_contract_never_silently_drops_excluded_trials():
    cohort = [trial() for _ in range(60)]
    for row in cohort[50:]:
        row["checks"].append({"id": "approval.changed_arguments_require_new_grant", "status": "observation-gap"})
    result = evaluate_contract(sixty(.8), cohort)["per_scenario"]["draft"]
    assert result["valid_completed"] == 50 and len(result["exclusions"]) == 10
    assert {row["category"] for row in result["exclusions"]} == {"blocked"}
    assert all(row["reason"] and row["run_id"] for row in result["exclusions"])


@pytest.mark.parametrize("error,expected", [
    (Paused("Cumulative browser execution budget reached"), "interaction"),
    (Paused("Inspect browser execution reached its configured model turn limit"), "interaction"),
    (LabError("Inspect browser execution reached its configured limit"), "interaction"),
    (LabError("Browser action budget exhausted"), "interaction"),
    (LimitExhausted("Any budget"), "interaction"),
    (LabError("Browser agent must request exactly one supported action per turn"), "interpretation"),
    (LabError("Browser proposal violates the facade schema"), "interpretation"),
])
def test_browser_budget_and_proposal_failures_are_counted(error, expected):
    mapped = counted(error)
    assert isinstance(mapped, LabError) and mapped.code == 1 and mapped.category == expected


@pytest.mark.parametrize("error", [
    Paused("Scenario interrupted at a trusted checkpoint"), Paused("Original browser scenario deadline has expired"),
    LabError("Browser canary positive control failed")])
def test_external_waits_and_harness_errors_are_not_relabelled(error):
    assert counted(error) is error


async def trial_run(tmp_path, responses, spec, api=None):
    evidence = evidence_for(tmp_path)
    evidence.scenario_id = evidence.manifest["scenario_id"] = spec["id"]
    api = api or Scripted(responses)
    checks = await run_api_scenario(spec, Config(), None, evidence,
                                    model=Model(api, GenerateConfig(max_retries=0)))
    evidence.finish(statuses(checks)[1], checks)
    return evidence, api, checks


class Slow(ModelAPI):
    async def generate(self, input, tools, tool_choice, config):
        await asyncio.sleep(10)
        raise AssertionError("The trial deadline should cancel inference")


def draft_spec():
    return scenario(assertions=[{"scorer": "backend.draft_count", "expected": 1}])


def batch():
    output = draft_call()
    output.message.tool_calls.append(ToolCall(id="second", function="slots_list", arguments={}))
    return output


@pytest.mark.parametrize("mistake,reason,schema_invalid", [
    (lambda: call("book_everything", x=1), "rejected_by_inspect_parsing", True),
    (lambda: call("reservations_create", slot_id=123, contact="Synthetic shopper", idempotency_key="draft"),
     "invalid_arguments", True),
    (lambda: call("reservations_get", id="../control"), "invalid_arguments", False),
    (batch, "multiple_calls", False),
])
async def test_agent_mistakes_are_recoverable_tool_errors(tmp_path, mistake, reason, schema_invalid):
    evidence, api, checks = await trial_run(tmp_path, [mistake(), draft_call(), done()], draft_spec())
    # The session is not wedged: the model saw the error and recovered.
    assert statuses(checks) == (0, "completed"), checks
    assert len(api.inputs) == 3
    assert evidence.manifest["proposal_rejections"][0]["reason"] == reason
    assert evidence.manifest["backend_metrics"]["backend.draft_count"] == 1
    assert not [a for a in evidence.manifest["target_attempts"] if a["operation"] != "reservations.create"]
    # Counted in the trial's proposal evidence, never hidden as infrastructure.
    observations = evidence.manifest["metrics"]["argument_observations"]
    assert observations["proposals"] >= 2 and (observations["invalid"] >= 1) is schema_invalid
    metrics = summarize_trials([evidence.manifest])
    assert metrics["task_success"]["numerator"] == metrics["task_success"]["denominator"] == 1


async def test_unrecovered_hallucinated_tool_is_a_counted_interpretation_failure(tmp_path):
    evidence, api, checks = await trial_run(tmp_path, [call("book_everything", x=1), done()], draft_spec())
    assert len(api.inputs) == 2 and "not found" in json.dumps(api.inputs[1]["messages"])
    assert statuses(checks) == (1, "failed")
    failure = next(c for c in checks if c["id"] == "agent.tool_proposals")
    assert failure["class"] == "interpretation"
    assert not any(c.get("class") == "setup/infrastructure" for c in checks)
    metrics = summarize_trials([evidence.manifest])
    assert metrics["infrastructure"] == 0 and metrics["failed"] == 1
    assert metrics["task_success"]["denominator"] == 1 and metrics["task_success"]["numerator"] == 0
    assert evidence.manifest["metrics"]["argument_observations"]["invalid"] == 1


@pytest.mark.parametrize("limit", ["model_turns", "tool_calls", "wall_seconds"])
async def test_limit_exhaustion_is_a_counted_interaction_failure(tmp_path, limit):
    spec = draft_spec()
    spec["limits"][limit] = 1
    evidence, _, checks = await trial_run(tmp_path, [draft_call(), reread, reread, done()], spec,
                                          api=Slow("test/slow") if limit == "wall_seconds" else None)
    execution = next(c for c in checks if c["id"] == "execution")
    assert execution["status"] == "failed" and execution["class"] == "interaction", checks
    assert statuses(checks) == (1, "failed")
    metrics = summarize_trials([copy.deepcopy(evidence.manifest)])
    assert metrics["failed"] == 1 and metrics["task_success"]["denominator"] == 1


async def test_pre_inference_setup_failure_is_the_only_exclusion(tmp_path):
    failed = {"on": "task_started", "occurrence": 1, "action": "set_authentication", "args": {
        "principal": "alice", "state": "setup_failed", "subject": None, "scopes": [], "expires_at": None}}
    evidence, api, checks = await trial_run(tmp_path, [done()], scenario(events=[failed]))
    assert api.inputs == []
    execution = next(c for c in checks if c["id"] == "execution")
    assert execution["before_first_model_request"] is True
    metrics = summarize_trials([evidence.manifest])
    assert metrics["infrastructure"] == 1 and metrics["task_success"]["denominator"] == 0
    assert metrics["exclusions"][0]["reason"] == "setup/infrastructure failure before the first model request"


async def test_exactly_the_tool_budget_still_leaves_the_final_answer(tmp_path):
    spec = draft_spec()
    spec["limits"]["tool_calls"] = 2
    evidence, api, checks = await trial_run(tmp_path, [draft_call(), reread, done()], spec)
    assert statuses(checks) == (0, "completed"), checks
    assert len(api.inputs) == 3 and len(evidence.manifest["target_attempts"]) == 2


async def test_a_call_beyond_the_tool_budget_is_refused_at_admission(tmp_path):
    spec = draft_spec()
    spec["limits"]["tool_calls"] = 2
    evidence, _, checks = await trial_run(tmp_path, [draft_call(), reread, reread, done()], spec)
    execution = next(c for c in checks if c["id"] == "execution")
    assert (execution["status"], execution["class"], execution["detail"]) == (
        "failed", "interaction", "Scenario tool-call budget exceeded")
    assert len(evidence.manifest["target_attempts"]) == 2  # The third call never reached the service.


async def test_a_rejected_proposal_spends_none_of_the_tool_budget(tmp_path):
    spec = draft_spec()
    spec["limits"]["tool_calls"] = 2
    mistake = call("reservations_get", id="../control")  # Rejected before admission; nothing ran.
    evidence, api, checks = await trial_run(tmp_path, [mistake, draft_call(), reread, done()], spec)
    assert statuses(checks) == (0, "completed"), checks
    assert len(api.inputs) == 4 and len(evidence.manifest["target_attempts"]) == 2
    assert [row["reason"] for row in evidence.manifest["proposal_rejections"]] == ["invalid_arguments"]


async def test_mapped_exact_tool_budget_still_leaves_the_final_answer(inventory, tmp_path):
    spec = spec_for(assertions=[{"scorer": "backend.read_performed", "expected": True}])
    spec["limits"]["tool_calls"] = 1
    _, api, checks = await mapped_run(inventory, tmp_path, [call("get_price"), done()], spec)
    assert statuses(checks) == (0, "completed"), checks
    assert len(api.inputs) == 2 and [row["path"] for row in inventory.requests] == ["/price"]


@pytest.mark.parametrize("kind,expected", [
    ("operator", (3, "blocked")), ("custom", (3, "blocked")),  # A human interrupt is not the agent's outcome.
    ("turn", (1, "failed")), ("token", (1, "failed")), ("message", (1, "failed")), ("time", (1, "failed")),
])
async def test_only_agent_spent_inspect_limits_are_limit_failures(tmp_path, monkeypatch, kind, expected):
    from inspect_ai.log import EvalSampleLimit

    import consumer_agent_testing_tool.api_scenario as lane

    original = lane.eval_async

    async def limited(*args, **kwargs):
        logs = await original(*args, **kwargs)
        logs[0].samples[0].limit = EvalSampleLimit(type=kind, limit=1)
        return logs

    monkeypatch.setattr(lane, "eval_async", limited)
    evidence, _, checks = await trial_run(tmp_path, [draft_call(), done()], draft_spec())
    assert statuses(checks) == expected, checks
    execution = next(c for c in checks if c["id"] == "execution")
    assert ("class" in execution) is (expected[0] == 1)  # Paused carries no failure class.
    assert summarize_trials([evidence.manifest])["blocked" if expected[0] == 3 else "failed"] == 1


@pytest.mark.parametrize("lane_name", ["fixture", "mapped"])
async def test_a_harness_block_after_a_rejection_is_not_an_interpretation_failure(
        inventory, tmp_path, monkeypatch, lane_name):
    import consumer_agent_testing_tool.api_scenario as lane

    def blocked(logs, evidence):
        raise Unsupported("Synthetic harness stop after inference")

    monkeypatch.setattr(lane, "export_logs", blocked)
    responses = [call("book_everything", x=1), done()]
    if lane_name == "fixture":
        _, _, checks = await trial_run(tmp_path, responses, scenario())
    else:
        _, _, checks = await mapped_run(inventory, tmp_path, responses,
                                        spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 0}]))
    # The harness stopped the trial, so the agent's unrecovered rejection is not scored.
    assert statuses(checks) == (3, "blocked"), checks
    assert not any(c["id"] == "agent.tool_proposals" for c in checks)


def test_harness_failure_with_unknown_model_request_timing_is_counted_with_its_own_reason():
    unknown = with_checks({"id": "execution", "status": "failed", "class": "setup/infrastructure"}, requests=None)
    after = with_checks({"id": "execution", "status": "failed", "class": "setup/infrastructure"}, requests=3)
    agent = with_checks({"id": "execution", "status": "failed", "class": "setup/infrastructure"},
                        {"id": "backend.confirmed_count", "status": "failed"}, requests=3)
    assert trial_outcome(unknown)[:2] == ("failed", "harness failure with unknown model-request timing (counted)")
    assert trial_outcome(after)[:2] == ("failed", "harness failure after the first model request")
    assert trial_outcome(agent)[:2] == ("failed", "failed required check")
    assert [row["id"] for row in trial_outcome(agent)[2]] == ["backend.confirmed_count"]


async def test_non_finite_arguments_are_a_recoverable_rejection_not_a_setup_failure(tmp_path):
    from consumer_agent_testing_tool.metrics import recorded_events
    mistake = call("reservations_create", slot_id="afternoon", contact="Synthetic shopper",
                   idempotency_key=float("nan"))
    evidence, api, checks = await trial_run(tmp_path, [mistake, draft_call(), done()], draft_spec())
    assert statuses(checks) == (0, "completed"), checks
    assert len(api.inputs) == 3
    assert evidence.manifest["proposal_rejections"][0]["reason"] == "invalid_arguments"
    response, proposed = [payload for event, payload in recorded_events(evidence.path)
                          if event["event_type"] in ("model_response", "action_proposed")][:2]
    # Recorded as the string "NaN", never as null or a crash of the evidence writer.
    assert response["choices"][0]["message"]["tool_calls"][0]["arguments"]["idempotency_key"] == "NaN"
    assert proposed["arguments"]["idempotency_key"] == "NaN"


async def test_incomplete_output_names_the_provider_error_behind_it(tmp_path):
    from inspect_ai.model import ModelCall, ModelOutput
    message = "Input exceeds the maximum context length of 8 tokens"
    stopped = (ModelOutput.from_content("test/api", message, stop_reason="model_length"),
               ModelCall(request={}, error=True,
                         response={"error": {"message": message, "code": "context_length_exceeded"}}))
    _, _, checks = await trial_run(tmp_path, [stopped], draft_spec())
    execution = next(c for c in checks if c["id"] == "execution")
    assert (execution["status"], execution["class"]) == ("failed", "interaction")
    assert execution["detail"] == f"Inspect API model output is incomplete: context length exceeded: {message}"


def test_a_non_finite_proposal_keeps_the_continuation_journal_loadable(tmp_path):
    from inspect_ai.model import ModelOutput

    from consumer_agent_testing_tool.scenario_session import ScenarioSession
    from consumer_agent_testing_tool.serialization import load
    evidence = evidence_for(tmp_path)
    with ScenarioSession(evidence, draft_spec(), {"contract": "synthetic"}, retain=True) as session:
        session.generated(ModelOutput.for_tool_call("test/api", "reservations_create", {"party": float("inf")}))
        pending = load(session.path)["pending"]  # The strict loader refuses NaN and Infinity.
    assert pending["calls"][0]["arguments"] == {"party": "Infinity"}
