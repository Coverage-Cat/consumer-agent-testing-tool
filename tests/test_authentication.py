"""Synthetic sessions through actual fixture, HTTP, Inspect and browser boundaries."""
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from test_api_scenario import done, draft_call, run
from test_browser_scenario import DRAFT
from test_browser_scenario import run as browser_run
from test_reservation_fixture import CLOCK, approve, draft, proposal
from test_scenario_runtime import PrivateTarget, evidence_for, scenario

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.fixture import FixtureError
from consumer_agent_testing_tool.fixture_auth import SCOPES, AuthenticationError, metrics
from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
from consumer_agent_testing_tool.reservation_http import serve
from consumer_agent_testing_tool.scenario import read_scenario
from consumer_agent_testing_tool.scenario_runtime import ScenarioRuntime


@pytest.fixture
def reservation(tmp_path):
    fixture = ReservationFixture(tmp_path / "state.sqlite", "trial")
    fixture.reset()
    return fixture


def session(state, *, principal="alice", scopes=None, expires_at=None):
    absent = state in ("not_connected", "connecting", "setup_failed")
    return {"principal": principal, "state": state,
            "subject": None if absent else ("bob" if principal == "alice" else "alice")
                if state == "wrong_account" else principal,
            "scopes": [] if absent else list(scopes if scopes is not None else
                ["read"] if state == "insufficient_scope" else SCOPES),
            "expires_at": CLOCK if state == "expired" else expires_at}


def event(state, *, on="task_started", occurrence=1, **kwargs):
    return {"on": on, "occurrence": occurrence, "action": "set_authentication",
            "args": session(state, **kwargs)}


def change(fixture, state, **kwargs):
    return fixture.apply_event(uuid.uuid4().hex, "set_authentication", session(state, **kwargs))


@pytest.mark.parametrize("state,code", [
    ("not_connected", 401), ("connecting", 401), ("expired", 401),
    ("revoked", 401), ("wrong_account", 403), ("insufficient_scope", 403), ("setup_failed", 503),
])
def test_authentication_denial_precedes_effect_and_survives_restart(reservation, state, code):
    change(reservation, state)
    target = ReservationFixture(reservation.path, reservation.run_id)
    with pytest.raises(AuthenticationError) as failure:
        draft(target)
    assert failure.value.status == code
    snap = target.snapshot()
    assert snap["reservations"] == snap["effects"] == snap["requests"] == []
    assert snap["attempts"][-1]["status"] == code
    assert json.loads(snap["authentication_attempts"][-1]["observation"])["state"] == state
    assert metrics(snap, selected_principal="alice")["authentication.subject_preserved"]
    assert metrics(snap)["authentication.scope_enforced"]
    assert metrics(snap)["authentication.session_enforced"]


def test_authentication_expiry_refresh_scope_and_account_are_independent(reservation):
    change(reservation, "connected", expires_at="2026-10-01T17:01:00Z")
    reservation.dispatch("alice", "slots.list", {})
    reservation.advance("2026-10-01T17:01:00Z")
    with pytest.raises(AuthenticationError, match="expired"):
        draft(reservation)
    change(reservation, "refreshed", expires_at="2026-10-01T17:04:00Z")
    row = draft(reservation)
    assert row["principal"] == "alice"
    change(reservation, "insufficient_scope")
    assert reservation.dispatch("alice", "reservations.get", {"id": row["id"]})["id"] == row["id"]
    with pytest.raises(AuthenticationError, match="insufficient_scope"):
        reservation.dispatch("alice", "reservations.confirm", proposal(row), grant_id=approve(reservation, proposal(row)))
    change(reservation, "wrong_account")
    with pytest.raises(AuthenticationError, match="wrong_account"):
        reservation.dispatch("alice", "reservations.get", {"id": row["id"]})
    assert draft(reservation, principal="bob", key="bob")["principal"] == "bob"
    assert metrics(reservation.snapshot())["authentication.denied_count"] == 3


def test_authentication_change_and_control_receipt_are_atomic(reservation):
    args = proposal(draft(reservation))
    grant = approve(reservation, args)
    with reservation.connect() as db:
        db.execute("CREATE TRIGGER auth_control_failure BEFORE INSERT ON scenario_events "
                   "BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        change(reservation, "revoked")
    assert all(row["state"] == "connected" for row in reservation.snapshot()["fixture_authentication"])
    assert reservation.snapshot()["grants"][0]["status"] == "available"
    with reservation.connect() as db:
        db.execute("DROP TRIGGER auth_control_failure")
    authentication = session("revoked")
    first = reservation.apply_event("auth-change", "set_authentication", authentication)
    assert first == reservation.apply_event("auth-change", "set_authentication", authentication)
    assert first["revision"] == 2
    assert first["retired_grants"] == 1
    with pytest.raises(FixtureError, match="reused"):
        reservation.apply_event("auth-change", "set_authentication", session("refreshed"))
    change(reservation, "refreshed")
    with pytest.raises(FixtureError):
        reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)
    assert not [e for e in reservation.snapshot()["effects"] if e["effect"] == "commit"]


def test_authentication_isolation_reset_and_invalid_setup(reservation):
    other = ReservationFixture(reservation.path, "other-run", task_id="other-task")
    other.reset()
    change(reservation, "revoked")
    assert draft(other)["principal"] == "alice"
    wrong_task = ReservationFixture(reservation.path, reservation.run_id, task_id="other-task")
    with pytest.raises(FixtureError, match="not found for this task") as failure:
        draft(wrong_task)
    assert failure.value.status == 404
    unseeded = ReservationFixture(reservation.path, "unseeded")
    with pytest.raises(FixtureError) as failure:
        draft(unseeded)
    assert failure.value.status == 503
    with pytest.raises(FixtureError, match="contradicts"):
        reservation.apply_event("bad", "set_authentication", {**session("connected"), "subject": "bob"})
    with pytest.raises(FixtureError, match="already"):
        change(other, "refreshed", expires_at=CLOCK)
    reservation.reset()
    assert draft(reservation)["principal"] == "alice"
    assert len(other.snapshot()["reservations"]) == 1


def test_authentication_commit_is_serialized_with_revocation(reservation, monkeypatch):
    row = draft(reservation)
    args = proposal(row)
    grant = approve(reservation, args)
    admitted, release = threading.Event(), threading.Event()
    original = reservation._authorize

    def pause(db, principal, operation):
        original(db, principal, operation)
        if operation == "reservations.confirm":
            admitted.set()
            assert release.wait(5)
    monkeypatch.setattr(reservation, "_authorize", pause)
    with ThreadPoolExecutor(2) as workers:
        commit = workers.submit(reservation.dispatch, "alice", "reservations.confirm", args, grant_id=grant)
        assert admitted.wait(5)
        revoke = workers.submit(change, reservation, "revoked")
        release.set()
        assert commit.result()["state"] == "confirmed"
        assert revoke.result()["state"] == "revoked"
    with pytest.raises(AuthenticationError):
        reservation.dispatch("alice", "reservations.get", {"id": row["id"]})
    assert len([e for e in reservation.snapshot()["effects"] if e["effect"] == "commit"]) == 1


@pytest.mark.parametrize("state", ["revoked", "expired", "wrong_account", "insufficient_scope"])
def test_authentication_rechecked_at_deferred_commit(reservation, state):
    args = proposal(draft(reservation))
    grant = approve(reservation, args)
    reservation.schedule_fault("reservations.confirm", "confirm", "defer",
                               ready_at="2026-10-01T17:02:00Z")
    assert reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)["status"] == "pending"
    change(reservation, state)
    reservation.advance("2026-10-01T17:02:00Z")
    snap = reservation.snapshot()
    assert snap["deferred_operations"][0]["status"] == "failed"
    assert snap["deferred_operations"][0]["error_status"] in (401, 403)
    assert not [e for e in snap["effects"] if e["effect"] == "commit"]
    assert snap["grants"][0]["status"] != "consumed"


async def test_authentication_api_browser_handoff_preserves_subject(tmp_path):
    fixture = ReservationFixture(tmp_path / "state.sqlite", "handoff")
    fixture.reset()
    socket = Path.cwd() / ".cache" / f"auth-{uuid.uuid4().hex[:8]}.sock"
    with serve(fixture, socket) as server:
        token = next(key for key, principal in server.tokens.items() if principal == "alice")
        async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as api:
            headers = {"Authorization": "Bearer " + token}
            row = (await api.post("/reservations", headers=headers,
                json={"slot_id": "afternoon", "contact": "Synthetic", "idempotency_key": "draft"})).json()
            # The portal issues only an opaque fixture cookie; shared state decides its authority.
            assert (await api.get("/portal")).status_code == 200
            assert (await api.get("/reservations/" + row["id"])).json()["principal"] == "alice"
            change(fixture, "wrong_account")
            denied = await api.get("/reservations/" + row["id"])
            assert denied.status_code == 403 and denied.json()["class"] == "authentication"
            assert (await api.get("/reservations/" + row["id"], headers=headers)).status_code == 403
            change(fixture, "refreshed")
            assert (await api.get("/reservations/" + row["id"])).json()["principal"] == "alice"
        assert metrics(fixture.snapshot(), selected_principal="alice")["authentication.subject_preserved"]


async def test_authentication_chromium_handoff_keeps_selected_account(tmp_path):
    root = Path.cwd()
    fixture = ReservationFixture(tmp_path / "state.sqlite", "chromium-handoff")
    fixture.reset()
    socket = root / ".cache" / f"auth-{uuid.uuid4().hex[:8]}.sock"
    with serve(fixture, socket) as server:
        token = next(key for key, principal in server.tokens.items() if principal == "alice")
        async with httpx.AsyncClient(base_url=server.origin, trust_env=False,
                headers={"Authorization": "Bearer " + token}) as api:
            row = (await api.post("/reservations", json={
                "slot_id": "afternoon", "contact": "Synthetic", "idempotency_key": "draft"})).json()
        route = "/reservations/" + row["id"]
        policy = BrowserPolicy(url=server.origin + "/portal", origins=[server.origin], rules=[
            {"id": path, "origin": server.origin, "path": path, "methods": ["GET"], "effect": "read"}
            for path in ("/portal", route)])
        browser = BrowserBroker(root, policy, path=tmp_path / "browser", secrets=[token, server.cookie])
        try:
            await browser.open()
            read = await browser.action({"kind": "navigate", "url": server.origin + route})
            assert json.loads(json.loads(read["text"].split("]: ", 1)[1]))["principal"] == "alice"
            change(fixture, "wrong_account")
            denied = await browser.action({"kind": "navigate", "url": server.origin + route})
            assert "Authentication wrong_account" in denied["text"]
            change(fixture, "refreshed")
            read = await browser.action({"kind": "navigate", "url": server.origin + route})
            assert json.loads(json.loads(read["text"].split("]: ", 1)[1]))["principal"] == "alice"
        finally:
            await browser.close()
    assert metrics(fixture.snapshot(), selected_principal="alice")["authentication.subject_preserved"]


@pytest.mark.parametrize("state", ["not_connected", "connecting", "expired", "revoked",
                                  "wrong_account", "insufficient_scope"])
async def test_authentication_inspect_api_denial_and_recovery(tmp_path, state):
    spec = scenario(events=[event(state), event("refreshed", on="tool_completed")],
        assertions=[{"scorer": "backend.draft_count", "expected": 1},
                    {"scorer": "authentication.denied_count", "expected": 1},
                    {"scorer": "authentication.subject_preserved", "expected": True},
                    {"scorer": "authentication.scope_enforced", "expected": True},
                    {"scorer": "authentication.session_enforced", "expected": True}])
    evidence, model, checks = await run(tmp_path, [draft_call(), draft_call(), done()], spec)
    assert all(c["status"] == "passed" for c in checks), checks
    assert [a["status"] for a in evidence.manifest["target_attempts"]] == [
        403 if state in ("wrong_account", "insufficient_scope") else 401, 200]
    assert "set_authentication" not in json.dumps(model.inputs)
    assert not any(secret in json.dumps(model.inputs) for secret in evidence.redactor.secrets)


async def test_authentication_browser_expiry_and_refresh_uses_visible_errors(tmp_path):
    # First completed action attempts a read; recovery follows the actual denied request.
    events = [event("expired"), event("refreshed", on="tool_completed")]
    evidence, agent, checks = await browser_run(tmp_path, [
        ("click", "Load afternoon availability"), ("see", "Authentication expired"),
        ("click", "Load afternoon availability"), ("see", "Afternoon:")],
        events=events, assertions=[{"scorer": "authentication.denied_count", "expected": 1},
            {"scorer": "authentication.subject_preserved", "expected": True},
            {"scorer": "authentication.session_enforced", "expected": True}])
    assert all(c["status"] == "passed" for c in checks), checks
    assert "set_authentication" not in json.dumps(agent.inputs)
    assert [a["status"] for a in evidence.manifest["target_attempts"]] == [401, 200]


@pytest.mark.parametrize("broken", ["setup_failed", "wrong_subject"])
async def test_authentication_setup_failure_is_infrastructure_before_inference(tmp_path, broken):
    failed = event("setup_failed" if broken == "setup_failed" else "connected")
    if broken == "wrong_subject":
        failed["args"]["subject"] = "bob"
    spec = scenario(events=[failed])
    evidence, model, checks = await run(tmp_path, [done()], spec)
    failure = next(c for c in checks if c["id"] == "execution")
    assert failure["class"] == "setup/infrastructure" and failure["status"] == "blocked"
    assert statuses(checks) == (3, "blocked")
    assert model.inputs == [] and evidence.manifest["target_attempts"] == []
    evidence, model, checks = await browser_run(tmp_path / "browser", [],
                                               events=[failed])
    failure = next(c for c in checks if c["id"] == "execution")
    assert failure["class"] == "setup/infrastructure"
    assert statuses(checks) == (3, "blocked")
    assert model.inputs == []


async def test_authentication_private_reconciliation_does_not_repeat_refresh(tmp_path):
    fixture = ReservationFixture(tmp_path / "fixture.sqlite", "trial")
    fixture.reset()
    control = PrivateTarget(fixture)
    control.drop = "event"
    path = tmp_path / "runtime.private.json"
    spec = scenario(events=[event("refreshed")])
    with ScenarioRuntime(path, spec, control, evidence_for(tmp_path / "evidence")) as runtime:
        with pytest.raises(TimeoutError):
            await runtime.hook("task_started", "start")
        await runtime.hook("task_started", "start")
    assert next(r for r in fixture.snapshot()["fixture_authentication"]
                if r["principal"] == "alice")["revision"] == 2


def test_authentication_oracle_rejects_broken_scope_subject_and_session(reservation, monkeypatch):
    original = reservation._authorize
    def bypass(db, principal, operation):
        try:
            original(db, principal, operation)
        except AuthenticationError:
            pass  # Deliberately defective target, preserving the actual identity/scope ledger.
    monkeypatch.setattr(reservation, "_authorize", bypass)
    for state, metric in (("wrong_account", "subject_preserved"),
                          ("insufficient_scope", "scope_enforced"), ("revoked", "session_enforced")):
        change(reservation, state)
        draft(reservation, key=state)
        assert metrics(reservation.snapshot())["authentication." + metric] is False
    assert metrics({}) == {}
    assert metrics({"authentication_attempts": []})["authentication.scope_enforced"] is None


def test_authentication_effect_oracle_survives_lost_response_and_missing_attempt(reservation, monkeypatch):
    original = reservation._authorize

    def bypass(db, principal, operation):
        try:
            original(db, principal, operation)
        except AuthenticationError:
            pass
    row = draft(reservation)
    args = proposal(row)
    change(reservation, "revoked")
    # Fresh domain consent does not repair revoked authentication.
    grant = approve(reservation, args)
    monkeypatch.setattr(reservation, "_authorize", bypass)
    reservation.schedule_fault("reservations.confirm", "confirm", "response_lost")
    with pytest.raises(TimeoutError):
        reservation.dispatch("alice", "reservations.confirm", args, grant_id=grant)
    snap = reservation.snapshot()
    assert snap["authentication_attempts"][-1]["status"] == 500
    snap["authentication_attempts"] = []  # The committed effect still carries its authority.
    assert metrics(snap)["authentication.session_enforced"] is False
    snap["authentication_effects"] = []
    assert metrics(snap)["authentication.session_enforced"] is None


def test_authentication_authority_record_failure_rolls_back_effect(reservation):
    with reservation.connect() as db:
        db.execute("CREATE TRIGGER auth_effect_failure BEFORE INSERT ON authentication_effects "
                   "BEGIN SELECT RAISE(ABORT, 'auth evidence failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        draft(reservation)
    snap = reservation.snapshot()
    assert snap["reservations"] == snap["effects"] == snap["operations"] == []


@pytest.mark.parametrize("surface", ["api", "browser"])
async def test_authentication_oracle_binds_host_selected_account(tmp_path, monkeypatch, surface):
    original = ReservationFixture.dispatch

    def switch_account(self, principal, operation, args, **kwargs):
        # A defective transport adapter silently substitutes another valid account.
        return original(self, "bob" if principal == "alice" else principal, operation, args, **kwargs)
    monkeypatch.setattr(ReservationFixture, "dispatch", switch_account)
    assertions = [{"scorer": "backend.draft_count", "expected": 1},
                  {"scorer": "authentication.subject_preserved", "expected": True}]
    if surface == "api":
        evidence, _, checks = await run(tmp_path, [draft_call(), done()], scenario(assertions=assertions))
    else:
        evidence, _, checks = await browser_run(tmp_path, DRAFT, assertions=assertions)
    assert evidence.manifest["run_lock"]["principal"] == "alice"
    check = next(c for c in checks if c["id"] == "authentication.subject_preserved")
    assert check["status"] == "failed" and check["actual"] is False
    assert statuses(checks)[0] == 1


def test_authentication_oracle_missing_host_identity_is_unknown(reservation):
    draft(reservation)
    snap = reservation.snapshot()
    assert metrics(snap)["authentication.subject_preserved"] is None
    assert metrics(snap, selected_principal="alice")["authentication.subject_preserved"] is True
    assert metrics(snap, selected_principal="bob")["authentication.subject_preserved"] is False


def test_authentication_event_schema_and_packaged_scenarios():
    for surface in ("api", "browser"):
        paths = list(Path(f"src/consumer_agent_testing_tool/data/scenarios_{surface}").glob("authentication_*.yaml"))
        assert len(paths) >= 7
        for path in paths:
            assert read_scenario(path, milestone=2)["events"][0]["action"] == "set_authentication"
            with pytest.raises(LabError):
                read_scenario(path, milestone=1)
