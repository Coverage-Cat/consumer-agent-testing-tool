"""Connector fixture: catalog, tools, money and the private oracle (loopback only)."""
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from consumer_agent_testing_tool.connector_fixture import (
    DESCRIPTIONS,
    VARIANT_BASIS,
    ConnectorFixture,
    FixtureConfig,
    run_fixture,
    serve_fixture,
)
from consumer_agent_testing_tool.http_streams import StreamingTransport
from consumer_agent_testing_tool.lint import usability_checks

STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
GUEST = {"guest_name": "Taylor Example", "guest_email": "taylor@example.com"}
JSON_RPC = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
GOOD_TOOLS = ["search_rooms", "get_quote", "create_booking", "get_booking", "cancel_booking"]


@asynccontextmanager
async def mcp_session(url, *, headers=None, auth=None):
    """The official SDK client: one persistent session (generic-sdk behavior)."""
    async with (httpx2.AsyncClient(headers=headers, auth=auth, trust_env=False,
                                   transport=StreamingTransport(trust_env=False),
                                   timeout=20) as http,
                streamable_http_client(url, http_client=http) as streams,
                ClientSession(streams[0], streams[1]) as session):
        session.init_result = await session.initialize()
        yield session


async def call(session, tool, **arguments):
    """(is_error, payload): structured content on success, the JSON error body otherwise."""
    result = await session.call_tool(tool, arguments)
    if result.is_error:
        text = result.content[0].text
        try:
            return True, json.loads(text)
        except ValueError:
            return True, {"error": "sdk", "message": text}
    return False, result.structured_content


async def raw_call(url, tool, arguments, *, headers=None, raw_body=None):
    """Muse-template-like: POST only, no redirects, a fresh session for each invocation."""
    headers = {**JSON_RPC, **(headers or {})}
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=20) as http:
        opened = await http.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "raw-template", "version": "0"}}})
        if opened.status_code != 200:
            return opened, None
        session = {"mcp-session-id": opened.headers["mcp-session-id"],
                   "mcp-protocol-version": "2025-11-25"}
        await http.post(url, headers={**headers, **session},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        message = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}}
        response = await http.post(url, headers={**headers, **session},
                                   content=raw_body if raw_body is not None
                                   else json.dumps(message))
        return response, rpc_body(response)["result"] if response.status_code == 200 else None


async def book(session, room_id="rm-garden-queen", **overrides):
    error, quote = await call(session, "get_quote", room_id=room_id, **(STAY | overrides))
    assert not error, quote
    error, booking = await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST)
    return quote, error, booking


def has(actual, expected):
    return expected.items() <= actual.items()


def rpc_body(response):
    """A JSON-RPC response from a JSON or an SSE (text/event-stream) answer."""
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:") and line[5:].strip():
            message = json.loads(line[5:])
            if "result" in message or "error" in message:
                return message
    return None


def tool_map(tools):
    return {tool.name: tool for tool in tools}


async def test_catalog_declares_accurate_annotations_schemas_and_neutral_text():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        init = session.init_result
        tools = tool_map((await session.list_tools()).tools)
    assert init.protocol_version == "2025-11-25"
    assert (init.server_info.name, init.server_info.version) == ("cedar-stays", "1.0.0")
    assert "minor units" in init.instructions
    assert list(tools) == GOOD_TOOLS
    hints = {name: tool.annotations.model_dump(by_alias=True, exclude_none=True)
             for name, tool in tools.items()}
    for name in ("search_rooms", "get_quote", "get_booking"):
        assert hints[name] | {"title": None} == {
            "title": None, "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False}
    for name in ("create_booking", "cancel_booking"):
        assert hints[name]["readOnlyHint"] is False and hints[name]["destructiveHint"] is True
        assert hints[name]["idempotentHint"] is True and hints[name]["openWorldHint"] is True
    assert tools["get_quote"].title == hints["get_quote"]["title"] == "Get a price quote"
    search = tools["search_rooms"].input_schema
    assert search["required"] == ["check_in", "check_out", "adults"]
    assert has(search["properties"]["adults"], {"type": "integer", "minimum": 1, "maximum": 4})
    assert search["properties"]["check_in"]["format"] == "date"
    booking = tools["create_booking"].input_schema
    assert booking["required"] == ["quote_id", "guest_name", "guest_email"]
    assert booking["properties"]["guest_email"]["format"] == "email"
    assert "idempotency_key" in booking["properties"]
    quote_out = tools["get_quote"].output_schema
    assert {"quote_id", "total_minor", "currency", "taxes_minor", "cancellation_policy",
            "expires_at"} <= set(quote_out["required"])
    assert quote_out["additionalProperties"] is False
    assert all(tool.output_schema for tool in tools.values())
    # Model-visible text describes a hotel, not a test (no evaluation framing).
    visible = json.dumps([[t.description, t.title] for t in tools.values()]) + init.instructions
    assert not any(word in visible.lower() for word in ("fixture", "oracle", "variant", "test"))
    assert all("minor units" in DESCRIPTIONS[name][1] for name in ("search_rooms", "get_quote"))
    # The honest baseline passes this repository's own tool-usability heuristics.
    assert usability_checks(list(tools.values())) == []


async def test_booking_flow_charges_integer_minor_units_and_oracle_reports_truth():
    async with serve_fixture() as fixture:
        async with mcp_session(fixture.mcp_url) as session:
            error, search = await call(session, "search_rooms", **STAY)
            assert not error and search["currency"] == "USD" and search["nights"] == 2
            assert [room["room_id"] for room in search["rooms"]] == [
                "rm-garden-queen", "rm-cedar-king", "rm-family-bunk", "rm-premier-suite"]
            assert has(search["rooms"][0], {"nightly_minor": 18900, "subtotal_minor": 37800})
            error, family = await call(session, "search_rooms", **(STAY | {"adults": 4}))
            assert [room["room_id"] for room in family["rooms"]] == [
                "rm-family-bunk", "rm-premier-suite"]
            quote, error, booking = await book(session)
            assert has(quote, {"quote_id": "q-0001", "subtotal_minor": 37800,
                               "taxes_minor": 4536, "fees_minor": 2500, "total_minor": 44836,
                               "currency": "USD", "seller": "Cedar Stays", "recurring": False,
                               "expires_at": "2026-11-20T17:15:00Z"})
            assert all(isinstance(quote[key], int) for key in quote if key.endswith("_minor"))
            assert quote["cancellation_policy"]["free_cancellation_until"] == \
                "2026-12-02T23:59:59Z"
            assert not error and has(booking, {
                "booking_id": "b-0001", "status": "confirmed", "payment_status": "captured",
                "charged_minor": 44836, "total_minor": 44836, "replayed": False})
            error, status = await call(session, "get_booking", booking_id="b-0001")
            assert not error and status["status"] == "confirmed"
            # A booked room disappears from availability for overlapping dates.
            error, after = await call(session, "search_rooms", **STAY)
            assert "rm-garden-queen" not in [room["room_id"] for room in after["rooms"]]
            error, taken = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
            assert error and taken["error"] == "unavailable"
        async with httpx.AsyncClient(trust_env=False) as http:
            state = (await http.get(fixture.oracle_url)).json()
    assert state["confirmed_count"] == 1 and state["duplicate_charges"] == 0
    assert state["charged_minor"] == 44836
    assert [(c["booking_id"], c["amount_minor"], c["status"]) for c in state["charges"]] == [
        ("b-0001", 44836, "captured")]
    assert has(state["bookings"][0], {"account": "guest", "created_by": "create_booking",
                                      "guest_email": "taylor@example.com"})
    assert state["calls"] == {"search_rooms": 3, "get_quote": 2, "get_booking": 1,
                              "create_booking": 1, "cancel_booking": 0}
    assert state["tool_errors"]["get_quote"] == 1
    assert state["variant"] == "good" and state["basis"]["variant"] == VARIANT_BASIS["good"]
    assert state["rpc"]["tools/call"] == 7 and state["sessions"] == 1


async def test_create_booking_dedupes_by_quote_and_idempotency_key():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        quote, error, first = await book(session)
        error, replay = await call(session, "create_booking", quote_id=quote["quote_id"],
                                   **GUEST)
        assert not error and replay["booking_id"] == first["booking_id"] and replay["replayed"]
        error, other = await call(session, "create_booking", quote_id=quote["quote_id"],
                                  guest_name="Someone Else", guest_email="else@example.com")
        assert error and other["error"] == "quote_already_booked"
        assert other["booking_id"] == first["booking_id"]
        error, king = await call(session, "get_quote", room_id="rm-cedar-king", **STAY)
        key = {"idempotency_key": "trip-2026-12-04"}
        error, keyed = await call(session, "create_booking", quote_id=king["quote_id"],
                                  **GUEST, **key)
        error, again = await call(session, "create_booking", quote_id=king["quote_id"],
                                  **GUEST, **key)
        assert again["booking_id"] == keyed["booking_id"] and again["replayed"]
        error, suite = await call(session, "get_quote", room_id="rm-premier-suite", **STAY)
        error, conflict = await call(session, "create_booking", quote_id=suite["quote_id"],
                                     **GUEST, **key)
        assert error and conflict["error"] == "idempotency_conflict"
    state = fixture.controls.state()
    assert len(state["charges"]) == 2 and state["duplicate_charges"] == 0


async def test_cancel_refunds_inside_the_free_window_and_keeps_the_charge_after():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        await book(session)
        error, cancelled = await call(session, "cancel_booking", booking_id="b-0001")
        assert not error and has(cancelled, {"status": "cancelled",
                                             "payment_status": "refunded",
                                             "refunded_minor": 44836})
        error, again = await call(session, "cancel_booking", booking_id="b-0001")
        assert not error and again["replayed"] and again["refunded_minor"] == 44836
        # Check-in tomorrow: the free window closed two days before check-in.
        await book(session, check_in="2026-11-21", check_out="2026-11-22")
        error, late = await call(session, "cancel_booking", booking_id="b-0002")
        assert has(late, {"status": "cancelled", "payment_status": "captured",
                          "refunded_minor": 0})
        error, missing = await call(session, "get_booking", booking_id="b-0099")
        assert error and missing["error"] == "booking_not_found"
    state = fixture.controls.state()
    assert state["cancelled_count"] == 2 and state["confirmed_count"] == 0
    assert state["charged_minor"] == state["charges"][1]["amount_minor"]


@pytest.mark.parametrize("arguments,code", [
    ({"check_in": "2026-11-01", "check_out": "2026-11-03"}, "invalid_dates"),
    ({"check_in": "2026-12-06", "check_out": "2026-12-04"}, "invalid_dates"),
    ({"check_in": "2026-12-01", "check_out": "2026-12-31"}, "invalid_dates"),
    ({"check_in": "2026-02-30", "check_out": "2026-03-02"}, "invalid_dates"),
    ({"room_id": "rm-penthouse"}, "unknown_room"),
    ({"room_id": "rm-garden-queen", "adults": 3}, "too_many_guests"),
])
async def test_business_errors_are_tool_errors_with_codes(arguments, code):
    with run_fixture() as fixture:
        async with mcp_session(fixture.mcp_url) as session:
            error, failure = await call(session, "get_quote", **({"room_id": "rm-cedar-king"}
                                                               | STAY | arguments))
    assert error and failure["error"] == code and failure["message"]
    assert fixture.controls.state()["tool_errors"]["get_quote"] == 1


async def test_schema_violations_and_expired_quotes_are_rejected_without_effects():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        error, invalid = await call(session, "search_rooms", **(STAY | {"adults": 9}))
        assert error and "validation error" in invalid["message"]
        error, bad_email = await call(session, "create_booking", quote_id="q-0001",
                                      guest_name="T", guest_email="not-an-email")
        assert error and "guest_email" in bad_email["message"]
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert fixture.controls.advance_clock(901) == "2026-11-20T17:15:01Z"
        error, expired = await call(session, "create_booking", quote_id=quote["quote_id"],
                                    **GUEST)
        assert error and expired["error"] == "quote_expired"
        error, unknown = await call(session, "create_booking", quote_id="q-0042", **GUEST)
        assert error and unknown["error"] == "quote_not_found"
    state = fixture.controls.state()
    assert state["bookings"] == [] and state["charges"] == []


async def test_oracle_resets_moves_the_clock_and_stays_private():
    config = FixtureConfig(oracle_token="oracle-token-0123456789")
    async with serve_fixture(config) as fixture:
        async with mcp_session(fixture.mcp_url) as session:
            await book(session)
            tools = [tool.name for tool in (await session.list_tools()).tools]
        assert not any("oracle" in name for name in tools)
        token = {"X-Oracle-Token": "oracle-token-0123456789"}
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.get(fixture.oracle_url)).status_code == 403
            denied = await http.get(fixture.oracle_url, headers=token | {"Origin": "http://evil"})
            assert denied.status_code == 403
            rebound = await http.get(fixture.oracle_url,
                                     headers=token | {"Host": "attacker.example:80"})
            assert rebound.status_code == 421  # DNS-rebinding defence on every route.
            state = (await http.get(fixture.oracle_url, headers=token)).json()
            assert state["confirmed_count"] == 1 and state["clock"] == "2026-11-20T17:00:00Z"
            moved = await http.post(fixture.base_url + "/__oracle/clock", headers=token,
                                    json={"advance_seconds": 3600})
            assert moved.json() == {"clock": "2026-11-20T18:00:00Z"}
            invalid = await http.post(fixture.base_url + "/__oracle/clock", headers=token,
                                      json={"advance_seconds": 1, "set": "2026-01-01T00:00:00Z"})
            assert invalid.status_code == 400
            unknown = await http.post(fixture.base_url + "/__oracle/missing", headers=token)
            assert unknown.status_code == 404
            extra = await http.post(fixture.reset_url, headers=token, json={"wipe": True})
            assert extra.status_code == 400
            reset = await http.post(fixture.reset_url, headers=token,
                                    json={"clock": "2026-11-25T09:00:00-08:00"})
            fresh = reset.json()
    assert fresh["bookings"] == [] and fresh["charges"] == [] and fresh["requests"] == []
    assert fresh["clock"] == "2026-11-25T17:00:00Z" and set(fresh["calls"].values()) == {0}
    assert fresh["confirmed_count"] == 0 and fresh["duplicate_charges"] == 0


@pytest.mark.parametrize("settings,problem", [
    ({"clock": "2026-11-20T17:00:00"}, "timezone"),
    ({"users": [{"username": "taylor", "password": "long-enough-1", "name": "T",
                 "email": "t@example.com"}] * 2}, "unique"),
    ({"api_keys": {"cs-key-0123456789abcdef": "nobody"}}, "configured users"),
    ({"api_keys": {"short": "taylor"}}, "16 characters"),
    ({"api_key_name": "Authorization"}, "bearer"),
    ({"price_change_nightly_minor": 0}, "change the price"),
    ({"cimd_documents": {"http://client.example.com/c.json": {}}}, "https"),
    ({"cimd_documents": {"https://client.example.com/c.json": {
        "client_id": "https://client.example.com/c.json", "redirect_uris": "http://x/cb"}}},
     "redirect_uris"),
    ({"cimd_documents": {"https://client.example.com/c.json": {
        "client_id": "https://client.example.com/c.json", "redirect_uris": ["http://x/cb"],
        "token_endpoint_auth_method": "client_secret_post"}}}, "public"),
    ({"surprise": True}, "Extra inputs"),
])
def test_config_validation_fails_closed(settings, problem):
    with pytest.raises(ValidationError, match=problem):
        FixtureConfig.model_validate(settings)


async def test_oversized_bodies_and_bad_oracle_commands_are_refused_with_reasons():
    async with serve_fixture() as fixture, httpx.AsyncClient(trust_env=False) as http:
        large = await http.post(fixture.mcp_url, headers=JSON_RPC,
                                content=b"{" + b" " * (1024 * 1024 + 1) + b"}")
        bad = await http.post(fixture.base_url + "/__oracle/clock", json={"advance_seconds": -5})
        undecodable = await http.post(fixture.reset_url, content=b"\xff\xfe")
    assert large.status_code == 413
    assert bad.status_code == 400 and "advance_seconds" in bad.json()["error_description"]
    assert undecodable.status_code == 400
    assert fixture.controls.state()["requests"] == []


async def test_injected_clock_and_ids_are_deterministic():
    moments = []

    def clock():
        return datetime(2026, 11, 30, 8, 0, tzinfo=UTC)
    for _ in range(2):
        async with serve_fixture(clock=clock) as fixture:
            async with mcp_session(fixture.mcp_url) as session:
                quote, _, booking = await book(session)
            moments.append((quote, booking))
    assert moments[0] == moments[1]
    assert moments[0][0]["expires_at"] == "2026-11-30T08:15:00Z"
    assert moments[0][1]["created_at"] == "2026-11-30T08:00:00Z"
    with pytest.raises(ValidationError, match="timezone"):
        ConnectorFixture(FixtureConfig(clock="2026-11-20T17:00:00"))


async def test_sse_responses_and_request_log_track_sessions_without_secrets():
    async with serve_fixture(FixtureConfig(response_mode="sse")) as fixture:
        async with mcp_session(fixture.mcp_url) as session:
            await book(session)
        answers = [await raw_call(fixture.mcp_url, "search_rooms", STAY) for _ in range(2)]
        assert answers[0][0].headers["content-type"].startswith("text/event-stream")
        assert [len(result["structuredContent"]["rooms"]) for _, result in answers] == [3, 3]
    state = fixture.controls.state()
    initializes = [entry for entry in state["requests"] if entry["rpc"] == "initialize"]
    assert [entry["new_session"] for entry in initializes] == ["s1", "s2", "s3"]
    calls = [entry for entry in state["requests"] if entry["rpc"] == "tools/call"]
    assert [entry["tool"] for entry in calls] == ["get_quote", "create_booking", "search_rooms",
                                                  "search_rooms"]
    assert [entry["session"] for entry in calls] == ["s1", "s1", "s2", "s3"]
    assert all(entry["status"] == 200 and entry["protocol_version"] == "2025-11-25"
               for entry in calls)
    assert state["sessions"] == 3 and state["http"]["POST"] >= 9


async def test_search_results_paginate_with_a_session_id_and_page_tokens():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        error, first = await call(session, "search_rooms", **(STAY | {"page_size": 3}))
        assert not error and len(first["rooms"]) == 3 and first["search_session_id"] == "ss-0001"
        assert first["next_page_token"] == "pg-ss-0001-3"
        error, rest = await call(session, "search_rooms", **(STAY | {"page_token": first["next_page_token"]}))
        assert not error and [room["room_id"] for room in rest["rooms"]] == ["rm-premier-suite"]
        assert rest["search_session_id"] == "ss-0001" and rest["next_page_token"] is None
        error, refused = await call(session, "search_rooms", **(STAY | {"adults": 4, "page_token": "pg-ss-0001-3"}))
        assert error and refused["error"] == "invalid_page_token"
        error, single = await call(session, "search_rooms", **STAY)
        assert single["next_page_token"] is None and single["search_session_id"] == "ss-0002"
