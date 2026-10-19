"""report.html of a connector run: one row per connector when the scenario declares several."""
import html
import re

from test_connector_multi_support import Router, multi_services, two_connectors
from test_connector_support import agent, answer, call, result_of, run_connector

from consumer_agent_testing_tool.connector_run import EXTENSIONS
from consumer_agent_testing_tool.sealed_report import connectors_section


async def test_multi_connector_reports_render_each_connector(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-router", Router)
    document = two_connectors(pine={"exposure": "disconnected"})
    document["extensions"]["probe-router"] = {}
    driver, _ = agent([call("connect_service", service="pine-lodge"), answer("Connected Pine Lodge.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver, services_=multi_services())
    assert code == 1, run.summary()  # The scenario's booking never happened; the report still renders.
    page = (run.path / "report.html").read_text()
    section = page[page.index("<h2>Connectors</h2>"):page.index("</table>", page.index("<h2>Connectors</h2>"))]
    rows = [html.unescape(re.sub(r"<[^>]+>", "|", row)) for row in section.split("<tr>")[2:]]
    assert "cedar-stays" in rows[0] and "by setup at turn 0" in rows[0] and "cedar-stays__get_quote" in rows[0]
    assert "pine-lodge" in rows[1] and "by router at turn 0" in rows[1] and "(declared disconnected)" in rows[1]
    assert "pine-lodge__create_booking" in rows[1] and "generic-sdk" in rows[1]
    assert result_of(run)["connectors"][1]["connected"] == {"by": "router", "turn": 0, "exposure": "in-prompt"}


async def test_single_connector_reports_keep_their_layout(tmp_path):
    run, code, _ = await run_connector(tmp_path)
    assert code == 0, run.summary()
    assert "<h2>Connectors</h2>" not in (run.path / "report.html").read_text()
    assert connectors_section(None) == "" and connectors_section([]) == ""
    # Escaped, and odd rows never break the table.
    assert "&lt;b&gt;" in connectors_section([{"name": "<b>"}, "odd"]) and "not connected" in connectors_section(
        [{"name": "x"}])


def test_the_metrics_table_gives_each_extension_metric_its_own_row():
    from consumer_agent_testing_tool.sealed_report import metrics_section
    metrics = {"dispatches": 0, "approval": None,
               "extensions": {"custom_connector": {"first_call_success": True, "http": {"statuses": {"200": 7}}},
                              "clock": {"now": "2026-11-30T09:00:00-08:00"}, "probe": {}}}
    rows = [html.unescape(re.sub(r"<[^>]+>", "|", row)) for row in metrics_section(metrics).split("<tr>")[2:]]
    names = [row.strip("|").split("|")[0] for row in rows]
    assert names == ["dispatches", "approval", "extensions.custom_connector.first_call_success",
                     "extensions.custom_connector.http", "extensions.clock.now", "extensions.probe"]
    assert '"statuses": {"200": 7}' in rows[3]
    # A connector run without connectors (an extension's own service): connector metrics are not applicable.
    unconnected = metrics_section(metrics, {"kind": "connector-run", "connectors": []})
    assert unconnected.count("not applicable: the scenario declares no connectors") == 2
    assert "not applicable" not in metrics_section(metrics, {"kind": "connector-run", "connectors": [{}, {}]})
