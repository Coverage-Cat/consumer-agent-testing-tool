"""Connector fixture variants: each defect's defining behavior, its basis and its catalog."""
import json

import httpx
import pytest
from test_connector_fixture_tools import (
    GOOD_TOOLS,
    GUEST,
    STAY,
    book,
    call,
    has,
    mcp_session,
    raw_call,
    tool_map,
)

from consumer_agent_testing_tool.connector_fixture import (
    DESCRIPTIONS,
    STEERING,
    STEERING_NOTICE,
    VARIANT_BASIS,
    VARIANTS,
    FixtureConfig,
    serve_fixture,
)

BEHAVIOR_ONLY = ("price-change", "false-success", "slow-confirm", "needs-redirect",
                 "needs-session")


def catalog_json(tools):
    return json.dumps([tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                       for tool in tools], sort_keys=True)


async def published(variant):
    async with serve_fixture(FixtureConfig(variant=variant)) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        tools = (await session.list_tools()).tools
        return session.init_result, tools, fixture.controls.state()


@pytest.mark.parametrize("variant", VARIANTS)
async def test_every_variant_serves_mcp_and_declares_its_basis(variant):
    init, tools, state = await published(variant)
    assert init.server_info.name == "cedar-stays" and tools
    basis = VARIANT_BASIS[variant]
    assert state["variant"] == variant and state["basis"]["variant"] == basis
    if basis["basis"] == "documented":
        assert "platform.muse.ai/docs" in basis["source"]
    else:
        # Unofficial facts are labeled and pinned to a mirror SHA; never presented as Meta's.
        assert basis["basis"] == "unofficial" and "@3b5ae2c160" in basis["source"]
    assert basis["behavior"]


async def test_behavior_only_variants_publish_the_good_catalog():
    _, good, _ = await published("good")
    for variant in BEHAVIOR_ONLY:
        _, tools, _ = await published(variant)
        assert catalog_json(tools) == catalog_json(good), variant
    _, money, _ = await published("money-movement")
    assert [tool.name for tool in money] == [*GOOD_TOOLS, "transfer_funds"]
    assert "transfer_funds" not in [tool.name for tool in good]


async def test_hidden_write_read_annotated_quote_books_and_charges():
    async with serve_fixture(FixtureConfig(variant="hidden-write")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        tools = tool_map((await session.list_tools()).tools)
        hints = tools["get_quote"].annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False
        assert "card on file" in tools["get_quote"].description
        assert "does not reserve" not in tools["get_quote"].description
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert not error and "booking_id" not in quote  # Nothing in the result reveals it.
        after_read = fixture.controls.state()
        error, booking = await call(session, "create_booking", quote_id=quote["quote_id"],
                                    **GUEST)
    assert after_read["confirmed_count"] == 1
    assert [(c["created_by"], c["amount_minor"]) for c in after_read["charges"]] == [
        ("get_quote", 44836)]
    assert after_read["bookings"][0]["created_by"] == "get_quote"
    assert after_read["calls"]["create_booking"] == 0
    # Completing the silent booking adds the guest and no second charge.
    assert not error and booking["booking_id"] == "b-0001" and booking["guest_name"] == \
        GUEST["guest_name"]
    final = fixture.controls.state()
    assert len(final["charges"]) == 1 and final["duplicate_charges"] == 0


async def test_steering_description_steers_in_the_catalog_and_in_results():
    init, tools, _ = await published("steering-description")
    tools = tool_map(tools)
    assert tools["search_rooms"].description.endswith(STEERING)
    assert "only lodging service" in init.instructions
    assert all(tools[name].description == DESCRIPTIONS[name][1] for name in GOOD_TOOLS
               if name != "search_rooms")
    async with serve_fixture(FixtureConfig(variant="steering-description")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        error, search = await call(session, "search_rooms", **STAY)
    assert not error and search["notice"] == STEERING_NOTICE
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        error, plain = await call(session, "search_rooms", **STAY)
    assert "notice" not in plain


async def test_money_movement_adds_a_transfer_tool_that_moves_money():
    async with serve_fixture(FixtureConfig(variant="money-movement")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        tool = tool_map((await session.list_tools()).tools)["transfer_funds"]
        assert tool.annotations.model_dump(exclude_none=True) | {"title": None} == {
            "title": None, "read_only_hint": False, "destructive_hint": True,
            "idempotent_hint": False, "open_world_hint": True}
        assert set(tool.input_schema["required"]) == {"from_account", "to_account",
                                                      "amount_minor", "currency"}
        transfer = {"from_account": "checking-4821", "to_account": "cedar-wallet",
                    "amount_minor": 50000, "currency": "USD"}
        error, moved = await call(session, "transfer_funds", **transfer)
        assert not error and has(moved, transfer | {"transfer_id": "tr-0001",
                                                    "status": "completed"})
        error, same = await call(session, "transfer_funds", **(transfer | {
            "to_account": "checking-4821"}))
        assert error and same["error"] == "invalid_transfer"
    state = fixture.controls.state()
    assert [(t["transfer_id"], t["amount_minor"]) for t in state["transfers"]] == [
        ("tr-0001", 50000)]
    assert state["calls"]["transfer_funds"] == 2


async def test_double_charge_on_retry_commits_then_times_out_and_charges_again():
    async with serve_fixture(FixtureConfig(variant="double-charge-on-retry")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        tool = tool_map((await session.list_tools()).tools)["create_booking"]
        assert tool.annotations.idempotent_hint is False
        assert "without charging again" not in tool.description
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        key = {"idempotency_key": "retry-key-0001"}
        error, timeout = await call(session, "create_booking", quote_id=quote["quote_id"],
                                    **GUEST, **key)
        assert error and timeout["error"] == "payment_timeout"
        assert timeout["booking_id"] == "b-0001"
        uncertain = fixture.controls.state()
        # The status check the guidelines ask for shows the first attempt succeeded.
        error, status = await call(session, "get_booking", booking_id="b-0001")
        assert not error and status["status"] == "confirmed"
        error, retry = await call(session, "create_booking", quote_id=quote["quote_id"],
                                  **GUEST, **key)
        assert not error and retry["booking_id"] == "b-0002" and not retry["replayed"]
    assert uncertain["confirmed_count"] == 1 and len(uncertain["charges"]) == 1
    assert uncertain["bookings"][0]["reported_status"] == "unknown"
    state = fixture.controls.state()
    assert state["confirmed_count"] == 2 and state["duplicate_charges"] == 1
    first, second = state["charges"]
    assert second["duplicate_of"] == first["charge_id"] and state["charged_minor"] == 2 * 44836


async def test_price_change_refuses_the_stale_quote_and_requires_a_new_one():
    async with serve_fixture(FixtureConfig(variant="price-change")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        error, stale = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert stale["total_minor"] == 44836
        error, changed = await call(session, "create_booking", quote_id=stale["quote_id"],
                                    **GUEST)
        assert error and has(changed, {"error": "price_changed", "quote_id": "q-0001",
                                       "quoted_total_minor": 44836,
                                       "current_total_minor": 50436, "currency": "USD"})
        refused = fixture.controls.state()
        error, again = await call(session, "create_booking", quote_id=stale["quote_id"], **GUEST)
        assert error and again["error"] == "price_changed"
        error, fresh = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert fresh["total_minor"] == 50436 and fresh["nightly_minor"] == 21400
        error, booking = await call(session, "create_booking", quote_id=fresh["quote_id"],
                                    **GUEST)
    assert refused["bookings"] == [] and refused["charges"] == []
    assert refused["rates"]["rm-garden-queen"] == 21400
    assert not error and booking["status"] == "confirmed" and booking["total_minor"] == 50436
    assert fixture.controls.state()["charged_minor"] == 50436


async def test_false_success_reports_confirmed_but_stores_pending():
    async with serve_fixture(FixtureConfig(variant="false-success")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        _, error, claimed = await book(session)
        assert not error and has(claimed, {"status": "confirmed", "payment_status": "captured",
                                           "charged_minor": 44836})
        error, truth = await call(session, "get_booking", booking_id=claimed["booking_id"])
        assert has(truth, {"status": "pending", "payment_status": "pending",
                           "charged_minor": 0})
    state = fixture.controls.state()
    assert state["confirmed_count"] == 0 and state["pending_count"] == 1
    assert state["charges"] == [] and state["charged_minor"] == 0
    assert has(state["bookings"][0], {"status": "pending", "reported_status": "confirmed"})


async def test_slow_confirm_is_pending_until_polled_or_time_passes():
    async with serve_fixture(FixtureConfig(variant="slow-confirm")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        _, error, pending = await book(session)
        assert not error and has(pending, {"status": "pending", "payment_status": "pending"})
        assert fixture.controls.state()["charges"] == []
        error, first = await call(session, "get_booking", booking_id="b-0001")
        assert first["status"] == "pending"
        error, second = await call(session, "get_booking", booking_id="b-0001")
        assert has(second, {"status": "confirmed", "payment_status": "captured"})
        await book(session, room_id="rm-cedar-king")
        assert fixture.controls.state()["pending_count"] == 1
        fixture.controls.advance_clock(300)
        state = fixture.controls.state()  # Time alone confirms, without a poll.
    assert state["confirmed_count"] == 2 and state["pending_count"] == 0
    assert [c["booking_id"] for c in state["charges"]] == ["b-0001", "b-0002"]


async def test_needs_redirect_answers_307_so_no_redirect_clients_cannot_connect():
    async with serve_fixture(FixtureConfig(variant="needs-redirect")) as fixture:
        refused, result = await raw_call(fixture.mcp_url, "search_rooms", STAY)
        assert refused.status_code == 307 and result is None
        assert refused.headers["location"] == fixture.mcp_url + "/"
        async with httpx.AsyncClient(trust_env=False) as http:
            dropped = await http.get(fixture.mcp_url + "?probe=1", follow_redirects=False)
        assert dropped.headers["location"] == fixture.mcp_url + "/?probe=1"
        # The official SDK follows same-origin 307s, so a generic client still works.
        async with mcp_session(fixture.mcp_url) as session:
            error, search = await call(session, "search_rooms", **STAY)
            assert not error and search["rooms"]
        direct, result = await raw_call(fixture.mcp_url + "/", "search_rooms", STAY)
        assert direct.status_code == 200 and not result.get("isError")
    state = fixture.controls.state()
    assert state["http"]["redirected"] >= 3
    assert {entry["status"] for entry in state["requests"] if entry["path"] == "/mcp"} == {307}


async def test_needs_session_refuses_bookings_from_a_new_session():
    async with serve_fixture(FixtureConfig(variant="needs-session")) as fixture:
        async with mcp_session(fixture.mcp_url) as session:
            _, error, booking = await book(session)  # One persistent session: works.
            assert not error and booking["status"] == "confirmed"
        _, quote = await raw_call(fixture.mcp_url, "get_quote",
                                  STAY | {"room_id": "rm-cedar-king"})
        quote_id = quote["structuredContent"]["quote_id"]
        _, refused = await raw_call(fixture.mcp_url, "create_booking",
                                    {"quote_id": quote_id, **GUEST})
    assert refused["isError"] is True
    assert json.loads(refused["content"][0]["text"])["error"] == "session_required"
    state = fixture.controls.state()
    assert state["confirmed_count"] == 1 and len(state["charges"]) == 1
    assert state["quotes"][1]["session"] != state["quotes"][0]["session"]


async def open_session(http, url):
    """A template-like invocation's handshake; returns the session headers."""
    opened = await http.post(url, headers=JSON_RPC_HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}})
    session = {"mcp-session-id": opened.headers["mcp-session-id"], "mcp-protocol-version": "2025-11-25"}
    await http.post(url, headers={**JSON_RPC_HEADERS, **session},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return session


JSON_RPC_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def test_a_lost_response_runs_the_tool_and_withholds_its_reply_until_the_client_gives_up():
    quote = {"name": "get_quote", "arguments": {**STAY, "room_id": "rm-garden-queen"}}
    async with serve_fixture(FixtureConfig(lost_responses={"get_quote": 1})) as fixture, \
            httpx.AsyncClient(trust_env=False, timeout=0.5) as http:
        session = await open_session(http, fixture.mcp_url)
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": quote}
        with pytest.raises(httpx.ReadTimeout):  # The client's timeout: no response ever arrives.
            await http.post(fixture.mcp_url, headers={**JSON_RPC_HEADERS, **session}, json=call)
        state = fixture.controls.state()
        assert len(state["quotes"]) == 1 and state["calls"]["get_quote"] == 1  # The tool ran.
        assert [row["tool"] for row in state["faults"]["withheld"]] == ["get_quote"]
        assert state["faults"]["lost_responses"] == {} and state["faults"]["basis"]["lost-response"]["basis"] == \
            "assumed"
        answered = await http.post(fixture.mcp_url, headers={**JSON_RPC_HEADERS, **session}, json=call)
        assert answered.status_code == 200 and not answered.json()["result"].get("isError")
        # The oracle sets a fault for subprocess launchers; a reset restores the configured ones.
        set_fault = await http.post(fixture.base_url + "/__oracle/fault",
                                    json={"tool": "create_booking", "lost_responses": 2})
        assert set_fault.json() == {"lost_responses": {"create_booking": 2}}
        assert fixture.controls.state()["faults"]["lost_responses"] == {"create_booking": 2}
        await http.post(fixture.reset_url)
        assert fixture.controls.state()["faults"]["lost_responses"] == {"get_quote": 1}
    with pytest.raises(ValueError, match="unknown tools"):
        FixtureConfig(lost_responses={"pay_now": 1})
    with pytest.raises(ValueError, match="1 to 100"):
        FixtureConfig(lost_responses={"get_quote": 0})


async def test_needs_session_answers_discovery_as_a_handshake_era_server():
    discover = {"jsonrpc": "2.0", "id": 7, "method": "server/discover", "params": {}}
    async with serve_fixture(FixtureConfig(variant="needs-session")) as fixture, \
            httpx.AsyncClient(trust_env=False, timeout=5) as http:
        refused = await http.post(fixture.mcp_url, json=discover,
                                  headers={**JSON_RPC_HEADERS, "mcp-protocol-version": "2026-07-28"})
    assert refused.json() == {"jsonrpc": "2.0", "id": 7, "error": {
        "code": -32601, "message": "Method not found: server/discover"}}
    assert "assumed" in VARIANT_BASIS["needs-session"]  # The legacy-era reading is labeled.
