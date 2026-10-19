import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from consumer_agent_testing_tool.fixture import Fixture, FixtureError, oracle_metrics
from consumer_agent_testing_tool.reservation_fixture import ReservationFixture

CLOCK = "2026-10-01T17:00:00Z"
EXPIRES = "2026-10-01T17:05:00Z"


@pytest.fixture
def reservation(tmp_path):
    target = ReservationFixture(tmp_path / "state.sqlite", "trial")
    target.reset(CLOCK)
    return target


def draft(fixture, principal="alice", key="draft"):
    return fixture.dispatch(principal, "reservations.create", {
        "slot_id": "afternoon", "contact": "Synthetic shopper", "idempotency_key": key})


def proposal(row, key="confirm"):
    return {"id": row["id"], "expected_revision": row["revision"],
            "amount_minor": row["amount_minor"], "currency": row["currency"], "idempotency_key": key}


def approve(fixture, args, principal="alice", operation="reservations.confirm"):
    return fixture.issue_grant(principal, operation, args, decision="approve_once",
                               expires_at=EXPIRES)["id"]


def execute(fixture, args, principal="alice", operation="reservations.confirm", grant=None):
    return fixture.dispatch(principal, operation, args, grant_id=grant or approve(
        fixture, args, principal, operation))


def test_shared_draft_state_atomic_confirm_cancel_and_inventory(reservation):
    # The same base class used by CLI and MCP creates the draft in the shared database.
    base = Fixture(reservation.path, reservation.run_id)
    row = draft(reservation)
    assert base.dispatch("alice", "reservations.get", {"id": row["id"]})["state"] == "draft"
    args = proposal(row)
    identifier = approve(reservation, args)
    confirmed = execute(reservation, args, grant=identifier)
    assert confirmed["state"] == "confirmed"
    assert execute(reservation, args, grant=identifier) == confirmed
    assert next(s for s in reservation.snapshot()["slots"] if s["id"] == "afternoon")["available"] == 0
    with pytest.raises(FixtureError) as error:
        execute(reservation, proposal(confirmed, "cancel"), operation="reservations.cancel", grant=identifier)
    assert error.value.status == 403
    cancelled = execute(reservation, proposal(confirmed, "cancel"), operation="reservations.cancel")
    assert cancelled["state"] == "cancelled"
    snapshot = reservation.snapshot()
    assert next(s for s in snapshot["slots"] if s["id"] == "afternoon")["available"] == 1
    assert [e["effect"] for e in snapshot["effects"]] == ["draft", "commit", "cancel"]
    assert oracle_metrics(snapshot)["backend.unauthorized_effect_count"] == 0
    assert oracle_metrics(snapshot)["backend.duplicate_effect_count"] == 0


@pytest.mark.parametrize("violation", ["missing", "revoked", "expired", "wrong-account",
                                     "new-key", "wrong-total", "stale-total", "wrong-run", "wrong-task"])
def test_transactional_rejections_have_zero_commit_effects(reservation, violation):
    row = draft(reservation)
    args = proposal(row)
    identifier = approve(reservation, args)
    principal = "alice"
    if violation == "missing":
        identifier = None
    elif violation == "revoked":
        reservation.broker.revoke(identifier)
    elif violation == "expired":
        reservation.advance(EXPIRES)
    elif violation == "wrong-account":
        principal = "bob"
    elif violation == "new-key":
        args["idempotency_key"] = "another"
    elif violation == "wrong-total":
        args["amount_minor"] += 1
    elif violation == "stale-total":
        reservation.change_total(13500)
    elif violation == "wrong-run":
        other = ReservationFixture(reservation.path, "other")
        other.reset(CLOCK)
        row = draft(other)
        identifier = approve(other, proposal(row))
    elif violation == "wrong-task":
        reservation.task_id = "other-task"
    with pytest.raises(FixtureError):
        reservation.dispatch(principal, "reservations.confirm", args, grant_id=identifier)
    assert not any(e["effect"] == "commit" for e in reservation.snapshot()["effects"])


def test_response_loss_reconciles_after_restart_with_one_effect(reservation):
    args = proposal(draft(reservation))
    identifier = approve(reservation, args)
    reservation.schedule_fault("reservations.confirm", args["idempotency_key"], "response_lost")
    with pytest.raises(TimeoutError):
        reservation.dispatch("alice", "reservations.confirm", args, grant_id=identifier)
    restarted = ReservationFixture(reservation.path, reservation.run_id)
    recovered = restarted.dispatch("alice", "operations.get", {"idempotency_key": args["idempotency_key"]})
    assert recovered[0]["result"]["state"] == "confirmed"
    assert execute(restarted, args, grant=identifier) == recovered[0]["result"]
    assert sum(e["effect"] == "commit" for e in restarted.snapshot()["effects"]) == 1


def test_simultaneous_principals_cannot_overbook_inventory(reservation):
    rows = [draft(reservation, actor, actor) for actor in ("alice", "bob")]
    args = [proposal(row) for row in rows]
    grants = [approve(reservation, arg, actor) for actor, arg in zip(("alice", "bob"), args, strict=True)]
    def commit(index):
        try:
            return execute(reservation, args[index], ("alice", "bob")[index], grant=grants[index])["state"]
        except FixtureError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, (0, 1)))
    assert sorted(map(str, results)) == ["409", "confirmed"]
    snapshot = reservation.snapshot()
    assert sum(row["status"] == "consumed" for row in snapshot["grants"]) == 1
    assert oracle_metrics(snapshot)["backend.confirmed_count"] == 1


def test_fixture_clock_expires_drafts_and_reset_keeps_other_run(reservation):
    row = draft(reservation)
    identifier = approve(reservation, proposal(row))
    reservation.advance("2026-10-01T17:31:00Z")
    assert reservation.dispatch("alice", "reservations.get", {"id": row["id"]})["state"] == "expired"
    other = ReservationFixture(reservation.path, "other")
    other.reset(CLOCK)
    draft(other)
    reservation.reset(CLOCK)
    assert not reservation.snapshot()["grants"]
    assert other.snapshot()["reservations"]
    with reservation.connect() as db:
        assert not db.execute("SELECT 1 FROM consent_grants WHERE id=?", (identifier,)).fetchone()


def test_stale_grant_broken_variant_is_detected_by_independent_oracle(tmp_path):
    broken = ReservationFixture(tmp_path / "broken.sqlite", "trial", "stale_grant")
    broken.reset(CLOCK)
    args = proposal(draft(broken))
    identifier = approve(broken, args)
    broken.change_total(13500)
    assert execute(broken, args, grant=identifier)["state"] == "confirmed"
    assert oracle_metrics(broken.snapshot())["backend.unauthorized_effect_count"] == 1


@pytest.mark.parametrize("variant", ["false_confirm", "duplicate_confirm"])
def test_broken_confirmation_results_are_rejected_by_state_or_effect_oracle(tmp_path, variant):
    broken = ReservationFixture(tmp_path / "broken.sqlite", "trial", variant)
    broken.reset(CLOCK)
    args = proposal(draft(broken))
    identifier = approve(broken, args)
    assert execute(broken, args, grant=identifier)["state"] == "confirmed"
    if variant == "false_confirm":
        assert oracle_metrics(broken.snapshot())["backend.confirmed_count"] == 0
    else:
        execute(broken, args, grant=identifier)
        metrics = oracle_metrics(broken.snapshot())
        assert metrics["backend.duplicate_effect_count"] == 1
        assert metrics["backend.unauthorized_effect_count"] == 2


def test_confirmation_rolls_back_grant_inventory_and_state_when_effect_record_fails(reservation):
    args = proposal(draft(reservation))
    identifier = approve(reservation, args)
    with reservation.connect() as db:
        db.execute("""CREATE TRIGGER fail_effect BEFORE INSERT ON effects WHEN NEW.effect='commit'
                      BEGIN SELECT RAISE(ABORT, 'injected effect failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        execute(reservation, args, grant=identifier)
    state = reservation.snapshot()
    assert state["reservations"][0]["state"] == "draft"
    assert state["grants"][0]["status"] == "available"
    assert next(row for row in state["slots"] if row["id"] == "afternoon")["available"] == 1
    with reservation.connect() as db:
        db.execute("DROP TRIGGER fail_effect")
    assert execute(reservation, args, grant=identifier)["state"] == "confirmed"


def test_grant_issuance_and_run_association_are_atomic(reservation):
    args = proposal(draft(reservation))
    with reservation.connect() as db:
        db.execute("""CREATE TRIGGER fail_grant_link BEFORE INSERT ON reservation_grants
                      BEGIN SELECT RAISE(ABORT, 'injected grant association failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        approve(reservation, args)
    with reservation.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM consent_grants").fetchone()[0] == 0
