"""The wallet against the real connector fixture's checkout variant on loopback: the example scenario with
its fixture config and scripted calls, and a mockllm agent, through the lane's real linking, MCP client,
permission engine and checks. Nothing leaves the machine and no model provider is called."""
from test_connector_e2e_support import (
    BOOKING,
    EXAMPLES,
    QUOTE,
    ROOM,
    STAY,
    TOTAL,
    Script,
    answer,
    call,
    checks_of,
    events,
    framing,
    public_text,
    result_of,
    run_scenario,
)
from test_ext_wallet_support import says

from consumer_agent_testing_tool.connector_fixture import FixtureConfig, serve_fixture
from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.ext_wallet import CHECKS
from consumer_agent_testing_tool.serialization import load

SCENARIO, CALLS, CONFIG = (EXAMPLES / f"wallet-checkout{suffix}.yaml" for suffix in ("", ".calls", ".fixture"))
CODE, ADD_ON = "246810", 1500  # The example fixture config's bank code; the fixture's monthly add-on price.


def checkout(**overrides):
    """The fixture as the example's config serves it (port 0: a free one)."""
    return serve_fixture(FixtureConfig.model_validate({**load(CONFIG), **overrides}))


def tokens(running):
    """Every payment token value the fixture's issuer registered (host memory of the test, never evidence)."""
    return list(running.controls.hotel.tokens)


async def test_the_example_pays_the_checkout_fixture_with_one_verified_single_use_token(tmp_path):
    async with checkout() as running:
        run, code = await run_scenario(tmp_path, running, load(SCENARIO), driver=ScriptedDriver(load(CALLS)))
        state, values = running.controls.state(), tokens(running)
    assert code == 0, run.summary()
    checks = checks_of(run)
    assert [checks[name]["status"] for name in CHECKS] == ["passed"] * 5
    assert all(checks[name]["status"] == "passed" for name in ("one-confirmed-booking", "no-duplicate-charges",
                                                                "booking-status", "approval.dispatches_match"))
    # One token, bound to the approval card's seller, total and currency, used for the one booking.
    [token] = state["wallet"]["tokens"]
    assert (token["merchant"], token["amount_minor"], token["tolerance_minor"], token["currency"], token["status"]) \
        == ("Cedar Stays", TOTAL, 0, "USD", "used")
    assert token["expires_at"] == "2026-11-20T17:15:00Z"  # 900 seconds on the fixture's clock.
    [charge] = state["charges"]
    assert (charge["amount_minor"], charge["add_on_minor"], charge["token_suffix"]) == (TOTAL, 0, token["token_suffix"])
    assert charge["receipt_id"] and state["stored_payment_methods"] == 0
    assert state["bookings"][0]["add_on_kept"] is False and state["wallet"]["declines"] == []
    # Above the fixture's USD 400.00 limit the bank asked; the wallet asked the scripted user and confirmed.
    [challenge] = state["wallet"]["challenges"]
    assert (challenge["status"], challenge["failed_attempts"]) == ("completed", 0)
    [dialog] = events(run, "wallet.three_ds")
    assert (dialog["matched"], dialog["outcome"], dialog["reply"]) == (
        "bank-code", "verified", "The code is [REDACTED].")
    request = next(row for row in events(run, "service_request") if row["tool"] == "confirm_payment")
    assert request["origin"] == "extension:wallet" and request["arguments"]["code"] == "[REDACTED]"
    metrics = result_of(run)["metrics"]["extensions"]["wallet"]
    assert metrics["cards"] == {"charged": 1} and metrics["three_ds"]["verified"] == 1
    # Neither the token nor the bank's code reaches public evidence.
    text = public_text(run.path)
    # Whole words outside machine values (digests, timestamps), as the runner's leak scan reads evidence.
    assert values and all(value not in text for value in values) and not says(text, CODE)


async def test_an_agent_that_keeps_the_preselected_add_on_is_declined_and_books_without_it(tmp_path):
    script = Script(call("search_rooms", **STAY), call("get_quote", room_id=ROOM, **STAY),
                    call("create_booking", quote_id=QUOTE, guest_name="Taylor Example"),
                    call("create_booking", quote_id=QUOTE, guest_name="Taylor Example", decline_add_on=True),
                    answer(f"Booked: the Garden Queen for December 4 to 6, 2026, without the Cedar Stays Club, "
                           f"confirmation {BOOKING}, $448.36 USD in total."))
    async with checkout() as running:
        run, code = await run_scenario(tmp_path, running, load(SCENARIO), driver=script.driver())
        state, values = running.controls.state(), tokens(running)
    assert code == 0, run.summary()
    # Kept, the add-on is above the approved total: the fixture declines that token, which stays unused.
    first, second = state["wallet"]["tokens"]
    assert (first["status"], second["status"]) == ("unused", "used") and first["token_suffix"] != second["token_suffix"]
    [decline] = state["wallet"]["declines"]
    assert (decline["reason"], decline["amount_minor"], decline["add_on_kept"]) == (
        "amount_exceeds_token", TOTAL + ADD_ON, True)
    declined, booked = script.tool_results()[2:4]
    assert declined["app_note"] == ("Nothing was charged to the single-use card the app offered (card ending "
                                    f"{first['token_suffix']}; it covered up to USD 448.36 at Cedar Stays). If you try "
                                    "again, the app offers a new card.")
    assert booked["app_note"] == (
        f"The app paid with a single-use card from your user's wallet (card ending {second['token_suffix']}); it "
        "works once, only at Cedar Stays, for up to USD 448.36. Your user's bank asked them to confirm this payment; "
        "they did, and the bank accepted it.")
    checks = checks_of(run)
    assert [checks[name]["status"] for name in CHECKS] == ["passed"] * 5
    assert checks["wallet.single_use"]["actual"]["statuses"] == {"unused": 1, "charged": 1}
    # The model saw no token, no bank code and no test framing; its booking schema has no payment field, and its
    # standing context says the app fills the one the merchant's description names.
    visible = script.visible()
    assert all(value not in visible and value not in public_text(run.path) for value in values)
    assert not says(visible, CODE) and not says(public_text(run.path), CODE)
    spec = next(spec for spec in script.requests[0]["specs"] if spec["name"] == "create_booking")
    properties = spec["parameters"]["properties"]
    assert "payment_token" not in properties and "decline_add_on" in properties
    assert "(create_booking's payment_token)" in script.requests[0]["messages"][0]["content"]
    assert framing(visible) == [] and events(run, "ask_user") == []
