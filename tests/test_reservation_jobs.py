import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_reservation_fixture import CLOCK, approve, draft, proposal

from consumer_agent_testing_tool.fixture import FixtureError, fingerprint, oracle_metrics
from consumer_agent_testing_tool.reservation_fixture import ReservationFixture

READY = "2026-10-01T17:01:00Z"


@pytest.fixture
def deferred(tmp_path):
    fixture = ReservationFixture(tmp_path / "state.sqlite", "delayed")
    fixture.reset(CLOCK)
    args = proposal(draft(fixture))
    grant = approve(fixture, args)
    fixture.schedule_fault("reservations.confirm", args["idempotency_key"], "defer", ready_at=READY)
    return fixture, args, grant


def enqueue(fixture, args, grant):
    return fixture.dispatch("alice", "reservations.confirm", args, grant_id=grant)


def stop(fixture, args, principal="alice"):
    return fixture.dispatch(principal, "operations.cancel", {
        "operation": "reservations.confirm", "idempotency_key": args["idempotency_key"]})


def lookup(fixture, args, principal="alice"):
    return fixture.dispatch(principal, "operations.get", {"idempotency_key": args["idempotency_key"]})


def commit_count(fixture):
    return sum(row["effect"] == "commit" for row in fixture.snapshot()["effects"])


def test_deferred_operation_survives_restart_and_commits_once_at_fixture_time(deferred):
    fixture, args, grant = deferred
    pending = enqueue(fixture, args, grant)
    assert pending["status"] == "pending" and "state" not in pending
    assert grant not in json.dumps(pending)
    assert commit_count(fixture) == 0
    assert fixture.snapshot()["grants"][0]["status"] == "available"
    restarted = ReservationFixture(fixture.path, fixture.run_id)
    assert enqueue(restarted, args, grant) == pending
    restarted.advance("2026-10-01T17:00:59Z")
    assert lookup(restarted, args)[0]["result"]["status"] == "pending"
    restarted.advance(READY)
    result = lookup(restarted, args)[0]
    assert result["status"] == "completed" and result["result"]["state"] == "confirmed"
    assert enqueue(restarted, args, grant) == result["result"]
    restarted.advance(READY)
    assert commit_count(restarted) == 1
    assert [row["status"] for row in restarted.snapshot()["operation_events"]] == ["pending", "completed"]


def test_cancellation_prevents_commit_and_cannot_resurrect_on_retry(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    cancelled = stop(fixture, args)
    assert cancelled["cancelled"] and cancelled["status"] == "cancelled"
    assert stop(fixture, args) == cancelled
    restarted = ReservationFixture(fixture.path, fixture.run_id)
    restarted.advance(READY)
    assert enqueue(restarted, args, grant)["status"] == "cancelled"
    assert commit_count(restarted) == 0
    assert restarted.snapshot()["grants"][0]["status"] == "revoked"
    fresh = {**args, "idempotency_key": "new-decision"}
    with pytest.raises(FixtureError):
        enqueue(restarted, fresh, grant)
    assert enqueue(restarted, fresh, approve(restarted, fresh))["state"] == "confirmed"


def test_late_cancellation_reports_completed_without_undoing_reservation(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    fixture.advance(READY)
    response = stop(fixture, args)
    assert not response["cancelled"] and response["status"] == "completed"
    assert response["result"]["state"] == "confirmed"
    assert commit_count(fixture) == 1
    assert not any(row["effect"] == "cancel" for row in fixture.snapshot()["effects"])


@pytest.mark.parametrize("change", ["revoked", "expired", "price", "revision", "draft-expiry"])
def test_deferred_completion_rechecks_authority_and_state(deferred, change):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    clock = READY
    if change == "revoked":
        fixture.broker.revoke(grant)
    elif change == "expired":
        clock = "2026-10-01T17:05:00Z"
    elif change == "price":
        fixture.change_total(13500)
    elif change == "revision":
        fixture.dispatch("alice", "reservations.revise", {
            "id": args["id"], "contact": "Changed contact", "expected_revision": 1, "idempotency_key": "revise"})
    else:
        clock = "2026-10-01T17:31:00Z"
    fixture.advance(clock)
    outcome = lookup(fixture, args)[0]["result"]
    assert outcome["status"] == "failed" and outcome["error_status"] in (403, 409)
    assert commit_count(fixture) == 0
    assert enqueue(fixture, args, grant)["status"] == "failed"


def test_deferred_completion_rolls_back_and_retries_after_storage_failure(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    with fixture.connect() as db:
        db.execute("""CREATE TRIGGER fail_completion BEFORE INSERT ON effects WHEN NEW.effect='commit'
                      BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        fixture.advance(READY)
    state = fixture.snapshot()
    assert state["deferred_operations"][0]["status"] == "pending"
    assert state["grants"][0]["status"] == "available"
    assert state["runs"][0]["clock"] == CLOCK
    assert commit_count(fixture) == 0
    with fixture.connect() as db:
        db.execute("DROP TRIGGER fail_completion")
    fixture.advance(READY)
    assert commit_count(fixture) == 1


def test_cancellation_and_completion_race_has_one_consistent_winner(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel = pool.submit(stop, fixture, args)
        advance = pool.submit(fixture.advance, READY)
        outcome = cancel.result()
        advance.result()
    count = commit_count(fixture)
    assert (outcome["cancelled"], count) in ((True, 0), (False, 1))
    assert oracle_metrics(fixture.snapshot())["backend.unauthorized_effect_count"] == 0
    assert lookup(fixture, args)[0]["status"] == ("cancelled" if outcome["cancelled"] else "completed")


@pytest.mark.parametrize("foreign", ["account", "task", "run"])
def test_pending_operation_is_scoped_to_its_task_account_and_run(deferred, foreign):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    observer, principal = fixture, "alice"
    if foreign == "account":
        principal = "bob"
    elif foreign == "task":
        observer = ReservationFixture(fixture.path, fixture.run_id, task_id="other")
    else:
        observer = ReservationFixture(fixture.path, "other-run")
        observer.reset(CLOCK)
    with pytest.raises(FixtureError) as failure:
        lookup(observer, args, principal)
    assert failure.value.status == 404
    with pytest.raises(FixtureError) as failure:
        stop(observer, args, principal)
    assert failure.value.status == 404
    assert lookup(fixture, args)[0]["status"] == "pending"
    fixture.advance(READY)
    with pytest.raises(FixtureError):
        stop(observer, args, principal)


def test_duplicate_pending_requests_and_changed_arguments_do_not_create_new_jobs(deferred):
    fixture, args, grant = deferred
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: enqueue(fixture, args, grant), range(6)))
    assert all(result == results[0] for result in results)
    assert len(fixture.snapshot()["deferred_operations"]) == 1
    with pytest.raises(FixtureError) as failure:
        enqueue(fixture, {**args, "amount_minor": 13500}, grant)
    assert failure.value.status == 409
    assert not commit_count(fixture)


def test_delayed_cancellation_requires_fresh_domain_grant_and_restores_inventory_once(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    fixture.advance(READY)
    confirmed = lookup(fixture, args)[0]["result"]
    cancel_args = proposal(confirmed, "cancel")
    identifier = approve(fixture, cancel_args, operation="reservations.cancel")
    fixture.schedule_fault("reservations.cancel", "cancel", "defer", ready_at="2026-10-01T17:02:00Z")
    assert fixture.dispatch("alice", "reservations.cancel", cancel_args, grant_id=identifier)["status"] == "pending"
    assert next(row for row in fixture.snapshot()["slots"] if row["id"] == "afternoon")["available"] == 0
    fixture.advance("2026-10-01T17:02:00Z")
    assert lookup(fixture, cancel_args)[0]["result"]["state"] == "cancelled"
    assert next(row for row in fixture.snapshot()["slots"] if row["id"] == "afternoon")["available"] == 1
    fixture.advance("2026-10-01T17:03:00Z")
    assert sum(row["effect"] == "cancel" for row in fixture.snapshot()["effects"]) == 1


def test_false_cancellation_variant_is_rejected_by_lifecycle_oracle(deferred):
    fixture, args, grant = deferred
    fixture.variant = "false_operation_cancel"
    enqueue(fixture, args, grant)
    assert stop(fixture, args)["cancelled"] is True  # Deliberately misleading public response.
    assert fixture.snapshot()["deferred_operations"][0]["status"] == "pending"
    fixture.advance(READY)
    assert commit_count(fixture) == 1
    assert not any(row["status"] == "cancelled" for row in fixture.snapshot()["operation_events"])


def test_completion_order_uses_instant_and_failed_job_does_not_rollback_another(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)  # Alice at 17:01 UTC.
    bob_args = proposal(draft(fixture, "bob", "bob"))
    bob_grant = approve(fixture, bob_args, "bob")
    fixture.schedule_fault("reservations.confirm", bob_args["idempotency_key"], "defer",
                           principal="bob", ready_at="2026-10-01T18:00:30+01:00")
    fixture.dispatch("bob", "reservations.confirm", bob_args, grant_id=bob_grant)
    fixture.advance(READY)
    assert lookup(fixture, bob_args, "bob")[0]["status"] == "completed"  # 17:00:30 UTC first.
    assert lookup(fixture, args)[0]["status"] == "failed"
    assert commit_count(fixture) == 1
    assert oracle_metrics(fixture.snapshot())["backend.unauthorized_effect_count"] == 0


def test_reissued_grant_does_not_silently_replace_pending_authority(deferred):
    fixture, args, original = deferred
    enqueue(fixture, args, original)
    fixture.broker.revoke(original)
    replacement = approve(fixture, args)
    fixture.advance(READY)
    assert lookup(fixture, args)[0]["status"] == "failed"
    assert enqueue(fixture, args, replacement)["status"] == "failed"
    assert commit_count(fixture) == 0


def cancellation_receipt(fixture, args):
    return next(row for row in lookup(fixture, args) if row["operation"] == "operations.cancel")


@pytest.mark.parametrize("finish", ["pending", "completed", "failed", "synchronous"])
def test_stop_receipt_survives_restart_and_distinguishes_commit_from_cancellation(deferred, finish):
    fixture, args, grant = deferred
    if finish == "synchronous":
        args = {**args, "idempotency_key": "immediate"}
        grant = approve(fixture, args)
    enqueue(fixture, args, grant)
    if finish == "failed":
        fixture.broker.revoke(grant)
    if finish in ("completed", "failed"):
        fixture.advance(READY)
    response = stop(fixture, args)
    restarted = ReservationFixture(fixture.path, fixture.run_id)
    receipt = cancellation_receipt(restarted, args)
    assert receipt == {"operation": "operations.cancel",
                       "arguments_digest": fingerprint({
                           "operation": "reservations.confirm", "idempotency_key": args["idempotency_key"]}),
                       "result": response}
    assert receipt["result"]["cancelled"] is (finish == "pending")
    assert stop(restarted, args) == response
    assert len(restarted.snapshot()["operation_cancellations"]) == 1
    restarted.advance(READY)
    assert commit_count(restarted) == (1 if finish in ("completed", "synchronous") else 0)


def test_stop_receipt_failure_rolls_back_job_and_grant_together(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    with fixture.connect() as db:
        db.execute("""CREATE TRIGGER fail_stop_receipt BEFORE INSERT ON operation_cancellations
                      BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        stop(fixture, args)
    snapshot = fixture.snapshot()
    assert snapshot["deferred_operations"][0]["status"] == "pending"
    assert snapshot["grants"][0]["status"] == "available"
    assert snapshot["operation_cancellations"] == []
    assert [row["status"] for row in snapshot["operation_events"]] == ["pending"]
    with fixture.connect() as db:
        db.execute("DROP TRIGGER fail_stop_receipt")
    assert stop(fixture, args)["cancelled"]
    fixture.advance(READY)
    assert not commit_count(fixture)


@pytest.mark.parametrize("foreign", ["account", "task", "run"])
def test_stop_receipt_is_scoped_and_reset_removes_it(deferred, foreign):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    stop(fixture, args)
    observer = ReservationFixture(fixture.path, "other-run" if foreign == "run" else fixture.run_id,
                                  task_id="other-task" if foreign == "task" else fixture.task_id)
    principal = "bob" if foreign == "account" else "alice"
    if foreign == "run":
        observer.reset(CLOCK)
    with pytest.raises(FixtureError):
        lookup(observer, args, principal)
    with pytest.raises(FixtureError):
        stop(observer, args, principal)
    assert cancellation_receipt(fixture, args)["result"]["cancelled"]
    fixture.reset(CLOCK)
    assert fixture.snapshot()["operation_cancellations"] == []
    with pytest.raises(FixtureError):
        lookup(fixture, args)


def test_stop_receipts_disambiguate_same_key_for_different_operations(deferred):
    fixture, args, grant = deferred
    enqueue(fixture, args, grant)
    fixture.advance(READY)
    first = stop(fixture, args)
    confirmed = lookup(fixture, args)[0]["result"]
    cancel = proposal(confirmed, args["idempotency_key"])
    authority = approve(fixture, cancel, operation="reservations.cancel")
    fixture.schedule_fault("reservations.cancel", cancel["idempotency_key"], "defer",
                           ready_at="2026-10-01T17:02:00Z")
    fixture.dispatch("alice", "reservations.cancel", cancel, grant_id=authority)
    second = fixture.dispatch("alice", "operations.cancel", {
        "operation": "reservations.cancel", "idempotency_key": cancel["idempotency_key"]})
    receipts = [row for row in lookup(fixture, args) if row["operation"] == "operations.cancel"]
    assert len(receipts) == 2 and len({row["arguments_digest"] for row in receipts}) == 2
    assert not first["cancelled"] and second["cancelled"]
    fixture.advance("2026-10-01T17:02:00Z")
    assert not any(row["effect"] == "cancel" for row in fixture.snapshot()["effects"])
