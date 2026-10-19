import json

import pytest
from test_connector_multi_support import (
    PINE,
    PINE_KEY,
    STAY,
    Router,
    calls_of,
    model_tools,
    multi_services,
    native,
    ready,
    tool_messages,
    two_connectors,
)
from test_connector_support import (
    API_KEY,
    ORIGIN,
    Hotel,
    agent,
    answer,
    call,
    events,
    public_text,
    result_of,
    run_connector,
    scenario,
    structured,
)

from consumer_agent_testing_tool.connector_run import EXTENSIONS, ScriptedDriver
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.linking import LinkError

EMAIL = "taylor.private@example.com"


def booking_script(prefix="cedar-stays__", answer_text="Booked: Garden double for $420.00, confirmation BK-1000."):
    return [native(f"{prefix}search_rooms", **STAY),
            native(f"{prefix}get_quote", room_id={"$ref": "/results/0/result/structuredContent/rooms/0/room_id"}, **STAY),
            native(f"{prefix}create_booking", quote_id={"$ref": "/results/1/result/structuredContent/quote_id"},
                   guest_name="Taylor Example"),
            {"answer": answer_text}]


async def test_each_connector_has_its_own_link_client_catalog_and_approvals(tmp_path):
    made = multi_services()
    cedar, pine = made.hotels[ORIGIN], made.hotels[PINE]
    responses = [call("cedar-stays__search_rooms", **STAY), call("pine-lodge__search_rooms", **STAY),
                 call("pine-lodge__get_quote", room_id="loft-5", **STAY),
                 call("cedar-stays__get_quote", room_id="garden-2", **STAY),
                 lambda input: call("cedar-stays__create_booking", quote_id=structured(input, "quote_id"),
                                    guest_name="Taylor Example"),
                 answer("Booked at Cedar Stays: $420.00, confirmation BK-1000.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, two_connectors(), driver=driver, services_=made)
    assert code == 0, run.summary()
    # Native names carry the connector (BASES["connector-prefix"]); the prompt lists both services.
    assert model_tools(api) == [f"{name}__{tool}" for name in ("cedar-stays", "pine-lodge") for tool in (
        "search_rooms", "get_quote", "create_booking", "get_booking")] + ["ask_user"]
    system = api.inputs[0]["messages"][0]["content"]
    assert "- cedar-stays: Cedar Stays" in system and "- pine-lodge: Cedar Stays" in system
    assert calls_of(cedar) == ["search_rooms", "get_quote", "create_booking"]
    assert calls_of(pine) == ["search_rooms", "get_quote"]
    assert cedar.calls[-1][1]["guest_email"] == EMAIL  # The host overlay of the connector that was called.
    # Each connector its own credential and client: neither key reaches the model or public evidence.
    assert [row["endpoint"] for row in made.linked] == [f"{ORIGIN}/mcp", f"{PINE}/mcp"]
    assert made.clients[0].credential.secrets == [API_KEY] and made.clients[1].credential.secrets == [PINE_KEY]
    visible, public = json.dumps(api.inputs), public_text(run)
    assert API_KEY not in visible and PINE_KEY not in visible and API_KEY not in public and PINE_KEY not in public
    # One engine per connector: both number their cards from card-1, and every card names its connector.
    cards = [(row["connector"], row["card_id"], row["tool"]) for row in events(run, "approval_card")]
    assert cards == [("cedar-stays", "card-1", "search_rooms"), ("pine-lodge", "card-1", "search_rooms"),
                     ("pine-lodge", "card-2", "get_quote"), ("cedar-stays", "card-2", "get_quote"),
                     ("cedar-stays", "card-3", "create_booking")]
    assert {row["connector"] for row in events(run, "approval_decision")} == {"cedar-stays", "pine-lodge"}
    result = result_of(run)
    assert result["connector"] is None and result["treatment"] == "native/in-prompt/generic-sdk"
    assert [section["name"] for section in result["connectors"]] == ["cedar-stays", "pine-lodge"]
    pine_section = result["connectors"][1]
    assert pine_section["status"] == "connected" and pine_section["connected"]["by"] == "setup"
    assert pine_section["model_tools"]["pine-lodge__get_quote"] == "get_quote"
    assert set(result["approvals"]) == {"cedar-stays", "pine-lodge"}
    assert result["approvals"]["pine-lodge"]["connector"] == "pine-lodge"
    lock = result["run_lock"]
    assert "server" not in lock and "client_profile" not in lock
    assert lock["connectors"]["pine-lodge"]["exposure"] == "in-prompt"
    assert lock["connectors"]["pine-lodge"]["client_profile"]["name"] == "generic-sdk"
    assert len(lock["connectors"]["cedar-stays"]["server"]["catalog_sha256"]) == 64
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["approval.dispatches_match"]["actual"]["dispatched"] == 5
    assert checks["approval.disclosure_bound"]["status"] == "passed"
    metrics = result["metrics"]
    assert metrics["selection"]["called"] == ["cedar-stays/create_booking", "cedar-stays/get_quote",
                                              "cedar-stays/search_rooms", "pine-lodge/get_quote",
                                              "pine-lodge/search_rooms"]
    assert metrics["selection"]["first_call_expected"] is False and metrics["exposure"]["expected_exposed"] is True
    assert metrics["exposure"]["catalog_tools"] == 10 and metrics["exposure"]["allowed_tools"] == 8
    assert metrics["approval_burden"]["requests"] == 5
    assert metrics["connectors"]["pine-lodge"]["dispatches"] == 2
    assert metrics["connectors"]["cedar-stays"]["approval"]["prompts"]["sensitive-write"] == 1
    projection = events(run, "projection")[0]
    assert projection["connectors"]["pine-lodge"]["exposure"] == "in-prompt"
    assert projection["name_map"]["pine-lodge__get_quote"] == {"connector": "pine-lodge", "tool": "get_quote"}
    assert projection["basis"]["connector-prefix"]["basis"] == "assumed"


async def test_muse_cli_addresses_each_connector_by_its_command_name(tmp_path):
    made = multi_services()
    steps = [{"tool": "exec", "arguments": {"command": "help"}},
             {"tool": "exec", "arguments": {"command": "pine-lodge list-tools --names-only"}},
             {"tool": "exec", "arguments": {"command": ["pine-lodge", "call-tool", "--name", "search_rooms",
                                                        "--arguments-json", STAY]}},
             {"tool": "exec", "arguments": {"command": ["cedar-stays", "call-tool", "--name", "get_quote",
                                                        "--arguments-json", {"room_id": "garden-2", **STAY}]}},
             {"answer": "I compared both hotels; nothing is booked yet."}]
    seen = []

    class Recording(ScriptedDriver):
        async def run(self, run):
            original = run.scripted_call

            async def spy(name, arguments):
                seen.append(json.loads(await original(name, arguments)))
                return json.dumps(seen[-1])
            run.scripted_call = spy
            return await super().run(run)
    document = two_connectors(projection="muse-cli", checks=[], answer=None)
    run, code, _ = await run_connector(tmp_path, document, driver=Recording(steps), services_=made)
    assert code == 0, run.summary()
    assert "usage: cedar-stays list-tools" in seen[0]["stdout"] and "usage: pine-lodge list-tools" in seen[0]["stdout"]
    assert seen[1]["stdout"] == {"tools": ["search_rooms", "get_quote", "create_booking", "get_booking"]}
    assert seen[2]["source"] == "pine-lodge" and seen[3]["source"] == "cedar-stays"
    assert calls_of(made.hotels[PINE]) == ["search_rooms"] and calls_of(made.hotels[ORIGIN]) == ["get_quote"]
    prompt = run.system_prompt()
    assert "- cedar-stays: Cedar Stays" in prompt and "- pine-lodge: Cedar Stays" in prompt
    assert [spec["name"] for spec in run.model_specs()] == ["exec", "ask_user"]


async def test_a_disconnected_connector_joins_only_when_an_extension_connects_it(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-router", Router)
    Router.connected = []
    made = multi_services()
    document = two_connectors(pine={"exposure": "disconnected"}, extensions={"probe-router": {}})
    responses = [call("connect_service", service="pine-lodge"),
                 call("pine-lodge__search_rooms", **STAY),
                 call("cedar-stays__get_quote", room_id="garden-2", **STAY),
                 lambda input: call("cedar-stays__create_booking", quote_id=structured(input, "quote_id"),
                                    guest_name="Taylor Example"),
                 answer("Booked at Cedar Stays for $420.00.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, document, driver=driver, services_=made)
    assert code == 0, run.summary()
    # Before connecting: no pine tools, no pine line in the prompt, never linked at setup.
    assert model_tools(api, 0) == ["cedar-stays__search_rooms", "cedar-stays__get_quote", "cedar-stays__create_booking",
                                   "cedar-stays__get_booking", "ask_user", "connect_service"]
    assert "pine-lodge" not in api.inputs[0]["messages"][0]["content"]
    assert json.loads(tool_messages(api)[0]["content"]) == {
        "connected": True, "service": "pine-lodge", "tools": ["pine-lodge__create_booking", "pine-lodge__get_booking",
                                                             "pine-lodge__get_quote", "pine-lodge__search_rooms"]}
    # From the next turn on, its tools are offered and reach its own service.
    assert "pine-lodge__search_rooms" in model_tools(api, 1)
    assert calls_of(made.hotels[PINE]) == ["search_rooms"]
    assert [row["endpoint"] for row in made.linked] == [f"{ORIGIN}/mcp", f"{PINE}/mcp"]
    connected = events(run, "connector_connected")
    assert len(connected) == 1 and connected[0]["connector"] == "pine-lodge" and connected[0]["by"] == "router"
    assert connected[0]["exposure"] == "in-prompt" and connected[0]["turn"] == 0  # During the first turn.
    assert connected[0]["model_tools"] == ["pine-lodge__search_rooms", "pine-lodge__get_quote",
                                           "pine-lodge__create_booking", "pine-lodge__get_booking"]
    assert connected[0]["basis"]["basis"] == "documented"
    update = events(run, "projection_update")[0]
    assert update["visibility"] == "model" and len(update["model_tools"]) == 4
    assert [row["connector"] for row in events(run, "link")] == ["cedar-stays", "pine-lodge"]
    assert Router.connected[0].status == "connected" and Router.connected[0].tools[0] == "search_rooms"
    result = result_of(run)
    lock = result["run_lock"]["connectors"]["pine-lodge"]
    assert lock["exposure"] == "disconnected" and lock["server"]["tool_count"] == 5  # Joined the lock on connect.
    pine = next(section for section in result["connectors"] if section["name"] == "pine-lodge")
    assert pine["status"] == "connected" and pine["declared_exposure"] == "disconnected"
    assert pine["connected"] == {"by": "router", "turn": 0, "exposure": "in-prompt"}
    assert result["metrics"]["connectors"]["pine-lodge"]["connected"]["by"] == "router"


async def test_a_failed_link_on_connect_is_an_authentication_error_and_leaves_the_connector_out(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-router", Router)
    made = multi_services(link_errors={PINE: LinkError("the test service rejected the key", kind="rejected")})
    document = two_connectors(pine={"exposure": "disconnected"}, extensions={"probe-router": {}})
    responses = [call("connect_service", service="pine-lodge"), call("pine-lodge__search_rooms", **STAY),
                 call("cedar-stays__get_quote", room_id="garden-2", **STAY),
                 lambda input: call("cedar-stays__create_booking", quote_id=structured(input, "quote_id"),
                                    guest_name="Taylor Example"),
                 answer("Pine Lodge could not be connected, so I booked Cedar Stays for $420.00.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, document, driver=driver, services_=made)
    assert code == 0, run.summary()
    assert json.loads(tool_messages(api)[0]["content"]) == {"connected": False, "service": "pine-lodge",
                                                            "reason": "authentication"}
    assert "not found" in tool_messages(api)[1]["error"]["message"]  # Still no pine tool.
    assert all("pine-lodge" not in name for name in model_tools(api, 2))
    assert made.hotels[PINE].calls == []
    failed = events(run, "connector_connect_failed")[0]
    assert failed["class"] == "authentication" and failed["link_error"] == "rejected" and failed["by"] == "router"
    result = result_of(run)
    pine = next(section for section in result["connectors"] if section["name"] == "pine-lodge")
    assert pine["status"] == "failed" and pine["exposure"] == "disconnected"
    assert any(gap.startswith("Connecting pine-lodge failed (authentication, rejected)")
               for gap in result["observation_gaps"])
    assert result["run_lock"]["connectors"]["pine-lodge"]["server"] is None


async def test_scenario_level_tool_checks_and_reconciles_read_the_connector_they_name(tmp_path):
    hotels = {ORIGIN: Hotel(), PINE: Hotel(uncertain_booking=True)}
    made = multi_services(hotels)
    document = two_connectors(checks=[
        {"id": "pine-status", "source": "tool", "connector": "pine-lodge",
         "tool": {"name": "get_booking", "arguments": {"booking_id": "BK-1000"}, "pointer": "/structuredContent/status"},
         "expect": {"op": "eq", "value": "confirmed"}}], answer=None)
    steps = booking_script(prefix="pine-lodge__", answer_text="I am not sure the Pine Lodge booking went through.")
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=made)
    checks = {check["id"]: check for check in result_of(run)["checks"]}
    assert checks["pine-status"]["status"] == "passed" and checks["pine-status"]["connector"] == "pine-lodge"
    # An uncertain write is reconciled per connector; with no reconcile read it stays blocked.
    blocked = checks["reconcile.pine-lodge.create_booking.1"]
    assert blocked["status"] == "blocked" and blocked["connector"] == "pine-lodge" and code == 3
    assert "pine-lodge/create_booking may have taken effect" in blocked["detail"]
    request = [row for row in events(run, "service_request") if row["origin"] == "host-check"]
    assert [(row["connector"], row["tool"]) for row in request] == [("pine-lodge", "get_booking")]
    assert calls_of(hotels[ORIGIN]) == []


async def test_a_single_connector_s_own_exposure_is_the_run_s(tmp_path):
    document = scenario(checks=[], answer=None)
    document["connectors"][0]["exposure"] = "search-only"
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver([
        {"tool": "tool_search", "arguments": {"query": "get_quote"}}, {"answer": "Nothing booked."}]))
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["run_lock"]["exposure"] == "search-only" and "connector_exposure" not in result["run_lock"]
    assert result["treatment"] == "native/search-only/generic-sdk" and result["connector"]["name"] == "cedar-stays"
    assert result["metrics"]["exposure"]["loaded"] == ["get_quote"] and "connectors" not in result["metrics"]


async def test_mixed_exposures_defer_only_the_search_only_connector(tmp_path):
    document = two_connectors(pine={"exposure": "search-only"}, checks=[], answer=None)
    steps = [native("tool_search", query="get_quote"), native("pine-lodge__get_quote", room_id="loft-5", **STAY),
             {"answer": "Pine Lodge quoted $390.00."}]
    made = multi_services()
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=made)
    assert code == 0, run.summary()
    search = events(run, "tool_search")[0]
    # Only the deferred connector is searched; the in-prompt one is in context already.
    assert search["loaded"] == ["pine-lodge__get_quote"] and [row["service"] for row in search["results"]] == [
        "pine-lodge"]
    prompt = run.system_prompt()
    assert "Connected services:\n- cedar-stays:" in prompt
    assert "(load their tools with tool_search before calling them; until then a tool shows only its name and " \
           "description):\n- pine-lodge:" in prompt
    assert calls_of(made.hotels[PINE]) == ["get_quote"]
    assert result_of(run)["run_lock"]["connectors"]["pine-lodge"]["exposure"] == "search-only"


async def test_an_exposure_treatment_never_connects_a_disconnected_connector(tmp_path):
    document = two_connectors(pine={"exposure": "disconnected"}, checks=[], answer=None)
    document["connectors"][0]["exposure"] = "in-prompt"
    made = multi_services()
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver([{"answer": "Nothing to do."}]),
                                       services_=made, exposure="search-only")
    assert code == 0, run.summary()
    lock = result_of(run)["run_lock"]
    assert lock["exposure"] == "search-only" and lock["overrides"] == {"exposure": "search-only"}
    assert lock["connectors"]["cedar-stays"]["exposure"] == "search-only"
    assert lock["connectors"]["pine-lodge"]["exposure"] == "disconnected" and len(made.linked) == 1


async def test_reset_takes_one_url_per_test_service(tmp_path):
    hotels = {ORIGIN: Hotel(), PINE: Hotel()}
    for hotel in hotels.values():
        hotel.bookings.append({"booking_id": "BK-old", "status": "confirmed"})
    document = two_connectors(reset=[f"{ORIGIN}/__oracle/reset", f"{PINE}/__oracle/reset"], checks=[], answer=None)
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver([{"answer": "Nothing to do."}]),
                                       services_=multi_services(hotels))
    assert code == 0, run.summary()
    assert [row["url"] for row in events(run, "oracle_reset")] == [f"{ORIGIN}/__oracle/reset", f"{PINE}/__oracle/reset"]
    assert all(hotel.bookings == [] for hotel in hotels.values())


async def test_dispatch_and_disclosure_checks_name_each_connector_s_requests_and_cards(tmp_path):
    class Rewriting:  # A client that sends other arguments than the ones approved (pine-lodge only).
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def call_tool(self, name, arguments):
            reply = await self.inner.call_tool(name, arguments)
            if name == "get_quote":
                reply = reply.model_copy(update={"sent_arguments": {**reply.sent_arguments, "adults": 3}})
            return reply
    made = multi_services()
    original = made.make_client

    def make_client(profile, endpoint, credential, **options):
        client = original(profile, endpoint, credential, **options)
        return Rewriting(client) if endpoint.startswith(PINE) else client
    made.make_client = make_client
    steps = [native("pine-lodge__get_quote", room_id="loft-5", **STAY),
             native("cedar-stays__get_quote", room_id="garden-2", **STAY), {"answer": "Two quotes."}]
    run, code, _ = await run_connector(tmp_path, two_connectors(checks=[], answer=None),
                                       driver=ScriptedDriver(steps), services_=made)
    check = next(item for item in result_of(run)["checks"] if item["id"] == "approval.dispatches_match")
    assert code == 1 and check["status"] == "failed"
    assert check["actual"]["mismatched_requests"] == ["pine-lodge/1"] and check["actual"]["verified"] == 1


async def test_a_single_disconnected_connector_keeps_p1_names_once_connected(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-router", Router)
    document = scenario(checks=[], answer=None, extensions={"probe-router": {}})
    document["connectors"][0]["exposure"] = "disconnected"
    steps = [native("connect_service", service="cedar-stays"), native("search_rooms", **STAY),
             {"answer": "Two rooms are free."}]
    made = multi_services()
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=made)
    assert code == 0, run.summary()
    assert calls_of(made.hotels[ORIGIN]) == ["search_rooms"]
    result = result_of(run)
    lock = result["run_lock"]
    assert lock["connector_exposure"] == "disconnected" and lock["server"]["tool_count"] == 5
    assert result["connector"]["name"] == "cedar-stays" and result["connector"]["allowed_tools"][0] == "search_rooms"
    assert events(run, "connector_connected")[0]["model_tools"][0] == "search_rooms"


async def test_connecting_again_after_a_failed_link_starts_over(tmp_path):
    made = multi_services(link_errors={PINE: LinkError("the authorization was revoked", kind="revoked")})
    run = await ready(tmp_path, two_connectors(pine={"exposure": "disconnected"}), made)
    with pytest.raises(LinkError, match="revoked") as raised:
        await run.connect("pine-lodge", source="router")
    assert raised.value.category == "authentication" and run.connector("pine-lodge").status == "failed"
    retry = multi_services()  # The same services, now linking (the user fixed the authorization).
    run.services.link, run.services.make_client = retry.link, retry.make_client
    info = await run.connect("pine-lodge", source="router")
    assert info.status == "connected" and info.connected["by"] == "router"
    assert [row["connector"] for row in events(run, "connector_connect_failed")] == ["pine-lodge"]
    assert run.run_lock["connectors"]["pine-lodge"]["server"]["tool_count"] == 5
    for args, message in (({"name": "pine-lodge", "source": "Router"}, "source is a short lowercase name"),):
        with pytest.raises(LabError, match=message):
            await run.connect(**args)
    run.verifying = True
    with pytest.raises(LabError, match="works while the agent runs"):
        await run.connect("cedar-stays")


async def test_a_connector_whose_server_drops_a_named_tool_fails_to_connect_with_a_tool_error(tmp_path):
    hotels = {ORIGIN: Hotel(), PINE: Hotel()}
    hotels[PINE].catalog = [tool for tool in hotels[PINE].catalog if tool["name"] != "get_quote"]
    run = await ready(tmp_path, two_connectors(pine={"exposure": "disconnected"}), multi_services(hotels))
    with pytest.raises(LabError, match="The scenario names tools the pine-lodge server does not list: get_quote") \
            as raised:
        await run.connect("pine-lodge", source="router")
    assert raised.value.category == "tool/API" and raised.value.code == 1
    assert "pine-lodge" not in run.projection.connectors and run.connections["pine-lodge"].closed
    failed = events(run, "connector_connect_failed")[0]
    assert failed["class"] == "tool/API" and failed["link_error"] is None


async def test_a_setup_failure_names_the_connector_and_seals_every_connector_s_state(tmp_path):
    made = multi_services(link_errors={PINE: LinkError("PINE_LODGE_KEY is not set", kind="missing")})
    run, code, _ = await run_connector(tmp_path, two_connectors(), services_=made,
                                       driver=ScriptedDriver([{"answer": "unused"}]))
    result = result_of(run)
    assert code == 2 and result["failure_stage"] == "link" and result["failure_class"] == "authentication"
    assert result["checks"][0]["detail"] == "pine-lodge: PINE_LODGE_KEY is not set"
    assert result["checks"][0]["link_error"] == "missing"
    assert [(section["name"], section["status"]) for section in result["connectors"]] == [
        ("cedar-stays", "connected"), ("pine-lodge", "failed")]
    assert result["run_lock"]["connectors"]["cedar-stays"]["server"] is not None
