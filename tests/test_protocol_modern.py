"""P3.1: the contract lane negotiates MCP 2026-07-28 with server/discover (stateless requests) beside the
handshake profiles, and records the negotiated era and version (reference fixture and loopback fakes)."""
import json

import pytest
from aiohttp import web
from mcp_types import PROTOCOL_VERSION_META_KEY, SERVER_INFO_META_KEY

from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import CONTRACT_REVISIONS, Config, Target, interface_profile
from consumer_agent_testing_tool.errors import Unsupported, normalize_error
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.protocol import check_contract

MODERN = "mcp-contract-2026-07-28"
TOOL = {"name": "lookup_record", "description": "Look up one synthetic record by key.",
        "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}}}


def evidence_for(tmp_path, name=MODERN):
    return Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=interface_profile(name),
                    lock={})


def by_id(checks):
    return {check["id"]: check for check in checks}


def rpc(body, result=None, *, error=None, status=200, headers=None):
    payload = {"jsonrpc": "2.0", "id": body["id"], **({"error": error} if error else {"result": result})}
    return web.json_response(payload, status=status, headers=headers or {})


async def serve(handler, log):
    async def post(request):
        body = await request.json()
        log.append({"method": body.get("method"), "version": request.headers.get("mcp-protocol-version")})
        return web.Response(status=202) if "id" not in body else await handler(body)

    async def get(request):
        return web.Response(status=405)

    async def delete(request):
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


def modern_server(*, accept_any_version=False, issue_session=False, anonymous=False):
    """A minimal 2026-07-28 JSON server: every request carries its own envelope; no initialize."""
    async def handle(body):
        version = ((body.get("params") or {}).get("_meta") or {}).get(PROTOCOL_VERSION_META_KEY)
        headers = {"Mcp-Session-Id": "leftover-session"} if issue_session else {}
        stamp = {} if anonymous else {"_meta": {SERVER_INFO_META_KEY: {"name": "modern-fixture", "version": "7.0.0"}}}
        if version != "2026-07-28" and not accept_any_version:
            return rpc(body, error={"code": -32022, "message": "Unsupported protocol version",
                                    "data": {"supported": ["2026-07-28"], "requested": version}}, status=400)
        cache = {"ttlMs": 0, "cacheScope": "private", "resultType": "complete", **stamp}
        if body["method"] == "server/discover":
            return rpc(body, {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, **cache},
                       headers=headers)
        if body["method"] == "tools/list":
            return rpc(body, {"tools": [TOOL], **cache}, headers=headers)
        if body["method"] == "tools/call":
            return rpc(body, {"content": [{"type": "text", "text": "synthetic record"}], "isError": False,
                              "resultType": "complete", **stamp}, headers=headers)
        return rpc(body, error={"code": -32601, "message": "Method not found"}, status=404)
    return handle


def handshake_server(version):
    """Speaks only the initialize handshake: server/discover is an unknown method."""
    async def handle(body):
        if body["method"] == "initialize":
            return rpc(body, {"protocolVersion": version, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "handshake-fixture", "version": "3.1.4"}},
                       headers={"Mcp-Session-Id": "handshake-session"})
        if body["method"] == "tools/list":
            return rpc(body, {"tools": [TOOL]})
        return rpc(body, error={"code": -32601, "message": "Method not found"})
    return handle


def mcp_target(url):
    return Target(kind="mcp", transport="streamable-http", base_url=url, permitted_destinations=[url],
                  source_revision="modern-fixture-7.0.0",
                  contract_cases=[{"tool": "lookup_record", "arguments": {"key": "a"}, "effect": "read"}])


def test_the_modern_revision_is_a_contract_profile_of_its_own_era():
    assert "2026-07-28" in CONTRACT_REVISIONS
    profile = interface_profile(MODERN)
    assert (profile["protocol_era"], profile["negotiation"]) == ("modern", "server/discover")
    assert interface_profile("mcp-contract-2025-06-18")["negotiation"] == "initialize"
    with pytest.raises(Unsupported, match="mcp-contract-2026-07-28"):
        interface_profile("mcp-contract-2099-01-01")


async def test_the_modern_profile_negotiates_with_discover_and_records_the_era(tmp_path):
    evidence = evidence_for(tmp_path)
    checks = await check_contract(Target(), evidence)  # The stdio reference fixture serves both eras.
    assert statuses(checks) == (0, "completed"), checks
    rows = by_id(checks)
    assert rows["negotiation"] == {"id": "negotiation", "status": "passed", "revision": "2026-07-28",
                                   "offered": "2026-07-28"}
    assert rows["unsupported-version-rejection"]["status"] == "passed"
    assert rows["unsupported-version-rejection"]["actual"] == {"code": -32022, "supported": ["2026-07-28"]}
    # No ping at 2026-07-28; stdio cancellation is notifications/cancelled and the fixture honors it.
    assert "ping" not in rows and "session-after-cancellation" not in rows
    assert rows["request-cancellation"]["status"] == rows["requests-after-cancellation"]["status"] == "passed"
    assert rows["protocol-error-separation"]["status"] == rows["abrupt-fixture-disconnect"]["status"] == "passed"
    assert evidence.manifest["protocol_negotiation"] == {
        "offered": "2026-07-28", "profile_revision": "2026-07-28", "method": "server/discover",
        "negotiated": "2026-07-28", "era": "modern", "supported_versions": ["2026-07-28"]}
    lock = evidence.manifest["run_lock"]
    assert (lock["negotiated_protocol_revision"], lock["negotiated_protocol_era"]) == ("2026-07-28", "modern")
    assert lock["server_info"] == {"name": "consumer-agent-testing-tool-reservation-fixture", "version": "1"}
    coverage = evidence.manifest["protocol_coverage"]
    assert (coverage["era"], coverage["negotiation"], coverage["offered_revision"]) == (
        "modern", "server/discover", "2026-07-28")
    assert "ping" in coverage["not_applicable"] and "requests usable after cancellation" in coverage["tested"]
    # Conformance stays pinned at 0.1.16, which has no 2026-07-28 scenarios: an upstream gap, not a pass.
    upstream = rows["upstream-conformance"]
    assert (upstream["status"], upstream["required"], upstream["gap"]) == ("unsupported", False, "upstream")
    assert "0.1.16" in upstream["detail"] and "0.2.0-alpha" in upstream["detail"]
    assert any("2026-07-28" in item for item in coverage["unverified"])
    events = [json.loads(line)["event_type"] for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    assert "protocol_discovered" in events and "protocol_initialized" not in events


async def test_modern_http_exchanges_are_stateless(tmp_path):
    evidence = evidence_for(tmp_path)
    checks = await check_contract(Target(transport="streamable-http"), evidence)
    assert statuses(checks) == (0, "completed"), checks
    rows = by_id(checks)
    assert rows["stateless-http"]["status"] == "passed"
    assert rows["stateless-http"]["actual"]["session_ids_issued"] == 0
    assert "http-session-termination" not in rows and "expired-http-session" not in rows
    # Over HTTP a client cancels by closing the response; the pinned SDK server keeps a JSON-mode handler
    # running, which breaks a SHOULD ("processing SHOULD cease"): a warning, never a silent pass.
    assert rows["request-cancellation"]["status"] == "warning"
    assert "SHOULD" in rows["request-cancellation"]["detail"]
    trace = evidence.manifest["http_transport"]
    assert {record["method"] for record in trace} == {"POST"} and not any(r["session_id_sha256"] for r in trace)
    # Every exchange carries its own version; the rejection probe deliberately stamps an unassigned one.
    assert {record["protocol_version"] for record in trace} == {"2026-07-28", "2099-01-01"}
    assert "initialize" not in {record["mcp_method"] for record in trace}
    assert "HTTP sessions" in evidence.manifest["protocol_coverage"]["not_applicable"]


async def test_handshake_profiles_keep_initialize_against_a_dual_era_server(tmp_path):
    evidence = evidence_for(tmp_path, "mcp-contract-2025-11-25")
    checks = await check_contract(Target(transport="streamable-http"), evidence)
    assert statuses(checks) == (0, "completed"), checks
    assert by_id(checks)["ping"]["status"] == "passed" and "unsupported-version-rejection" not in by_id(checks)
    assert "server/discover" not in {record["mcp_method"] for record in evidence.manifest["http_transport"]}
    negotiation = evidence.manifest["protocol_negotiation"]
    assert (negotiation["era"], negotiation["method"], negotiation["negotiated"]) == (
        "handshake", "initialize", "2025-11-25")
    assert evidence.manifest["run_lock"]["negotiated_protocol_era"] == "handshake"


async def test_a_developer_modern_server_passes_and_may_stay_anonymous(tmp_path):
    log = []
    runner, url = await serve(modern_server(anonymous=True), log)
    evidence = evidence_for(tmp_path)
    try:
        checks = await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    assert statuses(checks) == (0, "completed"), checks
    assert by_id(checks)["call:0:lookup_record"]["status"] == "passed"
    assert evidence.manifest["run_lock"]["server_info"] is None  # serverInfo is an optional stamp at 2026-07-28
    assert log[0] == {"method": "server/discover", "version": "2026-07-28"} and {"method": "server/discover",
                                                                                 "version": "2099-01-01"} in log
    assert "initialize" not in [entry["method"] for entry in log]
    assert "target cancellation and deadline probes" in evidence.manifest["protocol_coverage"]["unverified"]


@pytest.mark.parametrize("variant,check,status,code", [
    ({"accept_any_version": True}, "unsupported-version-rejection", "failed", 1),
    ({"issue_session": True}, "stateless-http", "warning", 0),
])
async def test_modern_server_defects_are_named(tmp_path, variant, check, status, code):
    runner, url = await serve(modern_server(**variant), [])
    evidence = evidence_for(tmp_path)
    try:
        checks = await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    assert by_id(checks)[check]["status"] == status and statuses(checks)[0] == code
    if status == "failed":
        assert by_id(checks)[check]["class"] == "documented contract violation"


async def test_the_modern_profile_names_the_era_a_handshake_server_speaks(tmp_path):
    log = []
    runner, url = await serve(handshake_server("2025-06-18"), log)
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(Exception) as raised:
            await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    error = normalize_error(raised.value)
    assert isinstance(error, Unsupported) and error.code == 3
    assert "select --interface-profile mcp-contract-2025-06-18" in str(error)
    # As the SDK's auto mode does: the discover probe, then the initialize fallback.
    assert [entry["method"] for entry in log][:2] == ["server/discover", "initialize"]
    negotiation = evidence.manifest["protocol_negotiation"]
    assert negotiation["discover_error"]["code"] == -32601 and negotiation["fallback"] == "initialize"
    assert (negotiation["negotiated"], negotiation["era"]) == ("2025-06-18", "handshake")
    assert evidence.manifest["run_lock"]["negotiated_protocol_era"] == "handshake"


async def test_a_refused_counter_offer_over_the_fallback_is_no_negotiated_era(tmp_path):
    # server/discover is unknown, then initialize counter-offers 2026-07-28, which the SDK refuses there.
    runner, url = await serve(handshake_server("2026-07-28"), [])
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(Exception) as raised:
            await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    error = normalize_error(raised.value)
    assert isinstance(error, Unsupported) and "counter-offered '2026-07-28'" in str(error)
    assert "select --interface-profile" not in str(error)  # It already selected the modern profile.
    negotiation = evidence.manifest["protocol_negotiation"]
    assert (negotiation["negotiated"], negotiation["fallback"]) == ("2026-07-28", "initialize")
    assert "era" not in negotiation and "negotiated_protocol_era" not in evidence.manifest["run_lock"]


async def test_a_2026_counter_offer_over_initialize_points_at_the_modern_profile(tmp_path):
    runner, url = await serve(handshake_server("2026-07-28"), [])
    evidence = evidence_for(tmp_path, "mcp-contract-2025-11-25")
    try:
        with pytest.raises(Exception) as raised:
            await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    error = normalize_error(raised.value)
    assert isinstance(error, Unsupported) and "select --interface-profile mcp-contract-2026-07-28" in str(error)
    assert "era" not in evidence.manifest["protocol_negotiation"]  # Nothing was negotiated.


async def test_a_handshake_profile_names_the_profile_a_2026_only_server_needs(tmp_path):
    # The default MCP profile (2025-11-25) meets a server that refuses initialize with -32022.
    log = []
    runner, url = await serve(modern_server(), log)
    evidence = evidence_for(tmp_path, "mcp-contract-2025-11-25")
    try:
        with pytest.raises(Exception) as raised:
            await check_contract(mcp_target(url), evidence)
    finally:
        await runner.cleanup()
    error = normalize_error(raised.value)
    assert isinstance(error, Unsupported) and error.code == 3  # Not a setup failure of the developer's own.
    assert "refused the MCP 2025-11-25 initialize handshake" in str(error) and "it supports 2026-07-28" in str(error)
    assert "select --interface-profile mcp-contract-2026-07-28" in str(error)
    assert [entry["method"] for entry in log] == ["initialize"]  # Nothing else was sent.
    assert evidence.manifest["protocol_negotiation"] == {
        "offered": "2025-11-25", "negotiated": None, "profile_revision": "2025-11-25", "method": "initialize",
        "refusal": {"code": -32022, "supported": ["2026-07-28"]}}
    assert "negotiated_protocol_era" not in evidence.manifest["run_lock"]


async def test_check_and_inspect_select_the_modern_profile_by_flag(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(Config().model_dump_json())
    arguments = ["--config", str(path), "--interface-profile", MODERN, "--output", str(tmp_path / "runs")]
    assert await execute(parser().parse_args(["check", *arguments])) == 0
    assert await execute(parser().parse_args(["inspect", *arguments])) == 0
    manifests = [json.loads(item.read_text()) for item in (tmp_path / "runs").glob("*/manifest.json")]
    assert len(manifests) == 2
    for manifest in manifests:
        assert manifest["run_lock"]["interface_profile"]["protocol_era"] == "modern"
        assert manifest["run_lock"]["negotiated_protocol_era"] == "modern"
        assert manifest["protocol_negotiation"]["method"] == "server/discover"
