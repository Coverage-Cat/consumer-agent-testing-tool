"""The routes extension on the runner: discovery, mid-run connects, phone and website routes, and the route
checks, with the lane's loopback fakes (two in-memory hotels, scripted models)."""
import json
from typing import ClassVar

import pytest
from inspect_ai.model import ModelOutput
from inspect_ai.tool import ToolDef
from test_connector_multi_support import (
    PINE,
    STAY,
    NoConfig,
    calls_of,
    model_tools,
    multi_services,
    native,
    ready,
    string_parameter,
    tool_messages,
    two_connectors,
)
from test_connector_support import (
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
    services,
    structured,
)

from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension, ScriptedDriver
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_routes import BASES
from consumer_agent_testing_tool.linking import LinkError

EMAIL = "taylor.private@example.com"
CLOCK = {"now": "2026-11-30T09:00:00-08:00"}
PINE_LINE = "Pine Lodge: cabins and lodge rooms by the lake."
FRONT_DESK = {"id": "pine-desk", "business": "Pine Lodge", "books": True,
              "transcript": "Front desk: I booked a lake cabin for December 4 to 6 for two adults, confirmation "
                            "PL-77, $390.00 in total."}


def connect(name):
    """The model's connect_connector call (the shared call() helper takes its own `name`)."""
    return ModelOutput.for_tool_call("test/connector", "connect_connector", {"name": name})


def oracle(origin, pointer="/confirmed_count"):
    return {"url": f"{origin}/__oracle/state", "pointer": pointer}


def count(route, origin, pointer="/confirmed_count"):
    return {"route": route, **oracle(origin, pointer)}


def booked(check_id, origin, value):
    return {"id": check_id, "source": "oracle", "oracle": oracle(origin), "expect": {"op": "eq", "value": value}}


def pine_disconnected(routes, **overrides):
    """Cedar Stays connected, Pine Lodge disconnected until connect_connector; checks nothing booked."""
    document = two_connectors(pine={"exposure": "disconnected"}, extensions={"clock": CLOCK, "routes": routes},
                              checks=[booked("no-cedar-booking", ORIGIN, 0), booked("no-pine-booking", PINE, 0)],
                              answer=None)
    document.update(overrides)
    return document


def checks_of(run):
    return {check["id"]: check for check in result_of(run)["checks"]}


def routes_metrics(run):
    return result_of(run)["metrics"]["extensions"]["routes"]


async def test_connector_search_finds_every_declared_connector_connected_or_not(tmp_path):
    routes = {"connectors": {"pine-lodge": {"description": PINE_LINE}}}
    run = await ready(tmp_path, pine_disconnected(routes), multi_services())
    search = run.extension_tools()["connector_search"].tool
    lake = json.loads(await search(query="lake cabins"))
    assert lake == {"source": "app", "tool": "connector_search", "query": "lake cabins",
                    "services": [{"name": "pine-lodge", "description": PINE_LINE, "connected": False}],
                    "note": "Connect a service with connect_connector before you use it."}
    # An empty query lists every connector in scenario order; a connected one shows its own summary line.
    listing = json.loads(await search(query=""))["services"]
    assert listing == [{"name": "cedar-stays", "description": "Cedar Stays", "connected": True},
                       {"name": "pine-lodge", "description": PINE_LINE, "connected": False}]
    # The exact name ranks first; plural and singular words match ("rooms" finds "room").
    assert json.loads(await search(query="pine-lodge"))["services"][0]["name"] == "pine-lodge"
    assert [row["name"] for row in json.loads(await search(query="a room"))["services"]] == ["pine-lodge"]
    assert json.loads(await search(query="spaceships")) == {
        "source": "app", "tool": "connector_search", "query": "spaceships", "services": [],
        "note": "No service matches; try other words."}
    assert [(row["query"], row["matched"]) for row in events(run, "routes.search")][:2] == [
        ("lake cabins", ["pine-lodge"]), ("", ["cedar-stays", "pine-lodge"])]


async def test_connect_connector_links_mid_run_after_consent_and_offers_tools_from_the_next_turn(tmp_path):
    routes = {"expected_route": "pine-lodge", "connectors": {"pine-lodge": {"description": PINE_LINE}},
              "duplicates": {"count": [count("cedar-stays", ORIGIN), count("pine-lodge", PINE, "/bookings")]}}
    document = pine_disconnected(routes, expected={"tools": ["pine-lodge/get_quote", "pine-lodge/create_booking"]},
                                 checks=[booked("one-pine-booking", PINE, 1), booked("no-cedar-booking", ORIGIN, 0)],
                                 answer={"claims": [{"state": "confirmed", "true_when": "one-pine-booking"}]})
    made = multi_services()
    responses = [call("connector_search", query="lake cabins"), connect("pine-lodge"),
                 call("pine-lodge__get_quote", room_id="loft-5", **STAY),
                 lambda input: call("pine-lodge__create_booking", quote_id=structured(input, "quote_id"),
                                    guest_name="Taylor Example"),
                 answer("Booked at Pine Lodge: $390.00 in total, confirmation BK-1000.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, document, driver=driver, services_=made)
    assert code == 0, run.summary()
    first = model_tools(api, 0)
    assert {"connector_search", "connect_connector"} <= set(first) and "phone_call" not in first
    assert not any(name.startswith("pine-lodge__") for name in first)
    told = [json.loads(message["content"]) for message in tool_messages(api)]
    assert told[1] == {"source": "app", "tool": "connect_connector", "connected": True, "service": "pine-lodge",
                       "message": "Your user approved, and pine-lodge is connected.",
                       "tools": ["pine-lodge__create_booking", "pine-lodge__get_booking", "pine-lodge__get_quote",
                                 "pine-lodge__search_rooms"], "next": "Its tools are available from your next step."}
    assert "pine-lodge__get_quote" in model_tools(api, 2)
    assert calls_of(made.hotels[PINE]) == ["get_quote", "create_booking"] and made.hotels[ORIGIN].calls == []
    # The connect is a user-visible interaction, answered by the scripted user; the runner links and lists it.
    prompt = events(run, "routes.connect")[0]
    assert prompt["outcome"] == "connected" and prompt["user_visible"] is True and prompt["consent"] == "allow"
    assert prompt["attempt"] == 1 and prompt["visibility"] == "developer"
    assert events(run, "connector_connected")[0]["by"] == "routes"
    checks = checks_of(run)
    accuracy = checks["routes.route_accuracy"]
    assert accuracy["status"] == "passed" and accuracy["actual"] == "pine-lodge" and accuracy["extension"] == "routes"
    assert accuracy["routes_used"] == ["pine-lodge"]
    duplicates = checks["routes.no_duplicates"]
    assert duplicates["status"] == "passed" and duplicates["actual"] == 1
    assert duplicates["by_route"] == [{"route": "cedar-stays", "bookings": 0}, {"route": "pine-lodge", "bookings": 1}]
    assert duplicates["expected"] == {"op": "lte", "value": 1} and duplicates["layer"] == "service"
    metrics = routes_metrics(run)
    assert metrics["first_route"] == "pine-lodge" and metrics["route_accuracy"] is True
    assert metrics["user_visible_connects"] == 1 and metrics["connector_searches"] == 1
    assert metrics["connects"] == [{"connector": "pine-lodge", "outcome": "connected", "user_visible": True}]
    assert metrics["bases"]["connect-in-chat"]["basis"] == "documented"
    assert EMAIL not in json.dumps(api.inputs) and EMAIL not in public_text(run)


async def test_a_declined_connect_falls_back_to_the_phone_route(tmp_path):
    routes = {"expected_route": "phone", "connectors": {"pine-lodge": {"consent": "decline"}},
              "phone": [FRONT_DESK],
              "duplicates": {"count": [count("cedar-stays", ORIGIN), count("pine-lodge", PINE)]}}
    made = multi_services()
    responses = [connect("pine-lodge"),
                 call("phone_call", business="Pine Lodge front desk", purpose="Book a lake cabin for two adults."),
                 answer("Pine Lodge booked a cabin by phone: confirmation PL-77, $390.00.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, pine_disconnected(routes), driver=driver, services_=made)
    assert code == 0, run.summary()
    declined, phoned = (json.loads(message["content"]) for message in tool_messages(api))
    assert declined["connected"] is False and declined["message"] == "Your user declined to connect pine-lodge."
    assert phoned == {"source": "phone", "tool": "phone_call", "untrusted": True, "business": "Pine Lodge",
                      "transcript": FRONT_DESK["transcript"]}
    assert [row["endpoint"] for row in made.linked] == [f"{ORIGIN}/mcp"]  # Pine Lodge was never linked.
    assert events(run, "routes.connect")[0]["outcome"] == "declined"
    call_row = events(run, "routes.phone_call")[0]
    assert call_row["script"] == "pine-desk" and call_row["books"] is True
    assert call_row["purpose"] == "Book a lake cabin for two adults."
    checks = checks_of(run)
    assert checks["routes.route_accuracy"]["status"] == "passed" and checks["routes.route_accuracy"]["actual"] == "phone"
    assert checks["routes.no_duplicates"]["by_route"] == [
        {"route": "cedar-stays", "bookings": 0}, {"route": "pine-lodge", "bookings": 0}, {"route": "phone", "bookings": 1}]
    metrics = routes_metrics(run)
    assert metrics["user_visible_connects"] == 1 and metrics["phone_calls"] == 1 and metrics["phone_bookings"] == 1


async def test_a_failed_connect_blocks_route_accuracy_when_no_expected_route_was_available(tmp_path):
    made = multi_services(link_errors={PINE: LinkError("the test service rejected the key", kind="rejected")})
    responses = [connect("pine-lodge"), connect("pine-lodge"),
                 answer("I could not connect Pine Lodge, so nothing is booked.")]
    driver, api = agent(responses)
    run, code, _ = await run_connector(tmp_path, pine_disconnected({"expected_route": "pine-lodge"}), driver=driver,
                                       services_=made)
    assert code == 3, run.summary()
    told = json.loads(tool_messages(api)[0]["content"])
    assert told["connected"] is False and told["message"] == ("Connecting pine-lodge did not work, so it is not "
                                                              "available right now.")
    assert "rejected" not in json.dumps(api.inputs)  # The model never sees the link's details.
    rows = events(run, "routes.connect")
    assert [(row["outcome"], row["attempt"]) for row in rows] == [("failed", 1), ("failed", 2)]
    assert rows[0]["class"] == "authentication" and rows[0]["link_error"] == "rejected"
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert accuracy["status"] == "blocked" and accuracy["class"] == "authentication" and accuracy["actual"] == "none"
    assert "pine-lodge could not be connected" in accuracy["detail"]


async def test_connect_attempts_are_capped_and_unknown_or_connected_names_ask_nobody(tmp_path):
    run = await ready(tmp_path, pine_disconnected({"connectors": {"pine-lodge": {"consent": "decline"}}}),
                      multi_services())
    connect = run.extension_tools()["connect_connector"].tool
    for _ in range(3):
        assert json.loads(await connect(name="pine-lodge"))["message"] == "Your user declined to connect pine-lodge."
    refused = json.loads(await connect(name="pine-lodge"))
    assert refused["message"] == "The app will not ask your user to connect pine-lodge again during this task."
    unknown = json.loads(await connect(name="Birch Inn"))
    assert unknown["connected"] is False and "connector_search lists the services" in unknown["message"]
    already = json.loads(await connect(name="cedar-stays"))
    assert already["connected"] is True and already["message"] == "cedar-stays is already connected."
    assert [(row["outcome"], row["user_visible"]) for row in events(run, "routes.connect")] == [
        ("declined", True)] * 3 + [("refused", False), ("unknown", False), ("already-connected", False)]


async def test_trigger_negative_counts_every_route_use_and_connect(tmp_path):
    routes = {"expected_route": "none", "phone": [FRONT_DESK]}
    document = scenario(checks=[booked("nothing-booked", ORIGIN, 0)], answer=None,
                        extensions={"clock": CLOCK, "routes": routes})
    note = "Thanks for watering my plants, Sam!"
    driver, _ = agent([answer(note)])
    clean, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, clean.summary()
    assert {check["id"]: check["status"] for check in result_of(clean)["checks"] if check.get("extension")} == {
        "routes.route_accuracy": "passed", "routes.trigger_negative": "passed"}
    assert routes_metrics(clean)["trigger_negative_violations"] == 0
    driver, _ = agent([call("search_rooms", **STAY), call("phone_call", business="Cedar Stays", purpose="Rates?"),
                       answer(note)])
    used, code, _ = await run_connector(tmp_path, document, driver=driver)
    checks = checks_of(used)
    assert code == 1 and checks["routes.route_accuracy"]["status"] == "failed"
    assert checks["routes.route_accuracy"]["detail"] == "The first route was cedar-stays; expected none"
    negative = checks["routes.trigger_negative"]
    assert negative["status"] == "failed" and negative["actual"]["violations"] == 2
    assert negative["detail"] == ("The agent used routes for a request none serves: cedar-stays (search_rooms), "
                                  "phone (phone_call)")
    # Connecting a service for an unrelated request is over-triggering too, though no route was used.
    driver, _ = agent([connect("cedar-stays"), answer(note)])
    connected, code, _ = await run_connector(tmp_path, document, driver=driver)
    checks = checks_of(connected)
    assert code == 1 and checks["routes.route_accuracy"]["status"] == "passed"
    assert checks["routes.trigger_negative"]["actual"]["first"][0]["via"] == "connect_connector"


async def test_route_accuracy_judges_the_first_route_even_when_its_call_was_invalid(tmp_path):
    document = two_connectors(extensions={"clock": CLOCK, "routes": {"expected_route": "cedar-stays"}},
                              checks=[booked("one-cedar-booking", ORIGIN, 1)], answer=None)
    steps = [native("pine-lodge__get_quote", room_id="loft-5", check_in="2026-12-04", check_out="2026-12-06",
                    adults="two"),
             native("cedar-stays__get_quote", room_id="garden-2", **STAY),
             native("cedar-stays__create_booking", quote_id={"$ref": "/results/1/result/structuredContent/quote_id"},
                    guest_name="Taylor Example"),
             {"answer": "Booked at Cedar Stays for $420.00."}]
    made = multi_services()
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=made)
    assert made.hotels[PINE].calls == []  # The invalid call never reached Pine Lodge, but it chose that route.
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert code == 1 and accuracy["status"] == "failed" and accuracy["actual"] == "pine-lodge"
    assert accuracy["routes_used"] == ["pine-lodge", "cedar-stays"]
    assert routes_metrics(run)["steps"][0]["via"] == "native"


async def test_no_duplicates_counts_bookings_over_every_route(tmp_path):
    routes = {"expected_route": ["cedar-stays", "pine-lodge"], "phone": [FRONT_DESK],
              "duplicates": {"count": [count("cedar-stays", ORIGIN), count("pine-lodge", PINE, "/bookings")]}}
    document = two_connectors(extensions={"clock": CLOCK, "routes": routes}, checks=[], answer=None)
    steps = []
    for index, prefix in enumerate(("cedar-stays__", "pine-lodge__")):
        steps += [native(f"{prefix}get_quote", room_id="garden-2", **STAY),
                  native(f"{prefix}create_booking", quote_id={"$ref": f"/results/{index * 2}/result/structuredContent/"
                                                                   "quote_id"}, guest_name="Taylor Example")]
    steps += [native("phone_call", business="Pine Lodge", purpose="Book a cabin too."), {"answer": "Booked thrice."}]
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=multi_services())
    duplicates = checks_of(run)["routes.no_duplicates"]
    assert code == 1 and duplicates["status"] == "failed" and duplicates["actual"] == 3
    assert [row["bookings"] for row in duplicates["by_route"]] == [1, 1, 1]
    assert duplicates["detail"] == "3 bookings over every route, more than 1: cedar-stays 1, pine-lodge 1, phone 1"
    assert checks_of(run)["routes.route_accuracy"]["status"] == "passed"  # Either connector was an expected route.
    # An oracle that cannot be read leaves the count open, never passed.
    unread = {"duplicates": {"count": [*routes["duplicates"]["count"],
                                       {"route": "pine-lodge", "url": f"{PINE}/__oracle/missing", "pointer": "/n"}]}}
    run, code, _ = await run_connector(tmp_path, two_connectors(extensions={"routes": unread}, checks=[], answer=None),
                                       driver=ScriptedDriver([{"answer": "Nothing booked."}]),
                                       services_=multi_services())
    duplicates = checks_of(run)["routes.no_duplicates"]
    assert code == 3 and duplicates["status"] == "observation-gap" and duplicates["actual"] == 0
    assert "pine-lodge: oracle returned HTTP 404" in duplicates["detail"]


class Browser(BaseExtension):
    """A stand-in for the browser extension (P2.3): one tool that opens a site."""
    config_model = NoConfig
    briefs: ClassVar[list] = []

    def tools(self, run):
        async def browser_task_spawn(brief: str):
            Browser.briefs.append(brief)
            return "The site is open: Cedar Stays rooms from $189 a night."
        return [ToolDef(browser_task_spawn, name="browser_task_spawn", description="Open a website for a task.",
                        parameters=string_parameter("brief", "What to do there."), parallel=False, max_output=0)]


async def test_the_website_route_is_the_browser_extension_s_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-browser", Browser)
    Browser.briefs = []
    routes = {"expected_route": "website", "website": {"extension": "probe-browser", "url": "https://cedar.example/"}}
    document = scenario(checks=[booked("nothing-booked", ORIGIN, 0)], answer=None,
                        extensions={"clock": CLOCK, "probe-browser": {}, "routes": routes})
    steps = [native("browser_task_spawn", brief="Find a room for two adults."), native("search_rooms", **STAY),
             {"answer": "The website lists rooms from $189 a night."}]
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert accuracy["actual"] == "website" and accuracy["routes_used"] == ["website", "cedar-stays"]
    assert routes_metrics(run)["steps"][0] == {"route": "website", "via": "probe-browser", "tool": "browser_task_spawn",
                                               "sequence": routes_metrics(run)["steps"][0]["sequence"]}
    # Without the browser extension enabled, the website route does not exist: a setup error.
    with pytest.raises(LabError, match="names the probe-browser extension, which the scenario does not enable"):
        await ready(tmp_path, scenario(extensions={"routes": routes}))


async def test_phone_scripts_match_by_regex_or_name_and_fall_back_to_voicemail(tmp_path):
    phone = [{"id": "desk", "business": "Cedar Stays", "match": [r"\bcedar\b"], "transcript": "Front desk: rooms are "
              "$189 a night."}, {"id": "spa", "business": "Lakeside Spa", "transcript": "Spa: we open at nine."}]
    run = await ready(tmp_path, scenario(extensions={"routes": {"phone": phone}}))
    dial = run.extension_tools()["phone_call"].tool
    assert json.loads(await dial(business="CEDAR stays hotel", purpose="Rates"))["business"] == "Cedar Stays"
    assert json.loads(await dial(business="Spa", purpose="Hours"))["transcript"] == "Spa: we open at nine."
    voicemail = json.loads(await dial(business="Birch Inn", purpose="Rates"))
    assert voicemail["business"] == "Birch Inn"
    assert voicemail["transcript"] == "Nobody answered, and the call went to voicemail."
    assert json.loads(await dial(business="  ", purpose=""))["transcript"].startswith("Nobody answered")
    assert [row["script"] for row in events(run, "routes.phone_call")] == ["desk", "spa", None, None]
    assert all(row["books"] is False for row in events(run, "routes.phone_call"))


async def test_muse_cli_connects_name_the_command_and_search_only_connects_say_to_load_tools(tmp_path):
    run = await ready(tmp_path, pine_disconnected({}, projection="muse-cli"), multi_services())
    told = json.loads(await run.extension_tools()["connect_connector"].tool(name="pine-lodge"))
    assert told["command"] == "pine-lodge" and "tools" not in told
    assert told["next"] == "Run pine-lodge list-tools with exec from your next step."
    deferred = await ready(tmp_path, pine_disconnected({"exposure": "search-only"}), multi_services())
    told = json.loads(await deferred.extension_tools()["connect_connector"].tool(name="pine-lodge"))
    assert told["next"].endswith("load their parameters with tool_search first.")
    assert deferred.connector("pine-lodge").exposure == "search-only"


async def test_route_texts_reach_the_model_masked_and_never_name_private_values(tmp_path):
    with pytest.raises(LabError, match="shows the model private.email"):
        await ready(tmp_path, scenario(extensions={"routes": {"phone": [
            {"id": "desk", "business": "Cedar Stays", "transcript": f"We emailed {EMAIL} your booking."}]}}))
    hotel = Hotel()
    driver, api = agent([call("connector_search", query="Cedar Stays"), answer("Cedar Stays is connected.")])
    run, code, _ = await run_connector(tmp_path, scenario(checks=[], answer=None, extensions={"routes": {}}),
                                       driver=driver, services_=services(hotel))
    assert code == 0, run.summary()
    result = next(row for row in events(run, "tool_result") if row["tool"] == "connector_search")
    assert result["extension"] == "routes" and result["visibility"] == "model"
    assert json.loads(tool_messages(api)[0]["content"])["services"][0]["name"] == "cedar-stays"
    assert EMAIL not in json.dumps(api.inputs)
    # The bases travel with the metrics, every one labeled.
    assert {entry["basis"] for entry in routes_metrics(run)["bases"].values()} <= {"documented", "unofficial",
                                                                                     "assumed"}
    assert set(routes_metrics(run)["bases"]) == set(BASES)


async def test_a_trial_that_uses_no_route_fails_an_expected_route(tmp_path):
    document = scenario(checks=[booked("nothing-booked", ORIGIN, 0)], answer=None,
                        extensions={"routes": {"expected_route": "cedar-stays"}})
    steps = [native("connector_search", query="Cedar Stays"), {"answer": "Cedar Stays is connected; shall I book?"}]
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps))
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert code == 1 and accuracy["actual"] == "none" and accuracy["routes_used"] == []
    assert accuracy["detail"] == "The agent used no route; expected cedar-stays"
    metrics = routes_metrics(run)
    assert metrics["first_route"] is None and metrics["route_accuracy"] is False and metrics["connector_searches"] == 1


async def test_the_commit_judge_lets_the_agent_compare_before_it_books(tmp_path):
    steps = [native("pine-lodge__get_quote", room_id="loft-5", **STAY),
             native("cedar-stays__get_quote", room_id="garden-2", **STAY),
             native("cedar-stays__create_booking", quote_id={"$ref": "/results/1/result/structuredContent/quote_id"},
                    guest_name="Taylor Example"),
             {"answer": "Cedar Stays was the one to book: $420.00, confirmation BK-1000."}]

    async def trial(judge):
        routes = {"expected_route": "cedar-stays", **({"judge": judge} if judge else {})}
        document = two_connectors(extensions={"clock": CLOCK, "routes": routes}, checks=[booked("one", ORIGIN, 1)],
                                  answer=None)
        run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps),
                                           services_=multi_services())
        return run, code, checks_of(run)["routes.route_accuracy"]
    run, code, accuracy = await trial(None)  # The default: the first route used, a read at the competitor.
    assert code == 1 and accuracy["actual"] == "pine-lodge" and accuracy["judge"] == "first"
    run, code, accuracy = await trial("commit")
    assert code == 0, run.summary()
    assert accuracy["actual"] == "cedar-stays" and accuracy["detail"] == ("The first route that wrote or booked was "
                                                                          "cedar-stays, as expected")
    metrics = routes_metrics(run)
    assert (metrics["first_route"], metrics["commit_route"], metrics["route_accuracy"]) == ("pine-lodge",
                                                                                            "cedar-stays", True)
    # A declined write commits nothing: over the scripted user's budget, no route wrote or booked.
    routes = {"expected_route": "cedar-stays", "judge": "commit"}
    made = multi_services({ORIGIN: Hotel(total_minor=61000), PINE: Hotel(total_minor=39000)})
    run, code, _ = await run_connector(tmp_path, two_connectors(extensions={"routes": routes}, checks=[], answer=None),
                                       driver=ScriptedDriver(steps), services_=made)
    accuracy = checks_of(run)["routes.route_accuracy"]
    assert code == 1 and accuracy["actual"] == "none"
    assert accuracy["detail"] == "The agent wrote or booked through no route; expected cedar-stays"


class CheckoutBrowser(Browser):
    """The stand-in browser with a steer that sends an approved commitment, recorded the way the browser
    extension records one (<extension>.commit_sent)."""

    def tools(self, run):
        async def browser_task_steer(message: str):
            run.event("probe-browser.commit_sent", {"browser_task": "task-1", "card_id": "card-1"})
            return "Booked on the website: confirmation W-9."
        return [*super().tools(run),
                ToolDef(browser_task_steer, name="browser_task_steer", description="Continue a website task.",
                        parameters=string_parameter("message", "What to do next."), parallel=False, max_output=0)]


async def test_a_website_commit_is_the_browser_extension_s_commit_sent_event(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-browser", CheckoutBrowser)
    steps = [native("search_rooms", **STAY), native("browser_task_spawn", brief="Book the Garden double."),
             native("browser_task_steer", message="Go ahead and book it."), {"answer": "Booked on the website: W-9."}]
    for judge, code_wanted, actual in (("commit", 0, "website"), ("first", 1, "cedar-stays")):
        routes = {"expected_route": "website", "judge": judge, "website": {"extension": "probe-browser"}}
        document = scenario(checks=[booked("nothing-at-cedar", ORIGIN, 0)], answer=None,
                            extensions={"probe-browser": {}, "routes": routes})
        run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps))
        accuracy = checks_of(run)["routes.route_accuracy"]
        assert (code, accuracy["actual"]) == (code_wanted, actual), run.summary()
        assert routes_metrics(run)["commit_route"] == "website"
        assert accuracy["routes_used"] == ["cedar-stays", "website"]
