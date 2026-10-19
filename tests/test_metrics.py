from uuid import uuid4

import pytest

from consumer_agent_testing_tool.metrics import summarize_trials


def test_metrics_preserve_exclusions_and_missing_evidence():
    complete = {"execution_status": "completed",
                "checks": [{"id": "backend.draft_count", "status": "passed", "actual": 1}]}
    failed = {"execution_status": "failed",
              "checks": [{"id": "backend.draft_count", "status": "failed", "actual": 0}]}
    # Excludable only because the harness failed before the first model request.
    infrastructure = {"execution_status": "failed", "checks": [
        {"id": "model", "status": "failed", "class": "setup/infrastructure"}],
        "metrics": {"usage_observations": {"requests": 0}}}
    blocked = {"execution_status": "blocked",
               "checks": [{"id": "answer", "status": "observation-gap"}]}
    metrics = summarize_trials([complete, failed, infrastructure, blocked])
    assert metrics["task_success"]["numerator"] == 1
    assert metrics["task_success"]["denominator"] == 2
    assert metrics["blocked"] == metrics["infrastructure"] == 1
    assert metrics["exposure_rate"] is None
    assert metrics["cost"] is None


@pytest.mark.parametrize("role,error,content,expected", [
    ("tool", None, "booking reservations.create --args '{}'", True),
    ("tool", None, "booking reservations.create_extra", False),
    ("tool", {"type": "unknown"}, "reservations.create", False),
    ("user", None, "Please call reservations.create", False),
    ("assistant", None, "I know reservations.create", False),
])
def test_exposure_requires_exact_operation_in_successful_tool_context(tmp_path, role, error, content, expected):
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.metrics import capability_exposure

    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested",
        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    evidence.event("surface_exposed", {"content": "reservations.create", "model_received": False})
    evidence.event("observation_emitted", {"messages": [{"role": role, "error": error, "content": content}]})
    result = capability_exposure(evidence.path, "reservations.create")
    assert result["exposure"] is expected


def test_documentation_seen_after_selection_does_not_count_as_prior_exposure(tmp_path):
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.metrics import capability_exposure

    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested",
        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    evidence.event("observation_emitted", {"messages": [{"role": "user", "content": "Create a draft"}]})
    proposal = evidence.event("action_proposed", {"command": "booking reservations.create"})
    observation = evidence.event("observation_emitted", {"messages": [
        {"role": "tool", "content": "reservations.create returned a draft"}]})
    result = capability_exposure(evidence.path, "reservations.create")
    assert result["ever_observed"] is True
    assert result["exposure"] is False
    assert result["first_exposure_event"] == observation
    assert result["first_matching_proposal_event"] == proposal


@pytest.mark.parametrize("tools,expected", [
    (None, None), ([], False), ([{"name": "lookup_item_extra"}], False),
    ([{"name": "lookup_item"}], True),
])
def test_native_exposure_uses_observed_definitions_not_metadata_or_messages(tmp_path, tools, expected):
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.metrics import capability_exposure
    evidence = Evidence(tmp_path, lane="behavioral", label="synthetic",
        profile=interface_profile("api-tools-surrogate-v1"), lock={})
    catalog = {"lookup_item": {"operation": "inventory.lookup"}}
    evidence.event("surface_exposed", catalog)
    evidence.event("observation_emitted", {"messages": [{"role": "tool",
        "content": 'inventory.lookup {"name": "lookup_item"}'}],
        **({"tools": tools} if tools is not None else {})})
    evidence.event("action_proposed", {"name": "lookup_item", "arguments": {}})
    assert capability_exposure(evidence.path, "inventory.lookup", catalog=catalog)["exposure"] is expected


def test_native_definition_observed_after_selection_cannot_retroactively_pass(tmp_path):
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.metrics import capability_exposure
    evidence = Evidence(tmp_path, lane="behavioral", label="synthetic",
        profile=interface_profile("api-tools-surrogate-v1"), lock={})
    catalog = {"lookup_item": {"operation": "inventory.lookup"}}
    evidence.event("observation_emitted", {"tools": []})
    proposal = evidence.event("action_proposed", {"name": "lookup_item"})
    observation = evidence.event("observation_emitted", {"tools": [{"name": "lookup_item"}]})
    result = capability_exposure(evidence.path, "inventory.lookup", catalog=catalog)
    assert result["exposure"] is False and result["ever_observed"] is True
    assert result["first_matching_proposal_event"] == proposal
    assert result["first_exposure_event"] == observation


def suite_trial(scenario_id, status, **extra):
    checks = {"passed": [{"id": "backend.draft_count", "status": "passed"}],
              "failed": [{"id": "backend.draft_count", "status": "failed"}],
              "infrastructure": [{"id": "model", "status": "failed", "class": "setup/infrastructure"}]}[status]
    return {"run_id": f"r-{uuid4().hex}", "scenario_id": scenario_id, "execution_status": "completed",
            "checks": checks, **({"metrics": {"usage_observations": {"requests": 0}}}
                                 if status == "infrastructure" else {}), **extra}


def test_suite_summary_breaks_down_scenarios_with_pass_hat_k():
    from consumer_agent_testing_tool.metrics import pass_hat_k
    trials = [suite_trial("draft", "passed"), suite_trial("draft", "failed"), suite_trial("draft", "passed"),
              suite_trial("compare", "passed"), suite_trial("compare", "infrastructure"),
              suite_trial("compare", "passed"), suite_trial("compare", "passed")]
    metrics = summarize_trials(trials)
    draft, compare = metrics["per_scenario"]["draft"], metrics["per_scenario"]["compare"]
    assert list(metrics["per_scenario"]) == ["draft", "compare"]
    assert (draft["attempts"], draft["valid_completed"], draft["failed"]) == (3, 3, 1)
    assert (compare["attempts"], compare["valid_completed"], compare["excluded"], compare["infrastructure"]) == (
        4, 3, 1, 1)
    assert draft["task_success"]["numerator"] == 2 and compare["success_per_attempt"]["denominator"] == 4
    # tau-bench C(c,k)/C(n,k): 2 of 3 is 2/3, 1/3, 0; 3 of 3 always passes; 3 of 4 attempts is 3/4, 1/2, 1/4, 0.
    assert draft["pass_hat_k"] == pytest.approx({"1": 2 / 3, "2": 1 / 3, "3": 0.0})
    assert compare["pass_hat_k"] == {"1": 1.0, "2": 1.0, "3": 1.0}
    assert compare["pass_hat_k_all_attempts"] == pytest.approx({"1": .75, "2": .5, "3": .25, "4": 0.0})
    # The suite value is the mean over scenarios, for k every scenario reaches.
    assert metrics["pass_hat_k"] == pytest.approx({"1": 5 / 6, "2": 2 / 3, "3": .5})
    assert metrics["pass_hat_k_all_attempts"] == pytest.approx({"1": 17 / 24, "2": 5 / 12, "3": 1 / 8})
    assert "mean over scenarios" in metrics["pass_hat_k_basis"]
    assert pass_hat_k(0, 0) == {} and pass_hat_k(1, 1) == {"1": 1.0}


def test_suite_pass_hat_k_needs_kept_trials_in_every_scenario():
    metrics = summarize_trials([suite_trial("draft", "passed"), suite_trial("compare", "infrastructure"),
                                {"execution_status": "completed", "checks": [
                                    {"id": "backend.draft_count", "status": "passed"}]}])
    assert metrics["per_scenario"]["compare"]["pass_hat_k"] == {}
    assert metrics["per_scenario"]["(no scenario id)"]["pass_hat_k"] == {"1": 1.0}
    assert metrics["pass_hat_k"] == {}  # A scenario with no kept trial leaves the suite value undefined.
    assert metrics["pass_hat_k_all_attempts"] == {"1": 2 / 3}
    assert summarize_trials([])["per_scenario"] == {} and summarize_trials([])["pass_hat_k"] == {}
