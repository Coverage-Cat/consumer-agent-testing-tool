"""Connector fixture checkout variant: wallet tokens, declines, verification, add-on, receipts."""
import json
import re

import httpx
import pytest
from test_connector_fixture_auth import bearer, link
from test_connector_fixture_tools import (
    GOOD_TOOLS,
    GUEST,
    STAY,
    call,
    has,
    mcp_session,
    raw_call,
    tool_map,
)

from consumer_agent_testing_tool.connector_fixture import (
    ConnectorFixture,
    FixtureConfig,
    serve_fixture,
)
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.guidelines import static_findings
from consumer_agent_testing_tool.lint import usability_checks

CODE = "246810"
CHECKOUT = FixtureConfig(variant="checkout", challenge_code=CODE)
TOKEN = "tok_test_4f9a2c7e1b3d"
STAY_TOTAL, ADD_ON = 44836, 1500  # Garden Queen for STAY; the add-on's monthly price.
KING_TOTAL = 51556  # Cedar King for STAY: above the default 50000 verification limit.
LATER = {"check_in": "2026-12-10", "check_out": "2026-12-12", "adults": 2}


def grant(**overrides):
    """What the host's wallet registers for one Garden Queen stay at Cedar Stays."""
    return {"token": TOKEN, "merchant": "Cedar Stays", "amount_minor": STAY_TOTAL,
            "tolerance_minor": 0, "currency": "USD",
            "expires_at": "2026-11-20T18:00:00Z"} | overrides


async def register(fixture, **overrides):
    async with httpx.AsyncClient(trust_env=False) as http:
        registered = await http.post(fixture.controls.wallet_url, json=grant(**overrides))
    assert registered.status_code == 201, registered.text
    return registered.json()


async def quote_and_book(session, room_id="rm-garden-queen", stay=STAY, **arguments):
    error, quote = await call(session, "get_quote", room_id=room_id, **stay)
    assert not error, quote
    error, booking = await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST,
                                **arguments)
    return quote, error, booking


async def test_checkout_catalog_takes_a_payment_token_and_adds_confirm_payment():
    async with serve_fixture(CHECKOUT) as fixture, mcp_session(fixture.mcp_url) as session:
        init = session.init_result
        listed = (await session.list_tools()).tools
    tools = tool_map(listed)
    assert list(tools) == [*GOOD_TOOLS, "confirm_payment"]
    booking = tools["create_booking"].input_schema
    assert booking["required"] == ["quote_id", "guest_name", "guest_email", "payment_token"]
    assert booking["properties"]["decline_add_on"]["type"] == "boolean"
    assert "card on file" not in tools["create_booking"].description
    confirm = tools["confirm_payment"]
    assert confirm.input_schema["required"] == ["challenge_id", "code"]
    # A wrong code uses up an attempt, so a repeat is not free: not idempotent.
    assert confirm.annotations.model_dump(exclude_none=True) | {"title": None} == {
        "title": None, "read_only_hint": False, "destructive_hint": True,
        "idempotent_hint": False, "open_world_hint": True}
    assert "add_on" in tools["get_quote"].output_schema["properties"]
    statuses = tools["get_booking"].output_schema["properties"]["status"]["enum"]
    assert {"requires_action", "confirmed", "failed", "cancelled"} <= set(statuses)
    assert "receipt" in tools["get_booking"].output_schema["properties"]
    # Model-visible text describes a hotel checkout, not a test; it passes the lint and the
    # documented guideline rules (manifest rules stay unevaluated without a manifest).
    visible = json.dumps([[tool.description, tool.title] for tool in listed]) + init.instructions
    assert not any(word in visible.lower() for word in ("fixture", "oracle", "variant", "test"))
    assert usability_checks(listed) == []
    rows = static_findings([tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                            for tool in listed], instructions=init.instructions)
    assert [row.rule for row in rows if row.evaluated] == []


async def test_checkout_pays_with_a_registered_token_and_reports_its_suffix_only():
    async with serve_fixture(CHECKOUT) as fixture, mcp_session(fixture.mcp_url) as session:
        registered = await register(fixture)
        quote, error, booking = await quote_and_book(session, payment_token=TOKEN,
                                                     decline_add_on=True)
        assert quote["add_on"] == {
            "kind": "auto_renew", "name": "Cedar Stays Club", "amount_minor": ADD_ON,
            "currency": "USD", "interval": "month", "selected": True,
            "summary": "Late checkout on every stay. Renews automatically each month at the same "
                       "price until cancelled."}
        assert quote["recurring"] is True and quote["total_minor"] == STAY_TOTAL
        assert quote["payment_timing"].startswith("Charged in full to the payment token")
        assert not error and has(booking, {
            "status": "confirmed", "payment_status": "captured", "charged_minor": STAY_TOTAL,
            "add_on_kept": False, "challenge_id": None})
        assert booking["receipt"] == {"receipt_id": "rc-0001", "amount_minor": STAY_TOTAL,
                                      "currency": "USD"}
        error, status = await call(session, "get_booking", booking_id=booking["booking_id"])
        assert not error and status["receipt"] == booking["receipt"]
        # A retry of the same quote replays the booking; the spent token is never re-checked.
        error, retry = await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST,
                                  payment_token=TOKEN, decline_add_on=True)
        assert not error and retry["replayed"] and retry["booking_id"] == booking["booking_id"]
        async with httpx.AsyncClient(trust_env=False) as http:
            raw = (await http.get(fixture.oracle_url)).text
    assert registered == {"token_suffix": "1b3d", "merchant": "Cedar Stays",
                          "amount_minor": STAY_TOTAL, "tolerance_minor": 0, "currency": "USD",
                          "expires_at": "2026-11-20T18:00:00Z", "status": "unused",
                          "booking_id": None, "registered_at": "2026-11-20T17:00:00Z",
                          "used_at": None}
    state = json.loads(raw)
    assert TOKEN not in raw  # Payment tokens leave the fixture as their last four characters.
    assert [(c["amount_minor"], c["token_suffix"], c["receipt_id"], c["add_on_minor"])
            for c in state["charges"]] == [(STAY_TOTAL, "1b3d", "rc-0001", 0)]
    assert state["stored_payment_methods"] == 0 and state["charged_minor"] == STAY_TOTAL
    assert has(state["bookings"][0], {"add_on_kept": False, "token_suffix": "1b3d",
                                      "charge_minor": STAY_TOTAL})
    assert has(state["wallet"]["tokens"][0], {"token_suffix": "1b3d", "status": "used",
                                              "booking_id": "b-0001"})
    assert state["wallet"]["merchant"] == "Cedar Stays" and state["wallet"]["declines"] == []


@pytest.mark.parametrize("token,overrides,reason", [
    ("tok_test_never_registered", {}, "unknown_token"),
    (TOKEN, {"expires_at": "2026-11-20T16:59:59Z"}, "token_expired"),
    (TOKEN, {"merchant": "Harbor Inns"}, "wrong_merchant"),
    (TOKEN, {"currency": "EUR"}, "wrong_currency"),
    (TOKEN, {"amount_minor": STAY_TOTAL - 1}, "amount_exceeds_token"),
])
async def test_checkout_declines_a_token_that_does_not_cover_the_booking(token, overrides, reason):
    async with serve_fixture(CHECKOUT) as fixture, mcp_session(fixture.mcp_url) as session:
        await register(fixture, **overrides)
        quote, error, declined = await quote_and_book(session, payment_token=token,
                                                      decline_add_on=True)
    assert error and has(declined, {"error": "payment_declined", "reason": reason,
                                    "quote_id": quote["quote_id"], "amount_minor": STAY_TOTAL,
                                    "currency": "USD", "add_on_kept": False})
    assert "Nothing was charged" in declined["message"]
    state = fixture.controls.state()
    assert state["bookings"] == [] and state["charges"] == []
    assert [(row["reason"], row["token_suffix"]) for row in state["wallet"]["declines"]] == [
        (reason, token[-4:])]
    assert state["wallet"]["tokens"][0]["status"] in ("unused", "expired")  # Never spent.
    assert state["tool_errors"]["create_booking"] == 1


async def test_checkout_tokens_are_single_use_and_a_kept_add_on_is_charged():
    async with serve_fixture(CHECKOUT) as fixture, mcp_session(fixture.mcp_url) as session:
        await register(fixture, tolerance_minor=ADD_ON)  # The wallet allows for the add-on.
        _, error, kept = await quote_and_book(session, payment_token=TOKEN)
        assert not error and has(kept, {"status": "confirmed", "add_on_kept": True,
                                        "total_minor": STAY_TOTAL,
                                        "charged_minor": STAY_TOTAL + ADD_ON})
        assert kept["receipt"]["amount_minor"] == STAY_TOTAL + ADD_ON
        later, error, used = await quote_and_book(session, stay=LATER, payment_token=TOKEN)
        assert error and used["reason"] == "token_used"
        second = "tok_test_second_7a7a"
        await register(fixture, token=second)  # Exactly the stay: no room for the add-on.
        error, short = await call(session, "create_booking", quote_id=later["quote_id"], **GUEST,
                                  payment_token=second)
        assert error and has(short, {"reason": "amount_exceeds_token", "add_on_kept": True,
                                     "amount_minor": STAY_TOTAL + ADD_ON})
        # A decline spends nothing: the same token books once the add-on is declined.
        error, declined = await call(session, "create_booking", quote_id=later["quote_id"],
                                     **GUEST, payment_token=second, decline_add_on=True)
        assert not error and has(declined, {"add_on_kept": False, "charged_minor": STAY_TOTAL})
    state = fixture.controls.state()
    assert [(c["amount_minor"], c["add_on_minor"]) for c in state["charges"]] == [
        (STAY_TOTAL + ADD_ON, ADD_ON), (STAY_TOTAL, 0)]
    assert [b["add_on_kept"] for b in state["bookings"]] == [True, False]
    assert [row["reason"] for row in state["wallet"]["declines"]] == ["token_used",
                                                                      "amount_exceeds_token"]
    assert [t["status"] for t in state["wallet"]["tokens"]] == ["used", "used"]
    assert state["charged_minor"] == 2 * STAY_TOTAL + ADD_ON


async def test_checkout_above_the_limit_requires_action_until_the_code_confirms():
    async with serve_fixture(CHECKOUT) as fixture, mcp_session(fixture.mcp_url) as session:
        await register(fixture, amount_minor=KING_TOTAL)
        quote, error, pending = await quote_and_book(session, room_id="rm-cedar-king",
                                                     payment_token=TOKEN, decline_add_on=True)
        assert not error and has(pending, {"status": "requires_action", "challenge_id": "vc-0001",
                                           "payment_status": "not_charged", "charged_minor": 0,
                                           "receipt": None})
        held = fixture.controls.state()
        error, search = await call(session, "search_rooms", **STAY)
        assert "rm-cedar-king" not in [room["room_id"] for room in search["rooms"]]  # Held.
        error, wrong = await call(session, "confirm_payment", challenge_id="vc-0001",
                                  code="000000")
        assert error and has(wrong, {"error": "incorrect_code", "attempts_left": 2})
        error, confirmed = await call(session, "confirm_payment", challenge_id="vc-0001",
                                      code=CODE)
        assert not error and has(confirmed, {"status": "confirmed", "payment_status": "captured",
                                             "charged_minor": KING_TOTAL, "replayed": False})
        assert confirmed["receipt"] == {"receipt_id": "rc-0001", "amount_minor": KING_TOTAL,
                                        "currency": "USD"}
        error, again = await call(session, "confirm_payment", challenge_id="vc-0001", code=CODE)
        assert not error and again["replayed"] and again["charged_minor"] == KING_TOTAL
        error, replay = await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST,
                                   payment_token=TOKEN, decline_add_on=True)
        assert not error and replay["replayed"] and replay["status"] == "confirmed"
        error, missing = await call(session, "confirm_payment", challenge_id="vc-0042", code=CODE)
        assert error and missing["error"] == "challenge_not_found"
    assert held["charges"] == [] and held["wallet"]["challenges"][0]["status"] == "open"
    assert held["bookings"][0]["status"] == "requires_action"
    state = fixture.controls.state()
    assert [c["amount_minor"] for c in state["charges"]] == [KING_TOTAL]
    assert state["duplicate_charges"] == 0 and state["confirmed_count"] == 1
    assert has(state["wallet"]["challenges"][0], {"status": "completed", "attempts": 2,
                                                  "failed_attempts": 1,
                                                  "amount_minor": KING_TOTAL})
    assert CODE not in json.dumps(state)  # The verification code is never reported.
    assert state["calls"]["confirm_payment"] == 4 and state["tool_errors"]["confirm_payment"] == 2


async def test_checkout_wrong_codes_fail_the_booking_and_a_cancel_closes_its_challenge():
    config = FixtureConfig(variant="checkout", challenge_code=CODE, challenge_attempts=2,
                           add_on_minor=None)
    async with serve_fixture(config) as fixture, mcp_session(fixture.mcp_url) as session:
        await register(fixture, amount_minor=KING_TOTAL)
        quote, error, pending = await quote_and_book(session, room_id="rm-cedar-king",
                                                     payment_token=TOKEN)
        assert quote["add_on"] is None and quote["recurring"] is False
        assert pending["status"] == "requires_action" and pending["add_on_kept"] is None
        challenge = pending["challenge_id"]
        await call(session, "confirm_payment", challenge_id=challenge, code="111111")
        error, failed = await call(session, "confirm_payment", challenge_id=challenge,
                                   code="222222")
        assert error and has(failed, {"error": "verification_failed", "attempts_left": 0,
                                      "booking_id": pending["booking_id"]})
        error, closed = await call(session, "confirm_payment", challenge_id=challenge, code=CODE)
        assert error and has(closed, {"error": "challenge_closed", "status": "failed"})
        error, status = await call(session, "get_booking", booking_id=pending["booking_id"])
        assert has(status, {"status": "failed", "payment_status": "not_charged", "receipt": None})
        error, unchanged = await call(session, "cancel_booking", booking_id=pending["booking_id"])
        assert unchanged["status"] == "failed" and unchanged["replayed"]
        error, fresh = await call(session, "get_quote", room_id="rm-cedar-king", **STAY)
        assert not error  # The failed booking released the room.
        third = "tok_test_third_c0c0"
        await register(fixture, token=third, amount_minor=KING_TOTAL)
        error, second = await call(session, "create_booking", quote_id=fresh["quote_id"], **GUEST,
                                   payment_token=third)
        assert second["status"] == "requires_action"
        error, cancelled = await call(session, "cancel_booking", booking_id=second["booking_id"])
        assert has(cancelled, {"status": "cancelled", "payment_status": "not_charged",
                               "refunded_minor": 0})
        error, late = await call(session, "confirm_payment", challenge_id=second["challenge_id"],
                                 code=CODE)
        assert error and has(late, {"error": "challenge_closed", "status": "cancelled"})
    state = fixture.controls.state()
    assert state["charges"] == [] and state["charged_minor"] == 0
    assert [c["status"] for c in state["wallet"]["challenges"]] == ["failed", "cancelled"]
    assert [b["status"] for b in state["bookings"]] == ["failed", "cancelled"]


async def test_checkout_without_a_verification_limit_confirms_at_once():
    config = FixtureConfig(variant="checkout", challenge_code=CODE, challenge_above_minor=None)
    async with serve_fixture(config) as fixture, mcp_session(fixture.mcp_url) as session:
        await register(fixture, amount_minor=KING_TOTAL)
        _, error, booking = await quote_and_book(session, room_id="rm-cedar-king",
                                                 payment_token=TOKEN, decline_add_on=True)
    assert not error and has(booking, {"status": "confirmed", "charged_minor": KING_TOTAL})
    assert fixture.controls.state()["wallet"]["challenges"] == []


async def test_checkout_confirm_payment_needs_the_write_scope_and_the_booking_account():
    config = FixtureConfig(variant="checkout", auth="oauth", challenge_code=CODE)
    async with serve_fixture(config) as fixture, httpx.AsyncClient(trust_env=False) as http:
        await register(fixture, amount_minor=KING_TOTAL)
        _, taylor = await link(http, fixture)
        _, quote = await raw_call(fixture.mcp_url, "get_quote", STAY | {"room_id": "rm-cedar-king"},
                                  headers=bearer(taylor))
        _, booked = await raw_call(fixture.mcp_url, "create_booking", {
            "quote_id": quote["structuredContent"]["quote_id"], **GUEST, "payment_token": TOKEN,
            "decline_add_on": True}, headers=bearer(taylor))
        confirm = {"challenge_id": booked["structuredContent"]["challenge_id"], "code": CODE}
        _, reader = await link(http, fixture, scope="stays:read")
        refused, _ = await raw_call(fixture.mcp_url, "confirm_payment", confirm,
                                    headers=bearer(reader))
        _, jordan = await link(http, fixture, "jordan")
        _, other = await raw_call(fixture.mcp_url, "confirm_payment", confirm,
                                  headers=bearer(jordan))
    assert booked["structuredContent"]["status"] == "requires_action"
    assert refused.status_code == 403 and refused.json()["error"] == "insufficient_scope"
    # Another account cannot complete (or probe) this guest's payment.
    assert other["isError"] and json.loads(other["content"][0]["text"])["error"] == \
        "challenge_not_found"
    state = fixture.controls.state()
    assert state["charges"] == [] and state["bookings"][0]["status"] == "requires_action"
    assert state["wallet"]["challenges"][0]["attempts"] == 0  # Neither refusal used an attempt.


async def test_wallet_registration_is_private_validated_and_checkout_only():
    config = FixtureConfig(variant="checkout", oracle_token="oracle-token-0123456789")
    async with serve_fixture(config) as fixture, httpx.AsyncClient(trust_env=False) as http:
        url, token = fixture.controls.wallet_url, {"X-Oracle-Token": "oracle-token-0123456789"}
        assert url == fixture.base_url + "/__wallet/register"
        assert (await http.post(url, json=grant())).status_code == 403
        assert (await http.post(url, json=grant(), headers=token | {"Origin": "http://evil"})
                ).status_code == 403
        created = await http.post(url, json=grant(), headers=token)
        assert created.status_code == 201 and created.json()["token_suffix"] == "1b3d"
        assert TOKEN not in created.text
        duplicate = await http.post(url, json=grant(), headers=token)
        assert duplicate.status_code == 409 and duplicate.json()["error"] == "token_exists"
        # Card data has no place here: the wallet registers opaque tokens only.
        for bad in (grant(currency="usd"), grant(amount_minor=0), grant(tolerance_minor=-1),
                    grant(expires_at="2026-11-20T18:00:00"), grant(token="tok_short"),
                    grant() | {"card_number": "4242424242424242"}):
            refused = await http.post(url, json=bad, headers=token)
            assert refused.status_code == 400, bad
        assert (await http.post(url, content=b"[1,", headers=token)).status_code == 400
        twin = fixture.controls.register_payment_token(**grant(token="tok_test_twin_00001"))
        assert twin["token_suffix"] == "0001" and twin["status"] == "unused"
        with pytest.raises(LabError, match="already registered"):
            fixture.controls.register_payment_token(**grant())
        assert len(fixture.controls.state()["wallet"]["tokens"]) == 2
        await http.post(fixture.reset_url, headers=token)
        assert fixture.controls.state()["wallet"]["tokens"] == []  # Each trial needs new tokens.
        # Left unset, the guest's verification code is generated per process, as credentials are.
        assert re.fullmatch(r"\d{6}", fixture.credentials["challenge_code"])
        announced = fixture.controls.announcement()
    assert announced["wallet"] == {"register_url": url, "merchant": "Cedar Stays"}
    assert announced["test_credentials"]["challenge_code"] == fixture.credentials["challenge_code"]
    async with serve_fixture() as plain, httpx.AsyncClient(trust_env=False) as http:
        missing = await http.post(plain.base_url + "/__wallet/register", json=grant())
        with pytest.raises(LabError, match="variant checkout"):
            plain.controls.register_payment_token(**grant())
        state = plain.controls.state()
    assert missing.status_code == 404 and "wallet" not in plain.controls.announcement()
    assert state["wallet"] is None and state["stored_payment_methods"] is None
    assert "challenge_code" not in plain.credentials
    assert "challenge_code" not in ConnectorFixture(CHECKOUT).generated  # Supplied: not announced.
