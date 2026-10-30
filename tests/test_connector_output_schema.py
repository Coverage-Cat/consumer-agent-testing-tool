"""The output-schema treatment on a connector run (P1.5): recorded with the client profile in the run lock and the
trial's treatment, strict refusals reported as a failed client.output_schema check, a CLI override recorded."""
import json
from pathlib import Path

import pytest
from test_mcp_clients_support import FakeMcp

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError

STEPS = [{"tool": "get_quote", "arguments": {"text": "garden room"}}, {"answer": "I could not get a quote."}]


def document(endpoint, base, **connector):
    """FakeMcp's legacy server behind the real SDK client: its get_quote answers without the total_minor its
    outputSchema requires."""
    return {"version": 1, "kind": "connector", "emulation_target": "muse", "id": "output-schema", "goal": "Quote me the garden room.",
            "connectors": [{"name": "fake-mcp", "endpoint": endpoint, "allowed_origins": [base],
                            "auth": {"mode": "none"}, "tools": {"allow": ["search_rooms", "get_quote"]}, **connector}],
            "permissions": {"rules": [{"id": "reads", "decision": "allow_task",
                                       "when": {"field": "tool_class", "op": "eq", "value": "read"}}]}}


async def trial(tmp_path, scenario):
    run = ConnectorRun(parse_scenario(scenario), driver=ScriptedDriver(STEPS), root=tmp_path, output=tmp_path / "runs")
    return run, await run.run()


def result_of(path):
    return json.loads((Path(path) / "result.json").read_text())


async def test_a_strict_treatment_refuses_the_result_and_fails_the_server_s_output_schema(tmp_path):
    with FakeMcp() as fake:
        run, code = await trial(tmp_path, document(fake.url, fake.base, output_schema="strict"))
    assert code == 1, run.summary()
    result = result_of(run.path)
    check = next(check for check in result["checks"] if check["id"] == "client.output_schema")
    assert (check["status"], check["class"], check["treatment"]) == ("failed", "tool/API", "strict")
    assert check["observed"] == [{"tool": "get_quote", "problem": "output-schema-mismatch",
                                  "at": "/structuredContent (required)", "count": 1}]
    assert check["basis"]["basis"] == "documented"
    # Recorded as a treatment: with the client profile in the run lock, and in the trial's treatment label.
    assert result["run_lock"]["client_profile"]["output_schema"] == "strict"
    assert result["treatment"] == "native/in-prompt/generic-sdk:strict-output-schema"
    assert result["run_lock"]["bases"]["output-schema"]["basis"] == "unknown"  # Muse's own handling is not public.
    # The model learned only that the response could not be read; the refused content never reached it.
    told = [json.loads(row["content"]) for row in map(json.loads, (run.path / "events.jsonl").read_text().splitlines())
            if row["event_type"] == "tool_result"]
    assert told[0]["message"] == "fake-mcp sent a response the app could not read; the outcome of get_quote is unknown."


async def test_the_lenient_default_keeps_the_result_and_only_notes_the_mismatch(tmp_path):
    with FakeMcp() as fake:
        run, code = await trial(tmp_path, document(fake.url, fake.base))
    assert code == 0, run.summary()
    result = result_of(run.path)
    assert "client.output_schema" not in {check["id"] for check in result["checks"]}
    assert result["run_lock"]["client_profile"]["output_schema"] == "lenient"
    assert result["treatment"] == "native/in-prompt/generic-sdk"
    assert [note["tool"] for note in result["connector"]["client_observations"]
            if note["kind"] == "output-schema-mismatch"] == ["get_quote"]


async def test_connector_run_output_schema_overrides_every_connector_and_is_recorded(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "calls.json"
    script.write_text(json.dumps(STEPS))
    with FakeMcp() as fake:
        scenario = tmp_path / "scenario.json"
        scenario.write_text(json.dumps(document(fake.url, fake.base)))
        code = await execute(parser().parse_args(["connector-run", str(scenario), "--driver", "scripted", "--script",
                                                  str(script), "--output-schema", "strict"]))
    run = next(Path(line) for line in capsys.readouterr().out.splitlines() if line.startswith(str(tmp_path / "runs")))
    assert code == 1
    result = result_of(run)
    assert result["run_lock"]["overrides"] == {"output_schema": "strict"}
    assert result["run_lock"]["client_profile"]["output_schema"] == "strict"


def test_the_treatment_is_strict_or_lenient_only():
    with pytest.raises(LabError, match="output_schema"):
        parse_scenario(document("http://127.0.0.1:9/mcp", "http://127.0.0.1:9", output_schema="refuse"))
