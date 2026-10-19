"""SdkClient: the official MCP Python SDK baseline, against the SDK's own server and loopback fakes."""
import pytest
from test_mcp_clients_support import FakeMcp, sdk_app, serve_asgi

from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.linking import NoCredential
from consumer_agent_testing_tool.mcp_clients import GENERIC_PROFILE, McpCallResult, SdkClient


def sdk(url, **profile):
    return SdkClient(GENERIC_PROFILE.model_copy(update=profile), url, NoCredential())


def notes(client, kind):
    return [note for note in client.observations if note["kind"] == kind]


async def test_the_generic_profile_negotiates_the_modern_era():
    with serve_asgi(sdk_app) as base:
        async with sdk(f"{base}/mcp") as client:
            tools = await client.list_tools()
            quote = await client.call_tool("get_quote", {"text": "x"})
            echo = await client.call_tool("search_rooms", {"text": "x"})
    assert client.protocol_version == "2026-07-28" and client.server_info["name"] == "catt-sdk-fixture"
    assert "tools" in client.capabilities
    assert [tool["name"] for tool in tools] == ["search_rooms", "get_quote", "create_booking"]
    assert tools[1]["outputSchema"]["required"] == ["total_minor"]
    assert echo.error is None and echo.result["structuredContent"] == {"echo": {"text": "x"}, "tool": "search_rooms"}
    # A structuredContent that fails its output schema is kept and recorded, not discarded.
    assert quote.result["structuredContent"] == {"total_minor": "forty"}
    assert notes(client, "output-schema-mismatch")[0]["tool"] == "get_quote"
    assert notes(client, "negotiated")[0] == {"kind": "negotiated", "version": "2026-07-28", "era": "modern", "count": 1}


async def test_a_handshake_only_profile_keeps_one_session_with_stream_and_delete():
    with serve_asgi(sdk_app) as base:
        client = sdk(f"{base}/mcp", protocol_versions=["2025-11-25", "2025-06-18"])
        await client.connect()
        assert len(await client.list_tools()) == 3
        assert (await client.call_tool("search_rooms", {})).error is None
        await client.close()
    assert client.protocol_version == "2025-11-25"
    assert {"session-issued", "get-stream-opened", "session-delete-sent"} <= {note["kind"] for note in client.observations}


async def test_legacy_servers_fall_back_to_initialize():
    with FakeMcp(version="2025-06-18") as fake:
        async with sdk(fake.url) as client:
            result = await client.call_tool("search_rooms", {"text": "x"})
    # JSON-RPC methods only: the SDK's GET stream (no method) may arrive before the notification.
    methods = [name for _, name in fake.methods() if name]
    assert methods[:3] == ["server/discover", "initialize", "notifications/initialized"]
    assert client.protocol_version == "2025-06-18" and result.result["structuredContent"] == {"echo": {"text": "x"}}


async def test_a_negotiated_version_outside_the_profile_is_unsupported():
    with serve_asgi(sdk_app) as base:
        client = sdk(f"{base}/mcp", protocol_versions=["2025-03-26"])
        with pytest.raises(Unsupported, match="outside client profile"):
            await client.connect()
    assert notes(client, "unsupported-version")[0]["negotiated"] == "2025-11-25"


async def test_a_call_without_a_response_is_uncertain():
    with serve_asgi(sdk_app) as base:
        async with sdk(f"{base}/mcp", timeout_seconds=2) as client:
            result = await client.call_tool("slow", {"seconds": 10})
            after = await client.call_tool("search_rooms", {})
    assert isinstance(result, McpCallResult) and result.uncertain and result.error["category"] == "timeout"
    assert after.error is None  # the session survives a timed-out call


async def test_server_answers_are_certain_and_upstream_failures_uncertain():
    with FakeMcp() as fake:
        async with sdk(fake.url) as client:
            rejected = await client.call_tool("fail", {})
            failed = await client.call_tool("server_error", {})
    assert rejected.error == {"code": -32602, "message": "Invalid arguments", "category": "protocol"}
    assert not rejected.uncertain
    assert failed.uncertain and failed.error["category"] == "http" and failed.error["status"] == 503


async def test_a_dead_session_reports_instead_of_raising():
    fake = FakeMcp()
    client = sdk(fake.url)
    await client.connect()
    fake.close()
    result = await client.call_tool("create_booking", {})
    again = await client.call_tool("create_booking", {})
    await client.close()
    # Nothing left the client, so nothing is uncertain.
    for outcome in (result, again):
        assert outcome.error["category"] == "transport" and not outcome.uncertain
        assert outcome.error["message"].endswith("the request was not sent")
    assert fake.calls == []


async def test_a_dropped_response_is_uncertain():
    with FakeMcp() as fake:
        async with sdk(fake.url) as client:
            dropped = await client.call_tool("drop", {})
    assert dropped.uncertain and dropped.error["category"] == "transport"
    assert [call["name"] for call in fake.calls] == ["drop"]


async def test_per_call_sessions_are_supported_for_the_sdk_too():
    with serve_asgi(sdk_app) as base:
        async with sdk(f"{base}/mcp", session_per_call=True) as client:
            assert len(await client.list_tools()) == 3
            assert (await client.call_tool("search_rooms", {})).error is None
    assert notes(client, "negotiated")[0]["count"] == 3  # connect, list and call each negotiated


@pytest.mark.parametrize("update, field", [({"post_only": True}, "post_only"), ({"retries": 2}, "retries"),
                                           ({"follow_redirects": False}, "follow_redirects")])
def test_sdk_profiles_cannot_promise_what_the_sdk_does_not_do(update, field):
    with pytest.raises(LabError, match=field):
        sdk("https://mcp.example.test/mcp", **update)


def test_generic_endpoint_rules():
    assert sdk("https://mcp.example.test:8443/mcp?tenant=a").observations == []
    with pytest.raises(LabError, match="HTTPS"):
        sdk("http://mcp.example.test/mcp")
    with pytest.raises(LabError, match="user info"):
        sdk("https://user@mcp.example.test/mcp")


async def test_the_allowlist_refuses_before_any_traffic():
    with FakeMcp() as fake:
        async with SdkClient(GENERIC_PROFILE, fake.url, NoCredential(), allowed_tools=["search_rooms"]) as client:
            before = len(fake.requests)
            refused = await client.call_tool("create_booking", {})
            assert len(fake.requests) == before
    assert refused.error["category"] == "allowlist"


async def test_the_wire_arguments_are_reported_with_every_sent_call():
    with FakeMcp() as fake:
        async with SdkClient(GENERIC_PROFILE, fake.url, NoCredential(), allowed_tools=["search_rooms", "fail"]) \
                as client:
            sent = await client.call_tool("search_rooms", {"text": "garden", "adults": 2})
            rejected = await client.call_tool("fail", {"text": "x"})
            refused = await client.call_tool("create_booking", {})
    # Captured from the serialized tools/call request, so a caller can compare it with the approval.
    assert sent.sent_arguments == {"text": "garden", "adults": 2} == fake.calls[0]["arguments"]
    assert rejected.sent_arguments == {"text": "x"} and refused.sent_arguments is None
    # The tool name too: a client that sent another tool than the approved one is caught by its name.
    assert sent.sent_name == "search_rooms" == fake.calls[0]["name"] and rejected.sent_name == "fail"
    assert refused.sent_name is None and {"sent_arguments", "sent_name"} <= refused.model_fields_set


@pytest.mark.parametrize("tool, code", [("server_error_32000", -32000), ("server_error_32001", -32001)])
async def test_server_errors_with_the_sdks_internal_codes_are_answers_not_lost_responses(tool, code):
    with FakeMcp() as fake:
        async with sdk(fake.url) as client:
            declined = await client.call_tool(tool, {"amount": 5})
    # The server answered: nothing is uncertain, and nobody is told "no response".
    assert declined.error == {"code": code, "message": "Payment declined: insufficient funds", "category": "protocol"}
    assert not declined.uncertain


async def test_server_error_messages_are_bounded():
    with FakeMcp() as fake:
        async with sdk(fake.url) as client:
            failed = await client.call_tool("long_error", {})
    assert failed.error["category"] == "protocol" and len(failed.error["message"]) == 500


async def test_oversized_responses_are_refused_like_the_templates(monkeypatch):
    from consumer_agent_testing_tool import mcp_clients
    monkeypatch.setattr(mcp_clients, "RESPONSE_LIMIT", 64 * 1024)
    with FakeMcp(big_bytes=256 * 1024) as fake:
        async with sdk(fake.url, timeout_seconds=3) as client:
            big = await client.call_tool("big", {})
            after = await client.call_tool("search_rooms", {})
    assert big.result is None and big.error["category"] == "protocol" and "65536 bytes" in big.error["message"]
    assert big.uncertain  # The request reached the server; its answer was not read.
    # The session ends with the refused read: later calls are refused before anything is sent.
    assert after.error["category"] == "transport" and not after.uncertain and after.sent_arguments is None
