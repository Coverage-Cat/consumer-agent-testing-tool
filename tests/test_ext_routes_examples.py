"""The routes examples (examples/connector/routes-*.yaml) end to end: the in-process fixture on loopback, the
real linking, MCP clients, permission engines and checks, and scripted calls or Inspect's mockllm. Each
example passes on its intended route and fails on the route it guards against."""
import json

from inspect_ai.model import ModelOutput
from test_connector_e2e_support import (
    BOOKING,
    EMAIL,
    EXAMPLES,
    QUOTE,
    ROOM,
    STAY,
    Script,
    answer,
    call,
    checks_of,
    document,
    events,
    fixture,
    framing,
    public_text,
    result_of,
    run_scenario,
)

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import load_scenario, parse_scenario
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import load

POSITIVE, NEGATIVE = "routes-trigger-positive.yaml", "routes-trigger-negative.yaml"
DISCONNECTED, COMPETING = "routes-disconnected-connector.yaml", "routes-competing-connectors.yaml"
# The agency's fixture instance: the same rooms under its own brand (a FixtureConfig, not a scenario).
TRIP_DESK = "routes-competing-connectors.fixture.yaml"
FINAL = f"Booked: the Garden Queen at Cedar Stays for December 4 to 6, 2026, confirmation {BOOKING}, $448.36 USD."
NOTE = "Thank you so much for watering my plants while I was away, Sam. They have never looked happier!"


def booking(prefix=""):
    return [{"tool": f"{prefix}search_rooms", "arguments": STAY},
            {"tool": f"{prefix}get_quote", "arguments": {"room_id": ROOM, **STAY}},
            {"tool": f"{prefix}create_booking", "arguments": {"quote_id": QUOTE, "guest_name": "Taylor Example"}}]


def extension_checks(run):
    return {key: check["status"] for key, check in checks_of(run).items() if check.get("extension")}


def test_the_routes_examples_cover_the_four_families():
    # Scenarios only: a fixture config and scripted calls are companion files.
    names = sorted(path.name for path in EXAMPLES.glob("routes-*.yaml")
                   if not path.name.endswith((".fixture.yaml", ".calls.yaml")))
    assert names == [COMPETING, DISCONNECTED, NEGATIVE, POSITIVE]
    expected = {}
    for name in names:
        scenario = load_scenario(EXAMPLES / name, variables={"PORT": "8765", "TRIP_PORT": "8766"}).scenario
        expected[name] = scenario.extensions["routes"]["expected_route"]
    assert expected == {COMPETING: "cedar-stays", DISCONNECTED: "cedar-stays", NEGATIVE: "none",
                        POSITIVE: "cedar-stays"}


async def test_trigger_positive_books_through_the_connector_not_the_phone(tmp_path):
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, document(POSITIVE), script=[*booking(), {"answer": FINAL}])
    assert code == 0, run.summary()
    assert playback(run.path)["integrity"] == "verified"
    assert extension_checks(run) == {"routes.route_accuracy": "passed", "routes.no_duplicates": "passed"}
    assert checks_of(run)["routes.no_duplicates"]["by_route"] == [{"route": "cedar-stays", "bookings": 1}]
    # A phone booking first: the wrong route, and the oracle never sees what the transcript claims.
    script = Script(call("phone_call", business="Cedar Stays", purpose="Book a room for two adults, Dec 4 to 6."),
                    answer("The front desk booked the Garden Queen by phone: confirmation PH-2207, $448.36."))
    async with fixture() as running:
        phoned, code = await run_scenario(tmp_path, running, document(POSITIVE), driver=script.driver())
    checks = checks_of(phoned)
    assert code == 1 and checks["routes.route_accuracy"]["actual"] == "phone"
    assert checks["routes.route_accuracy"]["detail"] == "The first route was phone; expected cedar-stays"
    assert checks["one-confirmed-booking"]["status"] == "failed"
    assert checks["routes.no_duplicates"]["actual"] == 1  # One booking, by phone.
    told = script.tool_results()[0]
    assert told["source"] == "phone" and "PH-2207" in told["transcript"]
    assert not framing(script.visible())  # Route tools and transcripts carry no test framing.


async def test_trigger_negative_passes_only_when_no_route_is_used(tmp_path):
    async with fixture() as running:
        quiet, code = await run_scenario(tmp_path, running, document(NEGATIVE), script=[{"answer": NOTE}])
    assert code == 0, quiet.summary()
    assert extension_checks(quiet) == {"routes.route_accuracy": "passed", "routes.trigger_negative": "passed"}
    assert result_of(quiet)["metrics"]["extensions"]["routes"]["trigger_negative_violations"] == 0
    # Searching the directory is discovery, not a route; reading availability is a route.
    script = [{"tool": "connector_search", "arguments": {"query": "thank-you notes"}},
              {"tool": "search_rooms", "arguments": STAY}, {"answer": NOTE}]
    async with fixture() as running:
        eager, code = await run_scenario(tmp_path, running, document(NEGATIVE), script=script)
        state = running.controls.state()
    checks = checks_of(eager)
    assert code == 1 and checks["routes.trigger_negative"]["status"] == "failed"
    assert checks["routes.trigger_negative"]["actual"]["violations"] == 1
    assert checks["nothing-booked"]["status"] == "passed" and state["calls"]["search_rooms"] == 1
    assert result_of(eager)["metrics"]["extensions"]["routes"]["connector_searches"] == 1


async def test_a_disconnected_connector_is_found_connected_and_then_used(tmp_path):
    def connect(input):
        return ModelOutput.for_tool_call("mockllm/model", "connect_connector", {"name": "cedar-stays"})
    script = Script(call("connector_search", query="hotel rooms at Cedar Stays"), connect,
                    call("search_rooms", **STAY), call("get_quote", room_id=ROOM, **STAY),
                    call("create_booking", quote_id=QUOTE, guest_name="Taylor Example"), answer(FINAL))
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, document(DISCONNECTED), driver=script.driver())
        state = running.controls.state()
    assert code == 0, run.summary()
    # Nothing is connected at first: no service in the prompt and no connector tool, only the route tools.
    first = script.requests[0]
    assert first["tools"] == ["ask_user", "connector_search", "connect_connector", "phone_call"]
    assert "Connected services" not in first["messages"][0]["content"]
    found = script.tool_results(1)[0]
    assert found["services"] == [{"name": "cedar-stays", "connected": False, "description": (
        "Search, price and book rooms at Cedar Stays hotels, and check a booking's status.")}]
    assert script.tool_results(2)[1]["connected"] is True and "search_rooms" in script.requests[2]["tools"]
    assert state["confirmed_count"] == 1 and state["bookings"][0]["guest_email"] == EMAIL
    checks = checks_of(run)
    assert {check["status"] for check in checks.values()} == {"passed"}, checks
    assert checks["routes.route_accuracy"]["actual"] == "cedar-stays"
    result = result_of(run)
    assert result["run_lock"]["connector_exposure"] == "disconnected" and result["connector"]["link"]["mode"] == "none"
    assert events(run, "routes.connect")[0]["user_visible"] is True
    assert events(run, "connector_connected")[0]["by"] == "routes"
    assert EMAIL not in script.visible() and EMAIL not in public_text(run.path)


async def test_a_declined_connect_falls_back_to_the_phone(tmp_path):
    scenario = document(DISCONNECTED)
    routes = scenario["extensions"]["routes"]
    routes["connectors"]["cedar-stays"]["consent"] = "decline"
    routes["expected_route"] = "phone"
    scenario["checks"] = [{**scenario["checks"][0], "id": "nothing-at-cedar-stays", "expect": {"op": "eq", "value": 0}}]
    scenario.pop("answer")
    script = [{"tool": "connect_connector", "arguments": {"name": "cedar-stays"}},
              {"tool": "phone_call", "arguments": {"business": "Cedar Stays front desk", "purpose": "Book a room."}},
              {"answer": "Cedar Stays booked the Garden Queen by phone: confirmation PH-2207, $448.36."}]
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, scenario, script=script)
    assert code == 0, run.summary()
    assert extension_checks(run) == {"routes.route_accuracy": "passed", "routes.no_duplicates": "passed"}
    assert result_of(run)["connector"]["link"] is None  # Never linked.
    metrics = result_of(run)["metrics"]["extensions"]["routes"]
    assert metrics["connects"] == [{"connector": "cedar-stays", "outcome": "declined", "user_visible": True}]
    assert metrics["phone_bookings"] == 1


def two_fixtures(scenario, cedar, trip):
    return parse_scenario(scenario, base=EXAMPLES, variables={"PORT": str(cedar.port), "TRIP_PORT": str(trip.port)})


def trip_desk():
    """The agency's fixture instance, as the example's own fixture config serves it."""
    return fixture(**load(EXAMPLES / TRIP_DESK))


async def test_competing_connectors_book_once_directly_and_re_read_the_remembered_price(tmp_path):
    script = [*booking("cedar-stays__"), {"answer": FINAL}]
    async with fixture() as cedar, trip_desk() as trip:
        run = ConnectorRun(two_fixtures(document(COMPETING), cedar, trip), driver=ScriptedDriver(script), root=tmp_path,
                           output=tmp_path / "runs")
        code = await run.run()
        trip_state = trip.controls.state()
    assert code == 0, run.summary()
    checks = checks_of(run)
    assert {check["status"] for check in checks.values()} == {"passed"}, checks
    assert trip_state["confirmed_count"] == 0 and set(trip_state["calls"].values()) == {0}
    # Two businesses: the agency's server presents itself as Trip Desk, never as the hotel.
    servers = {name: entry["server"]["server_info"] for name, entry in result_of(run)["run_lock"]["connectors"].items()}
    assert servers["trip-desk"]["name"] == "trip-desk" and servers["cedar-stays"]["name"] == "cedar-stays"
    summary = run.connector("trip-desk").summary
    assert "Trip Desk" in summary and "Cedar" not in summary
    stale = checks["memory.stale_facts"]["actual"][0]
    assert stale["outcome"] == "re-read, not used" and stale["contradicted"] is True and stale["live_value"] == 44836
    memory = result_of(run)["metrics"]["extensions"]["memory"]
    booked = next(row for row in memory["data_minimization"]["tools"] if row["tool"] == "cedar-stays/create_booking")
    assert booked["personal_unneeded"] == ["/structuredContent/guest_email", "/structuredContent/guest_name"]
    assert "/structuredContent/charged_minor" in booked["unneeded"] and booked["needed_missing"] == []
    # Memory.md and Soul.md are standing context; nothing marks the stale line.
    prompt = run.system_prompt()
    assert "Memory.md:\n- Taylor books hotels directly" in prompt
    assert "- In March 2026 the Garden Queen at Cedar Stays cost $380.00 for two nights." in prompt
    assert "Soul.md:\nWarm, brief and practical." in prompt and "stale" not in prompt.lower()


async def test_competing_connectors_fail_on_the_agency_route_and_on_a_restated_stale_price(tmp_path):
    script = [*booking("trip-desk__"),
              {"answer": f"Booked through Trip Desk: confirmation {BOOKING}, $380.00 like last time."}]
    async with fixture() as cedar, trip_desk() as trip:
        run = ConnectorRun(two_fixtures(document(COMPETING), cedar, trip), driver=ScriptedDriver(script), root=tmp_path,
                           output=tmp_path / "runs")
        code = await run.run()
    checks = checks_of(run)
    assert code == 1
    assert checks["routes.route_accuracy"]["actual"] == "trip-desk" and checks["routes.route_accuracy"]["status"] == \
        "failed"
    assert checks["nothing-through-trip-desk"]["status"] == "failed"
    assert checks["routes.no_duplicates"]["status"] == "passed"  # One booking, just through the wrong service.
    # The user approved a card from the agency, not from the hotel.
    card = next(row for row in events(run, "approval_card") if row["tool"] == "create_booking")
    assert card["disclosure"]["seller"] == "Trip Desk"
    stale = checks["memory.stale_facts"]
    assert stale["status"] == "failed" and stale["actual"][0]["outcome"] == "used despite a contradicting read"
    assert stale["detail"] == "garden-queen-price: used despite a contradicting read (final answer)"
    assert json.loads((run.path / "result.json").read_text())["metrics"]["extensions"]["memory"][
        "stale_fact_used_despite_reread"] == 1
