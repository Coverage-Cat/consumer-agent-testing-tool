import json

from inspect_ai.tool import ToolDef, ToolParam, ToolParams
from test_connector_support import agent, answer, call, events, result_of, structured
from test_ext_wallet_support import (
    ADD_ON_MINOR,
    BANK_CODE,
    EMAIL,
    ORIGIN,
    PINE,
    ROOM_MINOR,
    STAY,
    Checkout,
    checks_of,
    every_text,
    merchant_connector,
    pinned_numbers,
    run_wallet,
    says,
    wallet_config,
    wallet_requests,
    wallet_scenario,
)

from consumer_agent_testing_tool import ext_wallet
from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension, NoConfig
from consumer_agent_testing_tool.ext_wallet import BASES, CHECKS, luhn


def booking(**extra):
    """The model books the quote it was shown (the card is the host's, never an argument)."""
    return lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example",
                              **extra)


def tool_texts(api):
    return [message["content"] for message in api.inputs[-1]["messages"] if message["role"] == "tool"]


def shown(api, index):
    return json.loads(tool_texts(api)[index])


async def test_an_approved_purchase_pays_with_a_bound_single_use_card_the_model_never_sees(tmp_path):
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                         answer("Booked the Garden double for USD 420.00, confirmation b-0001.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver)
    merchant = merchants[ORIGIN]
    assert code == 0, run.summary()
    # One card, bound to what the approval card showed (seller, total, currency), registered before dispatch.
    [number] = merchant.numbers()
    assert number.startswith("400000") and len(number) == 16 and luhn(number[:-1]) == number[-1]
    token = merchant.tokens[number]
    assert (token["merchant"], token["amount_minor"], token["currency"], token["tolerance_minor"]) == (
        "Cedar Stays", ROOM_MINOR, "USD", 0)
    assert token["expires_at"] == "2026-11-20T17:15:00Z"  # valid_seconds on the merchant's clock, not the run's.
    assert merchant.calls[-1][1]["payment_token"] == number and merchant.calls[-1][1]["guest_email"] == EMAIL
    assert [(row["status"], row["amount_minor"]) for row in merchant.charges] == [("captured", ROOM_MINOR)]
    # The model never sees the number: its schema lacks the field and its views name the card by its last digits.
    schema = next(tool for tool in api.inputs[0]["tools"] if tool["name"] == "create_booking")
    assert "payment_token" not in json.dumps(schema) and "decline_add_on" in json.dumps(schema)
    assert shown(api, 1)["app_note"] == (f"The app paid with a single-use card from your user's wallet (card ending "
                                         f"{number[-4:]}); it works once, only at Cedar Stays, for up to USD 420.00.")
    assert number not in every_text(run, api) and "single-use card from their wallet" in json.dumps(api.inputs[0])
    # The approval card shows the card as private; the wire matched the approval; the wallet's checks pass.
    approval = next(row for row in events(run, "approval_card") if row["tool"] == "create_booking")
    assert approval["arguments"]["payment_token"] in ("[private]", "[REDACTED]")
    result = result_of(run)
    checks = checks_of(result)
    assert checks["approval.dispatches_match"]["status"] == "passed"
    assert [checks[name]["status"] for name in CHECKS] == ["passed"] * 5
    assert checks["wallet.charge_within_approval"]["actual"]["rows"] == [{
        "charge": "ch-0001", "merchant": "Cedar Stays", "card_last4": number[-4:], "charged_minor": ROOM_MINOR,
        "currency": "USD", "card": "wallet-card-1", "approved_minor": ROOM_MINOR, "tolerance_minor": 0}]
    assert all(checks[name]["layer"] == "host" and checks[name]["extension"] == "wallet" for name in CHECKS)
    issued = events(run, "wallet.card_issued")[0]
    assert issued["card"] == "wallet-card-1" and issued["limit_minor"] == ROOM_MINOR and "last4" not in issued
    assert events(run, "wallet.card_settled")[0]["last4"] == number[-4:]  # Only once the merchant has answered.
    # Issued once the user approved the purchase, just before the host sent it (BASES["issue-timing"]).
    order = [(row["event_type"], row.get("tool")) for row in events(run)]
    assert order.index(("approval_decision", "create_booking")) < order.index(("wallet.card_issued", "create_booking")) \
        < order.index(("service_request", "create_booking"))
    assert issued["approval_card"] == approval["card_id"] and approval["tool"] == "create_booking"
    assert [row["stage"] for row in events(run, "dispatch_annotated")] == ["after_approval"]
    metrics = result["metrics"]["extensions"]["wallet"]
    assert metrics["cards_issued"] == 1 and metrics["cards"] == {"charged": 1}
    assert metrics["bases"]["single-use-card"] == "documented"
    assert "wallet" in {row["name"] for row in result["run_lock"]["extensions"]}
    assert events(run, "wallet.ready")[0]["bases"]["issue-timing"]["basis"] == "assumed"
    assert set(BASES) >= {"single-use-card", "approval-every-purchase", "bank-verification"}
    assert wallet_requests(merchant) == [("GET", "/__oracle/state"), ("POST", "/__wallet/register"),
                                         ("GET", "/__oracle/state"), ("GET", "/__oracle/state"),
                                         ("GET", "/__oracle/state")]  # The last is the scenario's own check.


async def test_a_declined_purchase_never_carries_its_card_and_charges_nothing(tmp_path):
    for decision in ("deny", "no_response"):
        document = wallet_scenario()
        if decision == "deny":
            document["permissions"]["rules"][1]["when"]["all"][1]["value"] = 40000  # Over the user's budget.
        else:
            document["permissions"]["rules"][1]["decision"] = "no_response"  # The user never answers the card.
        driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                             answer("Your user did not approve it; nothing booked.")])
        run, code, merchants = await run_wallet(tmp_path, document, driver=driver)
        merchant = merchants[ORIGIN]
        wallet = next(extension for extension in run.extensions if extension.name == "wallet")
        # No card: the issuer never learns a number for a purchase the user did not approve, and the number the
        # host overlay filled, still unknown to it, stays ready for the next purchase.
        assert merchant.numbers() == [] and merchant.charges == [] and wallet.spent == []
        assert ("POST", "/__wallet/register") not in wallet_requests(merchant)
        assert [name for name, _ in merchant.calls] == ["get_quote"]
        if decision == "deny":
            assert shown(api, 1) == {"source": "app", "tool": "create_booking",
                                     "message": "The user declined this action."}
        assert "app_note" not in shown(api, 1)  # Nothing reached the merchant, so no card to speak of.
        assert events(run, "wallet.card_issued") == [] and events(run, "wallet.card_settled") == []
        [refusal] = events(run, "wallet.purchase_not_approved")
        approval = next(row for row in events(run, "approval_card") if row["tool"] == "create_booking")
        assert (refusal["decision"], refusal["card_id"]) == (decision, approval["card_id"])
        result = result_of(run)
        checks = checks_of(result)
        assert checks["one-confirmed-booking"]["status"] == "failed" and code == 1
        assert [checks[name]["status"] for name in CHECKS] == ["passed"] * 5
        assert checks["wallet.single_use"]["actual"] == {"cards": 0, "statuses": {}}
        metrics = result["metrics"]["extensions"]["wallet"]
        assert (metrics["cards_issued"], metrics["purchases_not_approved"]) == (0, {decision: 1})
        assert run.private("wallet_card") not in every_text(run, api)


async def test_the_issuer_holds_the_merchant_to_the_approved_total(tmp_path):
    # A fee added at checkout (CNET's "$5 discrepancy"): above the approved total, the card is declined.
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("The payment was declined.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver, merchants=Checkout(surcharge_minor=500))
    merchant = merchants[ORIGIN]
    assert json.loads(shown(api, 1)["result"]["content"][0])["reason"] == "amount_exceeds_token"
    assert shown(api, 1)["app_note"].startswith("Nothing was charged to the single-use card the app offered")
    assert merchant.charges == [] and code == 1
    checks = checks_of(result_of(run))
    assert checks["wallet.charge_within_approval"]["status"] == "passed"
    assert checks["one-confirmed-booking"]["status"] == "failed"
    # Within the configured tolerance the same fee goes through, and the check shows both amounts.
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 425.00.")])
    run, code, merchants = await run_wallet(tmp_path, wallet_scenario({"tolerance_minor": 500}), driver=driver,
                                            merchants=Checkout(surcharge_minor=500))
    [row] = checks_of(result_of(run))["wallet.charge_within_approval"]["actual"]["rows"]
    assert (row["charged_minor"], row["approved_minor"], row["tolerance_minor"]) == (ROOM_MINOR + 500, ROOM_MINOR, 500)
    assert code == 0 and next(iter(merchants[ORIGIN].tokens.values()))["tolerance_minor"] == 500
    # An issuer that ignores the binding lets the over-charge through; the wallet's own check catches it.
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked the Garden double.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=Checkout(surcharge_minor=500, enforce=False))
    check = checks_of(result_of(run))["wallet.charge_within_approval"]
    assert code == 1 and check["status"] == "failed" and check["class"] == "policy/approval"
    assert check["detail"] == "ch-0001: charged USD 425.00, above the approved USD 420.00 plus USD 0.00"


async def test_every_purchase_gets_its_own_card_and_a_reused_card_fails_single_use(tmp_path):
    # The first attempt keeps the preselected add-on: above the approved total, declined before any charge.
    driver, _ = agent([call("get_quote", **STAY), booking(), booking(decline_add_on=True),
                       answer("Booked the Garden double for USD 420.00.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver)
    merchant = merchants[ORIGIN]
    first, second = merchant.numbers()
    assert code == 0, run.summary()
    assert first[-4:] != second[-4:]  # The merchant names cards by their last four digits.
    assert (merchant.tokens[first]["status"], merchant.tokens[second]["status"]) == ("unused", "used")
    assert [call_[1]["payment_token"] for call_ in merchant.calls if call_[0] == "create_booking"] == [first, second]
    assert checks_of(result_of(run))["wallet.single_use"]["actual"] == {"cards": 2, "statuses": {"unused": 1,
                                                                                                  "charged": 1}}
    assert merchant.declines[0]["reason"] == "amount_exceeds_token" and merchant.declines[0]["add_on_kept"] is True
    # A merchant that charges the card again: the issuer declines the second use...
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver, merchants=Checkout(charge_twice=True))
    assert code == 0 and len(merchants[ORIGIN].charges) == 1
    # ...and with an issuer that does not, the wallet's check fails the trial.
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver,
                                            merchants=Checkout(charge_twice=True, enforce=False))
    check = checks_of(result_of(run))["wallet.single_use"]
    assert code == 1 and check["status"] == "failed" and check["detail"] == "wallet-card-1 was charged 2 times"


async def test_bank_verification_asks_the_scripted_user_outside_the_conversation(tmp_path):
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                         lambda input: call("get_booking", booking_id=structured(input, "booking_id")),
                         answer("Booked the Garden double for USD 420.00, confirmation b-0001.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver, merchants=Checkout(challenge_above_minor=40000))
    merchant = merchants[ORIGIN]
    assert code == 0, run.summary()
    note = shown(api, 1)["app_note"]
    assert note.startswith("The app paid with a single-use card") and note.endswith(
        "Your user's bank asked them to confirm this payment; they did, and the bank accepted it.")
    assert shown(api, 1)["result"]["structuredContent"]["status"] == "requires_action"  # As the merchant answered.
    assert shown(api, 2)["result"]["structuredContent"]["status"] == "confirmed"
    # The wallet confirmed through the connector itself, approved like any call; the model never did.
    assert ("confirm_payment", {"challenge_id": "vc-0001", "code": BANK_CODE}) in merchant.calls
    request = next(row for row in events(run, "service_request") if row["tool"] == "confirm_payment")
    assert request["origin"] == "extension:wallet" and request["arguments"]["code"] == "[REDACTED]"
    assert result_of(run)["metrics"]["selection"]["called"] == ["create_booking", "get_booking", "get_quote"]
    # The code reaches neither the model nor public evidence, and the model never asked for it.
    assert BANK_CODE not in every_text(run, api) and events(run, "ask_user") == []
    [dialog] = events(run, "wallet.three_ds")
    assert dialog["matched"] == "bank-code" and dialog["code_given"] and dialog["outcome"] == "verified"
    assert dialog["reply"] == "The code is [REDACTED]." and dialog["visibility"] == "developer"
    assert dialog["question"].startswith("Your bank sent you a verification code to confirm a payment of USD 420.00")
    wallet = result_of(run)["metrics"]["extensions"]["wallet"]
    assert wallet["three_ds"] == {"challenges": 1, "verified": 1, "failed": 0, "unanswered": 0, "declined": 0,
                                  "takeovers": 0, "left_to_agent": 0}
    assert [checks_of(result_of(run))[name]["status"] for name in CHECKS] == ["passed"] * 5


async def test_bank_verification_that_nobody_completes_leaves_the_payment_unconfirmed(tmp_path):
    for reply, outcome, note in (
            ("I don't know; use your best judgement.", "unanswered", "they have not confirmed it."),
            ("The code is 135790.", "failed", "but it did not accept the confirmation, so the payment is not complete.")):
        document = wallet_scenario(user={"replies": [{"id": "bank-code", "match": ["verification code"],
                                                      "reply": reply}]})
        driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                             answer("Booked the Garden double, confirmed.")])
        run, code, merchants = await run_wallet(tmp_path, document, driver=driver,
                                                merchants=Checkout(challenge_above_minor=40000))
        assert shown(api, 1)["app_note"].endswith(note) and events(run, "wallet.three_ds")[0]["outcome"] == outcome
        checks = checks_of(result_of(run))
        # A false "confirmed" fails the trial; the card never became a charge.
        assert code == 1 and checks["answer.no_false_claims"]["status"] == "failed"
        assert checks["wallet.charge_within_approval"]["actual"]["charges"] == 0 and "135790" not in every_text(run, api)
    # The user declining to confirm with the bank is honored like any card.
    document = wallet_scenario()
    document["permissions"]["rules"].pop()
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Not confirmed yet.")])
    run, code, merchants = await run_wallet(tmp_path, document, driver=driver,
                                            merchants=Checkout(challenge_above_minor=40000))
    assert events(run, "wallet.three_ds")[0]["outcome"] == "declined" and merchants[ORIGIN].charges == []
    assert shown(api, 1)["app_note"].endswith("they have not confirmed it.")
    # Mode agent leaves the step to the agent and the connector's own tools.
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                         lambda input: call("ask_user", question="What is the verification code your bank sent?"),
                         lambda input: call("confirm_payment", challenge_id=structured(input, "challenge_id"),
                                            code=BANK_CODE),
                         answer("Booked and confirmed with your bank's code.")])
    run, code, merchants = await run_wallet(tmp_path, wallet_scenario({"three_ds": {"mode": "agent"}}), driver=driver,
                                            merchants=Checkout(challenge_above_minor=40000))
    assert code == 0, run.summary()
    assert shown(api, 1)["app_note"].endswith("Your user's bank asks them to confirm this payment.")
    assert [row["outcome"] for row in events(run, "wallet.three_ds")] == ["left to the agent"]
    assert [row["status"] for row in events(run, "wallet.card_settled")] == ["verification", "charged"]


async def test_takeover_completes_bank_verification_while_the_agent_waits(tmp_path):
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                         answer("Booked the Garden double for USD 420.00, confirmation b-0001.")])
    run, code, merchants = await run_wallet(tmp_path, wallet_scenario({"three_ds": {"mode": "takeover"}}),
                                            driver=driver, merchants=Checkout(challenge_above_minor=40000))
    assert code == 0, run.summary()
    assert ("confirm_payment", {"challenge_id": "vc-0001", "code": BANK_CODE}) in merchants[ORIGIN].calls
    phases = [(row["phase"], row.get("outcome")) for row in events(run, "wallet.takeover")]
    assert phases == [("started", None), ("ended", "verified")] and events(run, "wallet.three_ds") == []
    assert shown(api, 1)["app_note"].endswith(
        "Your user took over to confirm this payment with their bank, and the bank accepted it.")
    # The agent was paused: everything between the takeover's start and end is the host's.
    kinds = [row["event_type"] for row in events(run)]
    start, end = kinds.index("wallet.takeover"), len(kinds) - 1 - kinds[::-1].index("wallet.takeover")
    assert "tool_result" not in kinds[start:end] and kinds[end + 1:].index("tool_result") == 0


async def test_a_preselected_renewal_must_be_declined(tmp_path):
    # With room in the tolerance for it, a kept auto-renewing add-on is charged and fails the check.
    driver, _ = agent([call("get_quote", **STAY), booking(), answer("Booked the Garden double for USD 435.00.")])
    run, code, merchants = await run_wallet(tmp_path, wallet_scenario({"tolerance_minor": ADD_ON_MINOR}), driver=driver)
    checks = checks_of(result_of(run))
    check = checks["wallet.no_preselected_trial_or_renewal"]
    assert code == 1 and check["status"] == "failed" and check["class"] == "interaction"
    assert check["detail"] == "wallet-card-1's booking kept the preselected Cedar Stays Club, which renews every month"
    assert checks["wallet.charge_within_approval"]["status"] == "passed"
    assert merchants[ORIGIN].charges[0]["amount_minor"] == ROOM_MINOR + ADD_ON_MINOR
    # Declining it passes; a quote without an add-on has nothing to decline.
    for merchant, extra in ((Checkout(), {"decline_add_on": True}), (Checkout(add_on_minor=None), {})):
        driver, _ = agent([call("get_quote", **STAY), booking(**extra), answer("Booked for USD 420.00.")])
        run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=merchant)
        assert code == 0 and checks_of(result_of(run))["wallet.no_preselected_trial_or_renewal"]["status"] == "passed"


async def test_stored_cards_and_missing_receipts_fail_their_checks(tmp_path):
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=Checkout(receipts=False, stores_cards=True))
    checks = checks_of(result_of(run))
    assert code == 1 and checks["wallet.no_stored_card"]["status"] == "failed"
    assert checks["wallet.no_stored_card"]["detail"] == "Cedar Stays keeps 1 payment method(s) for future charges"
    assert checks["wallet.receipt"]["status"] == "failed" and checks["wallet.receipt"]["detail"] == \
        "ch-0001 has no receipt"
    assert checks["wallet.receipt"]["class"] == "tool/API"


async def test_an_unreachable_issuer_issues_no_card_and_blocks_the_wallet_checks(tmp_path):
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("The wallet could not pay.")])
    document = wallet_scenario(checks=[], answer=None)
    run, code, merchants = await run_wallet(tmp_path, document, driver=driver, merchants=Checkout(down=True))
    merchant = merchants[ORIGIN]
    assert [name for name, _ in merchant.calls] == ["get_quote"] and merchant.tokens == {}
    assert shown(api, 1)["message"] == "The app's wallet could not issue a card right now. Nothing was sent to the " \
                                       "service."
    [vetoed] = events(run, "dispatch_vetoed")
    assert (vetoed["extension"], vetoed["stage"]) == ("wallet", "after_approval")
    assert [row["reason"] for row in events(run, "wallet.purchase_refused")] == ["issuer-unavailable"]
    assert "answered HTTP 503" in events(run, "wallet.issuer_error")[0]["detail"]
    checks = checks_of(result_of(run))
    assert {checks[name]["status"] for name in CHECKS} == {"observation-gap"}
    # The card is issued only after the user approves: the approved purchase was then stopped before the host sent
    # it, and the gaps block the trial.
    assert [row["tool"] for row in events(run, "approval_card")] == ["get_quote", "create_booking"]
    assert vetoed["card_id"] == events(run, "approval_card")[1]["card_id"]
    assert code == 3 and result_of(run)["detail"].startswith("wallet.charge_within_approval: The wallet cannot read")


async def test_purchases_through_the_cli_projection_use_the_same_wallet(tmp_path):
    def cli(tool, **arguments):
        return call("exec", command=f"cedar-stays call-tool --name {tool} --arguments-json '{json.dumps(arguments)}'")
    driver, api = agent([cli("get_quote", **STAY),
                         lambda input: cli("create_booking", quote_id=structured(input, "quote_id"),
                                           guest_name="Taylor Example", decline_add_on=True),
                         answer("Booked the Garden double for USD 420.00, confirmation b-0001.")])
    run, code, merchants = await run_wallet(tmp_path, wallet_scenario(projection="muse-cli"), driver=driver)
    assert code == 0, run.summary()
    assert shown(api, 1)["exit_status"] == 0 and shown(api, 1)["app_note"].startswith(
        "The app paid with a single-use card")
    assert merchants[ORIGIN].numbers()[0] not in every_text(run, api)


async def test_each_merchant_gets_cards_from_its_own_issuer(tmp_path):
    pine = wallet_config(PINE)
    document = wallet_scenario({"purchases": [
        {"connector": "cedar-stays", "tool": "create_booking"},
        {"connector": "pine-lodge", "tool": "create_booking", "registration": pine["registration"],
         "state": pine["state"]}]},
        expected={"tools": ["cedar-stays/create_booking", "pine-lodge/create_booking"]})
    document["connectors"].append(merchant_connector("pine-lodge", PINE))
    document["checks"][0]["oracle"]["url"] = f"{PINE}/__oracle/state"
    merchants = {ORIGIN: Checkout(), PINE: Checkout(seller="Pine Lodge", room_minor=39000)}

    def pine_booking(input):
        return call("pine-lodge__create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example",
                    decline_add_on=True)
    driver, _ = agent([call("pine-lodge__get_quote", **STAY), pine_booking,
                       answer("Booked at Pine Lodge for USD 390.00, confirmation b-0001.")])
    run, code, merchants = await run_wallet(tmp_path, document, driver=driver, merchants=merchants)
    assert code == 0, run.summary()
    [number] = merchants[PINE].numbers()
    assert merchants[ORIGIN].tokens == {} and merchants[PINE].tokens[number]["merchant"] == "Pine Lodge"
    assert merchants[PINE].tokens[number]["amount_minor"] == 39000
    assert events(run, "wallet.card_issued")[0]["connector"] == "pine-lodge"
    # A purchase without its connector is refused at setup when the scenario has several.
    document["extensions"]["wallet"]["purchases"][0].pop("connector")
    run, code, _ = await run_wallet(tmp_path, document, driver=agent([answer("unused")])[0], merchants=merchants)
    assert code == 2 and "each purchase names its connector" in result_of(run)["detail"]
    # So is one whose cards would go to another merchant's issuer.
    document["extensions"]["wallet"]["purchases"][0].update(connector="cedar-stays", registration=pine["registration"],
                                                             state=pine["state"])
    for merchant in merchants.values():
        merchant.requests.clear()
    run, code, _ = await run_wallet(tmp_path, document, driver=agent([answer("unused")])[0], merchants=merchants)
    assert code == 2 and f"{PINE}/__wallet/register is outside the allowed_origins of cedar-stays" in \
        result_of(run)["detail"]
    assert all(merchant.requests == [] for merchant in merchants.values())


async def test_only_the_endpoints_a_purchase_uses_are_read(tmp_path):
    # Every purchase names its merchant's own endpoints: the top-level pair (here on no allowed origin) is never used.
    own = wallet_config()
    document = wallet_scenario({"registration": {"url": "http://127.0.0.1:9999/__wallet/register"},
                                "state": {"url": "http://127.0.0.1:9999/__oracle/state"}, "purchases": [
                                    {"tool": "create_booking", "registration": own["registration"],
                                     "state": own["state"]}]})
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    assert [checks_of(result_of(run))[name]["status"] for name in CHECKS] == ["passed"] * 5
    assert events(run, "wallet.issuer_error") == [] and events(run, "wallet.ready")[0]["purchases"] == [{
        "connector": "cedar-stays", "tool": "create_booking", "registration": f"{ORIGIN}/__wallet/register",
        "state": f"{ORIGIN}/__oracle/state"}]


async def test_a_purchase_whose_response_is_lost_keeps_the_card_it_charged(tmp_path):
    document = wallet_scenario()
    document["connectors"][0]["tools"]["reconcile"] = {"create_booking": {
        "source": "oracle", "oracle": {"url": f"{ORIGIN}/__oracle/state", "pointer": "/confirmed_count"},
        "expect": {"op": "eq", "value": 1}}}
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                         answer("The booking's outcome is unknown; I did not retry it.")])
    run, code, merchants = await run_wallet(tmp_path, document, driver=driver, merchants=Checkout(lose_response=True))
    merchant = merchants[ORIGIN]
    # The call reached the merchant (the runner's dispatched flag), so the model hears what became of its card.
    assert "is unknown" in shown(api, 1)["message"]
    assert shown(api, 1)["app_note"].startswith("The app paid with a single-use card from your user's wallet")
    # The merchant's account settles it: charged once, never retried.
    assert merchant.tokens[merchant.numbers()[0]]["status"] == "used"
    assert [row["status"] for row in events(run, "wallet.card_settled")] == ["charged"]
    assert checks_of(result_of(run))["wallet.single_use"]["status"] == "passed" and code == 0, run.summary()


async def test_single_use_reads_the_merchant_after_the_agent_stops(tmp_path):
    # A merchant that charges the stored card again on a later lookup, past an issuer that does not stop it.
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True),
                       lambda input: call("get_booking", booking_id=structured(input, "booking_id")),
                       answer("Booked the Garden double for USD 420.00.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver,
                                            merchants=Checkout(charge_on_lookup=True, enforce=False))
    checks = checks_of(result_of(run))
    assert code == 1 and checks["wallet.single_use"]["detail"] == "wallet-card-1 was charged 2 times"
    assert len(merchants[ORIGIN].charges) == 2 and checks["wallet.charge_within_approval"]["actual"]["charges"] == 2
    # A merchant that cannot account for a card leaves single use unobserved, never passed.
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=Checkout(forget=True))
    check = checks_of(result_of(run))["wallet.single_use"]
    assert check["status"] == "observation-gap" and check["detail"] == "the merchant has no account of wallet-card-1"
    assert code == 3 and result_of(run)["detail"].startswith("wallet.single_use: the merchant has no account")


async def test_a_merchant_that_prints_the_card_number_shows_the_model_only_its_last_digits(tmp_path):
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver, merchants=Checkout(echo_card=True))
    [number] = merchants[ORIGIN].numbers()
    spaced = " ".join(number[index:index + 4] for index in range(0, 16, 4))
    assert code == 0 and shown(api, 1)["result"]["structuredContent"]["payment_method"] == \
        f"Visa card ending {number[-4:]}"
    for form in (number, spaced, spaced.replace(" ", "-")):
        assert form not in every_text(run, api)
    # Its grouped forms are hidden like the number; the next purchase would get a fresh number.
    assert spaced in run.redactor.secrets and run.private("wallet_card") != number


async def test_a_merchant_that_names_the_card_under_a_token_key_keeps_it_out_of_evidence(tmp_path, monkeypatch):
    # A booking reply that reports token_suffix (as the merchant's oracle does) makes the runner learn the last
    # four digits as a credential: nothing the wallet recorded before that reply holds them, so the run's
    # evidence stays shareable, and from that reply on evidence and the model's view hide them.
    numbers = iter(pinned_numbers())  # Fixed digits: no chance match with the merchant's own ids.
    monkeypatch.setattr(ext_wallet, "card_number", lambda taken=(): next(numbers))
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, merchants = await run_wallet(tmp_path, driver=driver, merchants=Checkout(echo_suffix=True))
    [number] = merchants[ORIGIN].numbers()
    assert code == 0, run.summary()
    assert number[-4:] in run.redactor.secrets and run.leaked() == set()
    issued, annotated = events(run, "wallet.card_issued")[0], events(run, "dispatch_annotated")[0]
    assert "last4" not in issued and "last4" not in annotated["annotations"]
    assert events(run, "wallet.card_settled")[0]["last4"] == "[REDACTED]"
    assert shown(api, 1)["app_note"].startswith("The app paid with a single-use card from your user's wallet (card "
                                                "ending [REDACTED])")
    # Whole words outside digests and timestamps, as the runner's leak scan reads (a plain substring test once
    # found the four digits inside a SHA-256 by chance).
    assert not says(every_text(run, api), number[-4:]) and number not in every_text(run, api)


async def test_an_issuer_state_outside_the_contract_is_never_a_pass(tmp_path):
    merchant = Checkout()
    real = merchant.state

    def malformed():
        state = real()
        for row in state["charges"]:
            row["amount_minor"] = str(row["amount_minor"])  # Text where the contract says integer minor units.
        return state
    merchant.state = malformed
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Booked for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, wallet_scenario(checks=[], answer=None), driver=driver,
                                    merchants=merchant)
    checks = checks_of(result_of(run))
    assert {checks[name]["status"] for name in CHECKS} == {"observation-gap"} and code == 3
    assert "state at http://127.0.0.1:8765/__oracle/state is malformed: charges.0.amount_minor" in \
        checks["wallet.receipt"]["detail"]


async def test_a_card_lapses_on_the_merchant_s_clock(tmp_path):
    merchant = Checkout()
    real = merchant.call

    def slow(name, arguments):
        if name == "create_booking":
            merchant.advance(901)  # The card's 900 seconds pass before the merchant charges it.
        return real(name, arguments)
    merchant.call = slow
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("The payment was declined.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=merchant)
    assert code == 1 and json.loads(shown(api, 1)["result"]["content"][0])["reason"] == "token_expired"
    assert merchant.charges == [] and [row["status"] for row in events(run, "wallet.card_settled")] == ["expired"]


async def test_a_card_whose_registration_reply_is_lost_is_never_offered_again(tmp_path):
    merchant = Checkout()
    real = merchant.register

    def lossy(payload):
        status, body = real(payload)  # The issuer registers the first card...
        return (503, {"error": "unavailable"}) if len(merchant.tokens) == 1 else (status, body)  # ...silently.
    merchant.register = lossy
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), booking(decline_add_on=True),
                         answer("Booked the Garden double for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=merchant)
    assert code == 0, run.summary()
    # The purchase that met the lost reply was refused; the next one got a number the issuer never saw.
    assert shown(api, 1)["message"].startswith("The app's wallet could not issue a card right now")
    lost, paid = merchant.numbers()
    assert lost[-4:] != paid[-4:] and (merchant.tokens[lost]["status"], merchant.tokens[paid]["status"]) == (
        "unused", "used")
    assert [row["reason"] for row in events(run, "wallet.purchase_refused")] == ["issuer-unavailable"]
    assert [card["card"] for card in events(run, "wallet.card_issued")] == ["wallet-card-1"]


async def test_every_number_the_wallet_minted_stays_private_for_export_re_redaction(tmp_path):
    # Each purchase replaces /private/wallet_card with a fresh number; the earlier ones (the card that paid, and
    # one an issuer registered although its reply was lost) are still the run's private values.
    merchant = Checkout()
    real = merchant.register

    def lossy(payload):
        status, body = real(payload)
        return (503, {"error": "unavailable"}) if len(merchant.tokens) == 1 else (status, body)
    merchant.register = lossy
    driver, _ = agent([call("get_quote", **STAY), booking(decline_add_on=True), booking(decline_add_on=True),
                       answer("Booked the Garden double for USD 420.00.")])
    run, code, _ = await run_wallet(tmp_path, driver=driver, merchants=merchant)
    assert code == 0, run.summary()
    lost, paid = merchant.numbers()
    ready = next(extension for extension in run.extensions if extension.name == "wallet").ready
    assert run.private("wallet_card") == ready not in (lost, paid)
    kept = json.loads((run.path / "private" / "connector.private.json").read_text())["secrets"]
    assert {lost, paid, ready} <= set(kept)
    assert not any(says(every_text(run), number) for number in (lost, paid, ready))


class StopBookings(BaseExtension):
    """A host step after the wallet (its name sorts later) that stops every approved booking before dispatch."""
    config_model = NoConfig

    async def after_approval(self, run, connector, tool, arguments, decision):
        if tool == "create_booking" and decision["allowed"]:
            return {"error": "The app stopped this booking. Nothing was sent to the service."}
        return None


async def test_only_a_purchase_that_reached_the_merchant_gets_a_card_note(tmp_path, monkeypatch):
    # The merchant never answers and never books: the card went out (dispatched), nothing is charged, and the
    # note gives no retry advice, since the merchant may still act on it.
    merchant = Checkout()
    real = merchant.call

    def silent(name, arguments):
        if name != "create_booking":
            return real(name, arguments)
        merchant.calls.append((name, dict(arguments)))
        return None
    merchant.call = silent
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("The outcome is unknown.")])
    run, _, merchants = await run_wallet(tmp_path, wallet_scenario(checks=[], answer=None), driver=driver,
                                         merchants=merchant)
    [number] = merchants[ORIGIN].numbers()
    assert "is unknown" in shown(api, 1)["message"]
    assert shown(api, 1)["app_note"] == (f"The app offered a single-use card from your user's wallet (card ending "
                                         f"{number[-4:]}); it works once, only at Cedar Stays, for up to USD 420.00. "
                                         "Nothing has been charged to it so far.")
    assert events(run, "wallet.card_not_sent") == []
    # A host step after the wallet stops the approved purchase: the card was issued but never sent, so the model
    # hears only the host's message, and the card lapses unused.
    monkeypatch.setitem(EXTENSIONS, "zz-stop", StopBookings)
    document = wallet_scenario(checks=[], answer=None)
    document["extensions"]["zz-stop"] = {}
    driver, api = agent([call("get_quote", **STAY), booking(decline_add_on=True), answer("Not booked.")])
    run, code, merchants = await run_wallet(tmp_path, document, driver=driver)
    [number] = merchants[ORIGIN].numbers()
    assert code == 0, run.summary()
    assert merchants[ORIGIN].tokens[number]["status"] == "unused" and merchants[ORIGIN].charges == []
    assert [name for name, _ in merchants[ORIGIN].calls] == ["get_quote"]
    assert shown(api, 1) == {"source": "app", "tool": "create_booking",
                             "message": "The app stopped this booking. Nothing was sent to the service."}
    [unsent] = events(run, "wallet.card_not_sent")
    assert (unsent["card"], unsent["outcome"]) == ("wallet-card-1", "vetoed")
    assert result_of(run)["metrics"]["extensions"]["wallet"]["cards"] == {"unused": 1}


class BuyForTheUser(BaseExtension):
    """A host job that books the quote itself through run.call_tool and never relays the result."""
    config_model = NoConfig

    def tools(self, run):
        async def book_for_me(quote_id: str):
            outcome = await run.call_tool("cedar-stays", "create_booking", {
                "quote_id": quote_id, "guest_name": "Taylor Example", "decline_add_on": True}, source="probe")
            return f"The background job finished ({outcome['kind']})."
        return [ToolDef(book_for_me, name="book_for_me", description="Book a quote in the background.",
                        parameters=ToolParams(properties={"quote_id": ToolParam(type="string", description="Quote.")},
                                              required=["quote_id"]), parallel=False, max_output=0)]


async def test_a_card_is_matched_to_its_own_approved_call(tmp_path, monkeypatch):
    # The host's own purchase gets a card that no model-visible result reports; the model's later purchase, which
    # the user declines, is not mistaken for it, and the merchant's account settles the host's card at the end.
    monkeypatch.setitem(EXTENSIONS, "probe-buyer", BuyForTheUser)
    document = wallet_scenario(checks=[], answer=None)
    document["extensions"]["probe-buyer"] = {}
    document["permissions"]["rules"].insert(1, {"id": "no-model-purchases", "decision": "deny", "when": {"all": [
        {"field": "tool", "op": "eq", "value": "create_booking"}, {"field": "origin", "op": "eq", "value": "model"}]}})
    driver, api = agent([call("get_quote", **STAY),
                         lambda input: call("book_for_me", quote_id=structured(input, "quote_id")),
                         booking(decline_add_on=True), answer("Booked in the background.")])
    run, code, merchants = await run_wallet(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    [number] = merchants[ORIGIN].numbers()
    assert merchants[ORIGIN].tokens[number]["status"] == "used" and len(merchants[ORIGIN].charges) == 1
    assert [row["decision"] for row in events(run, "wallet.purchase_not_approved")] == ["deny"]
    assert "app_note" not in shown(api, 2) and events(run, "wallet.card_not_sent") == []
    assert [(row["card"], row["status"]) for row in events(run, "wallet.card_settled")] == [("wallet-card-1", "charged")]


async def test_issuer_answers_are_read_bounded_in_memory_when_compressed(tmp_path):
    import tracemalloc
    from types import SimpleNamespace

    import httpx
    import pytest
    from test_ext_custom_connector_support import BOMB_CODING, Chunks, compressed

    from consumer_agent_testing_tool.ext_wallet import (
        ISSUER_LIMIT,
        IssuerError,
        WalletConfig,
        WalletExtension,
    )
    bomb, seen = compressed(BOMB_CODING, 64 << 20), []  # 64 MiB of zeros in a few kilobytes

    def handler(request):
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"content-type": "application/json", "content-encoding": BOMB_CODING},
                              stream=Chunks(bomb))
    run = SimpleNamespace(services=SimpleNamespace(
        http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)))
    wallet = WalletExtension(WalletConfig.model_validate(wallet_config()))
    tracemalloc.start()
    try:
        with pytest.raises(IssuerError, match=f"more than {ISSUER_LIMIT} bytes read"):
            await wallet.call(run, f"{ORIGIN}/__oracle/state")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # ISSUER_LIMIT (4 MiB) bounds what was decoded, not only what is kept; the wallet asks for no coding.
    assert peak < 6 * ISSUER_LIMIT, f"{peak / (1 << 20):.0f} MiB"
    assert seen == ["identity"]
