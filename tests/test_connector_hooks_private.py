"""The runner's pre-record hook (LaneExtension private_in_result): values an extension will withhold from a connector
result are registered before the raw service_response is recorded, so that record hides them and the run stays
shareable; content_trust's withheld one-time codes and reset links are the case it serves."""
import json
import re
from typing import ClassVar

from test_connector_e2e_support import (
    EXAMPLES,
    document,
    events,
    fixture,
    public_text,
    result_of,
    run_scenario,
)
from test_connector_support import BaseExtension, Hotel, run_connector, scenario

from consumer_agent_testing_tool.connector_run import EXTENSIONS, MACHINE_TOKENS, NoConfig
from consumer_agent_testing_tool.replay import export_public_run, playback
from consumer_agent_testing_tool.serialization import load

ROOM_CODE = "BK-1000"  # The fake hotel's first booking ID, which the probe treats as a value to withhold.


class Withholder(BaseExtension):
    """Names the booking ID in create_booking results as a value it withholds, and records what it was asked."""
    config_model = NoConfig
    asked: ClassVar[list] = []

    def private_in_result(self, run, connector, tool, result):
        Withholder.asked.append((connector, tool))
        result["structuredContent"] = "changed"  # A copy: the runner's own result is untouched.
        return [ROOM_CODE] if tool == "create_booking" else []


async def test_a_pre_record_hook_hides_its_values_from_the_raw_record_before_it_is_written(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-withholder", Withholder)
    monkeypatch.setattr(Withholder, "asked", [])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-withholder": {}}), hotel=Hotel())
    assert code == 0, run.summary()
    # Asked about every model call's result, never about the host's own verification reads.
    assert Withholder.asked == [("cedar-stays", "search_rooms"), ("cedar-stays", "get_quote"),
                                ("cedar-stays", "create_booking")]
    booked = next(row for row in events(run, "service_response") if row["tool"] == "create_booking")
    assert ROOM_CODE not in json.dumps(booked) and booked["result"]["structuredContent"]["status"] == "confirmed"
    assert result_of(run)["reporting_status"] == "completed" and playback(run.path)["integrity"] == "verified"
    assert ROOM_CODE in json.loads((run.path / "private" / "connector.private.json").read_text())["secrets"]


def present(path, value):
    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])")
    return pattern.search(MACHINE_TOKENS.sub(" ", public_text(path))) is not None


async def test_a_run_that_withholds_a_service_sent_code_is_shareable_again(tmp_path):
    code_, token = "583104", "Zq8LmN3pT6vR1xW9"
    brand = {"room_names": {"rm-cedar-king": f"Your sign-in code is {code_}",
                            "rm-premier-suite": f"Reset password: https://cs.example/r/{token}"}}
    scenario_ = document("fixture-booking.yaml", lambda item: item["extensions"].update(content_trust={}))
    async with fixture(brand=brand) as running:
        run, code = await run_scenario(tmp_path, running, scenario_, script=load(EXAMPLES / "fixture-booking.calls.json"))
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["reporting_status"] == "completed"
    # Neither the raw service_response nor anything else in the public evidence holds them.
    assert not present(run.path, code_) and not present(run.path, token)
    exported = export_public_run(run.path, tmp_path / "exports" / run.path.name)
    assert playback(exported)["integrity"] == "verified"
    withheld = result["metrics"]["extensions"]["content_trust"]["withheld"]
    assert (withheld["codes"], withheld["reset_links"], withheld["registered"]) == (1, 1, 2)
