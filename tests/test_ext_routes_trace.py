"""The route trace from the runner's own records: run.tool_calls for the routes the agent used (a custom
connection's http_request, a search-only tool called before tool_search loaded it), the run's events for
what it committed, run.oracle's shared snapshot for the duplicate count, and the route checks when the
agent stopped without a final answer."""
import pytest
from test_connector_e2e_support import STAY, fixture
from test_connector_multi_support import ready
from test_connector_support import (
    ORIGIN,
    agent,
    answer,
    call,
    events,
    result_of,
    run_connector,
    scenario,
)
from test_ext_custom_connector_support import ENV, EXAMPLES, KEY, QUOTE, document, service

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError

CUSTOM = "custom:cedar-stays-api"  # the route of the custom-booking example's service
GRANT = {"tool": "request_api_access", "arguments": {"service": "cedar-stays-api", "scheme": "api_key",
                                                     "placement": "header", "name": "X-Cedar-Key"}}
NOTHING_BOOKED = {"id": "nothing-booked", "source": "oracle", "expect": {"op": "eq", "value": 0},
                  "oracle": {"url": f"{ORIGIN}/__oracle/state", "pointer": "/confirmed_count"}}


def checks_of(run):
    return {check["id"]: check for check in result_of(run)["checks"]}


def routes_metrics(run):
    return result_of(run)["metrics"]["extensions"]["routes"]


def custom_scenario(routes, *, connector=False, **overrides):
    """The custom-booking example (the REST service at ${PORT}) with the routes extension and, with
    `connector`, a Cedar Stays MCP connector at ${MCP_PORT} beside it."""
    def change(scenario_):
        scenario_["extensions"]["routes"] = routes
        scenario_["answer"] = None
        if connector:
            scenario_["connectors"] = [{"name": "cedar-stays", "endpoint": "http://127.0.0.1:${MCP_PORT}/mcp",
                                        "allowed_origins": ["http://127.0.0.1:${MCP_PORT}"], "auth": {"mode": "none"}}]
        scenario_.update(overrides)
    return document(change)


def booking_steps(base, first=()):
    """`first`, then access, a quote and a booking through the custom connection, and an answer."""
    at = len(first)  # the grant's step
    keyed = {"X-Cedar-Key": {"$ref": f"/results/{at}/reference"}}
    return [*first, GRANT,
            {"tool": "http_request", "arguments": {"method": "POST", "url": f"{base}/v1/quotes", "headers": keyed,
                                                   "json": QUOTE}},
            {"tool": "http_request", "arguments": {
                "method": "POST", "url": f"{base}/v1/bookings", "headers": keyed,
                "json": {"quote_id": {"$ref": f"/results/{at + 1}/json/quote_id"}, "guest_name": "Taylor Example"}}},
            {"answer": "Booked at Cedar Stays."}]


async def trial(tmp_path, scenario_, steps, **ports):
    loaded = parse_scenario(scenario_, base=EXAMPLES, path=EXAMPLES / "custom-booking.yaml",
                            variables={name: str(port) for name, port in ports.items()})
    run = ConnectorRun(loaded, driver=ScriptedDriver(steps), root=tmp_path, output=tmp_path / "runs")
    return run, await run.run()


async def test_a_custom_connection_s_requests_are_the_route_custom_service(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    routes = {"expected_route": CUSTOM, "duplicates": {"count": [
        {"route": CUSTOM, "url": "http://127.0.0.1:${PORT}/__oracle/state", "pointer": "/confirmed_count"}]}}
    async with service() as running:
        run, code = await trial(tmp_path, custom_scenario(routes), booking_steps(running.base_url), PORT=running.port)
    assert code == 0, run.summary()
    checks = checks_of(run)
    accuracy = checks["routes.route_accuracy"]
    assert accuracy["status"] == "passed" and accuracy["actual"] == CUSTOM and accuracy["routes_used"] == [CUSTOM]
    assert checks["routes.no_duplicates"]["by_route"] == [{"route": CUSTOM, "bookings": 1}]
    metrics = routes_metrics(run)
    assert (metrics["first_route"], metrics["commit_route"], metrics["route_accuracy"]) == (CUSTOM, CUSTOM, True)
    # The access request is no route step; the two requests are (their places in run.tool_calls).
    assert metrics["steps"] == [{"route": CUSTOM, "via": "custom_connector", "tool": "http_request", "sequence": place}
                                for place in (2, 3)]


async def test_the_commit_judge_finds_the_write_a_custom_connection_reported(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    async def judged(judge):
        routes = {"expected_route": CUSTOM, "judge": judge, "duplicates": {"count": [
            {"route": "cedar-stays", "url": "http://127.0.0.1:${MCP_PORT}/__oracle/state", "pointer": "/confirmed_count"},
            {"route": CUSTOM, "url": "http://127.0.0.1:${PORT}/__oracle/state", "pointer": "/confirmed_count"}]}}
        async with fixture() as hotel, service() as api:
            # A look at the hotel's connector first, then the booking through its API.
            steps = booking_steps(api.base_url, first=[{"tool": "search_rooms", "arguments": STAY}])
            run, code = await trial(tmp_path, custom_scenario(routes, connector=True), steps, PORT=api.port,
                                    MCP_PORT=hotel.port)
            return run, code, hotel.controls.state()
    run, code, hotel = await judged("commit")
    assert code == 0, run.summary()
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert accuracy["actual"] == CUSTOM and accuracy["routes_used"] == ["cedar-stays", CUSTOM]
    assert hotel["calls"]["search_rooms"] == 1 and hotel["confirmed_count"] == 0
    metrics = routes_metrics(run)
    assert (metrics["first_route"], metrics["commit_route"]) == ("cedar-stays", CUSTOM)
    assert checks_of(run)["routes.no_duplicates"]["by_route"] == [{"route": "cedar-stays", "bookings": 0},
                                                                  {"route": CUSTOM, "bookings": 1}]
    run, code, _ = await judged("first")
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert code == 1 and accuracy["detail"] == f"The first route was cedar-stays; expected {CUSTOM}"


async def test_an_unrelated_request_must_not_reach_for_a_custom_api(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    unrelated = {"checks": [{**NOTHING_BOOKED, "oracle": {"url": "http://127.0.0.1:${PORT}/__oracle/state",
                                                          "pointer": "/confirmed_count"}}]}
    note = {"answer": "Thanks for watering my plants, Sam!"}
    async with service() as running:
        # Asking the user for access is a prompt, like a connect, even when no request follows.
        asked, code = await trial(tmp_path, custom_scenario({"expected_route": "none"}, **unrelated), [GRANT, note],
                                  PORT=running.port)
        assert code == 1 and checks_of(asked)["routes.route_accuracy"]["status"] == "passed"
        negative = checks_of(asked)["routes.trigger_negative"]
        assert negative["actual"]["first"] == [{"route": CUSTOM, "via": "request_api_access", "sequence": 1}]
        me = {"tool": "http_request", "arguments": {"method": "GET", "url": f"{running.base_url}/v1/me",
                                                    "headers": {"X-Cedar-Key": {"$ref": "/results/0/reference"}}}}
        used, code = await trial(tmp_path, custom_scenario({"expected_route": "none"}, **unrelated), [GRANT, me, note],
                                 PORT=running.port)
    checks = checks_of(used)
    assert code == 1 and checks["routes.route_accuracy"]["detail"] == f"The first route was {CUSTOM}; expected none"
    assert checks["routes.trigger_negative"]["actual"]["violations"] == 2
    assert checks["routes.trigger_negative"]["detail"] == ("The agent used routes for a request none serves: "
                                                           f"{CUSTOM} (request_api_access), {CUSTOM} (http_request)")
    assert routes_metrics(used)["trigger_negative_violations"] == 2


async def test_a_custom_route_needs_the_custom_connection_it_names(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, code = await trial(tmp_path, custom_scenario({"expected_route": "custom:pine-lodge-api"}),
                                [{"answer": "unused"}], PORT=running.port)
    assert code == 2 and (f"names the route custom:pine-lodge-api, but the scenario enables no custom_connector for that "
                          f"service (its route is {CUSTOM})") in checks_of(run)["execution"]["detail"]
    counted = {"duplicates": {"count": [{"route": CUSTOM, "url": f"{ORIGIN}/__oracle/state", "pointer": "/n"}]}}
    with pytest.raises(LabError, match=f"names the route {CUSTOM}, but the scenario enables no custom_connector"):
        await ready(tmp_path, scenario(extensions={"routes": counted}))


async def test_a_custom_service_named_without_its_prefix_is_refused_with_its_route(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    counted = {"duplicates": {"count": [{"route": "cedar-stays-api", "url": "http://127.0.0.1:${PORT}/__oracle/state",
                                         "pointer": "/confirmed_count"}]}}
    async with service() as running:
        for routes, field in (({"expected_route": "cedar-stays-api"}, "expected_route names an undeclared connector"),
                              (counted, "duplicates.count names undeclared connectors:")):
            run, code = await trial(tmp_path, custom_scenario(routes), [{"answer": "unused"}], PORT=running.port)
            detail = checks_of(run)["execution"]["detail"]
            assert code == 2 and field in detail and "cedar-stays-api" in detail
            assert detail.endswith(f"; the custom connection's route is {CUSTOM}")


async def test_a_search_only_tool_called_before_it_was_loaded_still_chose_its_route(tmp_path):
    document_ = scenario(exposure="search-only", checks=[NOTHING_BOOKED], answer=None,
                         extensions={"routes": {"expected_route": "cedar-stays"}})
    steps = [{"tool": "get_quote", "arguments": {"room_id": "garden-2", **STAY}},
             {"answer": "I could not quote the room yet."}]
    run, code, hotel = await run_connector(tmp_path, document_, driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    assert hotel.calls == []  # Nothing reached the service, but the agent reached for that connector.
    assert checks_of(run)["routes.route_accuracy"]["actual"] == "cedar-stays"
    assert routes_metrics(run)["steps"] == [{"route": "cedar-stays", "via": "native", "tool": "get_quote",
                                             "sequence": 1, "deferred": True}]


async def test_the_duplicate_count_reads_the_snapshot_the_scenario_s_checks_read(tmp_path):
    state = f"{ORIGIN}/__oracle/state"
    routes = {"expected_route": "cedar-stays",
              "duplicates": {"count": [{"route": "cedar-stays", "url": state, "pointer": "/confirmed_count"}]}}
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"routes": routes}))
    assert code == 0, run.summary()
    assert checks_of(run)["routes.no_duplicates"]["actual"] == 1
    # One independent read per oracle URL in a trial (run.oracle): every check judges the same state.
    assert [row["url"] for row in events(run, "oracle_snapshot")].count(state) == 1


async def test_route_checks_are_reported_when_the_agent_stops_without_an_answer(tmp_path):
    document_ = scenario(checks=[NOTHING_BOOKED], answer=None, limits={"model_turns": 2, "tool_calls": 12,
                                                                       "wall_seconds": 120},
                         extensions={"routes": {"expected_route": "cedar-stays"}})
    driver, _ = agent([call("search_rooms", **STAY), call("search_rooms", **STAY), answer("unreached")])
    run, code, _ = await run_connector(tmp_path, document_, driver=driver)
    assert code == 1 and result_of(run)["failure_class"] == "interaction"
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert accuracy["status"] == "passed" and accuracy["actual"] == "cedar-stays"
