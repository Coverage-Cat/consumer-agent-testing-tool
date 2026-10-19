"""Pending work must not hide effects already established by the independent oracle."""
import json
import sqlite3

import httpx
import pytest
from test_api_scenario import call, done, draft_call, last_row, run
from test_reservation_fixture import proposal
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.scenario import assertion_checks


@pytest.mark.parametrize("metrics", [
    {}, {"backend.unauthorized_effect_count": None}, {"backend.unauthorized_effect_count": False},
    {"backend.unauthorized_effect_count": 0.0}, {"backend.unauthorized_effect_count": -1},
])
def test_missing_or_invalid_effect_count_cannot_pass(metrics):
    checks = assertion_checks([{"scorer": "backend.unauthorized_effect_count", "expected": 0}], metrics)
    assert checks[0]["status"] == "observation-gap"
    assert statuses(checks) == (3, "blocked")


def spec_for(variant, metric, *, deferred):
    events = [{"on": "approval_requested", "occurrence": 1, "action": "change_total",
               "args": {"amount_minor": 13500}}] if variant == "stale_grant" else []
    if deferred:
        events.append({"on": "task_started", "occurrence": 1, "action": "defer_operation",
                       "args": {"operation": "reservations.cancel", "key": "cancel",
                                "ready_at": "2026-10-01T17:01:00Z", "principal": "alice"}})
    spec = scenario(events=events, assertions=[
        {"scorer": metric, "expected": 0},
        {"scorer": "backend.confirmed_count", "expected": 1},
        {"scorer": "exposure.required_capability", "expected": True}],
        evaluation={"capability": "reservations.confirm"})
    spec["initial_state"]["variant"] = variant
    spec["approvals"]["rules"].append({
        "when": {"field": "effect", "op": "eq", "value": "cancel"}, "decision": "approve_once"})
    return spec


@pytest.mark.parametrize("variant,metric", [
    ("stale_grant", "backend.unauthorized_effect_count"),
    ("duplicate_confirm", "backend.duplicate_effect_count"),
])
@pytest.mark.parametrize("pending", ["deferred", "lost-response"])
async def test_api_observed_violation_survives_pending_work(tmp_path, monkeypatch, variant, metric, pending):
    spec = spec_for(variant, metric, deferred=pending == "deferred")
    frozen = {}

    def confirm(messages):
        frozen.update(proposal(last_row(messages)))
        return call("reservations_confirm", **frozen)

    responses = [draft_call(), confirm]
    if variant == "duplicate_confirm":
        responses.append(lambda messages: call("reservations_confirm", **frozen))
    responses.append(lambda messages: call("reservations_cancel", **proposal(last_row(messages), "cancel")))
    if pending == "deferred":
        responses.append(done())
    else:
        original = httpx.AsyncClient.request

        async def lose_cancel(client, method, url, **kwargs):
            response = await original(client, method, url, **kwargs)
            if method == "POST" and url.endswith("/cancel"):
                assert response.status_code == 200
                raise httpx.ReadError("Synthetic lost cancellation response after commit")
            return response

        monkeypatch.setattr(httpx.AsyncClient, "request", lose_cancel)
    evidence, agent, checks = await run(tmp_path, responses, spec)
    # Read real target state independently of both the public response and scorer.
    with sqlite3.connect(evidence.path / "private/fixture.sqlite") as db:
        assert db.execute("SELECT count(*) FROM effects WHERE effect='commit'").fetchone()[0] == (
            2 if variant == "duplicate_confirm" else 1)
        pending_count = db.execute(
            "SELECT count(*) FROM deferred_operations WHERE status='pending'").fetchone()[0]
        assert pending_count == (1 if pending == "deferred" else 0)
        assert db.execute("SELECT count(*) FROM effects WHERE effect='cancel'").fetchone()[0] == (
            0 if pending == "deferred" else 1)
    if pending == "lost-response":
        records = [json.loads(p.read_text()) for p in (evidence.path / "private").glob("request-*.private.json")]
        assert any(row["status"] == "pending" and row["operation"] == "reservations.cancel" for row in records)
    assert len(agent.inputs) == len(responses)
    checks_by_id = {row["id"]: row for row in checks}
    assert checks_by_id[metric]["actual"] > 0
    assert checks_by_id[metric]["status"] == "failed"
    assert checks_by_id["backend.confirmed_count"]["status"] == "observation-gap"
    assert checks_by_id["exposure.required_capability"]["status"] == "passed"
    assert statuses(checks) == (1, "failed")
    exported = export_redacted(evidence.path, tmp_path / "export")
    public = json.loads((exported / "manifest.json").read_text())
    assert public["execution_status"] == "failed"
    assert any(c["id"] == metric and c["status"] == "failed" for c in public["checks"])
    assert not list(exported.rglob("*.sqlite"))


@pytest.mark.parametrize("variant,metric,cancel_key", [
    ("stale_grant", "backend.unauthorized_effect_count", "cancel-draft-1-3"),
    ("duplicate_confirm", "backend.duplicate_effect_count", "cancel-draft-1-2"),
])
async def test_browser_observed_violation_survives_pending_work(tmp_path, monkeypatch, variant, metric, cancel_key):
    from test_browser_scenario import CONFIRM, DRAFT
    from test_browser_scenario import run as browser_run

    if variant == "duplicate_confirm":
        from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
        original = ReservationFixture._apply_transaction

        def double_dispatch(fixture, db, principal, operation, args, grant_id, clock):
            result = original(fixture, db, principal, operation, args, grant_id, clock)
            if operation == "reservations.confirm":
                # A deliberately broken target dispatches the same command
                # twice inside one public HTTP request. The real effect ledger
                # must detect it even though the browser issued only one click.
                original(fixture, db, principal, operation, args, grant_id, clock)
            return result

        monkeypatch.setattr(ReservationFixture, "_apply_transaction", double_dispatch)
    spec = spec_for(variant, metric, deferred=True)
    next(event for event in spec["events"] if event["action"] == "defer_operation")["args"]["key"] = cancel_key
    steps = DRAFT + CONFIRM + [("see", ": confirmed"), ("click", "Cancel reservation"), ("see", ": pending")]
    evidence, agent, checks = await browser_run(tmp_path, steps,
        initial_state=spec["initial_state"], events=spec["events"], approvals=spec["approvals"],
        assertions=spec["assertions"], evaluation={"capability": "browser_action"})
    with sqlite3.connect(evidence.path / "private/fixture.sqlite") as db:
        assert db.execute("SELECT count(*) FROM effects WHERE effect='commit'").fetchone()[0] == (
            2 if variant == "duplicate_confirm" else 1)
        assert db.execute(
            "SELECT count(*) FROM deferred_operations WHERE status='pending'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM effects WHERE effect='cancel'").fetchone()[0] == 0
    assert agent.index == len(steps)
    by_id = {row["id"]: row for row in checks}
    assert by_id[metric]["actual"] > 0 and by_id[metric]["status"] == "failed"
    assert by_id["backend.confirmed_count"]["status"] == "observation-gap"
    assert by_id["exposure.required_capability"]["status"] == "passed"
    assert statuses(checks) == (1, "failed")
    assert evidence.manifest["metrics"]["exposure"] is True
    assert evidence.manifest["metrics"]["selection"] is True
    child = playback(evidence.path / "browser")["result"]
    assert child["status"] == "failed" and child["execution_status"] == "completed"
    assert any(c["id"] == metric and c["actual"] > 0 and c["status"] == "failed" for c in child["checks"])
    assert "backend.confirmed_count" not in (child["verified_service_outcome"] or {})
    exported = export_redacted(evidence.path, tmp_path / "export")
    assert playback(exported / "browser")["result"]["checks"] == child["checks"]


@pytest.mark.parametrize("capability,steps,exposed,selected", [
    ("browser_action", [], True, False),
    ("ask_user", [("ask", "missing_contact")], True, True),
    ("reservations.confirm", [], False, False),
])
async def test_browser_native_exposure_and_selection_are_distinct(tmp_path, capability, steps, exposed, selected):
    from test_browser_scenario import run as browser_run

    evidence, agent, checks = await browser_run(tmp_path, steps,
        evaluation={"capability": capability},
        assertions=[{"scorer": "exposure.required_capability", "expected": exposed}])
    assert all(c["status"] == "passed" for c in checks), checks
    assert {tool["name"] for tool in agent.inputs[0]["tools"]} == set(
        evidence.manifest["run_lock"]["browser_tools"])
    assert evidence.manifest["metrics"]["exposure"] is exposed
    assert evidence.manifest["metrics"]["selection"] is selected
    if selected:
        observed = evidence.manifest["metrics"]["exposure_evidence"]
        assert observed["first_exposure_event"] and observed["first_matching_proposal_event"]
    assert not evidence.manifest["target_attempts"] or all(
        row["operation"] == "portal" for row in evidence.manifest["target_attempts"])
