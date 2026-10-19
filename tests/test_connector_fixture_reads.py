"""Connector fixture read and time variants: etag conditional reads, rate-limited status checks,
quote expiry and the oracle's clock advance."""
import json

import httpx
from test_connector_fixture_tools import GUEST, STAY, book, call, has, mcp_session, tool_map
from test_connector_fixture_variants import catalog_json, published

from consumer_agent_testing_tool.connector_fixture import DESCRIPTIONS, FixtureConfig, serve_fixture
from consumer_agent_testing_tool.guidelines import static_findings
from consumer_agent_testing_tool.lint import usability_checks


def clean_catalog(tools, instructions):
    """No lint warning and no evaluated guideline finding (manifest rules need a manifest)."""
    rows = static_findings([tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                            for tool in tools], instructions=instructions)
    return usability_checks(tools) == [] and [row.rule for row in rows if row.evaluated] == []


async def test_etag_reads_answer_not_modified_while_the_booking_is_unchanged():
    async with serve_fixture(FixtureConfig(variant="etag")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        listed = (await session.list_tools()).tools
        await book(session)
        await book(session, room_id="rm-cedar-king")
        error, first = await call(session, "get_booking", booking_id="b-0001")
        assert not error and has(first, {"booking_id": "b-0001", "status": "confirmed"})
        error, same = await call(session, "get_booking", booking_id="b-0001",
                                 if_none_match=first["etag"])
        assert not error and same == {"not_modified": True}  # No body: the last read holds.
        error, stale = await call(session, "get_booking", booking_id="b-0001",
                                  if_none_match="0123456789abcdef")
        assert not error and stale == first
        error, other = await call(session, "get_booking", booking_id="b-0002",
                                  if_none_match=first["etag"])
        assert other["booking_id"] == "b-0002" and other["etag"] != first["etag"]
        await call(session, "cancel_booking", booking_id="b-0001")
        error, changed = await call(session, "get_booking", booking_id="b-0001",
                                    if_none_match=first["etag"])
        assert has(changed, {"status": "cancelled", "payment_status": "refunded"})
        assert changed["etag"] != first["etag"]
        init = session.init_result
    tools = tool_map(listed)
    read = tools["get_booking"]
    assert read.input_schema["required"] == ["booking_id"]
    assert "if_none_match" in read.input_schema["properties"]
    assert read.description.startswith(DESCRIPTIONS["get_booking"][1])
    assert "if_none_match" in read.description
    # One object schema with both answers; writes keep the plain booking shape.
    assert read.output_schema["type"] == "object" and len(read.output_schema["anyOf"]) == 2
    assert "etag" not in tools["create_booking"].output_schema["properties"]
    assert clean_catalog(listed, init.instructions)
    state = fixture.controls.state()
    assert state["booking_reads"] == {"full": 4, "conditional": 4, "not_modified": 1}
    assert state["early_repeats"] is None
    assert "If-None-Match" in state["basis"]["variant"]["assumed"]  # The analog is labeled.


async def test_good_variant_counts_full_reads_and_ignores_no_conditions():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        await book(session)
        for _ in range(2):
            error, booking = await call(session, "get_booking", booking_id="b-0001")
            assert not error and "etag" not in booking
    assert fixture.controls.state()["booking_reads"] == {"full": 2, "conditional": 0,
                                                         "not_modified": 0}


async def test_rate_limited_status_checks_refuse_early_repeats_with_retry_after():
    config = FixtureConfig(variant="rate-limited", rate_limit_seconds=45)
    async with serve_fixture(config) as fixture, mcp_session(fixture.mcp_url) as session:
        listed = (await session.list_tools()).tools
        await book(session)
        await book(session, room_id="rm-cedar-king")
        error, first = await call(session, "get_booking", booking_id="b-0001")
        assert not error and first["status"] == "confirmed"
        error, early = await call(session, "get_booking", booking_id="b-0001")
        assert error and has(early, {"error": "rate_limited", "booking_id": "b-0001",
                                     "retry_after_seconds": 45})
        error, other = await call(session, "get_booking", booking_id="b-0002")
        assert not error and other["booking_id"] == "b-0002"  # The limit is per booking.
        fixture.controls.advance_clock(30)
        error, still = await call(session, "get_booking", booking_id="b-0001")
        assert error and still["retry_after_seconds"] == 15  # Early repeats keep the window.
        fixture.controls.advance_clock(15)
        error, later = await call(session, "get_booking", booking_id="b-0001")
        assert not error and later == first
        error, missing = await call(session, "get_booking", booking_id="b-0099")
        assert error and missing["error"] == "booking_not_found"
        init = session.init_result
    description = tool_map(listed)["get_booking"].description
    assert description.startswith(DESCRIPTIONS["get_booking"][1])
    assert "once every 45 seconds" in description and "retry_after_seconds" in description
    assert clean_catalog(listed, init.instructions)
    state = fixture.controls.state()
    assert "Retry-After" in state["basis"]["variant"]["assumed"]  # The analog is labeled.
    assert state["early_repeats"] == 2 and state["tool_errors"]["get_booking"] == 3
    assert state["booking_reads"]["full"] == 3
    # Nothing but get_booking is limited: writes and searches answer as in the good variant.
    assert state["confirmed_count"] == 2 and state["tool_errors"]["create_booking"] == 0


async def test_quote_expiry_refuses_quotes_after_the_configured_interval():
    config = FixtureConfig(variant="quote-expiry", quote_expiry_seconds=300)
    async with serve_fixture(config) as fixture, mcp_session(fixture.mcp_url) as session, \
            httpx.AsyncClient(trust_env=False) as http:
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert not error and quote["expires_at"] == "2026-11-20T17:05:00Z"
        moved = await http.post(fixture.base_url + "/__oracle/advance", json={"seconds": 300})
        assert moved.json() == {"clock": "2026-11-20T17:05:00Z"}
        error, held = await call(session, "get_quote", room_id="rm-cedar-king", **STAY)
        await http.post(fixture.base_url + "/__oracle/advance", json={"seconds": 1})
        error, expired = await call(session, "create_booking", quote_id=quote["quote_id"],
                                    **GUEST)
        assert error and expired["error"] == "quote_expired"
        assert quote["expires_at"] in expired["message"]
        error, fresh = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert fresh["quote_id"] == "q-0003" and fresh["expires_at"] == "2026-11-20T17:10:01Z"
        assert fresh["total_minor"] == quote["total_minor"]
        error, booking = await call(session, "create_booking", quote_id=fresh["quote_id"], **GUEST)
        assert not error and booking["status"] == "confirmed"
        error, live = await call(session, "create_booking", quote_id=held["quote_id"], **GUEST)
        assert not error and live["quote_id"] == "q-0002"  # Issued at 17:05:00, lives to 17:10.
    state = fixture.controls.state()
    assert [b["quote_id"] for b in state["bookings"]] == ["q-0003", "q-0002"]
    assert len(state["charges"]) == 2 and state["tool_errors"]["create_booking"] == 1


async def test_quote_expiry_publishes_the_good_catalog_with_a_shorter_default_lifetime():
    _, good, _ = await published("good")
    _, tools, _ = await published("quote-expiry")
    assert catalog_json(tools) == catalog_json(good)
    async with serve_fixture(FixtureConfig(variant="quote-expiry")) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
    assert not error and quote["expires_at"] == "2026-11-20T17:02:00Z"  # 120 s by default.
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
    assert not error and quote["expires_at"] == "2026-11-20T17:15:00Z"  # Others keep 900 s.


async def test_oracle_advance_moves_the_fixture_clock_and_stays_private():
    config = FixtureConfig(oracle_token="oracle-token-0123456789")
    async with serve_fixture(config) as fixture, httpx.AsyncClient(trust_env=False) as http:
        url = fixture.base_url + "/__oracle/advance"
        token = {"X-Oracle-Token": "oracle-token-0123456789"}
        assert (await http.post(url, json={"seconds": 60})).status_code == 403
        moved = await http.post(url, json={"seconds": 60}, headers=token)
        assert moved.json() == {"clock": "2026-11-20T17:01:00Z"}
        for bad in ({"seconds": -1}, {"seconds": 1.5}, {"advance_seconds": 60}, {}):
            refused = await http.post(url, json=bad, headers=token)
            assert refused.status_code == 400, bad
        assert (await http.get(url, headers=token)).status_code == 404
        state = (await http.get(fixture.oracle_url, headers=token)).json()
    assert state["clock"] == "2026-11-20T17:01:00Z"
    assert json.dumps(state).count("oracle-token") == 0
