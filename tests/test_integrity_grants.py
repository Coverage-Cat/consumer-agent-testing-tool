"""Scenario-bounded grant lifetime; a token refresh keeps the user's unused approvals (B9)."""
import json
import uuid

import pytest
from test_api_scenario import call, confirm, done, draft_call, run
from test_authentication import session
from test_reservation_fixture import CLOCK, approve, draft, proposal
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.fixture import FixtureError
from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
from consumer_agent_testing_tool.scenario import read_scenario
from consumer_agent_testing_tool.serialization import load

READY = "2026-10-01T17:10:00Z"  # Later than the default 300-second grant.


@pytest.fixture
def reservation(tmp_path):
    fixture = ReservationFixture(tmp_path / "state.sqlite", "grants")
    fixture.reset(CLOCK)
    return fixture


def change(fixture, state, **kwargs):
    return fixture.apply_event(uuid.uuid4().hex, "set_authentication", session(state, **kwargs))


def long_job(grant_seconds=None):
    spec = scenario(events=[
        {"on": "task_started", "occurrence": 1, "action": "defer_operation", "args": {
            "operation": "reservations.confirm", "key": "confirm", "ready_at": READY, "principal": "alice"}},
        {"on": "tool_completed", "occurrence": 2, "action": "advance_time", "args": {"clock": READY}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    if grant_seconds is not None:
        spec["approvals"]["grant_seconds"] = grant_seconds
    return spec


@pytest.mark.parametrize("grant_seconds,confirmed", [(None, 0), (900, 1)])
async def test_grant_lifetime_is_configurable_for_long_deferred_work(tmp_path, grant_seconds, confirmed):
    spec = long_job(grant_seconds)
    evidence, _, checks = await run(tmp_path, [draft_call(), confirm,
        call("operations_get", idempotency_key="confirm"), done()], spec)
    assert evidence.manifest["run_lock"]["grant_seconds"] == (grant_seconds or 300)
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == confirmed
    job = evidence.manifest["target_attempts"]
    assert any(a["operation"] == "reservations.confirm" and a["status"] == 200 for a in job)
    assert statuses(checks) == ((0, "completed") if confirmed else (1, "failed")), checks
    private = load(evidence.path / "private/scenario.private.json")
    expiry = next(iter(private["approvals"].values()))["expires_at"]
    assert expiry == ("2026-10-01T17:15:00+00:00" if grant_seconds else "2026-10-01T17:05:00+00:00")


@pytest.mark.parametrize("value", [0, 86401, 300.5, 300.0, True, "300"])
def test_grant_lifetime_is_bounded(tmp_path, value):
    document = long_job()
    document["approvals"]["grant_seconds"] = value
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    with pytest.raises(LabError):
        read_scenario(path, milestone=2)
    document["approvals"]["grant_seconds"] = 3600
    path.write_text(json.dumps(document))
    assert read_scenario(path, milestone=2)["approvals"]["grant_seconds"] == 3600


def test_refresh_for_the_same_account_keeps_unused_grants(reservation):
    args = proposal(draft(reservation))
    grant = approve(reservation, args)
    assert change(reservation, "refreshed")["retired_grants"] == 0
    assert reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)["state"] == "confirmed"


@pytest.mark.parametrize("state", ["expired", "revoked", "wrong_account", "insufficient_scope", "connected"])
def test_every_other_session_transition_retires_unused_grants(reservation, state):
    args = proposal(draft(reservation))
    grant = approve(reservation, args)
    assert change(reservation, state)["retired_grants"] == 1
    change(reservation, "refreshed")
    with pytest.raises(FixtureError):
        reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)


def test_refresh_after_an_account_switch_still_retires_authority(reservation):
    args = proposal(draft(reservation))
    change(reservation, "wrong_account")
    grant = approve(reservation, args)  # Issued while another account's session was active.
    assert change(reservation, "refreshed")["retired_grants"] == 1
    with pytest.raises(FixtureError, match="Grant absent"):
        reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)


def test_refresh_does_not_fail_an_accepted_deferred_job(reservation):
    args = proposal(draft(reservation))
    grant = approve(reservation, args)
    reservation.schedule_fault("reservations.confirm", args["idempotency_key"], "defer",
                               ready_at="2026-10-01T17:01:00Z")
    assert reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)["status"] == "pending"
    change(reservation, "refreshed")
    reservation.advance("2026-10-01T17:01:00Z")
    job = reservation.snapshot()["deferred_operations"][0]
    assert job["status"] == "completed" and job["error"] is None
