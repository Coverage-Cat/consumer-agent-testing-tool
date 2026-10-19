"""Exercise retained API leases over the real private Unix transport."""
import copy
import sqlite3
import time
from types import SimpleNamespace

import pytest
from test_mapped_scenario import inventory, spec_for  # noqa: F401 — shared real HTTP/Unix fixture
from test_scenario_runtime import evidence_for

from consumer_agent_testing_tool.api_control import ApiControlClient
from consumer_agent_testing_tool.api_lease import CAPABILITIES
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.serialization import load
from consumer_agent_testing_tool.surface import Paused, atomic_json


def session_for(root, *, resuming=False, deadline=None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = SimpleNamespace(private=root, resuming=resuming,
                              state={"deadline": deadline or time.time() + 120})

    def remaining():
        value = session.state["deadline"] - time.time()
        if value <= 0:
            raise Paused("Original scenario deadline has expired")
        return value

    session.remaining_seconds = remaining
    return session


@pytest.fixture
def retained(inventory, monkeypatch):  # noqa: F811 — imported pytest fixture
    original = inventory.control
    inventory.lease = None
    inventory.retained_fault = None
    inventory.retained_events = {}

    def control(connection, request):
        operation, args = request["operation"], request["args"]
        if operation == "event":
            saved = inventory.retained_events.get(args["event_id"])
            if saved:
                inventory.controls.append(operation)
                assert saved["args"] == args
                return saved["result"]
            result = original(connection, request)
            inventory.retained_events[args["event_id"]] = {"args": copy.deepcopy(args), "result": result}
            return result
        if operation == "describe":
            result = original(connection, request)
            result["capabilities"].extend(sorted(CAPABILITIES))
            return result
        if operation == "acquire_retained":
            if inventory.lease:
                inventory.controls.append(operation)
                return {"busy": True}
            result = original(connection, {**request, "operation": "acquire",
                                           "args": {"initial_state": args["initial_state"]}})
            inventory.controls[-1] = operation
            if result.get("reset_completed") is not True:
                return result
            inventory.lease = {key: args[key] for key in
                               ("lease_id", "initial_state_sha256", "expires_at")}
            inventory.lease.update(state_id="synthetic-state-epoch",
                                   user_profile=result["user_profile"])
            return {**inventory.lease, "clock": result["clock"],
                    "reset_completed": True, "grants_retired": True}
        if operation in ("resume", "suspend"):
            inventory.controls.append(operation)
            if inventory.lease is None or request["identity"] != inventory.identity \
                    or any(args[key] != inventory.lease[key] for key in args):
                return {"error": "wrong lease"}
            if operation == "resume":
                if inventory.owner:
                    return {"busy": True}
                inventory.owner = connection
                result = {**inventory.lease, "clock": inventory.clock,
                          "state_preserved": True, "grants_retired": True}
                if inventory.retained_fault:
                    key, value = inventory.retained_fault
                    result[key] = value
                return result
            if connection is not inventory.owner:
                return {"error": "not the lease owner"}
            inventory.owner = None
            for grant in inventory.grants.values():
                inventory.broker.revoke(grant)
            return {**args, "suspended": True, "grants_retired": True}
        result = original(connection, request)
        if operation == "release":
            inventory.lease = None
        return result

    monkeypatch.setattr(inventory, "control", control)
    return inventory


def client_for(store, evidence, session):
    return ApiControlClient(store.target, store.mapping, spec_for(), evidence, session)


async def start(store, tmp_path):
    evidence = evidence_for(tmp_path)
    session = session_for(evidence.path / "private")
    atomic_json(session.private / "scenario.private.json", {"clock": spec_for()["initial_state"]["clock"]})
    first = client_for(store, evidence, session)
    profile = await first.connect(retain=True)
    return evidence, session, first, profile


async def test_retained_api_lease_resumes_without_reset_and_retires_authority(retained, tmp_path):
    evidence, session, first, profile = await start(retained, tmp_path)
    retained.price, retained.revision = 13500, 2
    await first.close(suspend=True)
    assert retained.owner is None and retained.lease
    resumed = client_for(retained, evidence, session_for(session.private, resuming=True,
                                                        deadline=session.state["deadline"]))
    try:
        assert await resumed.connect(retain=True) == profile
        assert (retained.price, retained.revision) == (13500, 2)
        assert retained.controls.count("acquire_retained") == 1
        assert retained.controls.count("resume") == 1
        assert "acquire" not in retained.controls
        assert retained.requests == []
        assert (await resumed.snapshot())["metrics"]["backend.confirmed_count"] == 0
    finally:
        await resumed.close()
    assert retained.lease is None
    assert load(session.private / "api-lease.private.json")["status"] == "released"
    public = (evidence.path / "events.jsonl").read_text()
    for key in ("lease_id", "state_id"):
        assert first.retained_lease.record[key] not in public
    with pytest.raises(LabError, match="identity"):
        await client_for(retained, evidence, session_for(session.private, resuming=True,
                         deadline=session.state["deadline"])).connect(retain=True)


@pytest.mark.parametrize("fault", [
    ("state_id", "a-different-state"),
    ("expires_at", "2099-01-01T00:00:00+00:00"),
    ("user_profile", {"name": "Different Account"}),
    ("grants_retired", False),
    ("state_preserved", False),
    ("clock", "2026-10-01T18:00:00Z"),
])
async def test_retained_resume_rejects_changed_state_or_unretired_grants(retained, tmp_path, fault):
    evidence, session, first, _ = await start(retained, tmp_path)
    await first.close(suspend=True)
    retained.retained_fault = fault
    resumed = client_for(retained, evidence, session_for(session.private, resuming=True,
                                                        deadline=session.state["deadline"]))
    try:
        with pytest.raises(LabError, match="confirm|differs"):
            await resumed.connect(retain=True)
        assert not resumed.acquired
        assert retained.controls.count("acquire_retained") == 1
        assert retained.requests == []
    finally:
        await resumed.close()


async def test_retained_adapter_without_capability_never_resets(inventory, tmp_path):  # noqa: F811
    evidence = evidence_for(tmp_path)
    client = client_for(inventory, evidence, session_for(evidence.path / "private"))
    try:
        with pytest.raises(Unsupported, match="retained-trial-v1"):
            await client.connect(retain=True)
        assert inventory.controls == ["describe"]
        assert inventory.requests == []
    finally:
        await client.close()


async def test_retained_private_acquisition_loss_cannot_reset_again(retained, tmp_path, monkeypatch):
    original = retained.control

    def lose(connection, request):
        result = original(connection, request)
        if request["operation"] == "acquire_retained":
            connection.close()
        return result

    monkeypatch.setattr(retained, "control", lose)
    evidence = evidence_for(tmp_path)
    session = session_for(evidence.path / "private")
    first = client_for(retained, evidence, session)
    try:
        with pytest.raises(LabError):
            await first.connect(retain=True)
    finally:
        await first.close()
    assert load(session.private / "api-lease.private.json")["status"] == "acquiring"
    controls = copy.deepcopy(retained.controls)
    resumed = client_for(retained, evidence, session_for(session.private, resuming=True,
                                                        deadline=session.state["deadline"]))
    with pytest.raises(LabError, match="identity"):
        await resumed.connect(retain=True)
    assert retained.controls == controls


@pytest.mark.parametrize("change", ["identity", "deadline", "permissions", "symlink", "expired"])
async def test_retained_checkpoint_rejected_before_connection(retained, tmp_path, change):
    evidence, session, first, _ = await start(retained, tmp_path)
    await first.close(suspend=True)
    path = session.private / "api-lease.private.json"
    state = load(path)
    deadline = session.state["deadline"]
    if change == "identity":
        state["identity"]["principal"] = "another-user"
        atomic_json(path, state)
    elif change == "deadline":
        deadline += 10
    elif change == "expired":
        deadline = time.time() - 1
    elif change == "permissions":
        path.chmod(0o644)
    else:
        other = session.private / "other.private.json"
        path.rename(other)
        path.symlink_to(other.name)
    prior = list(retained.controls)
    resumed = client_for(retained, evidence, session_for(session.private, resuming=True, deadline=deadline))
    with pytest.raises((LabError, Paused)):
        await resumed.connect(retain=True)
    assert retained.controls == prior


async def test_suspension_acknowledgment_loss_can_resume_same_state(retained, tmp_path, monkeypatch):
    evidence, session, first, _ = await start(retained, tmp_path)
    original = retained.control

    def lose(connection, request):
        result = original(connection, request)
        if request["operation"] == "suspend":
            connection.close()
        return result

    monkeypatch.setattr(retained, "control", lose)
    with pytest.raises(LabError):
        await first.close(suspend=True)
    assert load(session.private / "api-lease.private.json")["status"] == "suspending"
    resumed = client_for(retained, evidence, session_for(session.private, resuming=True,
                                                        deadline=session.state["deadline"]))
    try:
        await resumed.connect(retain=True)
        assert retained.controls.count("acquire_retained") == 1
    finally:
        await resumed.close()


async def test_retained_suspension_revokes_real_unconsumed_grant(retained, tmp_path):
    _, _, first, _ = await start(retained, tmp_path)
    arguments = {"amount_minor": 12500, "revision": 1, "key": "unused"}
    await first.prepare("proposal:1", retained.mapping.operations["buy_item"], arguments)
    grant = await first.control("grant", proposal_id="proposal:1", principal="customer",
        target_operation="orders.submit", arguments=arguments, decision="approve_once",
        expires_at="2026-10-01T17:05:00Z")
    await first.close(suspend=True)
    with sqlite3.connect(retained.path) as db:
        assert db.execute("SELECT status FROM consent_grants WHERE id=?", (grant["id"],)).fetchone() == ("revoked",)


async def test_retained_resume_allows_only_exact_pending_clock_event(retained, tmp_path):
    evidence, session, first, _ = await start(retained, tmp_path)
    await first.close(suspend=True)
    clock = "2026-10-01T18:00:00Z"
    runtime = {"clock": retained.clock, "events": {"event:0": {
        "status": "pending", "event": {"action": "advance_time", "args": {"clock": clock}}}}}
    atomic_json(session.private / "scenario.private.json", runtime)
    retained.clock = clock
    resumed = client_for(retained, evidence, session_for(session.private, resuming=True,
                                                        deadline=session.state["deadline"]))
    try:
        await resumed.connect(retain=True)
        with pytest.raises(LabError, match="Recovered scenario clock"):
            resumed.retained_lease.verify_clock(SimpleNamespace(state=runtime))
        runtime["clock"] = clock
        resumed.retained_lease.verify_clock(SimpleNamespace(state=runtime))
    finally:
        await resumed.close()
