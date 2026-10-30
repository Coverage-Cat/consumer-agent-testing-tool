"""Connector lane end to end: a developer's own stateless server that pages AWS-style (nextToken).

Pagination keys (nextToken, a bare token beside the page, skipToken, resume_token) are not
credentials: the model must see each one and pass it back, under both client profiles, and the
host registers none of them as a credential (the provider guard would refuse to send them).
"""
import json

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from test_connector_e2e_support import Script, answer, call, events, result_of
from test_mcp_clients_support import serve_asgi

from consumer_agent_testing_tool.connector_run import ConnectorRun
from consumer_agent_testing_tool.connector_scenario import parse_scenario

CLASSES = [{"class_id": f"c-{number}", "title": f"Class {number}"} for number in range(100, 106)]
TOKENS = ("nextToken", "token", "skipToken", "resume_token")


def paging_app(base):
    server = MCPServer("paging-yoga", version="0.1.0", log_level="WARNING")

    @server.tool(name="list_classes", description="List yoga classes, two per page.",
                 annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    def list_classes(nextToken: str | None = None) -> dict:
        start = int(nextToken.removeprefix("pg-tok-")) if nextToken else 0
        following = f"pg-tok-{start + 2}" if start + 2 < len(CLASSES) else None
        return {"classes": CLASSES[start:start + 2], **dict.fromkeys(TOKENS, following)}
    # JSON responses, as the lane fixture answers: the template reads each reply to its end.
    return server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1", json_response=True)


def scenario(endpoint, profile):
    return {"version": 1, "kind": "connector", "emulation_target": "muse", "id": "paging", "goal": "List every yoga class for me.",
            "connectors": [{"name": "paging-yoga", "endpoint": endpoint, "allowed_origins": [endpoint[:-4]],
                            "client_profile": profile, "tools": {"allow": ["list_classes"]}}],
            "permissions": {"rules": [{"id": "reads", "decision": "allow_task",
                                       "when": {"field": "tool", "op": "eq", "value": "list_classes"}}]},
            "checks": [{"id": "listed", "source": "tool", "tool": {"name": "list_classes", "pointer": "/isError"},
                        "expect": {"op": "eq", "value": False}}]}


def shown(message):
    """The page the model was shown: the JSON text content of its last tool result."""
    return json.loads(json.loads(message.text)["result"]["content"][0])


def follow(key):
    """A model that passes back the page token it was shown under `key`."""
    return lambda input: call("list_classes", nextToken=shown(input[-1])[key])


@pytest.mark.parametrize("profile", ["generic-sdk", "muse-template-unofficial-2026-10"])
async def test_the_model_pages_with_the_aws_style_next_token_a_correct_server_returns(tmp_path, profile):
    script = Script(call("list_classes"), follow("nextToken"), follow("token"), answer("There are six classes."))
    with serve_asgi(paging_app) as base:
        run = ConnectorRun(parse_scenario(scenario(f"{base}/mcp", profile)), driver=script.driver(), root=tmp_path,
                           output=tmp_path / "runs")
        code = await run.run()
    assert code == 0, run.summary()
    pages = [json.loads(script.tool_results(turn)[-1]["result"]["content"][0]) for turn in (1, 2, 3)]
    # Every key the server pages with reached the model as sent, and passing it back reached the next page.
    assert [{key: page[key] for key in TOKENS} for page in pages] == [
        dict.fromkeys(TOKENS, "pg-tok-2"), dict.fromkeys(TOKENS, "pg-tok-4"), dict.fromkeys(TOKENS, None)]
    assert [row["class_id"] for page in pages for row in page["classes"]] == [row["class_id"] for row in CLASSES]
    responses = [row for row in events(run, "service_response") if row["origin"] == "model"]
    assert [row["model_view_masked"]["count"] for row in responses] == [0, 0, 0]
    assert [row["credential_values_learned"] for row in responses] == [0, 0, 0]
    assert "pg-tok-2" not in run.redactor.credentials  # Never registered: the provider guard lets it through.
    assert not any(gap.startswith("The app hid") for gap in result_of(run)["observation_gaps"])
