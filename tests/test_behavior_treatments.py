from datetime import UTC, datetime, timedelta

import pytest

from consumer_agent_testing_tool.errors import LabError


def test_session_lease_rejects_other_principals_and_expires():
    from consumer_agent_testing_tool.behavior_sessions import SessionLease
    ticks = [0.0]
    lease = SessionLease("study", "principal", ["https://fixture.example"], 60,
                         monotonic=lambda: ticks[0])
    state = {"cookies": [{"name": "session", "value": "synthetic", "domain": "fixture.example",
                           "path": "/", "expires": -1}],
             "origins": [{"origin": "https://fixture.example", "localStorage": [{"name": "cart", "value": "1"}]}]}
    lease.save(state)
    assert lease.restore("study", "principal", ["https://fixture.example"]) == state
    with pytest.raises(LabError):
        lease.restore("study", "other", ["https://fixture.example"])
    ticks[0] = 61
    assert lease.restore("study", "principal", ["https://fixture.example"]) is None
    assert lease.last_outcome == "expired"


def test_session_does_not_restore_disconnected_or_cross_origin_storage():
    from consumer_agent_testing_tool.behavior_sessions import SessionLease
    lease = SessionLease("study", "owner", ["https://fixture.example"], 60)
    with pytest.raises(LabError):
        lease.save({"cookies": [], "origins": [{"origin": "https://other.example"}]})
    lease.disconnect()
    with pytest.raises(LabError):
        lease.save({"cookies": [], "origins": []})
    assert lease.restore("study", "owner", ["https://fixture.example"]) is None


def test_availability_is_sticky_and_samples_actual_boundary():
    from consumer_agent_testing_tool.behavior_clock import AvailabilityWindow, VirtualClock
    clock = VirtualClock(datetime(2026, 10, 30, tzinfo=UTC))
    window = AvailabilityWindow(clock, duration=60, max_gap=10)
    window.sample(route_reachable=True, authentication_healthy=True)
    clock.advance(5)
    window.sample(route_reachable=False, authentication_healthy=True)
    clock.advance(5)
    window.sample(route_reachable=True, authentication_healthy=True)
    assert window.status == "interrupted" and not window.release_allowed
    assert window.record()["clock"] == "virtual"


def test_schedule_restart_no_duplicate_and_expiry_is_not_last_read(tmp_path):
    from consumer_agent_testing_tool.behavior_clock import DurableSchedule, VirtualClock
    clock = VirtualClock(datetime(2026, 10, 30, tzinfo=UTC))
    path = tmp_path / "jobs.sqlite"
    with DurableSchedule(path, clock) as jobs:
        jobs.add("watch", every=10, until=clock.utc() + timedelta(seconds=30), max_actions=1)
        clock.advance(10)
        assert jobs.attempt("watch") == 1
        jobs.record("watch", "read", evidence="read-1")
        jobs.record("watch", "effect", evidence="effect-1")
        assert not jobs.action_allowed("watch")
        assert not jobs.expired("watch")
    with DurableSchedule(path, clock) as jobs:
        assert jobs.attempt("watch") is None
        clock.advance(21)
        assert jobs.expired("watch")
        assert not jobs.cleaned("watch")
        jobs.record("watch", "cleanup", evidence="scheduler-read")
        assert jobs.cleaned("watch")


def test_credential_use_does_not_infer_model_visibility_and_preview_does_not_consume():
    from consumer_agent_testing_tool.behavior_credentials import CredentialJournal
    journal = CredentialJournal()
    journal.receive("code", synthetic=True, allowed_destinations=["fixture"], receipt="receipt-1")
    assert not journal.consume("code", destination="fixture", method="GET", evidence="preview")
    assert journal.consume("code", destination="fixture", method="POST", evidence="ledger")
    assert journal.visibility("code") == "unknown"
    journal.disclose("code", destination="answer", owner_requested=True)
    journal.disclose("code", destination="collector", owner_requested=False)
    assert [row["stage"] for row in journal.events][-2:] == ["synthetic-disclosure", "egress"]
    journal.receive("real", synthetic=False, allowed_destinations=["fixture"], receipt="receipt-2")
    with pytest.raises(LabError):
        journal.disclose("real", destination="answer", owner_requested=True)


def test_synthetic_disclosure_does_not_weaken_real_secret_filter():
    from consumer_agent_testing_tool.behavior_credentials import SyntheticCredentialFixture
    fixture = SyntheticCredentialFixture("fixture")
    message = fixture.issue("code")
    with pytest.raises(LabError, match="receipt"):
        fixture.use("code", message["value"], method="POST", evidence="read")
    fixture.acknowledge("code", message["receipt"])
    assert not fixture.use("code", message["value"], method="GET", evidence="preview")
    assert fixture.use("code", message["value"], method="POST", evidence="ledger")
    assert fixture.disclose("code", owner_requested=True) == message["value"]
    assert message["value"] not in str(fixture.journal.events)
