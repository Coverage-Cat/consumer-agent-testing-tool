"""The routes extension's config: strict validation when a scenario loads, and its binding to the scenario's
connectors at setup (a mismatch is a setup error before any model call)."""
import re

import pytest
from test_connector_multi_support import PINE, multi_services, ready, two_connectors
from test_connector_support import ORIGIN, agent, answer, result_of, run_connector, scenario

from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_routes import RoutesConfig, terms

DESK = {"id": "desk", "business": "Cedar Stays", "transcript": "Front desk: rooms are $189 a night."}
STATE = f"{ORIGIN}/__oracle/state"


@pytest.mark.parametrize(("routes", "message"), [
    ({"expected_route": ["none", "phone"], "phone": [DESK]},
     "expected_route none (a request no route serves) excludes every other route"),
    ({"expected_route": "phone"}, "expected_route phone needs phone scripts"),
    ({"expected_route": "website"}, "expected_route website needs a website block"),
    ({"expected_route": []}, "expected_route is one route or a nonempty list of distinct routes"),
    ({"expected_route": ["phone", "phone"], "phone": [DESK]}, "a nonempty list of distinct routes"),
    ({"expected_route": "Cedar Stays"}, "'Cedar Stays' is not a connector name, website, phone or none"),
    ({"phone": [DESK, DESK]}, "phone script ids must be unique"),
    ({"phone": [{**DESK, "match": ["("]}]}, "phone.desk.match: invalid regular expression"),
    ({"duplicates": {"count": [{"route": "phone", "url": STATE, "pointer": "/n"}]}},
     "phone bookings come from phone scripts"),
    ({"duplicates": {"count": [{"route": "website", "url": STATE, "pointer": "/n"}]}},
     "no website block declares that route"),
    ({"duplicates": {"count": [{"route": "cedar-stays", "url": "http://example.com/state", "pointer": "/n"}]}},
     "plain http is allowed only for loopback"),
    ({"duplicates": {"count": [{"route": "cedar-stays", "url": STATE, "pointer": "n"}]}}, "must be a JSON pointer"),
    ({"duplicates": {"count": []}}, "at least 1 item"),
    ({"duplicates": {"count": [{"route": "cedar-stays", "url": STATE, "pointer": "/confirmed_count"},
                               {"route": "pine-lodge", "url": STATE, "pointer": "/confirmed_count"}]}},
     f"duplicates.count lists {STATE} /confirmed_count for both cedar-stays and pine-lodge: list each oracle once"),
    ({"connectors": {"phone": {}}}, "connectors.phone: keys are the scenario's connector names"),
    ({"connectors": {"cedar-stays": {"consent": "maybe"}}}, "'allow' or 'decline'"),
    ({"website": {"extension": "browser", "url": "http://example.com/"}}, "plain http is allowed only for loopback"),
    ({"judge": "last"}, "'first' or 'commit'"),
    ({"surprise": True}, "Extra inputs are not permitted"),
])
def test_routes_config_is_strict_and_fails_closed(routes, message):
    with pytest.raises(LabError, match=re.escape(message)):
        parse_scenario(scenario(extensions={"routes": routes}))


def test_expected_routes_take_one_route_or_any_of_several():
    assert RoutesConfig.model_validate({"expected_route": "cedar-stays"}).expected() == ["cedar-stays"]
    assert RoutesConfig.model_validate({"expected_route": ["cedar-stays", "phone"], "phone": [DESK]}).expected() == [
        "cedar-stays", "phone"]
    assert RoutesConfig.model_validate({}).expected() == []
    assert terms("Search rooms at CedarStays hotels, a booking's status") == {
        "search", "room", "cedar", "stay", "hotel", "booking", "statu"}


@pytest.mark.parametrize(("document", "message"), [
    (scenario(extensions={"routes": {"expected_route": "birch-inn"}}),
     "extensions.routes.expected_route names an undeclared connector, birch-inn"),
    (scenario(extensions={"routes": {"connectors": {"birch-inn": {"description": "Cabins."}}}}),
     "extensions.routes.connectors names undeclared connectors: birch-inn"),
    (scenario(extensions={"routes": {"duplicates": {"count": [{"route": "birch-inn", "url": STATE,
                                                               "pointer": "/n"}]}}}),
     "extensions.routes.duplicates.count names undeclared connectors: birch-inn"),
    (two_connectors(pine={"exposure": "disconnected"}, extensions={"routes": {
        "expected_route": "pine-lodge", "connectors": {"pine-lodge": {"consent": "decline"}}}}),
     "expected_route pine-lodge can never be used: it starts disconnected and the scripted user declines"),
    (scenario(extensions={"routes": {"website": {"extension": "routes"}}}),
     "names the routes extension, which the scenario does not enable as its browser route"),
    (scenario(extensions={"routes": {"connectors": {"cedar-stays": {
        "description": "Cedar Stays, which knows taylor.private@example.com"}}}}), "shows the model private.email"),
])
async def test_routes_config_binds_to_the_scenario_at_setup(tmp_path, document, message):
    with pytest.raises(LabError, match=re.escape(message)):
        await ready(tmp_path, document, multi_services())


async def test_a_connector_named_like_a_route_is_refused(tmp_path):
    document = scenario(extensions={"routes": {}})
    document["connectors"][0]["name"] = "phone"
    with pytest.raises(LabError, match="a connector named phone clashes with the phone route"):
        await ready(tmp_path, document)


async def test_a_bad_binding_stops_the_trial_at_setup_before_the_model_is_asked(tmp_path):
    driver, api = agent([answer("unused")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"routes": {"expected_route": "birch-inn"}}),
                                       driver=driver)
    result = result_of(run)
    assert code == 2 and result["failure_stage"] == "project" and api.inputs == []
    assert "expected_route names an undeclared connector, birch-inn" in result["detail"]


async def test_the_run_lock_records_the_routes_config_digest(tmp_path):
    routes = {"expected_route": "cedar-stays", "duplicates": {"count": [{"route": "cedar-stays", "url": STATE,
                                                                         "pointer": "/confirmed_count"}]}}
    run = await ready(tmp_path, two_connectors(extensions={"routes": {**routes, "duplicates": {"count": [
        *routes["duplicates"]["count"], {"route": "pine-lodge", "url": f"{PINE}/__oracle/state",
                                         "pointer": "/bookings"}]}}}), multi_services())
    entries = {entry["name"]: entry for entry in run.run_lock["extensions"]}
    assert set(entries) == {"clock", "routes"} and len(entries["routes"]["config_sha256"]) == 64
    assert [tool for tool in run.extension_tools()] == ["connector_search", "connect_connector"]
