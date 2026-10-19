"""Contract lane accepts profile-selected 2025-06-18 and 2025-03-26 servers (F10)."""
import json
import uuid

import pytest
from aiohttp import web

from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Config, Target, interface_profile
from consumer_agent_testing_tool.errors import Unsupported, normalize_error
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.protocol import check_contract


async def legacy_server(version, log):
    """A minimal Streamable HTTP JSON server that answers initialize with one fixed revision."""
    sessions = set()

    async def post(request):
        body = await request.json()
        log.append({"method": body.get("method"), "version": request.headers.get("mcp-protocol-version")})
        if "id" not in body:
            return web.Response(status=202)
        method, headers = body["method"], {}
        if method == "initialize":
            session = uuid.uuid4().hex
            sessions.add(session)
            headers["Mcp-Session-Id"] = session
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "legacy-fixture", "version": "3.1.4"}}
        elif request.headers.get("mcp-session-id") not in sessions:
            return web.json_response({"jsonrpc": "2.0", "id": body["id"],
                                      "error": {"code": -32600, "message": "Unknown session"}}, status=404)
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup_record", "description": "Look up one synthetic record by key.",
                                 "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "synthetic record"}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            return web.json_response({"jsonrpc": "2.0", "id": body["id"],
                                      "error": {"code": -32601, "message": "Method not found"}})
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=headers)

    async def get(request):
        return web.Response(status=405)

    async def delete(request):
        sessions.discard(request.headers.get("mcp-session-id"))
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/mcp", post)
    app.router.add_get("/mcp", get)
    app.router.add_delete("/mcp", delete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp"


def mcp_target(url):
    return Target(kind="mcp", transport="streamable-http", base_url=url, permitted_destinations=[url],
                  source_revision="legacy-fixture-3.1.4",
                  contract_cases=[{"tool": "lookup_record", "arguments": {"key": "a"}, "effect": "read"}])


def evidence_for(tmp_path, name):
    return Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                    profile=interface_profile(name), lock={})


async def test_profile_selected_2025_06_18_server_passes_and_records_the_negotiation(tmp_path):
    log = []
    runner, url = await legacy_server("2025-06-18", log)
    evidence = evidence_for(tmp_path, "mcp-contract-2025-06-18")
    try:
        checks = await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    assert statuses(checks) == (0, "completed"), checks
    negotiation = next(check for check in checks if check["id"] == "negotiation")
    assert negotiation == {"id": "negotiation", "status": "passed", "revision": "2025-06-18", "offered": "2025-11-25"}
    assert evidence.manifest["protocol_negotiation"]["negotiated"] == "2025-06-18"
    assert evidence.manifest["run_lock"]["negotiated_protocol_revision"] == "2025-06-18"
    assert evidence.manifest["run_lock"]["server_info"] == {"name": "legacy-fixture", "version": "3.1.4"}
    coverage = evidence.manifest["protocol_coverage"]
    assert (coverage["revision"], coverage["negotiated_revision"]) == ("2025-06-18", "2025-06-18")
    upstream = [check for check in checks if check["id"].startswith("upstream")]
    # The pinned suite defines its server scenarios for 2025-06-18; they run under that label.
    assert upstream and all(check.get("revision") == "2025-06-18" for check in upstream)
    assert all(entry["version"] == "2025-06-18" for entry in log if entry["method"] != "initialize")


async def test_2025_03_26_server_runs_direct_checks_and_names_the_conformance_gap(tmp_path):
    runner, url = await legacy_server("2025-03-26", [])
    evidence = evidence_for(tmp_path, "mcp-contract-2025-03-26")
    try:
        checks = await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    assert statuses(checks) == (0, "completed"), checks
    upstream = next(check for check in checks if check["id"] == "upstream-conformance")
    assert upstream["status"] == "unsupported" and upstream["required"] is False
    assert "2025-03-26" in upstream["detail"]
    assert any("upstream conformance" in item for item in evidence.manifest["protocol_coverage"]["unverified"])
    assert any(check["id"] == "call:0:lookup_record" and check["status"] == "passed" for check in checks)


@pytest.mark.parametrize("negotiated,profile,hint", [
    ("2025-06-18", "mcp-contract-2025-11-25", "select --interface-profile mcp-contract-2025-06-18"),
    ("2025-11-25", "mcp-contract-2025-06-18", "select --interface-profile mcp-contract-2025-11-25"),
    ("2024-11-05", "mcp-contract-2025-11-25", "no contract profile covers it"),
])
async def test_unaccepted_negotiations_fail_clearly_and_are_recorded(tmp_path, negotiated, profile, hint):
    runner, url = await legacy_server(negotiated, [])
    evidence = evidence_for(tmp_path, profile)
    try:
        with pytest.raises(Exception) as raised:
            await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    # The SDK task group wraps the refusal; the CLI unwraps it the same way.
    error = normalize_error(raised.value)
    assert isinstance(error, Unsupported) and hint in str(error) and error.code == 3
    assert evidence.manifest["protocol_negotiation"]["negotiated"] == negotiated


async def test_unimplemented_and_unknown_revisions_stay_explicit(tmp_path):
    with pytest.raises(Unsupported, match="mcp-contract-2025-06-18"):
        interface_profile("mcp-contract-2024-11-05")
    # P3.1 implements 2026-07-28 with server/discover (tests/test_protocol_modern.py runs it). A revision no
    # contract profile covers still stops at the lane, before any connection.
    assert interface_profile("mcp-contract-2026-07-28")["negotiation"] == "server/discover"
    profile = {**interface_profile("mcp-contract-2025-11-25"), "protocol_revision": "2024-11-05"}
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=profile, lock={})
    with pytest.raises(Unsupported, match="2024-11-05 negotiation is not implemented"):
        await check_contract(Target(), evidence)


async def test_inspect_defaults_to_the_mcp_contract_profile_for_mcp_targets(tmp_path):
    runner, url = await legacy_server("2025-11-25", [])
    config = Config(targets={"legacy": mcp_target(url)})
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    try:
        code = await execute(parser().parse_args(["inspect", "legacy", "--config", str(path),
                                                  "--output", str(tmp_path / "runs")]))
    finally:
        await runner.cleanup()
    assert code == 0
    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["run_lock"]["interface_profile"]["name"] == "mcp-contract-2025-11-25"
    assert manifest["run_lock"]["negotiated_protocol_revision"] == "2025-11-25"


@pytest.mark.parametrize("negotiated", ["2099-01-01", "2026-07-28"])
async def test_revisions_the_sdk_refuses_fail_clearly_and_are_recorded(tmp_path, negotiated):
    runner, url = await legacy_server(negotiated, [])
    config = Config(targets={"legacy": mcp_target(url)})
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    try:
        with pytest.raises(Exception) as raised:
            await execute(parser().parse_args(["check", "legacy", "--config", str(path),
                                               "--output", str(tmp_path / "runs")]))
    finally:
        await runner.cleanup()
    error = normalize_error(raised.value)
    assert isinstance(error, Unsupported) and error.code == 3
    assert f"Server negotiated MCP '{negotiated}'" in str(error) and "2025-06-18" in str(error)
    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["protocol_negotiation"] == {
        "offered": "2025-11-25", "negotiated": negotiated, "profile_revision": "2025-11-25"}
    assert manifest["checks"][0]["class"] == "unsupported" and negotiated in manifest["checks"][0]["detail"]
