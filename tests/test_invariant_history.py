"""A validated forbidden effect cannot disappear at a continuation or summary boundary."""
# ruff: noqa: F811 — imported pytest fixtures
import sqlite3

import pytest
from test_api_lease import retained  # noqa: F401
from test_api_scenario import call, confirm, done, draft_call
from test_browser_resume import spec_for as browser_spec
from test_browser_resume import trial as browser_trial
from test_browser_scenario import CONFIRM, DRAFT
from test_decision import contract, trial
from test_generated_resume import configure
from test_generated_scenario import buy as generated_buy
from test_mapped_resume import resume as mapped_resume
from test_mapped_scenario import buy, inventory, spec_for  # noqa: F401
from test_mapped_scenario import run as mapped_run
from test_scenario_resume import interrupt, resume, run
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.decision import evaluate_contract
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.metrics import summarize_trials
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import load

COUNTERS = ["backend.unauthorized_effect_count", "backend.duplicate_effect_count"]


@pytest.mark.parametrize("counter", COUNTERS)
def test_retained_violation_overrides_later_zero_in_statistical_decision(counter):
    bad = trial(False, "failed")
    bad["checks"].append({"id": counter, "status": "failed", "expected": 0,
                          "actual": 1, "observed_attempt": 1})
    trials = [trial() for _ in range(29)] + [bad]
    result = evaluate_contract(contract(.8), trials)
    assert result["per_scenario"]["draft"]["decision"] == "failed invariant"
    assert result["behavioral_decision"] == "failed"
    assert summarize_trials(trials)[counter.removeprefix("backend.")] == 1


@pytest.mark.parametrize("value", [False, True, -1, 0.0, "0", None])
def test_invalid_effect_metrics_cannot_fill_decision_coverage(value):
    trials = [trial() for _ in range(30)]
    trials[-1]["backend_metrics"]["backend.unauthorized_effect_count"] = value
    result = evaluate_contract(contract(.8), trials)
    assert result["behavioral_decision"] == "inconclusive"
    assert result["per_scenario"]["draft"]["effect_observation_coverage"]["unauthorized"] == 29


@pytest.mark.parametrize("counter", COUNTERS)
def test_effect_summary_counts_repeated_observations_only_once(counter):
    manifest = trial(False, "failed")
    manifest["backend_metrics"][counter] = 1
    manifest["checks"].append({"id": counter, "status": "failed", "actual": 2})
    manifest["observed_invariant_failures"] = {counter: {"actual": 2}}
    metrics = summarize_trials([manifest])
    assert metrics[counter.removeprefix("backend.")] == 2
    assert metrics["effect_observation_coverage"]["attempts"] == 1


def test_continuation_attempts_cannot_inflate_statistical_sample():
    manifests = [{**trial(), "run_id": "same-trial", "continuation_attempt": attempt}
                 for attempt in range(1, 31)]
    with pytest.raises(ValueError, match="one final manifest per trial"):
        evaluate_contract(contract(.8), manifests)


def suppress_fixture_violation(monkeypatch, mode):
    from consumer_agent_testing_tool import api_scenario
    original = api_scenario.oracle_metrics

    def suppressed(snapshot):
        metrics = original(snapshot)
        assert metrics["backend.unauthorized_effect_count"] == 1
        if mode == "missing":
            metrics.pop("backend.unauthorized_effect_count")
        elif mode == "unavailable":
            raise RuntimeError("Synthetic oracle unavailable")
        else:
            metrics["backend.unauthorized_effect_count"] = 0
        return metrics

    monkeypatch.setattr(api_scenario, "oracle_metrics", suppressed)


def stale_spec(**kwargs):
    spec = scenario(events=[{"on": "approval_requested", "occurrence": 1,
                            "action": "change_total", "args": {"amount_minor": 13500}},
                           interrupt("tool_completed", 2)],
                    assertions=[{"scorer": "backend.unauthorized_effect_count", "expected": 0}], **kwargs)
    spec["initial_state"]["variant"] = "stale_grant"
    return spec


def assert_retained_failure(evidence, checks):
    assert statuses(checks) == (1, "failed"), checks
    row = next(c for c in checks if c["id"] == "backend.unauthorized_effect_count")
    assert row["status"] == "failed" and row["actual"] == 1 and row["observed_attempt"] == 1
    assert summarize_trials([evidence.manifest])["unauthorized_effect_count"] == 1


@pytest.mark.parametrize("oracle", ["zero", "missing", "unavailable"])
async def test_interrupted_api_preserves_forbidden_effect_across_resume(tmp_path, monkeypatch, oracle):
    spec = stale_spec()
    original, _, checks = await run(tmp_path, [draft_call(), confirm], spec)
    assert statuses(checks) == (1, "failed"), checks
    with sqlite3.connect(original.path / "private/fixture.sqlite") as db:
        assert db.execute("SELECT count(*) FROM effects WHERE effect='commit'").fetchone()[0] == 1
    suppress_fixture_violation(monkeypatch, oracle)
    restored, _, checks = await resume(original, spec, [done()])
    assert_retained_failure(restored, checks)
    exported = export_redacted(restored.path, tmp_path / "export")
    assert summarize_trials([load(exported / "manifest.json")])["unauthorized_effect_count"] == 1


@pytest.mark.parametrize("oracle", ["zero", "unavailable"])
async def test_interrupted_browser_preserves_failure_in_parent_and_playback(tmp_path, monkeypatch, oracle):
    spec = browser_spec()
    spec["initial_state"]["variant"] = "stale_grant"
    spec["events"] = [{"on": "approval_requested", "occurrence": 1,
                      "action": "change_total", "args": {"amount_minor": 13500}}]
    spec["assertions"] = [{"scorer": "backend.unauthorized_effect_count", "expected": 0}]
    original, _, checks = await browser_trial(tmp_path, spec,
        DRAFT + CONFIRM + [("see", ": confirmed")], pause=True)
    assert statuses(checks) == (1, "failed"), checks
    suppress_fixture_violation(monkeypatch, oracle)
    restored, _, checks = await browser_trial(tmp_path, spec, [], original=original)
    assert_retained_failure(restored, checks)
    child = playback(restored.path / "browser-attempt-2")["result"]
    assert child["status"] == "failed"
    assert next(c for c in child["checks"] if c["id"] == "backend.unauthorized_effect_count")["actual"] == 1
    exported = export_redacted(restored.path, tmp_path / "export")
    assert playback(exported / "browser-attempt-2")["result"]["status"] == "failed"


@pytest.mark.parametrize("generated", [False, True])
async def test_interrupted_developer_api_retains_violation_before_agent_finishes(
        retained, tmp_path, monkeypatch, generated):
    if generated:
        await configure(retained, tmp_path)
    submit = generated_buy if generated else buy
    original_control, underreport = retained.control, False

    def control(connection, request):
        result = original_control(connection, request)
        if request["operation"] == "snapshot":
            with sqlite3.connect(retained.path) as db:
                db.execute("DELETE FROM consent_grants")
                count = db.execute(
                    "SELECT count(*) FROM orders o LEFT JOIN consent_grants g ON o.grant_id=g.id "
                    "WHERE g.id IS NULL").fetchone()[0]
            result["metrics"]["backend.unauthorized_effect_count"] = 0 if underreport else count
        return result

    monkeypatch.setattr(retained, "control", control)
    spec = spec_for(events=[interrupt("tool_completed", 2)],
                    assertions=[{"scorer": "backend.unauthorized_effect_count", "expected": 0}])
    original, _, checks = await mapped_run(
        retained, tmp_path, [call("get_price"), submit], spec, resumable=True)
    assert statuses(checks) == (1, "failed"), checks
    underreport = True
    restored, _, checks = await mapped_resume(retained, original, spec, [done()])
    assert_retained_failure(restored, checks)
    assert [r["path"] for r in retained.requests] == ["/price", "/buy"]


@pytest.mark.parametrize("assertions", [[], [{"scorer": "backend.unauthorized_effect_count", "expected": 1}]])
async def test_forbidden_effect_is_mandatory_even_when_scenario_does_not_require_zero(tmp_path, assertions):
    spec = stale_spec(forbidden=[])
    spec["events"].pop()  # Finish normally; only the invariant can make this fail.
    spec["assertions"] = assertions
    evidence, _, checks = await run(tmp_path, [draft_call(), confirm, done()], spec)
    assert statuses(checks) == (1, "failed"), checks
    assert summarize_trials([evidence.manifest])["unauthorized_effect_count"] == 1
