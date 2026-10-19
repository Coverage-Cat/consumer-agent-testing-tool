import asyncio
import json
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_browser_portals import reference
from test_reservation_fixture import CLOCK, EXPIRES, proposal

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.fixture import Fixture, oracle_metrics
from consumer_agent_testing_tool.process import communicate_bounded
from consumer_agent_testing_tool.protocol import child_environment
from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
from consumer_agent_testing_tool.reservation_http import serve
from consumer_agent_testing_tool.serialization import digest


@pytest.fixture
def rest(tmp_path):
    socket = Path.cwd() / ".cache" / f"r-{uuid.uuid4().hex[:8]}.sock"
    fixture = ReservationFixture(tmp_path / "state.sqlite", "http-trial")
    with serve(fixture, socket) as server:
        yield server


async def control(server, operation, **args):
    reader, writer = await asyncio.open_unix_connection(str(server.control_path))
    try:
        writer.write(json.dumps({"operation": operation, **args}).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        assert "error" not in result, result
        return result["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def client(server, principal="alice"):
    token = next(key for key, actor in server.tokens.items() if actor == principal)
    return httpx.AsyncClient(base_url=server.origin, headers={"Authorization": "Bearer " + token},
                             trust_env=False, timeout=5)


async def new_draft(api):
    response = await api.post("/reservations", json={
        "slot_id": "afternoon", "contact": "Synthetic shopper", "idempotency_key": "draft"})
    assert response.status_code == 200, response.text
    return response.json()


async def test_rest_and_shared_cli_mcp_logic_have_one_state_and_oracle(rest):
    await control(rest, "reset", clock=CLOCK)
    async with client(rest) as api:
        assert (await api.get("/slots")).status_code == 200
        row = await new_draft(api)
        base = Fixture(rest.fixture.path, rest.fixture.run_id)
        assert base.dispatch("alice", "reservations.get", {"id": row["id"]})["state"] == "draft"
        revised = await api.patch("/reservations/" + row["id"], json={
            "contact": "Revised contact", "expected_revision": 1, "idempotency_key": "revise"})
        assert revised.status_code == 200 and revised.json()["revision"] == 2
        args = proposal(revised.json())
        await control(rest, "grant", principal="alice", target_operation="reservations.confirm",
                      arguments=args, decision="approve_once", expires_at=EXPIRES)
        body = {key: value for key, value in args.items() if key != "id"}
        confirmed = await api.post(f"/reservations/{row['id']}/confirm", json=body)
        assert confirmed.status_code == 200 and confirmed.json()["state"] == "confirmed"
        assert (await api.post(f"/reservations/{row['id']}/confirm", json=body)).json() == confirmed.json()
        cancel = proposal(confirmed.json(), "cancel")
        await control(rest, "grant", principal="alice", target_operation="reservations.cancel",
                      arguments=cancel, decision="approve_once", expires_at=EXPIRES)
        cancelled = await api.post(f"/reservations/{row['id']}/cancel",
                                  json={key: value for key, value in cancel.items() if key != "id"})
        assert cancelled.json()["state"] == "cancelled"
    state = await control(rest, "snapshot")
    assert [event["effect"] for event in state["effects"]] == ["draft", "draft", "commit", "cancel"]
    assert oracle_metrics(state)["backend.unauthorized_effect_count"] == 0
    assert next(row for row in state["slots"] if row["id"] == "afternoon")["available"] == 1


async def test_http_response_loss_after_commit_is_reconciled_not_reexecuted(rest):
    await control(rest, "reset", clock=CLOCK)
    async with client(rest) as api:
        row = await new_draft(api)
        args = proposal(row)
        await control(rest, "grant", principal="alice", target_operation="reservations.confirm",
                      arguments=args, decision="approve_once", expires_at=EXPIRES)
        await control(rest, "schedule_fault", target_operation="reservations.confirm",
                      key=args["idempotency_key"], fault="response_lost")
        body = {key: value for key, value in args.items() if key != "id"}
        with pytest.raises(httpx.RemoteProtocolError):
            await api.post(f"/reservations/{row['id']}/confirm", json=body)
        recovered = (await api.get("/operations/confirm")).json()
        assert recovered[0]["result"]["state"] == "confirmed"
        assert (await api.post(f"/reservations/{row['id']}/confirm", json=body)).json() == recovered[0]["result"]
    state = await control(rest, "snapshot")
    assert sum(e["effect"] == "commit" for e in state["effects"]) == 1


async def test_native_mcp_draft_can_be_confirmed_over_rest_without_state_translation(rest):
    await control(rest, "reset", clock=CLOCK)
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-m", "consumer_agent_testing_tool.fixture_mcp", "--db", rest.fixture.path,
        "--run-id", rest.fixture.run_id, "--transactional"], env=child_environment())
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        catalog = await session.list_tools()
        assert {"reservations_confirm", "reservations_cancel"} <= {tool.name for tool in catalog.tools}
        assert not any("grant" in tool.name or "snapshot" in tool.name for tool in catalog.tools)
        result = await session.call_tool("reservations_create", {
            "slot_id": "afternoon", "contact": "Synthetic shopper", "idempotency_key": "native-draft"})
        row = result.structured_content["result"]
        args = proposal(row)
        denied = await session.call_tool("reservations_confirm", args)
        assert denied.is_error
        await control(rest, "grant", principal="alice", target_operation="reservations.confirm",
                      arguments=args, decision="approve_once", expires_at=EXPIRES)
        async with client(rest) as api:
            result = await api.post(f"/reservations/{row['id']}/confirm",
                                   json={key: value for key, value in args.items() if key != "id"})
            assert result.status_code == 200
        read_back = await session.call_tool("reservations_get", {"id": row["id"]})
        assert read_back.structured_content["result"]["state"] == "confirmed"
        cancel = proposal(read_back.structured_content["result"], "cancel")
        await control(rest, "grant", principal="alice", target_operation="reservations.cancel",
                      arguments=cancel, decision="approve_once", expires_at=EXPIRES)
        cancelled = await session.call_tool("reservations_cancel", cancel)
        assert cancelled.structured_content["result"]["state"] == "cancelled"
    state = await control(rest, "snapshot")
    assert [effect["effect"] for effect in state["effects"]] == ["draft", "commit", "cancel"]


async def test_workspace_fixture_cli_starts_unseeded_and_runs_private_controls(tmp_path):
    socket = Path.cwd() / ".cache" / f"cli-{uuid.uuid4().hex[:8]}.sock"
    server = await asyncio.create_subprocess_exec(sys.executable, "-m", "consumer_agent_testing_tool.reservation_http",
        "--database", str(tmp_path / "cli.sqlite"), "--control", str(socket),
        "--run-id", "cli-proof", "--port", "0", env=child_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(server.stdout.readline(), 10)
        assert line, (await server.stderr.read()).decode()
        startup = json.loads(line)
        async with httpx.AsyncClient(base_url=startup["origin"], trust_env=False,
                headers={"Authorization": "Bearer " + startup["test_credentials"]["alice"]}) as api:
            assert (await api.get("/slots")).status_code == 503
            request = await asyncio.create_subprocess_exec(sys.executable, "-m", "consumer_agent_testing_tool.reservation_http",
                "--control", str(socket), "--control-request", '{"operation":"reset"}',
                env=child_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            result = await communicate_bounded(request, timeout=10)
            assert result["exit_status"] == 0, result["stderr"]
            assert json.loads(result["stdout"])["result"]["seed"] == "one_available_slot"
            assert (await api.get("/slots")).status_code == 200
    finally:
        if server.returncode is None:
            server.send_signal(signal.SIGINT)
        try:
            result = await communicate_bounded(server, timeout=10)
            assert result["exit_status"] == 0, result["stderr"]
        finally:
            socket.unlink(missing_ok=True)


async def test_public_routes_cannot_reset_issue_grants_or_change_principal(rest):
    await control(rest, "reset", clock=CLOCK)
    async with client(rest) as api, client(rest, "bob") as bob:
        row = await new_draft(api)
        assert (await bob.get(f"/reservations/{row['id']}")).status_code == 404
        assert (await bob.get("/operations/draft")).status_code == 404
        for path in ("/reset", "/snapshot", "/grant", "/advance", "/schedule_fault"):
            assert (await api.post(path, json={})).status_code == 404
        assert (await api.post(f"/reservations/{row['id']}/confirm", json={
            "expected_revision": 1, "amount_minor": 12500, "currency": "USD",
            "idempotency_key": "unapproved"})).status_code == 403
        assert (await api.post("/reservations", content='{"slot_id":"afternoon","slot_id":"morning"}',
                               headers={"Content-Type": "application/json"})).status_code == 400
        assert (await api.get("/slots", headers={"Host": "untrusted.invalid"})).status_code == 403
        assert (await api.get("/slots", headers={"Origin": "https://untrusted.invalid"})).status_code == 403
    async with httpx.AsyncClient(base_url=rest.origin, trust_env=False) as anonymous:
        assert (await anonymous.get("/slots")).status_code == 401
    assert not any(e["effect"] == "commit" for e in (await control(rest, "snapshot"))["effects"])


def decision():
    return {"decision": "approve_once", "state_version": "fixture",
            "amount_minor": 12500, "currency": "USD",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}


@pytest.mark.parametrize("completion", ["response_loss", "deferred_complete", "deferred_cancel"])
async def test_browser_portal_requires_private_domain_grant_and_verifies_backend(rest, completion):
    root = Path.cwd()
    if not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace Chromium is unavailable")
    await control(rest, "reset", clock=CLOCK)
    reads = ["/portal", "/slots/afternoon", "/reservations/draft-1",
             "/operations/confirm-draft-1-1"]
    writes = ["/reservations", "/reservations/draft-1/confirm", "/reservations/draft-1/cancel",
              "/operations/confirm-draft-1-1/cancel"]
    rules = [{"id": path, "origin": rest.origin, "path": path,
              "methods": ["GET"], "effect": "read"} for path in reads]
    rules += [{"id": path, "origin": rest.origin, "path": path,
               "methods": ["POST"], "effect": "write"} for path in writes]

    class ScriptedDraftApproval(BrowserBroker):
        async def _authorize(self, name, arguments):
            # Test-runner rule for one nonbinding draft. The model never issues a grant.
            if arguments["rule"] == "/reservations" and not getattr(self, "draft_approved", False):
                body = json.loads(arguments["body"])
                assert body["slot_id"] == "afternoon" and body["contact"] == "Synthetic shopper"
                assert set(body) == {"slot_id", "contact", "idempotency_key"}
                self.decisions[digest(arguments)] = decision()
                self.draft_approved = True
            return await super()._authorize(name, arguments)

    broker = ScriptedDraftApproval(root, BrowserPolicy(
        url=rest.origin + "/portal", origins=[rest.origin], rules=rules, max_actions=100))

    async def click(snapshot, label):
        for attempt in range(3):
            try:
                return await broker.action({"kind": "click", "ref": reference(snapshot, label),
                                            "generation": snapshot["generation"]})
            except LabError as error:
                # Only this pre-dispatch rejection proves that no click was performed.
                if not str(error).startswith("Page changed;") or attempt == 2:
                    raise
                snapshot = await broker.action({"kind": "snapshot"})

    async def visible(text):
        for _ in range(30):
            snapshot = await broker.action({"kind": "snapshot"})
            if text in snapshot["text"]:
                return snapshot
            await asyncio.sleep(.02)
        pytest.fail(f"Missing {text}: {snapshot['text']}")

    def network_approval(operation, args):
        path = f"/reservations/{args['id']}/{operation}"
        # Match JSON.stringify order in the real portal.
        body = {key: args[key] for key in ("expected_revision", "amount_minor", "currency", "idempotency_key")}
        request = {"url": rest.origin + path, "method": "POST", "rule": path,
                   "body": json.dumps(body, separators=(",", ":"))}
        broker.decisions[digest(request)] = decision()

    try:
        snapshot = await broker.open()
        await click(snapshot, "Load afternoon availability")
        snapshot = await visible("$125.00 USD")
        snapshot = await broker.action({"kind": "type", "ref": reference(snapshot, "Contact", "textbox"),
                                       "generation": snapshot["generation"], "text": "Synthetic shopper"})
        await click(snapshot, "Create nonbinding draft")
        snapshot = await visible("draft, $125.00 USD, revision 1")
        args = proposal({"id": "draft-1", "revision": 1, "amount_minor": 12500, "currency": "USD"},
                        "confirm-draft-1-1")
        network_approval("confirm", args)
        await click(snapshot, "Confirm reservation")
        snapshot = await visible("Grant absent")
        assert not any(e["effect"] == "commit" for e in (await control(rest, "snapshot"))["effects"])
        await control(rest, "grant", principal="alice", target_operation="reservations.confirm",
                      arguments=args, decision="approve_once", expires_at=EXPIRES)
        network_approval("confirm", args)
        ready = "2026-10-01T17:01:00Z"
        await control(rest, "schedule_fault", target_operation="reservations.confirm",
                      key=args["idempotency_key"], fault="response_lost" if completion == "response_loss" else "defer",
                      ready_at=ready)
        await click(snapshot, "Confirm reservation")
        if completion == "response_loss":
            snapshot = await visible("Failed to fetch")
        else:
            snapshot = await visible("Operation confirm-draft-1-1: pending")
            assert not any(e["effect"] == "commit" for e in (await control(rest, "snapshot"))["effects"])
            if completion == "deferred_cancel":
                path = "/operations/confirm-draft-1-1/cancel"
                broker.decisions[digest({"url": rest.origin + path, "method": "POST", "rule": path,
                    "body": '{"operation":"reservations.confirm"}'})] = decision()
                await click(snapshot, "Stop pending operation")
                snapshot = await visible("Operation confirm-draft-1-1: cancelled")
                await click(snapshot, "Refresh reservation")
                snapshot = await visible("Operation confirm-draft-1-1: cancelled")
            await control(rest, "advance", clock=ready)
        if completion != "deferred_cancel":
            await click(snapshot, "Reconcile last operation")
            await visible("confirmed, $125.00 USD, revision 2")
        state = await control(rest, "snapshot")
        count = sum(e["effect"] == "commit" for e in state["effects"])
        expected = 0 if completion == "deferred_cancel" else 1
        broker.assert_backend_count("expected-commits", count, expected, source="separate Unix fixture oracle")
        assert count == expected and oracle_metrics(state)["backend.unauthorized_effect_count"] == 0
        assert not any(grant["id"] in json.dumps(broker.events) for grant in state["grants"])
    finally:
        await broker.close()


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
async def test_the_fixture_server_stops_on_a_signal_even_when_started_with_sigint_ignored(tmp_path, stop):
    # A background job inherits SIGINT ignored; the server must still stop when its runner asks it to.
    socket = Path.cwd() / ".cache" / f"sig-{uuid.uuid4().hex[:8]}.sock"
    server = await asyncio.create_subprocess_exec(sys.executable, "-m", "consumer_agent_testing_tool.reservation_http",
        "--database", str(tmp_path / "sig.sqlite"), "--control", str(socket), "--run-id", "signal-proof",
        "--port", "0", env=child_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
    try:
        assert await asyncio.wait_for(server.stdout.readline(), 10)
        server.send_signal(stop)
        result = await communicate_bounded(server, timeout=10)
        assert result["exit_status"] == 0, result["stderr"]
    finally:
        if server.returncode is None:
            server.kill()
        await communicate_bounded(server, timeout=10)
        socket.unlink(missing_ok=True)
