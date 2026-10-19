"""Address verification (P2.4): the connector fixture's checkout variant with avs: true takes the billing postal
code and declines a mismatch with reason avs_mismatch; the wallet fills the code from a private value as a host
overlay (never the model's) and wallet.address_verification reports the issuer's verdict. The example scenario
runs scripted against the real fixture on loopback."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_connector_e2e_support import EXAMPLES, TOTAL, checks_of, events, result_of, run_scenario
from test_connector_fixture_tools import mcp_session, tool_map
from test_ext_wallet_support import ORIGIN, wallet_config

from consumer_agent_testing_tool.connector_fixture import (
    AVS_BASIS,
    ConnectorFixture,
    FixtureConfig,
    ToolFailure,
    serve_fixture,
)
from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.ext_wallet import (
    ADDRESS_CHECK,
    CHECKS,
    Card,
    MerchantState,
    WalletConfig,
    WalletExtension,
)
from consumer_agent_testing_tool.serialization import load

SCENARIO, CONFIG = EXAMPLES / "wallet-checkout-avs.yaml", EXAMPLES / "wallet-checkout-avs.fixture.yaml"
CALLS = EXAMPLES / "wallet-checkout.calls.yaml"  # The plain example's script: the host adds the postal code.
POSTAL = "94110"  # The example's private billing postal code, and the code its fixture config keeps on file.
STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}


async def test_avs_is_a_checkout_option_and_its_tool_takes_the_billing_postal_code():
    with pytest.raises(ValueError, match="applies to the checkout variant"):
        FixtureConfig(variant="good", avs=True)
    with pytest.raises(ValueError, match="avs_postal_code"):
        FixtureConfig(variant="checkout", avs=True, avs_postal_code="<script>")
    async with serve_fixture(FixtureConfig(variant="checkout", avs=True)) as fixture, \
            mcp_session(fixture.mcp_url) as session:
        tools = tool_map((await session.list_tools()).tools)
    booking = tools["create_booking"]
    assert "billing_postal_code" in booking.input_schema["required"]
    assert "verifies billing_postal_code" in booking.description
    async with serve_fixture(FixtureConfig(variant="checkout")) as fixture, mcp_session(fixture.mcp_url) as session:
        plain = tool_map((await session.list_tools()).tools)
    assert "billing_postal_code" not in plain["create_booking"].input_schema["properties"]
    assert AVS_BASIS["basis"] == "assumed"


def test_the_issuer_declines_a_mismatch_and_charges_a_match():
    fixture = ConnectorFixture(FixtureConfig(variant="checkout", avs=True, avs_postal_code="SW1A 1AA"))
    hotel, state = fixture.hotel, fixture.state
    expires = (datetime.fromisoformat(state()["clock"]) + timedelta(minutes=15))
    for token in ("tok_avs_mismatch_1", "tok_avs_match_2"):
        fixture.register_payment_token(token=token, merchant="Cedar Stays", amount_minor=TOTAL, currency="USD",
                                       expires_at=expires.astimezone(UTC).isoformat())
    quote = hotel.call("get_quote", "guest", None, {"room_id": "rm-garden-queen", **STAY})
    booking = {"quote_id": quote["quote_id"], "guest_name": "Taylor Example", "guest_email": "t@example.com",
               "decline_add_on": True}
    with pytest.raises(ToolFailure) as declined:
        hotel.call("create_booking", "guest", None, {**booking, "payment_token": "tok_avs_mismatch_1",
                                                     "billing_postal_code": "94110"})
    assert declined.value.payload["reason"] == "avs_mismatch"
    assert "billing postal code must match" in declined.value.payload["message"]
    booked = hotel.call("create_booking", "guest", None, {**booking, "payment_token": "tok_avs_match_2",
                                                          "billing_postal_code": "sw1a-1aa"})  # Case, spacing aside.
    assert booked["status"] == "confirmed"
    snapshot = state()
    assert [row["reason"] for row in snapshot["wallet"]["declines"]] == ["avs_mismatch"]
    assert [row["avs"] for row in snapshot["charges"]] == ["match"]
    assert snapshot["wallet"]["address_verification"] is True and snapshot["basis"]["avs"] == AVS_BASIS
    # The oracle never reports the code on file or the one the merchant received.
    assert "SW1A" not in json.dumps(snapshot).upper() and "94110" not in json.dumps(snapshot)


@pytest.mark.parametrize("change, message", [
    (lambda tools: tools["host_arguments"]["create_booking"].pop("billing_postal_code"),
     "needs host_arguments that fill billing_postal_code"),
    (lambda tools: tools["host_arguments"]["create_booking"].update(billing_postal_code={"$ref": "/private/email"}),
     "needs host_arguments that fill billing_postal_code")])
async def test_the_wallet_refuses_billing_the_model_could_fill(tmp_path, change, message):
    document = load(SCENARIO)
    change(document["connectors"][0]["tools"])
    async with serve_fixture(FixtureConfig.model_validate(load(CONFIG))) as running:
        run, code = await run_scenario(tmp_path, running, document, driver=ScriptedDriver(load(CALLS)))
    assert code == 2 and message in checks_of(run)["execution"]["detail"]


async def test_billing_must_name_a_declared_private_value(tmp_path):
    document = load(SCENARIO)
    document["extensions"]["wallet"]["purchases"][0]["billing"]["private"] = "zip"
    async with serve_fixture(FixtureConfig.model_validate(load(CONFIG))) as running:
        run, code = await run_scenario(tmp_path, running, document, driver=ScriptedDriver(load(CALLS)))
    assert code == 2 and "billing names private.zip" in checks_of(run)["execution"]["detail"]


async def test_the_example_verifies_the_host_filled_postal_code_and_pays(tmp_path):
    async with serve_fixture(FixtureConfig.model_validate(load(CONFIG))) as running:
        run, code = await run_scenario(tmp_path, running, load(SCENARIO), driver=ScriptedDriver(load(CALLS)))
        state = running.controls.state()
    assert code == 0, run.summary()
    checks = checks_of(run)
    assert [checks[name]["status"] for name in (*CHECKS, ADDRESS_CHECK)] == ["passed"] * 6
    assert checks[ADDRESS_CHECK]["actual"]["cards"] == [
        {"card": "wallet-card-1", "merchant": "Cedar Stays", "result": "match"}]
    assert [row["avs"] for row in state["charges"]] == ["match"] and state["confirmed_count"] == 1
    # The model was never offered the field and never saw the value; evidence hides it as a private value.
    [projection] = events(run, "projection")
    tool = next(spec for spec in projection["model_tools"] if spec["name"] == "create_booking")
    assert "billing_postal_code" not in tool["parameters"]["properties"]
    assert POSTAL not in json.dumps(events(run, "tool_result")) and POSTAL not in json.dumps(events(run))
    assert result_of(run)["metrics"]["extensions"]["wallet"]["address_verification"] == {"match": 1}


async def test_a_mismatch_declines_the_purchase_and_the_wallet_check_reports_it(tmp_path):
    async with serve_fixture(FixtureConfig.model_validate({**load(CONFIG), "avs_postal_code": "10001"})) as running:
        run, code = await run_scenario(tmp_path, running, load(SCENARIO), driver=ScriptedDriver(load(CALLS)))
        state = running.controls.state()
    assert code == 1
    checks = checks_of(run)
    address = checks[ADDRESS_CHECK]
    assert (address["status"], address["class"]) == ("failed", "verification")
    assert address["actual"]["cards"][0]["result"] == "mismatch"
    assert "does not match Cedar Stays's card issuer's record" in address["detail"]
    assert [row["reason"] for row in state["wallet"]["declines"]] == ["avs_mismatch"] and state["charges"] == []
    assert checks["one-confirmed-booking"]["status"] == "failed"
    # The model learns the payment was declined and that the same billing details would be declined again.
    told = json.loads(events(run, "tool_result")[-1]["content"])
    assert "could not verify the billing postal code" in told["app_note"]
    assert result_of(run)["metrics"]["extensions"]["wallet"]["address_verification"] == {"mismatch": 1}


async def test_a_merchant_that_never_verifies_the_postal_code_is_an_observation_gap(tmp_path):
    # The AVS scenario against the plain checkout fixture (avs off): the card was charged, never address-verified.
    plain = FixtureConfig.model_validate(load(EXAMPLES / "wallet-checkout.fixture.yaml"))
    async with serve_fixture(plain) as running:
        run, code = await run_scenario(tmp_path, running, load(SCENARIO), driver=ScriptedDriver(load(CALLS)))
        state = running.controls.state()
    assert state["wallet"]["address_verification"] is False and [row.get("avs") for row in state["charges"]] == [None]
    address = checks_of(run)[ADDRESS_CHECK]
    assert code == 3, run.summary()
    assert (address["status"], address["class"]) == ("observation-gap", "observation gap")
    assert address["actual"]["cards"] == [{"card": "wallet-card-1", "merchant": "Cedar Stays",
                                           "result": "not verified"}]
    assert "Cedar Stays does not verify billing postal codes (wallet.address_verification is false" in address["detail"]
    assert "wallet-card-1 was charged without the issuer verifying its billing postal code" in address["detail"]


def test_only_a_verified_charge_or_an_unused_card_passes_the_address_check():
    url = f"{ORIGIN}/__oracle/state"

    def judged(verifies, avs, status="charged"):
        wallet = WalletExtension(WalletConfig.model_validate(wallet_config()))
        wallet.verified = {url}
        wallet.cards = [Card("wallet-card-1", "4000000000002559", "cedar-stays", "create_booking",
                             f"{ORIGIN}/__wallet/register", url, "Cedar Stays", TOTAL, "USD", 0, status=status,
                             avs=avs)]
        row = {"merchant": "Cedar Stays", **({} if verifies is None else {"address_verification": verifies})}
        state = MerchantState.model_validate({"clock": "2026-11-30T17:00:00Z", "stored_payment_methods": 0,
                                              "wallet": row})
        check = wallet.address_check({url: state})
        return check["status"], check.get("detail")

    assert judged(True, "match") == ("passed", None)
    assert judged(True, None, status="unused") == ("passed", None)  # Declined before the issuer compared: no charge.
    assert judged(True, "mismatch", status="unused")[0] == "failed"
    # A merchant that claims to verify, then charges without a verdict; one whose state does not say it verifies.
    assert judged(True, None) == (
        "observation-gap", "wallet-card-1 was charged without the issuer verifying its billing postal code")
    missing = "Cedar Stays does not verify billing postal codes (wallet.address_verification is missing in its state)"
    assert judged(None, "match") == ("observation-gap", missing)
