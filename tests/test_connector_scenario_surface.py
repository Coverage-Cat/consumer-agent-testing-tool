"""connectors: [] when an enabled extension provides the agent's service itself (custom_connector's HTTP API)."""
import json
from typing import Any

import pytest
from inspect_ai.tool import ToolDef, ToolParams
from test_connector_multi_support import NoConfig
from test_connector_support import ORIGIN, Hotel, result_of, run_connector, scenario, services

from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    BaseExtension,
    ConnectorRun,
    ScriptedDriver,
)
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError


class Surface(BaseExtension):
    """A service of the extension's own (an HTTP API stand-in): the agent needs no connector."""
    config_model = NoConfig
    provides_surface = True

    def tools(self, run):
        async def book_room(**kwargs: Any):
            return json.dumps({"booking_id": "b-0001", "status": "confirmed"})
        return [ToolDef(book_room, name="book_room", description="Book a room through the hotel's API.",
                        parameters=ToolParams(), parallel=False, max_output=0)]

    def egress(self, run):
        return ["https://api.cedar.example"]


def surface(**overrides):
    document = scenario(connectors=[], expected={"tools": []}, answer=None,
                        extensions={"probe-surface": {}, "clock": {"now": "2026-11-30T09:00:00-08:00"}},
                        checks=[{"id": "nothing-booked-on-the-mcp-side", "source": "oracle",
                                 "oracle": {"url": f"{ORIGIN}/__oracle/state", "pointer": "/confirmed_count"},
                                 "expect": {"op": "eq", "value": 0}}])
    document.update(overrides)
    return document


def test_a_scenario_without_connectors_needs_an_extension_that_provides_the_service(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-surface", Surface)
    monkeypatch.setitem(EXTENSIONS, "probe-plain", BaseExtension)
    assert parse_scenario(surface(), base=tmp_path).scenario.connectors == []
    tool_check = {"id": "by-tool", "source": "tool", "tool": {"name": "get_booking", "pointer": "/status"},
                  "expect": {"op": "eq", "value": "confirmed"}}
    for overrides, message in (({"extensions": {"probe-plain": {}}}, "declare at least one connector"),
                               ({"extensions": {}}, "declare at least one connector"),
                               ({"projection": "muse-cli"}, "a scenario without connectors uses native"),
                               ({"expected": {"tools": ["book_room"]}}, "expected.tools: this scenario declares no"),
                               ({"permissions": {"important_reads": ["x"]}}, "important_reads: this scenario"),
                               ({"checks": [tool_check]}, "checks.by-tool: a tool check")):
        with pytest.raises(LabError, match=message):
            parse_scenario(surface(**overrides), base=tmp_path)


async def test_a_run_without_connectors_offers_the_extension_s_tools_and_seals(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-surface", Surface)
    script = ScriptedDriver([{"tool": "book_room", "arguments": {}}, {"answer": "Booked: b-0001."}])
    run, code, _ = await run_connector(tmp_path, surface(), driver=script)
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["connector"] is None and result["connectors"] == [] and result["approvals"] == {}
    assert result["run_lock"]["connectors"] == {} and "client_profile" not in result["run_lock"]
    assert result["treatment"] == "native/in-prompt/none" and result["metrics"]["connectors"] == {}
    assert result["locations"]["target"] == ["https://api.cedar.example"]
    assert [check["status"] for check in result["checks"]] == ["passed", "passed"]
    assert "Connected services" not in run.system_prompt() and run.tool_calls[0]["extension"] == "probe-surface"
    with pytest.raises(LabError, match="declares no connectors"):
        run.connector()
    # A treatment override revalidates the scenario with the same extension context.
    loaded = parse_scenario(surface(), base=tmp_path)
    overridden = ConnectorRun(loaded, driver=script, root=tmp_path, output=tmp_path / "runs",
                              services=services(Hotel()), exposure="search-only")
    assert overridden.scenario.exposure == "search-only" and overridden.scenario.connectors == []


async def test_the_custom_connector_example_runs_without_a_placeholder_connector(tmp_path, monkeypatch):
    from test_ext_custom_connector_support import ENV, KEY, document, loaded, script, service

    from consumer_agent_testing_tool.ext_custom_connector import CustomConnector
    # The extension gives the agent the service's HTTP API itself, and the shipped example declares connectors: [].
    assert CustomConnector.provides_surface is True
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        found = loaded(document(), running)
        assert found.scenario.connectors == []
        run = ConnectorRun(found, driver=ScriptedDriver(script(running.base_url)), root=tmp_path,
                           output=tmp_path / "runs")
        code = await run.run()
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["connectors"] == [] and {check["status"] for check in result["checks"]} == {"passed"}
    assert result["metrics"]["task_success"] is True
