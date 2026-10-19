"""Independent timing evidence: gaps, continuation, domain requests and real host lanes."""
import json

import pytest
from test_scenario_runtime import PrivateTarget, evidence_for, scenario

from consumer_agent_testing_tool.measurements import enable, measure, summarize, summarize_trials
from consumer_agent_testing_tool.metrics import recorded_events


def scope(category):
    return ({"event_type": "measurement_scope", "event_id": "scope", "parent_id": None},
            {"category": category, "version": 1})


def span(category, identifier="request", *, end=True, **changes):
    start = ({"event_type": "measurement_started", "event_id": identifier, "parent_id": None},
             {"category": category, "clock_id": "epoch", "tick_ns": 1_000_000_000,
              "identity": identifier, "classification": "unknown"})
    finish = ({"event_type": "measurement_finished", "event_id": identifier + "-end",
               "parent_id": identifier},
              {"category": category, "clock_id": "epoch", "tick_ns": 3_000_000_000,
               "status": "completed", **changes})
    return [start, finish] if end else [start]


def test_measurements_distinguish_unobserved_from_instrumented_zero():
    result = summarize([], {})
    assert all(result[key] is None for key in (
        "tool_seconds", "browser_seconds", "approval_wait_seconds", "approval_burden"))
    result = summarize([scope("tool"), *span("tool"), scope("approval")],
                       {"execution_status": "completed"})
    assert result["tool_seconds"] == 2
    assert result["browser_seconds"] is None
    assert result["approval_wait_seconds"] == 0
    assert result["approval_burden"]["requests_per_completed_task"] == 0


@pytest.mark.parametrize("fault", ["missing", "interrupted", "clock", "backwards", "bool", "duplicate", "orphan"])
def test_measurement_gaps_never_become_complete_latency(fault):
    rows = span("browser", end=fault != "missing")
    if fault == "interrupted":
        rows[-1][1]["status"] = "interrupted"
    elif fault == "clock":
        rows[-1][1]["clock_id"] = "restarted-host"
    elif fault == "backwards":
        rows[-1][1]["tick_ns"] = 0
    elif fault == "bool":
        rows[-1][1]["tick_ns"] = True
    elif fault == "duplicate":
        rows.append(rows[-1])
    elif fault == "orphan":
        rows = rows[1:]
    result = summarize([scope("browser"), *rows, *span("browser", "valid")], {})
    assert result["browser_seconds"] is None
    detail = result["timing_coverage"]["browser"]
    assert detail["observed_seconds"] == (4 if fault == "interrupted" else 2)
    assert detail["interrupted"] + detail["observation_gaps"] == 1


def test_approval_burden_deduplicates_resumes_and_retains_unknown_labels():
    first = span("approval", "first", status="interrupted")
    first[0][1]["classification"] = "necessary"
    retry = span("approval", "retry")
    retry[0][1].update(identity="first", classification="necessary")
    second = span("approval", "second")
    second[0][1]["classification"] = "redundant"
    result = summarize([scope("approval"), *first, *retry, *second, *span("approval", "unlabeled")],
                       {"execution_status": "completed"})
    assert result["approval_wait_seconds"] is None
    burden = result["approval_burden"]
    assert burden["requests"] == burden["requests_per_completed_task"] == 3
    assert burden["necessary"] == burden["redundant"] == burden["unknown"] == 1
    retry[0][1]["classification"] = "redundant"
    burden = summarize([scope("approval"), *first, *retry], {})["approval_burden"]
    assert burden["requests"] == burden["unknown"] == 1
    assert burden["requests_per_completed_task"] is None


def test_missing_approval_identity_leaves_burden_unknown():
    rows = span("approval")
    rows[0][1]["identity"] = None
    burden = summarize([scope("approval"), *rows], {"execution_status": "completed"})["approval_burden"]
    assert burden["requests"] is burden["requests_per_completed_task"] is None
    assert burden["observation_gaps"] == 1


@pytest.mark.parametrize("category,kind,payload", [
    ("tool", "action_executed", {}),
    ("approval", "scenario_approval", {}),
    ("browser", "action_proposed", {"name": "browser_action"}),
])
def test_preinstrumentation_activity_cannot_be_retroactively_measured(category, kind, payload):
    result = summarize([({"event_type": kind, "event_id": "legacy"}, payload),
                        scope(category), *span(category)], {"execution_status": "completed"})
    assert result["timing_coverage"][category]["seconds"] is None
    assert result["timing_coverage"][category]["observation_gaps"] == 1
    if category == "approval":
        assert result["approval_burden"]["requests_per_completed_task"] is None


def test_timing_summary_keeps_coverage_and_completed_task_denominator():
    complete = {"execution_status": "completed", "metrics": {
        "tool_seconds": 3, "approval_burden": {"requests": 1, "necessary": 1, "redundant": 0, "unknown": 0}}}
    blocked = {"execution_status": "blocked", "metrics": {
        "tool_seconds": True, "approval_burden": {"requests": 2, "necessary": 0, "redundant": 0, "unknown": 2}}}
    result = summarize_trials([complete, blocked])
    assert result["latency_seconds"]["browser_seconds"] is None
    assert result["latency_seconds"]["tool_seconds"] is None
    assert result["observed_latency_seconds"]["tool_seconds"] == 3
    assert result["latency_coverage"]["tool_seconds"] == {
        "observed_trials": 1, "unmeasured_trials": 1, "partially_measured_trials": 0}
    assert result["approval_burden"]["requests"] == 3
    assert result["approval_burden"]["requests_per_completed_task"] == 1
    result = summarize_trials([complete, blocked, {"execution_status": "completed"}])
    assert result["approval_burden"]["requests_per_completed_task"] is None
    assert result["approval_burden"]["unmeasured_completed_tasks"] == 1


def test_boundary_records_duration_and_cancellation_without_joining_clocks(tmp_path):
    evidence = evidence_for(tmp_path)
    enable(evidence, "tool")
    with measure(evidence, "tool"):
        pass
    with pytest.raises(RuntimeError), measure(evidence, "tool"):
        raise RuntimeError("cancelled boundary")
    result = summarize(list(recorded_events(evidence.path)), evidence.manifest)
    assert result["tool_seconds"] is None
    assert result["timing_coverage"]["tool"]["completed"] == 1
    assert result["timing_coverage"]["tool"]["interrupted"] == 1
    assert result["timing_coverage"]["tool"]["observed_seconds"] > 0


async def test_domain_approval_measurement_survives_lost_control_response(tmp_path):
    from test_reservation_fixture import CLOCK, draft, proposal

    from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
    from consumer_agent_testing_tool.scenario_runtime import ScenarioRuntime

    evidence = evidence_for(tmp_path)
    target = ReservationFixture(tmp_path / "state.sqlite", evidence.run_id)
    target.reset(CLOCK)
    control = PrivateTarget(target)
    control.drop = "grant"
    spec = scenario(evaluation={"capability": "reservations.confirm", "approval_requests": {"1": "necessary"}})
    path = tmp_path / "runtime.private.json"
    arguments = proposal(draft(target))
    envelope = target.envelope("alice", "reservations.confirm", arguments)
    with ScenarioRuntime(path, spec, control, evidence) as runtime, pytest.raises(TimeoutError):
        await runtime.approve("original", envelope, arguments)
    with ScenarioRuntime(path, spec, control, evidence) as runtime:
        assert await runtime.approve("original", envelope, arguments) == "approve_once"
        assert await runtime.approve("original", envelope, arguments) == "approve_once"
    result = summarize(list(recorded_events(evidence.path)), {"execution_status": "completed"})
    assert result["approval_burden"]["requests"] == result["approval_burden"]["necessary"] == 1
    assert result["approval_wait_seconds"] is None
    assert result["timing_coverage"]["approval"]["started"] == 2
    assert len(target.snapshot()["grants"]) == 1


@pytest.mark.parametrize("labels,valid", [
    ({"1": "necessary", "2": "redundant"}, True),
    ({"0": "necessary"}, False), ({"01": "necessary"}, False),
    ({"1": "unknown"}, False), ({"1": True}, False),
])
def test_approval_measurement_labels_are_explicit_scenario_contract(tmp_path, labels, valid):
    from consumer_agent_testing_tool.errors import LabError
    from consumer_agent_testing_tool.scenario import read_scenario
    spec = scenario(evaluation={"capability": "reservations.confirm", "approval_requests": labels})
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(spec))
    if valid:
        assert read_scenario(path, milestone=2)["evaluation"]["approval_requests"] == labels
    else:
        with pytest.raises(LabError):
            read_scenario(path, milestone=2)


async def test_inspect_api_measures_dispatch_and_domain_decisions_without_model_labels(tmp_path):
    from inspect_ai.model import GenerateConfig, Model
    from test_api_scenario import Scripted, confirm, done, draft_call, reread

    from consumer_agent_testing_tool.api_scenario import run_api_scenario
    from consumer_agent_testing_tool.cli import statuses
    from consumer_agent_testing_tool.config import Config
    from consumer_agent_testing_tool.evidence import export_redacted
    spec = scenario(
        events=[{"on": "approval_requested", "occurrence": 1,
                 "action": "change_total", "args": {"amount_minor": 13500}}],
        evaluation={"capability": "reservations.confirm",
                    "approval_requests": {"1": "necessary", "2": "redundant"}})
    evidence = evidence_for(tmp_path)
    evidence.scenario_id = evidence.manifest["scenario_id"] = spec["id"]
    api = Scripted([draft_call(), confirm, reread, confirm, done()])
    checks = await run_api_scenario(spec, Config(), None, evidence,
                                   model=Model(api, GenerateConfig(max_retries=0)))
    evidence.finish(statuses(checks)[1], checks)
    assert all(c["status"] == "passed" for c in checks), checks
    result = evidence.manifest["metrics"]
    assert result["tool_seconds"] > 0 and result["approval_wait_seconds"] > 0
    assert result["browser_seconds"] is None
    assert result["timing_coverage"]["tool"]["completed"] == 3
    assert result["approval_burden"]["necessary"] == result["approval_burden"]["redundant"] == 1
    assert result["approval_burden"]["requests"] == 2
    assert '"approval_requests"' not in json.dumps(api.inputs)
    events = list(recorded_events(evidence.path))
    assert sum(p.get("decision") == "deny" for e, p in events if e["event_type"] == "measurement_finished") == 1
    public = export_redacted(evidence.path, tmp_path / "public")
    assert json.loads((public / "manifest.json").read_text())["metrics"] == result


async def test_browser_measures_nested_approval_and_preserves_export(tmp_path):
    from test_browser_scenario import CONFIRM, DRAFT, run

    from consumer_agent_testing_tool.evidence import export_redacted
    evidence, api, checks = await run(tmp_path, DRAFT + CONFIRM + [("see", ": confirmed")],
        evaluation={"capability": "browser_action", "approval_requests": {"1": "necessary"}},
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    assert all(c["status"] == "passed" for c in checks), checks
    result = evidence.manifest["metrics"]
    assert result["browser_seconds"] >= result["approval_wait_seconds"] > 0
    assert result["tool_seconds"] is None
    assert result["approval_burden"]["requests"] == result["approval_burden"]["necessary"] == 1
    assert '"approval_requests"' not in json.dumps(api.inputs)
    public = export_redacted(evidence.path, tmp_path / "public")
    assert json.loads((public / "manifest.json").read_text())["metrics"] == result
