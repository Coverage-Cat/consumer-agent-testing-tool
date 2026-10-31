"""Portable traces still use host admission and only host-visible responses."""
import copy
import json

import pytest
from test_connector_support import Hotel, events, native_script, result_of, run_connector, scenario

from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.errors import LabError


def portable_script():
    steps = native_script()
    for step in steps:
        if "tool" in step:
            step["connector"] = "cedar-stays"
    steps = json.loads(json.dumps(steps).replace("/results/", "/responses/").replace("/result/", "/"))
    steps[1]["arguments"]["room_id"] = {"$ref": "/responses/0/structuredContent/rooms/0/room_id"}
    steps[2]["arguments"]["quote_id"] = {"$ref": "/responses/1/structuredContent/quote_id"}
    steps[-1] = {"answer": ["Booked for $420.00 USD, confirmation ",
                           {"$ref": "/responses/2/structuredContent/booking_id"}, "."]}
    return steps


@pytest.mark.parametrize("target", ["muse", "instinct"])
@pytest.mark.parametrize("projection", ["native", "cli"])
async def test_one_trace_books_through_both_surrogates_and_projections(tmp_path, target, projection):
    doc = scenario()
    doc["emulation_target"] = target
    run, code, hotel = await run_connector(
        tmp_path, doc, driver=ScriptedDriver(portable_script()), projection=projection)
    assert code == 0, result_of(run)
    assert hotel.state()["confirmed_count"] == len(hotel.charges) == 1
    assert hotel.bookings[0]["guest_email"] == doc["private"]["email"]


@pytest.mark.parametrize("projection", ["native", "cli"])
async def test_portable_call_does_not_bypass_permission_denial(tmp_path, projection):
    doc = scenario()
    doc["permissions"]["rules"] = []
    steps = [{"connector": "cedar-stays", "tool": "create_booking",
              "arguments": {"quote_id": "unknown", "guest_name": "Taylor"}},
             {"answer": "Nothing was booked."}]
    run, _, hotel = await run_connector(tmp_path, doc, driver=ScriptedDriver(steps), projection=projection)
    assert not hotel.calls and not hotel.bookings
    assert any(row["decision"]["kind"] == "deny" for row in events(run, "approval_decision"))


@pytest.mark.parametrize("projection", ["native", "cli"])
async def test_portable_response_never_recovers_truncated_data(tmp_path, projection):
    hotel = Hotel()
    original = hotel.call

    def oversized(name, arguments):
        value = original(name, arguments)
        if name == "search_rooms":
            value["structuredContent"] = {"padding": "x" * 20000, "hidden": "cannot-recover"}
        return value

    hotel.call = oversized
    doc = scenario()
    doc.update(checks=[], answer=None, expected={})
    doc["limits"]["result_characters"] = 1024
    steps = [{"connector": "cedar-stays", "tool": "search_rooms",
              "arguments": {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}},
             {"answer": [{"$ref": "/responses/0/structuredContent/hidden"}]}]
    run, code, _ = await run_connector(tmp_path, doc, driver=ScriptedDriver(steps),
                                        projection=projection, hotel=hotel)
    assert code == 2
    assert "does not resolve" in result_of(run)["detail"]
    assert run.final_text is None


@pytest.mark.parametrize("connector", ["", 3, None, "bad/name"])
def test_portable_connector_name_is_validated(connector):
    with pytest.raises(LabError):
        ScriptedDriver([{"connector": connector, "tool": "a", "arguments": {}}])


async def test_portable_unknown_connector_cannot_fall_back_to_another_service(tmp_path):
    steps = copy.deepcopy(portable_script())
    steps[0]["connector"] = "wrong-service"
    run, code, hotel = await run_connector(tmp_path, driver=ScriptedDriver(steps))
    assert code == 2 and not hotel.calls
    assert "connector" in result_of(run)["detail"]
