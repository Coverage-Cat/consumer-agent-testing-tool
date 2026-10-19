import json
from typing import Any, ClassVar

import pytest
from inspect_ai.tool import ToolDef, ToolError, ToolParams
from test_connector_multi_support import (
    STAY,
    NoConfig,
    Wallet,
    calls_of,
    multi_services,
    native,
    ready,
    string_parameter,
    tool_messages,
    two_connectors,
)
from test_connector_support import (
    Hotel,
    agent,
    answer,
    call,
    events,
    public_text,
    result_of,
    run_connector,
    scenario,
    services,
    structured,
)

from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    BaseExtension,
    ScriptedDriver,
)
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError, LimitExhausted

EMAIL = "taylor.private@example.com"
CARD = "4242424242424242"


async def test_call_tool_takes_the_model_s_path_but_is_never_the_model_s_proposal(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-wallet", Wallet)
    Wallet.outcomes = []
    hotel = Hotel()
    responses = [call("get_quote", room_id="garden-2", **STAY),
                 lambda input: call("pay_with_wallet", quote_id=structured(input, "quote_id")),
                 answer("Paid with your wallet: $420.00, confirmation BK-1000.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-wallet": {}}), driver=driver,
                                       hotel=hotel)
    assert code == 0, run.summary()
    assert calls_of(hotel) == ["get_quote", "create_booking"] and hotel.calls[1][1]["guest_email"] == EMAIL
    assert Wallet.outcomes[0]["kind"] == "result" and json.loads(tool_messages(api)[1]["content"]) == {
        "outcome": "result"}
    # Same pipeline: a card for the scripted user (the joined quote's total), the dispatch and its evidence.
    card = next(row for row in events(run, "approval_card") if row["tool"] == "create_booking")
    assert card["disclosure"]["total"] == 42000 and card["arguments"]["guest_email"] == "[private]"
    request = next(row for row in events(run, "service_request") if row["tool"] == "create_booking")
    assert request["origin"] == "extension:wallet"
    assert events(run, "extension_call")[0]["origin"] == "extension:wallet"
    response = next(row for row in events(run, "service_response") if row["tool"] == "create_booking")
    assert response["origin"] == "extension:wallet" and response["captured"] == ["booking_id"]
    # Never the model's proposal: selection and argument validity see only get_quote.
    result = result_of(run)
    metrics = result["metrics"]
    assert metrics["selection"]["called"] == ["get_quote"] and metrics["argument_observations"]["proposals"] == 1
    assert metrics["extension_calls"] == 1 and metrics["dispatches"] == 2
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["approval.dispatches_match"]["actual"]["verified"] == 2
    assert checks["approval.disclosure_bound"]["status"] == "passed" and checks["one-confirmed-booking"]["status"] == \
        "passed"


async def test_call_tool_refusals_and_declines(tmp_path):
    run = await ready(tmp_path, scenario(), services(Hotel(total_minor=61000)))
    outcome = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", "adults": "two"}, source="job")
    assert outcome["kind"] == "invalid" and "Invalid arguments for get_quote" in outcome["message"]
    # Over the budget the scripted user approves: declined, nothing dispatched.
    quote = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="job")
    assert quote["kind"] == "result"
    declined = await run.call_tool("cedar-stays", "create_booking", {
        "quote_id": quote["result"]["structuredContent"]["quote_id"], "guest_name": "Taylor Example"}, source="job")
    assert declined == {"kind": "declined", "message": "The user declined this action."}
    assert run.proposals == [] and [row["tool"] for row in run.dispatches] == ["get_quote"]
    for args, message in ((("cedar-stays", "cancel_booking", {"booking_id": "BK-1"}), "not an allowed tool"),
                          (("elsewhere", "get_quote", {}), "No connector named 'elsewhere'"),
                          (("cedar-stays", "get_quote", []), "arguments are a JSON object")):
        with pytest.raises(LabError, match=message):
            await run.call_tool(*args, source="job")
    with pytest.raises(LabError, match="source is a short lowercase name"):
        await run.call_tool("cedar-stays", "get_quote", {}, source="Wallet!")
    run.scenario.limits.tool_calls = 3
    with pytest.raises(LimitExhausted, match="Extension tool-call budget exceeded"):
        await run.call_tool("cedar-stays", "get_booking", {"booking_id": "BK-1"}, source="job")
    run.verifying = True
    with pytest.raises(LabError, match="while the agent runs"):
        await run.call_tool("cedar-stays", "get_booking", {"booking_id": "BK-1"}, source="job")


async def test_call_tool_needs_a_connected_connector(tmp_path):
    run = await ready(tmp_path, two_connectors(pine={"exposure": "disconnected"}), multi_services())
    with pytest.raises(LabError, match="pine-lodge is not connected"):
        await run.call_tool("pine-lodge", "get_quote", {}, source="job")
    assert run.connector_names == ["cedar-stays", "pine-lodge"]
    info = run.connector("pine-lodge")
    assert info.status == "disconnected" and info.exposure == "disconnected" and info.tools == () \
        and info.model_tools == {} and info.summary is None
    assert run.connector().name == "cedar-stays" and run.connector().model_tools["cedar-stays__get_quote"] == "get_quote"
    with pytest.raises(LabError, match="No connector named 'nope'"):
        run.connector("nope")
    with pytest.raises(LabError, match="exposure is in-prompt or search-only"):
        await run.connect("pine-lodge", exposure="disconnected")
    connected = await run.connect("pine-lodge", exposure="search-only", source="router")
    assert connected.status == "connected" and connected.exposure == "search-only"
    assert run.projection.deferred_tool("pine-lodge__get_quote") == ("pine-lodge", "get_quote")
    assert await run.connect("pine-lodge") == run.connector("pine-lodge")  # Connected already: no new link.
    assert len(run.services.linked) == 2 and len(events(run, "connector_connected")) == 1


class Notes(BaseExtension):
    """Queues resume messages and adds checks through the run, and records the hooks it saw."""
    config_model = NoConfig
    seen: ClassVar[list] = []

    async def on_tool_result(self, run, connector, tool, result):
        if tool == "create_booking":
            run.resume_message(f"Email from Cedar Stays to {EMAIL}: your booking is confirmed.", source="notes")
            run.resume_message("Calendar: the stay was added.", source="notes")
            run.add_check({"id": "notes.saw_booking", "status": "passed"}, extension="notes")
        return result

    async def on_idle(self, run):
        Notes.seen.append(("idle", len(run.resumes)))

    async def on_final(self, run, final_text):
        return [{"id": "notes.final", "status": "passed"}]


async def test_resume_messages_are_delivered_first_masked_and_joined(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-notes", Notes)
    Notes.seen = []
    responses = [call("get_quote", room_id="garden-2", **STAY),
                 lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor"),
                 answer("Booked for $420.00."), answer("Booked, and the confirmation email arrived; $420.00.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-notes": {}}), driver=driver)
    assert code == 0, run.summary()
    resumed = [message for message in api.inputs[-1]["messages"] if message["role"] == "user"][-1]["content"]
    # Queued messages resume the agent before any on_idle hook runs; credentials and private values masked.
    assert resumed == "Email from Cedar Stays to [REDACTED]: your booking is confirmed.\n\nCalendar: the stay was added."
    assert EMAIL not in json.dumps(api.inputs)
    assert Notes.seen == [("idle", 0)]  # Only at the second stop, when nothing was queued.
    rows = events(run, "extension_resumed")
    assert [row["extension"] for row in rows] == ["notes", "notes"] and rows[0]["visibility"] == "model"
    ids = [check["id"] for check in result_of(run)["checks"]]
    # Added checks come after on_final's, with the extension's name.
    assert ids[-2:] == ["notes.final", "notes.saw_booking"]
    assert {"id": "notes.saw_booking", "status": "passed", "extension": "notes"} in result_of(run)["checks"]


async def test_add_check_validates_and_a_failed_added_check_fails_the_trial(tmp_path):
    run = await ready(tmp_path, scenario())
    for check, message in (({"id": "x", "status": "fine"}, "takes {id, status"),
                           ({"status": "passed"}, "takes {id, status"),
                           ({"id": "x", "status": "passed", "kind": "y"}, "no kind field"),
                           ({"id": "x", "status": "failed", "class": "reporting"}, "no reporting class"),
                           ({"id": "execution", "status": "passed"}, "is taken"),
                           ({"id": "approval.mine", "status": "passed"}, "is taken"),
                           ({"id": "one-confirmed-booking", "status": "passed"}, "is taken")):
        with pytest.raises(LabError, match=message):
            run.add_check(check)
    assert run.add_check({"id": "routes.first_route", "status": "failed", "detail": "used the phone"},
                         extension="routes")["extension"] == "routes"
    with pytest.raises(LabError, match="is taken"):
        run.add_check({"id": "routes.first_route", "status": "passed"})

    class Failing(ScriptedDriver):
        async def run(self, run):
            run.add_check({"id": "routes.trigger_negative", "status": "failed", "detail": "a connector was used"})
            return "Nothing booked."
    run, code, _ = await run_connector(tmp_path, scenario(checks=[], answer=None), driver=Failing([{"answer": "x"}]))
    assert code == 1 and result_of(run)["detail"] == "routes.trigger_negative: a connector was used"


class Scheduler(BaseExtension):
    """Advances the fixture clock between background jobs and resumes the agent once."""
    config_model = NoConfig

    async def on_idle(self, run):
        clock = run.fixture_clock
        if clock is None or clock.moves:
            return None
        clock.advance(3600, reason="next poll")
        return f"Background check at {clock.now().isoformat()}: the booking is still confirmed."


async def test_the_fixture_clock_is_the_clock_extension_s_and_moves_only_forward(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-scheduler", Scheduler)
    driver, api = agent([answer("Nothing to do."), answer("Still confirmed.")])
    document = scenario(checks=[], answer=None,
                        extensions={"probe-scheduler": {}, "clock": {"now": "2026-11-30T09:00:00-08:00"}})
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    assert [message for message in api.inputs[-1]["messages"] if message["role"] == "user"][-1]["content"] == \
        "Background check at 2026-11-30T10:00:00-08:00: the booking is still confirmed."
    clock = result_of(run)["metrics"]["extensions"]["clock"]
    assert clock["now"] == "2026-11-30T10:00:00-08:00" and clock["advanced_seconds"] == 3600 and clock["moves"] == 1
    assert run.fixture_clock.set("2026-11-30T19:00:00+00:00").isoformat() == "2026-11-30T11:00:00-08:00"
    with pytest.raises(LabError, match="moves forward"):
        run.fixture_clock.advance(-1)
    with pytest.raises(LabError, match="moves forward"):
        run.fixture_clock.set("2026-11-30T08:00:00-08:00")
    assert "Current date and time: Monday, 2026-11-30 11:00" in run.system_prompt()  # It moves the context's time.
    host = await ready(tmp_path, scenario(extensions={}))
    assert host.fixture_clock is None


class Card(BaseExtension):
    """Issues a single-use card number as a host-private value that host_arguments fill."""
    config_model = NoConfig
    private_names = ("wallet_card",)

    def tools(self, run):
        async def issue_card(**kwargs: Any):
            run.register_private("wallet_card", CARD)
            return f"A single-use card ending {CARD[-4:]} is ready; the number is {run.private('wallet_card')}."
        return [ToolDef(issue_card, name="issue_card", description="Issue a single-use card.", parameters=ToolParams(),
                        parallel=False, max_output=0)]


async def test_registered_private_values_fill_overlays_and_never_reach_the_model_or_evidence(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-card", Card)
    hotel = Hotel()
    booking = next(tool for tool in hotel.catalog if tool["name"] == "create_booking")
    booking["inputSchema"]["properties"]["card_number"] = {"type": "string", "description": "Card to charge."}
    document = scenario(extensions={"probe-card": {}})
    document["connectors"][0]["tools"]["host_arguments"]["create_booking"]["card_number"] = {"$ref": "/private/wallet_card"}
    responses = [call("get_quote", room_id="garden-2", **STAY),
                 lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor"),
                 call("issue_card"),
                 lambda input: call("create_booking", quote_id="q-1-garden-2", guest_name="Taylor"),
                 answer("Booked for $420.00 with a single-use card.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, document, driver=driver, hotel=hotel)
    assert code == 0, run.summary()
    told = [message["content"] for message in tool_messages(api)]
    assert "a value it supplies is not available yet" in json.loads(told[1])["message"]  # Before the card exists.
    assert told[2] == "A single-use card ending 4242 is ready; the number is [REDACTED]."
    assert hotel.calls[-1][1]["card_number"] == CARD and calls_of(hotel) == ["get_quote", "create_booking"]
    assert CARD not in json.dumps(api.inputs) and CARD not in public_text(run)
    card = [row for row in events(run, "approval_card") if row["tool"] == "create_booking"][-1]
    assert card["arguments"]["card_number"] in ("[private]", "[REDACTED]")  # Evidence also hides card fields by name.
    assert events(run, "private_registered")[0]["name"] == "wallet_card"
    assert CARD in json.loads((run.path / "private/connector.private.json").read_text())["secrets"]
    # Without the extension that registers it, the reference is an undeclared private value.
    with pytest.raises(LabError, match="/private/wallet_card is not declared under private"):
        parse_scenario({**document, "extensions": {}})
    with pytest.raises(LabError, match="private.wallet_card is registered at run time by an extension"):
        parse_scenario({**document, "private": {**document["private"], "wallet_card": "0000-0000"}})


async def test_private_values_are_host_side_and_registration_fails_closed(tmp_path):
    run = await ready(tmp_path, scenario())
    assert run.private("email") == EMAIL and run.private("missing", None) is None
    with pytest.raises(LabError, match="No host-private value named 'missing'"):
        run.private("missing")
    for name, value, message in (("email", "other@example.com", "is the scenario's"), ("bad name", "abcdef", "identifiers"),
                                 ("pin", "12", "redaction can hide"), ("flag", True, "redaction can hide"),
                                 ("mixed", {"a": "abcdef", "b": None}, "redaction can hide"), ("empty", [], "redaction")):
        with pytest.raises(LabError, match=message):
            run.register_private(name, value)
    run.register_private("loyalty", {"number": 48151623, "tier": "platinum-tier"})
    assert run.private("loyalty") == {"number": 48151623, "tier": "platinum-tier"}
    assert "48151623" in run.redactor.secrets and run.mask({"echo": 48151623}) == {"echo": "[REDACTED]"}


class Leaky(BaseExtension):
    """Extension tools: one echoes host-private data, one raises, one reports an error to the model."""
    config_model = NoConfig

    def tools(self, run):
        async def profile_lookup(**kwargs: Any):
            return {"email": run.private("email"), "note": "x" * 40000}

        async def broken(**kwargs: Any):
            raise RuntimeError("extension bug")

        async def picky(service: str):
            raise ToolError(f"Unknown service {service} for {EMAIL}")
        return [ToolDef(profile_lookup, name="profile_lookup", description=f"Your user's profile ({EMAIL}).",
                        parameters=ToolParams(), parallel=False, max_output=0),
                ToolDef(picky, name="picky", description="Picky.", parameters=string_parameter("service", "Service."),
                        parallel=False, max_output=0),
                ToolDef(broken, name="broken", description="Broken.", parameters=ToolParams(), parallel=False,
                        max_output=0)]


async def test_extension_tool_output_is_masked_bounded_and_a_failure_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-leaky", Leaky)
    driver, api = agent([call("profile_lookup"), call("picky", service="spa"), call("broken"), answer("unused")])
    run, code, hotel = await run_connector(tmp_path, scenario(extensions={"probe-leaky": {}}, checks=[], answer=None),
                                           driver=driver)
    messages = tool_messages(api)
    shown = json.loads(messages[0]["content"])
    assert shown["source"] == "app" and shown["truncated"]["field"] == "message"
    assert len(messages[0]["content"]) <= run.scenario.limits.result_characters
    assert messages[1]["error"]["message"] == "Unknown service spa for [REDACTED]"
    # Descriptions and output alike: the model never sees a host-private value.
    assert EMAIL not in json.dumps(api.inputs) and len(api.inputs) == 3  # The run stopped at broken.
    assert "Your user's profile ([REDACTED])." in json.dumps(api.inputs[0]["tools"])
    assert code == 2 and "RuntimeError" in result_of(run)["detail"] and hotel.calls == []
    assert [row["extension"] for row in events(run, "tool_result")] == ["probe-leaky", "probe-leaky"]


async def test_extension_events_name_their_audience(tmp_path):
    run = await ready(tmp_path, scenario())
    run.event("routes.route_chosen", {"route": "cedar-stays"}, "model", provenance="routes extension")
    row = events(run, "routes.route_chosen")[0]
    assert row["visibility"] == "model" and row["route"] == "cedar-stays" and row["provenance"] == "routes extension"
    for args, message in ((("routes.x", {}, "everyone"), "audiences are developer or model"),
                          (("Routes X", {}), "lowercase names"), (("routes.x", {"kind": "y"}), "without a kind field"),
                          (("routes.x", ["not", "an", "object"]), "must be an object")):
        with pytest.raises(LabError, match=message):
            run.event(*args)


async def test_on_idle_failures_stop_the_run_with_their_own_error(tmp_path, monkeypatch):
    class Crashing(BaseExtension):
        config_model = NoConfig

        async def on_idle(self, run):
            raise LabError("the scheduler lost its jobs", category="setup/infrastructure")
    monkeypatch.setitem(EXTENSIONS, "probe-crashing", Crashing)
    driver, _ = agent([answer("Done.")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-crashing": {}}, checks=[], answer=None),
                                       driver=driver)
    assert code == 2 and result_of(run)["detail"] == "the scheduler lost its jobs"


async def test_scripted_runs_reach_extension_tools_through_the_same_wrapper(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-wallet", Wallet)
    Wallet.outcomes = []
    steps = [native("get_quote", room_id="garden-2", **STAY),
             native("pay_with_wallet", quote_id={"$ref": "/results/0/result/structuredContent/quote_id"}),
             {"answer": "Booked: $420.00, confirmation BK-1000."}]
    run, code, hotel = await run_connector(tmp_path, scenario(extensions={"probe-wallet": {}}),
                                           driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    assert Wallet.outcomes[0]["kind"] == "result" and calls_of(hotel) == ["get_quote", "create_booking"]
    assert events(run, "tool_result")[-1]["extension"] == "probe-wallet"


async def test_resume_messages_are_validated_bounded_and_capped(tmp_path):
    run = await ready(tmp_path, scenario())
    for text in ("", "   ", None, 3):
        with pytest.raises(LabError, match="nonempty text"):
            run.resume_message(text)
    run.resume_message("x" * 9000, source="scheduler")
    queued = run.resumes[-1]
    assert len(queued["text"]) <= 8000 and queued["text"].endswith("more characters]")
    assert queued["source"] == "scheduler"
    assert (await run.idle()).startswith("xxx") and run.resumes == []
    run.counters["idle_resumes"] = 20
    run.resume_message("One more update.")
    assert await run.idle() is None and run.resumes == []
    assert any("1 queued resume message(s) were not delivered" in gap for gap in run.observation_gaps)
