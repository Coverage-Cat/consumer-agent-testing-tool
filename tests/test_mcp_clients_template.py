"""MuseTemplateClient: the unofficial Muse custom-connector template behavior, against loopback fakes."""
import socket
import time

import pytest
from test_mcp_clients_support import FakeMcp, sdk_app, serve_asgi

from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.linking import (
    ApiKeyAuth,
    ApiKeyCredential,
    LinkError,
    NoCredential,
)
from consumer_agent_testing_tool.mcp_clients import (
    GENERIC_PROFILE,
    MUSE_TEMPLATE_PROFILE,
    TEMPLATE_BASIS,
    TEMPLATE_BEHAVIORS,
    TRANSPORT_SESSION,
    ConnectorClient,
    MuseTemplateClient,
    SdkClient,
    make_client,
)


def template(url, credential=None, **kwargs):
    profile = MUSE_TEMPLATE_PROFILE.model_copy(update=kwargs.pop("profile", {}))
    return MuseTemplateClient(profile, url, credential or NoCredential(), **kwargs)


def kinds(client):
    return [note["kind"] for note in client.observations]


def test_profile_carries_pinned_unofficial_basis():
    assert MUSE_TEMPLATE_PROFILE.basis == TEMPLATE_BASIS
    assert "rallat/muse-skills@3b5ae2c160" in TEMPLATE_BASIS and "sneakers-the-rat/muse-skills@25b2494c" in TEMPLATE_BASIS
    assert MUSE_TEMPLATE_PROFILE.protocol_versions == ["2025-11-25", "2025-06-18", "2025-03-26"]
    assert (MUSE_TEMPLATE_PROFILE.timeout_seconds, MUSE_TEMPLATE_PROFILE.retries) == (45.0, 0)
    assert MUSE_TEMPLATE_PROFILE.post_only and MUSE_TEMPLATE_PROFILE.session_per_call
    assert not MUSE_TEMPLATE_PROFILE.follow_redirects and MUSE_TEMPLATE_PROFILE.require_https_443
    assert {basis for basis, _ in TEMPLATE_BEHAVIORS.values()} == {"unofficial", "assumed"}
    assert all(source for _, source in TEMPLATE_BEHAVIORS.values())
    assert not GENERIC_PROFILE.post_only and "official SDK" in GENERIC_PROFILE.basis


async def test_each_invocation_is_one_post_only_session():
    with FakeMcp() as fake:
        client = template(fake.url)
        assert isinstance(client, ConnectorClient)
        await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("search_rooms", {"text": "December"})
        await client.close()
    assert [tool["name"] for tool in tools] == ["search_rooms", "get_quote", "create_booking"]
    assert result.error is None and result.result["structuredContent"] == {"echo": {"text": "December"}}
    assert client.protocol_version == "2025-11-25" and client.server_info == {"name": "fake-mcp", "version": "7"}
    assert client.capabilities == {"tools": {}} and client.instructions == "Book rooms."
    # Four invocations (connect, one per tools/list page, call), each initialize -> initialized -> request, as
    # the template's list-tools [--cursor] and call-tool commands run (mcp_client.py:142-157); POST only.
    assert {method for method, _ in fake.methods()} == {"POST"}
    assert [name for _, name in fake.methods()] == [
        "initialize", "notifications/initialized",
        "initialize", "notifications/initialized", "tools/list",
        "initialize", "notifications/initialized", "tools/list",
        "initialize", "notifications/initialized", "tools/call"]
    initialize = fake.requests[0]["body"]["params"]
    assert initialize["protocolVersion"] == "2025-11-25" and initialize["capabilities"] == {}
    assert "mcp-protocol-version" not in fake.requests[0]["headers"]
    # The template's wire shape: params {} on notifications/initialized and on a first tools/list page.
    assert fake.requests[1]["body"] == {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    assert fake.requests[4]["body"]["params"] == {} and fake.requests[7]["body"]["params"] == {"cursor": "page-1"}
    # Each session id lives for exactly one invocation; later requests carry the negotiated version.
    sessions = [row["headers"].get("mcp-session-id") for row in fake.requests]
    assert sessions[0] is None and len(set(fake.sessions)) == 4
    assert sessions[2:8] == [None, fake.sessions[1], fake.sessions[1], None, fake.sessions[2], fake.sessions[2]]
    assert fake.calls[0]["session"] == fake.sessions[3]
    assert all(row["headers"]["mcp-protocol-version"] == "2025-11-25" for row in fake.requests
               if row["body"]["method"] != "initialize")
    assert {"loopback-allowance", "session-issued"} <= set(kinds(client))
    session_note = next(note for note in client.observations if note["kind"] == "session-issued")
    assert session_note["count"] == 4 and session_note["basis"] == "unofficial"
    assert "mcp-cli.md:40" in session_note["source"]


@pytest.mark.parametrize("sse", [False, True])
async def test_paging_keeps_raw_entries_over_json_and_event_streams(sse):
    with FakeMcp(sse=sse, pages=[["a"], ["b"], ["c"]]) as fake:
        client = template(fake.url)
        await client.connect()
        tools = await client.list_tools()
    assert [tool["name"] for tool in tools] == ["a", "b", "c"]
    assert all(tool["x-vendor"] == "kept" for tool in tools)  # the full wire entry, unknown fields included
    cursors = [(row["body"].get("params") or {}).get("cursor") for row in fake.requests
               if row["body"]["method"] == "tools/list"]
    assert cursors == [None, "page-1", "page-2"]


async def test_a_cursor_bound_to_its_session_is_a_template_incompatibility():
    # The template's list-tools --cursor runs in a new process and session (mcp_client.py:142-157), so a cursor
    # the server honors only in the session that issued it never reaches page two.
    with FakeMcp(session_cursors=True) as fake:
        client = template(fake.url)
        await client.connect()
        with pytest.raises(Unsupported, match="page 2's cursor was refused in a new session") as raised:
            await client.list_tools()
    assert "Invalid cursor" in str(raised.value)
    listed = [row for row in fake.requests if row["body"]["method"] == "tools/list"]
    assert [row["body"]["params"] for row in listed] == [{}, {"cursor": f"{fake.sessions[1]}:page-1"}]
    assert [row["headers"]["mcp-session-id"] for row in listed] == fake.sessions[1:3]
    note = next(note for note in client.observations if note["kind"] == "paging-needs-session")
    assert note["page"] == 2 and note["basis"] == "unofficial" and note["source"].startswith("mcp_client.py:142-143")
    # One session for every page (a persistent variant, like the official SDK's client) pages it fine.
    with FakeMcp(session_cursors=True) as fake:
        client = template(fake.url, profile={"session_per_call": False})
        await client.connect()
        assert [tool["name"] for tool in await client.list_tools()] == ["search_rooms", "get_quote", "create_booking"]
    assert "paging-needs-session" not in kinds(client)


@pytest.mark.parametrize("status", [200, 204])
async def test_a_notification_answered_other_than_202_is_a_template_incompatibility(status):
    # mcp_client.py:73-76: only 202 Accepted returns; any other answer to notifications/initialized fails the
    # invocation ("MCP server did not accept the initialization notification"), where other clients go on.
    with FakeMcp(notification_status=status) as fake:
        client = template(fake.url)
        with pytest.raises(Unsupported, match=f"HTTP {status}; the unofficial Muse template accepts only 202"):
            await client.connect()
    assert [name for _, name in fake.methods()] == ["initialize", "notifications/initialized"]
    note = next(note for note in client.observations if note["kind"] == "notification-refused")
    assert (note["status"], note["method"], note["basis"]) == (status, "notifications/initialized", "unofficial")
    assert "mcp_client.py:136" in note["source"] and ":73-76" in note["source"]
    with FakeMcp(notification_status=status) as fake:
        async with SdkClient(GENERIC_PROFILE, fake.url, NoCredential()) as baseline:
            assert len(await baseline.list_tools()) == 3  # The official SDK client accepts the reply.


def test_template_behaviors_cite_the_pinned_lines_they_reproduce():
    assert TEMPLATE_BEHAVIORS["initialized-notification"][0] == "unofficial"
    assert TEMPLATE_BEHAVIORS["pagination"][0] == "unofficial"
    assert TEMPLATE_BEHAVIORS["session-per-invocation"][0] == "unofficial"
    assert not any("not shown" in TEMPLATE_BEHAVIORS[name][1]
                   for name in ("initialized-notification", "pagination", "session-per-invocation"))
    # clientInfo is shown (mcp_client.py:124): the harness name is a recorded deviation; only the User-Agent is not.
    basis, source = TEMPLATE_BEHAVIORS["client-identity"]
    assert basis == "assumed" and "deliberate deviation" in source and "mcp_client.py:124" in source
    assert "muse-custom-connector" in source and "clientInfo and User-Agent are not shown" not in source


@pytest.mark.parametrize("options, message", [
    ({"cursor_loop": True}, "repeated a cursor"), ({"duplicate": True}, "duplicate tool names")])
async def test_broken_catalogs_fail_as_connector_defects(options, message):
    with FakeMcp(**options) as fake:
        client = template(fake.url)
        await client.connect()
        with pytest.raises(LabError, match=message) as raised:
            await client.list_tools()
    assert raised.value.code == 1 and raised.value.category == "tool/API"


async def test_counter_offer_inside_the_template_set_is_accepted():
    with FakeMcp(version="2025-06-18") as fake:
        client = template(fake.url)
        await client.connect()
        assert (await client.call_tool("search_rooms", {})).error is None
    assert client.protocol_version == "2025-06-18"
    assert fake.requests[-1]["headers"]["mcp-protocol-version"] == "2025-06-18"


@pytest.mark.parametrize("options", [
    {"version": "2024-11-05"}, {"version": "2026-07-28"},
    {"initialize_error": {"code": -32602, "message": "Unsupported protocol version",
                          "data": {"supported": ["2026-07-28"]}}}])
async def test_versions_outside_the_template_set_are_unsupported(options):
    with FakeMcp(**options) as fake:
        client = template(fake.url)
        with pytest.raises(Unsupported, match="template"):
            await client.connect()
    note = next(note for note in client.observations if note["kind"] == "unsupported-version")
    assert note["offered"] == "2025-11-25" and note["basis"] == "unofficial"
    assert "notifications/initialized" not in [name for _, name in fake.methods()]


async def test_the_tools_capability_is_required():
    with FakeMcp(capabilities={"resources": {}}) as fake:
        client = template(fake.url)
        with pytest.raises(Unsupported, match="tools capability"):
            await client.connect()
    assert "missing-tools-capability" in kinds(client)


async def test_redirects_are_refused_and_recorded():
    with FakeMcp(redirect="http://127.0.0.1:9/elsewhere?token=leak#frag") as fake:
        client = template(fake.url)
        with pytest.raises(Unsupported, match="follows no redirects"):
            await client.connect()
    assert fake.methods() == [("POST", "initialize")]  # the redirect target was never contacted
    note = next(note for note in client.observations if note["kind"] == "redirect-refused")
    assert note["status"] == 307 and note["location"] == "http://127.0.0.1:9/elsewhere"
    assert note["source"].startswith("mcp_client.py:26-28")


async def test_server_requests_in_event_streams_are_recorded_and_left_unanswered():
    with FakeMcp(sse=True) as fake:
        client = template(fake.url)
        await client.connect()
        result = await client.call_tool("chatty", {"text": "hi"})
    assert result.error is None and result.result["structuredContent"] == {"echo": {"text": "hi"}}
    request = next(note for note in client.observations if note["kind"] == "server-request")
    assert request["method"] == "sampling/createMessage" and request["basis"] == "assumed"
    assert {"kind": "server-notification", "method": "notifications/message", "count": 1} in client.observations
    # No POST ever answered the server's request.
    assert all("result" not in (row["body"] or {}) and "error" not in (row["body"] or {}) for row in fake.requests)


async def test_a_call_without_a_response_is_uncertain_and_never_retried():
    with FakeMcp(delay=1.0) as fake:
        client = template(fake.url, profile={"timeout_seconds": 0.3})
        await client.connect()
        result = await client.call_tool("slow", {"text": "book it"})
        calls = list(fake.calls)
    assert result.uncertain and result.result is None
    assert result.error["category"] == "timeout" and "not retried" in result.error["message"]
    assert [call["name"] for call in calls] == ["slow"]


async def test_a_dropped_response_or_upstream_failure_is_uncertain():
    with FakeMcp() as fake:
        client = template(fake.url)
        await client.connect()
        dropped = await client.call_tool("drop", {})
        failed = await client.call_tool("server_error", {})
    assert dropped.uncertain and dropped.error["category"] == "transport"
    assert failed.uncertain and failed.error == {"code": None, "category": "http", "status": 503,
                                                 "message": "the server answered tools/call with HTTP 503"}
    assert [call["name"] for call in fake.calls] == ["drop", "server_error"]


async def test_a_call_that_never_left_is_certain():
    fake = FakeMcp()
    client = template(fake.url)
    await client.connect()
    fake.close()
    result = await client.call_tool("create_booking", {})
    assert not result.uncertain and result.error["category"] == "transport"
    assert fake.calls == []


async def test_jsonrpc_errors_and_tool_errors_are_answers():
    with FakeMcp() as fake:
        client = template(fake.url)
        await client.connect()
        rejected = await client.call_tool("fail", {})
        tool_error = await client.call_tool("is_error", {"text": "x"})
    assert rejected.error == {"code": -32602, "message": "Invalid arguments", "category": "protocol"}
    assert not rejected.uncertain
    assert tool_error.error is None and tool_error.result["isError"] is True


async def test_servers_needing_a_session_from_an_earlier_call_are_flagged():
    with FakeMcp(needs_prior_session=True) as fake:
        client = template(fake.url)
        await client.connect()
        await client.list_tools()
        result = await client.call_tool("book", {})
    assert result.error["category"] == "protocol" and not result.uncertain
    note = next(note for note in client.observations if note["kind"] == "needs-session")
    assert note["status"] == 400 and note["sent_session"] is True


async def test_tool_errors_about_an_earlier_session_are_flagged():
    with FakeMcp() as fake:
        client = template(fake.url)
        await client.connect()
        bound = await client.call_tool("session_bound", {})
        plain = await client.call_tool("is_error", {"text": "sold out"})
        domain = await client.call_tool("domain_session", {})
    assert bound.result["isError"] and plain.result["isError"] and domain.result["isError"]
    # Tool text is a labeled hint, never protocol evidence: only a text that names the MCP session counts,
    # so a yoga studio's "That session is full." flags nothing.
    assert [note for note in client.observations if note["kind"] == "needs-session"] == []
    flagged = [note for note in client.observations if note["kind"] == "session-hint"]
    assert flagged == [{"kind": "session-hint", "tool": "session_bound", "signal": "a tool error names the MCP session",
                        "basis": "assumed", "source": TEMPLATE_BEHAVIORS["session-hint"][1], "count": 1}]


async def test_a_persistent_session_variant_reuses_one_handshake():
    with FakeMcp() as fake:
        client = template(fake.url, profile={"session_per_call": False})
        await client.connect()
        await client.list_tools()
        await client.call_tool("search_rooms", {})
        await client.close()
    assert [name for _, name in fake.methods()].count("initialize") == 1
    assert len({call["session"] for call in fake.calls}) == 1


async def test_the_allowlist_refuses_before_any_traffic():
    with FakeMcp() as fake:
        client = template(fake.url, allowed_tools={"search_rooms"})
        await client.connect()
        before = len(fake.requests)
        refused = await client.call_tool("create_booking", {"quote_id": "q1"})
        assert len(fake.requests) == before
    assert refused.error["category"] == "allowlist" and not refused.uncertain and refused.elapsed_ms == 0
    assert next(note for note in client.observations if note["kind"] == "allowlist-refused")["tool"] == "create_booking"


class CountingCredential(NoCredential):
    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1
        return False


@pytest.mark.parametrize("forbid, kind", [("insufficient_scope", "insufficient-scope"), ("access_denied", "rejected")])
async def test_a_403_is_a_refusal_and_never_triggers_a_refresh(forbid, kind):
    credential = CountingCredential()
    with FakeMcp(forbid=forbid) as fake:
        client = template(fake.url, credential)
        with pytest.raises(LinkError) as raised:
            await client.connect()
    assert raised.value.kind == kind and credential.refreshes == 0
    assert fake.methods() == [("POST", "initialize")]


async def test_an_unauthenticated_connect_is_a_link_error():
    with FakeMcp(require=lambda handler: False) as fake:
        client = template(fake.url)
        with pytest.raises(LinkError) as raised:
            await client.connect()
    # No credential was configured, so none was "rejected": the server requires one.
    assert raised.value.kind == "required" and raised.value.category == "authentication"
    assert "requires authorization (HTTP 401)" in str(raised.value) and "rejected" not in str(raised.value)
    with FakeMcp(require=lambda handler: False) as fake:
        keyed = template(fake.url, ApiKeyCredential(ApiKeyAuth(mode="api_key", env="CATT_KEY"), fake.url, "k" * 16))
        with pytest.raises(LinkError) as raised:
            await keyed.connect()
    assert raised.value.kind == "rejected" and "rejected the credential" in str(raised.value)


@pytest.mark.parametrize("url, error, message", [
    ("https://mcp.example.test:8443/mcp", Unsupported, "port 443"),
    ("https://mcp.example.test/mcp?tenant=1", Unsupported, "query"),
    ("https://user:pw@mcp.example.test/mcp", Unsupported, "user info"),
    ("https://mcp.example.test/mcp#top", Unsupported, "fragment"),
    ("http://mcp.example.test/mcp", LabError, "HTTPS"),
    ("ftp://mcp.example.test/mcp", LabError, "absolute HTTP"),
])
def test_template_endpoint_rules_refuse_before_any_traffic(url, error, message):
    with pytest.raises(error, match=message):
        template(url)


def test_the_loopback_allowance_is_explicit_and_recorded():
    client = template("http://127.0.0.1:8123/mcp")
    note = client.observations[0]
    assert note["kind"] == "loopback-allowance" and note["basis"] == "assumed"
    assert template("https://mcp.example.test/mcp").observations == []
    with pytest.raises(Unsupported, match="allowance is disabled"):
        template("http://127.0.0.1:8123/mcp", allow_loopback=False)


@pytest.mark.parametrize("placement, name", [("query_param", "api_key"), ("url_path_segment", None)])
def test_the_template_sends_credentials_in_headers_only(placement, name):
    credential = ApiKeyCredential(ApiKeyAuth(mode="api_key", env="CATT_KEY", placement=placement, name=name),
                                  "http://127.0.0.1:8123/mcp", "key-value-123456")
    with pytest.raises(Unsupported, match="header only"):
        template("http://127.0.0.1:8123/mcp", credential)


def test_profiles_must_match_their_implementation():
    with pytest.raises(LabError, match="retries"):
        template("https://mcp.example.test/mcp", profile={"retries": 1})
    with pytest.raises(LabError, match="not implemented by MuseTemplateClient"):
        MuseTemplateClient(GENERIC_PROFILE, "https://mcp.example.test/mcp", NoCredential())
    assert isinstance(make_client(MUSE_TEMPLATE_PROFILE, "https://mcp.example.test/mcp", NoCredential()),
                      MuseTemplateClient)
    assert isinstance(make_client(GENERIC_PROFILE, "https://mcp.example.test/mcp", NoCredential()), SdkClient)


async def test_using_a_client_before_connect_is_an_error():
    with pytest.raises(LabError, match="Connect"):
        await template("https://mcp.example.test/mcp").list_tools()


@pytest.mark.parametrize("json_response", [False, True])
async def test_the_template_interoperates_with_the_official_sdk_server(json_response):
    with serve_asgi(lambda base: sdk_app(base, json_response=json_response)) as base:
        async with template(f"{base}/mcp") as client:
            tools = await client.list_tools()
            result = await client.call_tool("search_rooms", {"text": "x"})
    assert client.protocol_version == "2025-11-25" and client.server_info["name"] == "catt-sdk-fixture"
    assert [tool["name"] for tool in tools] == ["search_rooms", "get_quote", "create_booking"]
    assert result.result["structuredContent"] == {"echo": {"text": "x"}, "tool": "search_rooms"}


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_an_unreachable_endpoint_is_a_setup_error():
    client = template(f"http://127.0.0.1:{unused_port()}/mcp")
    with pytest.raises(LabError) as raised:
        await client.connect()
    assert raised.value.code == 2 and raised.value.category == "setup/infrastructure"


async def test_the_wire_arguments_are_reported_with_every_sent_call():
    with FakeMcp() as fake:
        client = template(fake.url, allowed_tools={"search_rooms", "fail", "slow"})
        await client.connect()
        sent = await client.call_tool("search_rooms", {"text": "garden", "adults": 2})
        rejected = await client.call_tool("fail", {"text": "x"})
        refused = await client.call_tool("create_booking", {"quote_id": "q1"})
    # Parsed back from the bytes posted, so a caller can compare them with what was approved.
    assert sent.sent_arguments == {"text": "garden", "adults": 2} == fake.calls[0]["arguments"]
    assert rejected.sent_arguments == {"text": "x"} and refused.sent_arguments is None
    # The tool name too: a client that sent another tool than the approved one is caught by its name.
    assert sent.sent_name == "search_rooms" == fake.calls[0]["name"] and rejected.sent_name == "fail"
    assert refused.sent_name is None and {"sent_arguments", "sent_name"} <= refused.model_fields_set


async def test_a_dripping_reply_cannot_outlast_the_invocation_deadline():
    with FakeMcp(drip_seconds=0.25) as fake:
        client = template(fake.url, profile={"timeout_seconds": 1.0})
        await client.connect()
        started = time.monotonic()
        result = await client.call_tool("drip", {})
        elapsed = time.monotonic() - started
    # Every byte arrives within the per-read timeout; the whole invocation still ends at the deadline.
    assert elapsed < 2.5 and result.uncertain and result.error["category"] == "timeout"
    note = next(note for note in client.observations if note["kind"] == "invocation-deadline")
    assert note["basis"] == "assumed" and note["stage"] == "tools/call"
    assert TEMPLATE_BEHAVIORS["invocation-deadline"][0] == "assumed"


class RefreshingCredential(NoCredential):
    """A credential the server accepts only after one host-side refresh."""

    def __init__(self):
        self.token = "old-token-123456"

    def apply(self, url, headers):
        return url, {**headers, "Authorization": f"Bearer {self.token}"}

    async def refresh(self):
        self.token = "new-token-654321"
        return True

    def describe(self):
        return {"mode": "oauth", "placement": "bearer_header"}


async def test_a_host_refresh_after_a_401_is_labeled_as_not_template_behavior():
    with FakeMcp(require=lambda handler: handler.headers.get("authorization") == "Bearer new-token-654321") as fake:
        client = template(fake.url, RefreshingCredential())
        await client.connect()
    note = next(note for note in client.observations if note["kind"] == "credential-refreshed")
    assert note == {"kind": "credential-refreshed", "status": 401, "basis": "assumed",
                    "source": TEMPLATE_BEHAVIORS["host-credential-refresh"][1], "count": 1}
    assert "retries nothing" in note["source"]


async def test_a_stateless_server_s_domain_session_error_over_http_400_is_no_session_requirement():
    # The server never issues Mcp-Session-Id; its yoga "session" error comes as a JSON-RPC error with HTTP 400.
    with FakeMcp(session=False) as fake:
        client = template(fake.url)
        await client.connect()
        result = await client.call_tool("domain_session_400", {})
    assert result.error == {"code": -32602, "message": "Invalid params: session s-100 is full",
                            "category": "protocol", "status": 400}
    assert "session-issued" not in kinds(client) and "needs-session" not in kinds(client)


@pytest.mark.parametrize("message, transport", [
    ("Session not found", True), ("Bad Request: Missing session ID", True),
    ("Not Found: Invalid or expired session ID", True), ("Bad Request: Mcp-Session-Id header is required", True),
    ("Bad Request: No valid session ID provided", True), ("Invalid params: session s-100 is full", False),
    ("Unknown session id s-100", False), ("session s-100 not found", False), ("That session is full.", False),
])
def test_only_the_transport_s_own_messages_reject_a_session_without_one_sent(message, transport):
    # Without a session the client sent, only a whole transport rejection message is protocol evidence.
    assert bool(TRANSPORT_SESSION.fullmatch(message.lower())) is transport
