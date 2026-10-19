"""Two connectors end to end: two in-process fixtures on loopback, the real linking, MCP clients,
permission engines and checks, and a disconnected connector an extension connects mid-run."""
import copy
import json

from test_connector_e2e_support import (
    BOOKING,
    EMAIL,
    EXAMPLES,
    NATIVE,
    QUOTE,
    ROOM,
    STAY,
    checks_of,
    document,
    events,
    fixture,
    public_text,
    result_of,
)
from test_connector_multi_support import Router

from consumer_agent_testing_tool.connector_run import EXTENSIONS, ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.replay import playback


def two_hotels():
    """The native example plus a second fixture as pine-lodge, disconnected until the router connects it."""
    scenario = document(NATIVE)
    first = scenario["connectors"][0]
    second = copy.deepcopy(first)
    second.update(name="pine-lodge", endpoint="http://127.0.0.1:${PINE}/mcp", allowed_origins=["http://127.0.0.1:${PINE}"],
                  exposure="disconnected")
    second["tools"]["capture"] = {"pine_booking_id": {"from": "create_booking",
                                                      "pointer": "/structuredContent/booking_id"}}
    scenario["connectors"].append(second)
    scenario["expected"] = {"tools": [f"cedar-stays/{tool}" for tool in scenario["expected"]["tools"]]}
    for check in scenario["checks"]:
        if check["source"] == "tool":
            check["connector"] = "cedar-stays"
    scenario["checks"].append({"id": "nothing-at-pine", "source": "oracle", "oracle": {
        "url": "http://127.0.0.1:${PINE}/__oracle/state", "pointer": "/confirmed_count"}, "expect": {"op": "eq", "value": 0}})
    scenario["reset"] = ["http://127.0.0.1:${PORT}/__oracle/reset", "http://127.0.0.1:${PINE}/__oracle/reset"]
    scenario["extensions"]["probe-router"] = {}
    return scenario


async def test_two_fixtures_one_connected_mid_run_book_once_through_their_own_clients(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-router", Router)
    Router.connected = []
    script = [{"tool": "connect_service", "arguments": {"service": "pine-lodge"}},
              {"tool": "pine-lodge__search_rooms", "arguments": STAY},
              {"tool": "cedar-stays__search_rooms", "arguments": STAY},
              {"tool": "cedar-stays__get_quote", "arguments": {"room_id": ROOM, **STAY}},
              {"tool": "cedar-stays__create_booking", "arguments": {"quote_id": QUOTE, "guest_name": "Taylor Example"}},
              {"answer": f"Booked: the Garden Queen at Cedar Stays for December 4 to 6, 2026, confirmation {BOOKING}, "
                         "$448.36 USD in total."}]
    async with fixture() as cedar, fixture() as pine:
        loaded = parse_scenario(two_hotels(), base=EXAMPLES, variables={"PORT": str(cedar.port), "PINE": str(pine.port)})
        run = ConnectorRun(loaded, driver=ScriptedDriver(script), root=tmp_path, output=tmp_path / "runs")
        code = await run.run()
        cedar_state, pine_state = cedar.controls.state(), pine.controls.state()
    assert code == 0, run.summary()
    assert playback(run.path)["integrity"] == "verified"
    assert cedar_state["confirmed_count"] == 1 and cedar_state["bookings"][0]["guest_email"] == EMAIL
    assert pine_state["confirmed_count"] == 0 and pine_state["calls"]["search_rooms"] == 1
    checks = checks_of(run)
    assert {check["status"] for check in checks.values()} == {"passed"}, checks
    assert checks["booking-status"]["connector"] == "cedar-stays"
    assert checks["approval.dispatches_match"]["actual"]["dispatched"] == 4
    result = result_of(run)
    sections = {section["name"]: section for section in result["connectors"]}
    assert sections["pine-lodge"]["connected"]["by"] == "router" and sections["pine-lodge"]["link"]["mode"] == "none"
    assert sections["cedar-stays"]["server"]["protocol_version"] and sections["pine-lodge"]["server"]["tool_count"] == 5
    assert result["run_lock"]["connectors"]["pine-lodge"]["server"]["catalog_sha256"] == \
        result["run_lock"]["connectors"]["cedar-stays"]["server"]["catalog_sha256"]  # Two builds of one fixture.
    assert [row["connector"] for row in events(run, "catalog")] == ["cedar-stays", "pine-lodge"]
    assert result["metrics"]["connectors"]["pine-lodge"]["dispatches"] == 1
    assert json.loads(next(row["content"] for row in events(run, "tool_result") if row["tool"] == "connect_service")
                      )["connected"] is True
    assert EMAIL not in public_text(run.path)
