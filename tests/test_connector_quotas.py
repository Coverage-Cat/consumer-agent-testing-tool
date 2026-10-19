"""Per-connector tool-call quotas (P1.5): connectors[].limits.tool_calls, recorded in the run lock and
exhausted like the scenario's own budget, for the model's native and muse-cli calls and run.call_tool's."""
import json

import pytest
from test_connector_multi_support import multi_services, native, ready, two_connectors
from test_connector_support import events, result_of, run_connector, scenario

from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError, LimitExhausted

STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}


def limited(calls, **overrides):
    document = scenario(**overrides)
    document["connectors"][0]["limits"] = {"tool_calls": calls}
    return document


async def test_a_connector_quota_stops_the_run_like_the_scenario_budget(tmp_path):
    run, code, hotel = await run_connector(tmp_path, limited(2))
    assert code == 1
    result = result_of(run)
    checks = {check["id"]: check for check in result["checks"]}
    execution = checks["execution"]
    assert (execution["status"], execution["class"]) == ("failed", "interaction")
    assert execution["detail"] == "Connector cedar-stays tool-call quota exceeded (2)"
    # The third call was refused before anything was sent, as the scenario's budget refuses one.
    assert [name for name, _ in hotel.calls] == ["search_rooms", "get_quote"]
    assert json.loads(events(run, "tool_result")[-1]["content"])["message"] == "The app stopped this task."
    assert [row["limit"] for row in events(run, "connector_quota_exhausted")] == [2]
    # Recorded as a treatment of the trial, and the calls each quota counted.
    assert result["run_lock"]["connector_limits"] == {"tool_calls": 2}
    assert result["run_lock"]["bases"]["tool-call-quotas"]["basis"] == "assumed"
    assert result["metrics"]["connector_quotas"] == {"cedar-stays": {"tool_calls": 3, "limit": 2}}
    # The outcome checks still run once something was dispatched: nothing was booked.
    assert checks["one-confirmed-booking"]["status"] == "failed"


async def test_a_quota_the_trial_stays_within_changes_nothing(tmp_path):
    run, code, _ = await run_connector(tmp_path, limited(3))
    assert code == 0, run.summary()
    assert result_of(run)["metrics"]["connector_quotas"] == {"cedar-stays": {"tool_calls": 3, "limit": 3}}
    # Without a quota, neither the lock nor the metrics name one.
    plain, code, _ = await run_connector(tmp_path)
    assert code == 0 and "connector_limits" not in result_of(plain)["run_lock"]
    assert "connector_quotas" not in result_of(plain)["metrics"]


async def test_a_muse_cli_call_tool_counts_against_its_connector_s_quota(tmp_path):
    steps = [{"tool": "exec", "arguments": {"command": "cedar-stays list-tools --names-only"}},
             {"tool": "exec", "arguments": {"command": ["cedar-stays", "call-tool", "--name", "search_rooms",
                                                        "--arguments-json", STAY]}},
             {"tool": "exec", "arguments": {"command": ["cedar-stays", "call-tool", "--name", "search_rooms",
                                                        "--arguments-json", STAY]}},
             {"answer": "Searched."}]
    run, code, hotel = await run_connector(tmp_path, limited(1, projection="muse-cli"), driver=ScriptedDriver(steps))
    assert code == 1
    # Listing is discovery, not a call of the connector's tools; the second call-tool is never sent.
    assert [name for name, _ in hotel.calls] == ["search_rooms"]
    assert result_of(run)["checks"][0]["detail"] == "Connector cedar-stays tool-call quota exceeded (1)"
    assert json.loads(events(run, "tool_result")[-1]["content"])["stderr"] == "The app stopped this task."


async def test_extension_calls_share_their_connector_s_quota_and_other_connectors_keep_theirs(tmp_path):
    document = two_connectors()
    document["connectors"][0]["limits"] = {"tool_calls": 1}
    run = await ready(tmp_path, document, multi_services())
    assert run.run_lock["connectors"]["cedar-stays"]["connector_limits"] == {"tool_calls": 1}
    assert "connector_limits" not in run.run_lock["connectors"]["pine-lodge"]
    found = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="job")
    assert found["kind"] == "result"
    with pytest.raises(LimitExhausted, match=r"Connector cedar-stays tool-call quota exceeded \(1\)"):
        await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="job")
    assert (await run.call_tool("pine-lodge", "get_quote", {"room_id": "garden-2", **STAY}, source="job"))[
        "kind"] == "result"
    assert run.metrics([], "failed")["connector_quotas"] == {"cedar-stays": {"tool_calls": 2, "limit": 1}}


async def test_a_native_call_of_one_connector_leaves_the_other_s_quota_alone(tmp_path):
    document = two_connectors()
    document["connectors"][1]["limits"] = {"tool_calls": 1}
    steps = [native("cedar-stays__search_rooms", **STAY), native("cedar-stays__search_rooms", **STAY),
             native("pine-lodge__search_rooms", **STAY), native("pine-lodge__search_rooms", **STAY),
             {"answer": "Searched both."}]
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), services_=multi_services())
    assert code == 1
    assert result_of(run)["checks"][0]["detail"] == "Connector pine-lodge tool-call quota exceeded (1)"
    assert result_of(run)["metrics"]["connector_quotas"] == {"pine-lodge": {"tool_calls": 2, "limit": 1}}


@pytest.mark.parametrize("limits, message", [
    ({"tool_calls": 0}, "greater than or equal to 1"), ({"tool_calls": 1001}, "less than or equal to 1000"),
    ({}, "Field required"), ({"tool_calls": 2, "model_turns": 3}, "Extra inputs")])
def test_connector_limits_are_strict(limits, message):
    document = scenario()
    document["connectors"][0]["limits"] = limits
    with pytest.raises(LabError, match=message):
        parse_scenario(document)


def test_a_declared_quota_is_part_of_the_scenario_digest():
    assert parse_scenario(limited(5)).sha256 != parse_scenario(scenario()).sha256
