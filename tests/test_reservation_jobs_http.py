import asyncio
import json
import signal
import sys
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_reservation_fixture import CLOCK, EXPIRES, proposal
from test_reservation_http import client, control, new_draft
from test_reservation_http import rest as shared_rest
from test_reservation_jobs import READY

from consumer_agent_testing_tool.process import communicate_bounded
from consumer_agent_testing_tool.protocol import child_environment

rest = shared_rest  # Register the shared fixture under its public pytest name.


async def test_rest_pending_job_is_cancelled_through_native_mcp_before_commit(rest):
    await control(rest, "reset", clock=CLOCK)
    async with client(rest) as api:
        row = await new_draft(api)
        args = proposal(row)
        await control(rest, "grant", principal="alice", target_operation="reservations.confirm",
                      arguments=args, decision="approve_once", expires_at=EXPIRES)
        await control(rest, "schedule_fault", target_operation="reservations.confirm", key=args["idempotency_key"],
                      fault="defer", ready_at=READY)
        body = {key: value for key, value in args.items() if key != "id"}
        pending = await api.post(f"/reservations/{row['id']}/confirm", json=body)
        assert pending.status_code == 202 and pending.json()["status"] == "pending"
        assert "grant_id" not in pending.json() and "state" not in pending.json()
        assert (await api.get("/operations/confirm")).json()[0]["status"] == "pending"
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-m", "consumer_agent_testing_tool.fixture_mcp", "--db", rest.fixture.path, "--run-id", rest.fixture.run_id,
            "--transactional"], env=child_environment())
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            response = await session.call_tool("operations_cancel", {
                "operation": "reservations.confirm", "idempotency_key": args["idempotency_key"]})
            assert not response.is_error and response.structured_content["result"]["cancelled"]
        await control(rest, "advance", clock=READY)
        assert (await api.get("/operations/confirm")).json()[0]["status"] == "cancelled"
        repeated = await api.post(f"/reservations/{row['id']}/confirm", json=body)
        assert repeated.status_code == 200 and repeated.json()["status"] == "cancelled"
    assert not any(row["effect"] == "commit" for row in (await control(rest, "snapshot"))["effects"])


async def test_http_cancellation_after_deferred_commit_does_not_claim_an_undo(rest):
    await control(rest, "reset", clock=CLOCK)
    async with client(rest) as api:
        args = proposal(await new_draft(api))
        await control(rest, "grant", principal="alice", target_operation="reservations.confirm",
                      arguments=args, decision="approve_once", expires_at=EXPIRES)
        await control(rest, "schedule_fault", target_operation="reservations.confirm",
                      key=args["idempotency_key"], fault="defer", ready_at=READY)
        await api.post(f"/reservations/{args['id']}/confirm",
                       json={key: value for key, value in args.items() if key != "id"})
        await control(rest, "advance", clock=READY)
        response = await api.post("/operations/confirm/cancel", json={"operation": "reservations.confirm"})
        assert response.status_code == 200
        assert response.json()["status"] == "completed" and not response.json()["cancelled"]
        assert (await api.get(f"/reservations/{args['id']}")).json()["state"] == "confirmed"


async def test_pending_http_operation_survives_service_process_restart(tmp_path):
    socket = Path.cwd() / ".cache" / f"jobs-{uuid.uuid4().hex[:8]}.sock"
    process = None

    async def start():
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "consumer_agent_testing_tool.reservation_http", "--database", str(tmp_path / "state.sqlite"),
            "--control", str(socket), "--port", "0", "--run-id", "restart-job",
            env=child_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, (await process.stderr.read()).decode()
        return json.loads(line)

    async def private(operation, **arguments):
        reader, writer = await asyncio.open_unix_connection(str(socket))
        try:
            writer.write(json.dumps({"operation": operation, **arguments}).encode() + b"\n")
            await writer.drain()
            response = json.loads(await reader.readline())
            assert "error" not in response, response
            return response["result"]
        finally:
            writer.close()
            await writer.wait_closed()

    async def stop_process():
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
        result = await communicate_bounded(process, timeout=10)
        assert result["exit_status"] == 0, result["stderr"]

    def api(packet):
        return httpx.AsyncClient(base_url=packet["origin"], trust_env=False,
            headers={"Authorization": "Bearer " + packet["test_credentials"]["alice"]})

    try:
        packet = await start()
        await private("reset", clock=CLOCK)
        async with api(packet) as http:
            args = proposal(await new_draft(http))
            await private("grant", principal="alice", target_operation="reservations.confirm",
                          arguments=args, decision="approve_once", expires_at=EXPIRES)
            await private("schedule_fault", target_operation="reservations.confirm", key=args["idempotency_key"],
                          fault="defer", ready_at=READY)
            response = await http.post(f"/reservations/{args['id']}/confirm",
                                      json={key: value for key, value in args.items() if key != "id"})
            assert response.status_code == 202
        await stop_process()
        packet = await start()
        async with api(packet) as http:
            assert (await http.get("/operations/confirm")).json()[0]["status"] == "pending"
            await private("advance", clock=READY)
            assert (await http.get("/operations/confirm")).json()[0]["result"]["state"] == "confirmed"
        assert sum(row["effect"] == "commit" for row in (await private("snapshot"))["effects"]) == 1
    finally:
        if process:
            await stop_process()
        socket.unlink(missing_ok=True)
