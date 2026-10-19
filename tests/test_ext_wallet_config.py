import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_connector_multi_support import ready
from test_connector_support import agent, answer, call, events, result_of, structured
from test_ext_wallet_support import (
    EMAIL,
    ORIGIN,
    STAY,
    Checkout,
    checks_of,
    run_wallet,
    wallet_config,
    wallet_requests,
    wallet_scenario,
    wallet_services,
)

from consumer_agent_testing_tool.connector_run import EXTENSIONS, ScriptedDriver, discover
from consumer_agent_testing_tool.connector_scenario import load_scenario, parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_wallet import (
    CHECKS,
    WalletConfig,
    WalletExtension,
    card_number,
    luhn,
    money,
    raw_result,
)
from consumer_agent_testing_tool.serialization import load

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"
EXAMPLE, CALLS = EXAMPLES / "wallet-checkout.yaml", EXAMPLES / "wallet-checkout.calls.yaml"


def test_wallet_config_is_strict_and_fails_closed():
    parsed = WalletConfig.model_validate(wallet_config())
    assert (parsed.tolerance_minor, parsed.valid_seconds) == (0, 900)
    assert (parsed.three_ds.mode, parsed.three_ds.tool) == ("ask", "confirm_payment")
    registration, state = f"{ORIGIN}/__wallet/register", f"{ORIGIN}/__oracle/state"
    for bad, message in (
            ({"registration": {"url": "http://issuer.example/__wallet/register"}},
             "plain http is allowed only for loopback"),
            ({"registration": {"url": f"{registration}?key=1"}}, "must not contain a query"),
            ({"state": {"url": "http://user:pw@127.0.0.1:8765/__oracle/state"}}, "must not embed credentials"),
            ({"registration": registration}, "valid dictionary or instance"),
            ({"issuer": {"url": f"{ORIGIN}/__wallet"}}, "Extra inputs are not permitted"),  # No issuer API of its own.
            ({"purchases": []}, "at least 1 item"),
            ({"purchases": [{"tool": "create_booking"}, {"tool": "create_booking"}]}, "lists a tool twice"),
            ({"purchases": [{"tool": "create_booking", "connector": "Cedar Stays"}]}, "should match pattern"),
            ({"purchases": [{"tool": "create_booking", "registration": {"url": registration}}]},
             "names both registration and state of its merchant, or neither"),
            ({"purchases": [{"tool": "create_booking", "state": {"url": f"{state}#x"}}]}, "must not contain a query"),
            ({"tolerance_minor": -1}, "greater than or equal to 0"), ({"tolerance_minor": "500"}, "valid integer"),
            ({"valid_seconds": 30}, "greater than or equal to 60"),
            ({"three_ds": "ask"}, "valid dictionary or instance of Verification"),
            ({"three_ds": {"mode": "skip"}}, "'ask', 'takeover' or 'agent'"),
            ({"three_ds": {"tool": ""}}, "at least 1 character"),
            ({"card": "4242424242424242"}, "Extra inputs are not permitted")):
        with pytest.raises(ValidationError, match=message):
            WalletConfig.model_validate(wallet_config(**bad))


def test_the_wallet_is_discovered_and_provides_its_card_to_scenarios(tmp_path):
    assert discover("wallet") is WalletExtension and EXTENSIONS["wallet"] is WalletExtension
    loaded = parse_scenario(wallet_scenario(), base=tmp_path)
    assert loaded.scenario.extensions["wallet"]["purchases"] == [{"tool": "create_booking"}]
    document = wallet_scenario()
    with pytest.raises(LabError, match="/private/wallet_card is not declared under private"):
        parse_scenario({**document, "extensions": {"clock": document["extensions"]["clock"]}})
    with pytest.raises(LabError, match="private.wallet_card is registered at run time by an extension"):
        parse_scenario({**document, "private": {**document["private"], "wallet_card": "4000001234567899"}})
    with pytest.raises(LabError, match=r"Invalid extensions.wallet: .*three_ds"):
        parse_scenario(wallet_scenario({"three_ds": "skip"}))


def broken(change):
    document = wallet_scenario()
    change(document, document["connectors"][0]["tools"])
    return document


@pytest.mark.parametrize("change, message", [
    (lambda d, t: t["host_arguments"]["create_booking"].pop("payment_token"),
     "create_booking needs host_arguments that fill its payment field with {$ref: /private/wallet_card}"),
    (lambda d, t: t["disclosure"]["create_booking"].pop("total"),
     "binds each card to what its approval card shows: declare disclosure total for create_booking"),
    (lambda d, t: d["extensions"]["wallet"].update(registration={"url": "http://127.0.0.1:9999/__wallet/register"}),
     "http://127.0.0.1:9999/__wallet/register is outside the allowed_origins of cedar-stays"),
    (lambda d, t: d["extensions"]["wallet"]["purchases"][0].update(
        registration={"url": f"{ORIGIN}/__wallet/register"}, state={"url": "https://bank.example/__oracle/state"}),
     "https://bank.example/__oracle/state is outside the allowed_origins of cedar-stays"),
    (lambda d, t: t["allow"].remove("confirm_payment"),
     ("three_ds mode ask confirms the bank's code with confirm_payment: add it to the tools.allow of cedar-stays, "
      "or use mode agent")),
    (lambda d, t: t.update(reconcile={"create_booking": {
        "source": "tool", "expect": {"op": "eq", "value": "confirmed"},
        "tool": {"name": "get_booking", "pointer": "/structuredContent/status",
                 "arguments": {"booking_id": {"$ref": "/private/wallet_card"}}}}}),
     "reconcile.create_booking must not send /private/wallet_card"),
    (lambda d, t: t["host_arguments"].update(get_booking={"booking_id": {"$ref": "/private/wallet_card"}}),
     "host_arguments.get_booking of cedar-stays names /private/wallet_card, but only a wallet purchase"),
    (lambda d, t: d["checks"].append({"id": "probe", "source": "tool", "expect": {"op": "eq", "value": 1},
                                      "tool": {"name": "get_booking", "pointer": "/x",
                                               "arguments": {"booking_id": {"$ref": "/private/wallet_card"}}}}),
     "check probe must not send /private/wallet_card"),
    (lambda d, t: t["classes"].update(create_booking="write"),
     "pays only through sensitive writes, which are approved on every use; create_booking is classified write"),
    (lambda d, t: d["extensions"]["wallet"].update(purchases=[{"tool": "create_booking", "connector": "pine"}]),
     "names 'pine', which this scenario does not declare"),
    (lambda d, t: (t["allow"].remove("create_booking"), t["classes"].clear(), t["host_arguments"].clear(),
                   t["disclosure"].clear(), t["capture"].clear()),
     "create_booking is not in tools.allow"),
])
async def test_setup_refuses_a_wallet_that_could_overpay_or_leak_its_card(tmp_path, change, message):
    driver = ScriptedDriver([{"answer": "unused"}])
    run, code, merchants = await run_wallet(tmp_path, broken(change), driver=driver)
    assert code == 2 and message in result_of(run)["detail"]
    assert result_of(run)["failure_stage"] == "project" and merchants[ORIGIN].requests == []


async def test_a_connector_connected_later_is_classified_before_the_wallet_pays(tmp_path):
    document = wallet_scenario()
    document["connectors"][0]["exposure"] = "disconnected"
    document["connectors"][0]["tools"]["classes"] = {"create_booking": "write"}
    run = await ready(tmp_path, document, wallet_services(Checkout()))
    assert run.connector().status == "disconnected"  # Setup could not see its classes.
    await run.connect("cedar-stays")
    with pytest.raises(LabError, match="create_booking is classified write"):
        await run.extensions[-1].before_dispatch(run, "cedar-stays", "create_booking", {"payment_token": "x"})


async def test_the_wallet_card_never_reaches_another_tool(tmp_path):
    # A scenario value that happens to use a run-time name the wallet derives is never replaced.
    document = wallet_scenario(private={"email": EMAIL, "wallet_card_forms_1": "the-scenario-s-own"})
    run = await ready(tmp_path, document, wallet_services(Checkout()))
    wallet = next(extension for extension in run.extensions if extension.name == "wallet")
    number = run.private("wallet_card")
    assert number == wallet.ready and number in run.redactor.secrets
    assert run.private("wallet_card_forms_1") == "the-scenario-s-own" and run.private("wallet_card_forms_2") == [
        " ".join(number[index:index + 4] for index in range(0, 16, 4)),
        "-".join(number[index:index + 4] for index in range(0, 16, 4))]
    verdict = await wallet.before_dispatch(run, "cedar-stays", "get_booking", {"booking_id": f"b-1 {number}"})
    assert verdict == {"error": "The app does not share your user's wallet card with this tool. Nothing was sent to "
                                "the service."}
    assert await wallet.before_dispatch(run, "cedar-stays", "get_booking", {"booking_id": "b-1"}) is None
    # A purchase whose arguments lack the ready card (an overlay that resolved elsewhere) gets no card.
    refused = await wallet.before_dispatch(run, "cedar-stays", "create_booking", {"quote_id": "q-1"})
    assert refused["error"].startswith("The app could not attach a card from your user's wallet")
    # Unknown purchase terms (no quote the call names) bind nothing: no card, nothing registered.
    refused = await wallet.before_dispatch(run, "cedar-stays", "create_booking", {"quote_id": "q-9",
                                                                                  "payment_token": number})
    assert refused["error"].startswith("The app's wallet could not confirm this purchase's merchant, total and")
    assert wallet.cards == [] and run.services.merchants[ORIGIN].requests == []


def test_card_numbers_money_and_result_shapes():
    numbers = {card_number() for _ in range(50)}
    assert len(numbers) == 50 and all(len(item) == 16 and item.startswith("400000") for item in numbers)
    assert all(luhn(item[:-1]) == item[-1] for item in numbers) and luhn("7992739871") == "3"
    assert card_number(taken=numbers) not in numbers
    assert (money(44900, "USD"), money(5, "EUR"), money(1200, "JPY")) == ("USD 449.00", "EUR 0.05", "JPY 1200")
    native = {"source": "s", "result": {"isError": False, "content": ["{\"a\": 1}"], "structuredContent": {"a": 1}}}
    assert raw_result(native)["content"] == [{"type": "text", "text": "{\"a\": 1}"}]
    assert raw_result({"exit_status": 0, "stdout": {"structuredContent": {"a": 1}}}) == {"structuredContent": {"a": 1}}
    assert raw_result({"source": "app", "message": "The user declined this action."}) is None


async def test_the_example_scenario_runs_through_the_wallet(tmp_path):
    # The example and its scripted calls against the in-memory merchant (test_ext_wallet_fixture.py runs the
    # real fixture with the example's fixture config).
    loaded = load_scenario(EXAMPLE, variables={"PORT": "8765"})
    wallet = loaded.scenario.extensions["wallet"]
    assert (wallet["registration"], wallet["state"], wallet["three_ds"]) == (
        {"url": f"{ORIGIN}/__wallet/register"}, {"url": f"{ORIGIN}/__oracle/state"},
        {"mode": "ask", "tool": "confirm_payment"})
    from consumer_agent_testing_tool.connector_run import ConnectorRun
    merchant = Checkout(challenge_above_minor=40000)  # As the example's fixture config: the bank asks.
    run = ConnectorRun(loaded, driver=ScriptedDriver(load(CALLS)), root=tmp_path, output=tmp_path / "runs",
                       services=wallet_services(merchant))
    code = await run.run()
    assert code == 0, run.summary()
    checks = checks_of(result_of(run))
    assert [checks[name]["status"] for name in CHECKS] == ["passed"] * 5
    assert all(checks[name]["status"] == "passed" for name in ("one-confirmed-booking", "booking-status"))
    assert ("POST", "/__wallet/register") in wallet_requests(merchant)
    [(_, arguments)] = [row for row in merchant.calls if row[0] == "create_booking"]
    assert arguments["decline_add_on"] is True and arguments["payment_token"] == merchant.numbers()[0]
    assert merchant.challenges[0]["status"] == "completed" and merchant.charges[0]["receipt_id"]


async def test_an_agent_given_the_wallet_context_is_never_asked_for_card_details(tmp_path):
    driver, api = agent([call("get_quote", **STAY),
                         lambda input: call("create_booking", quote_id=structured(input, "quote_id"),
                                            guest_name="Taylor Example", decline_add_on=True),
                         answer("Booked for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver)
    system = api.inputs[0]["messages"][0]["content"]
    assert code == 0 and system.endswith("When your user approves a purchase, the app pays with a single-use card from "
                                         "their wallet and fills in the card details itself (create_booking's "
                                         "payment_token); you never see, ask for or type card numbers or payment "
                                         "tokens.")
    # The payment field is the host's: no model-visible schema has it, and the model never asked the user.
    assert "payment_token" not in json.dumps(api.inputs[0]["tools"]) and events(run, "ask_user") == []
