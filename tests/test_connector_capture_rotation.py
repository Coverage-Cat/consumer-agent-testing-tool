"""Follow-up tools can rotate one host-owned token without giving it to the model."""
import copy
import json

import pytest
from test_connector_support import (
    Hotel,
    agent,
    booking_responses,
    public_text,
    run_connector,
    scenario,
)

from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError


class RotatingHotel(Hotel):
    def __init__(self):
        super().__init__()
        self.issued = []
        for tool in self.catalog:
            if tool["name"] in ("get_quote", "create_booking"):
                tool["inputSchema"]["properties"]["intake_access_token"] = {"type": "string"}
                tool["inputSchema"].setdefault("required", []).append("intake_access_token")

    def call(self, name, arguments):
        if name in ("get_quote", "create_booking"):
            assert arguments["intake_access_token"] == self.issued[-1]
        result = super().call(name, arguments)
        if name in ("search_rooms", "get_quote") and not result.get("isError"):
            token = f"catt-synthetic-intake-access-{len(self.issued) + 1}-private"
            self.issued.append(token)
            result["structuredContent"]["intake_access_token"] = token
            result["content"][0]["text"] = json.dumps(result["structuredContent"])
        return result


def rotating_scenario(target="muse"):
    document = scenario(emulation_target=target)
    tools = document["connectors"][0]["tools"]
    tools["capture"]["intake_access"] = {
        "from": ["search_rooms", "get_quote"], "pointer": "/structuredContent/intake_access_token"}
    for name in ("get_quote", "create_booking"):
        tools["host_arguments"].setdefault(name, {})["intake_access_token"] = {"$ref": "/captured/intake_access"}
    return document


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_capture_rotates_across_tools_and_hides_every_token(tmp_path, target):
    hotel = RotatingHotel()
    driver, model = agent(booking_responses())
    run, code, _ = await run_connector(
        tmp_path, rotating_scenario(target), driver=driver, hotel=hotel)
    assert code == 0, run.summary()
    assert len(hotel.bookings) == 1
    assert len(hotel.issued) == 2 and run.captured["intake_access"] == hotel.issued[-1]
    visible = json.dumps(model.inputs)
    for token in hotel.issued:
        assert token not in visible
        assert token not in public_text(run)
    assert hotel.calls[1][1]["intake_access_token"] == hotel.issued[0]
    assert hotel.calls[2][1]["intake_access_token"] == hotel.issued[1]


@pytest.mark.parametrize("sources", [[], ["get_quote", "get_quote"], [""], ["arguments"], ["absent_tool"]])
def test_capture_sources_are_reviewed_nonempty_distinct_tool_names(sources):
    document = rotating_scenario()
    document["connectors"][0]["tools"]["capture"]["intake_access"]["from"] = sources
    with pytest.raises(LabError):
        parse_scenario(document)


async def test_error_result_cannot_replace_the_latest_accepted_capture(tmp_path):
    hotel = RotatingHotel()
    original = hotel.call

    def bad_quote(name, arguments):
        result = original(name, arguments)
        if name == "get_quote":
            result = copy.deepcopy(result)
            result["isError"] = True
        return result

    hotel.call = bad_quote
    from test_connector_support import native_script

    from consumer_agent_testing_tool.connector_run import ScriptedDriver

    script = native_script()
    script[2:] = [{"answer": "The quote failed. I have not booked anything."}]
    run, _, _ = await run_connector(
        tmp_path, rotating_scenario(), driver=ScriptedDriver(script), hotel=hotel)
    assert run.captured["intake_access"] == hotel.issued[0]
    assert hotel.bookings == []
