"""The shipped everything-weather example, run against a stand-in for the MCP reference server.

The example targets @modelcontextprotocol/server-everything 1.0.0, which needs npm and the network. This
stand-in publishes the same five tools (names, titles, schemas and annotations as that release lists them)
and the same results, so the example's scenario and both scripted calls files keep working as the runner
changes. It does not prove the real server's behavior; the example's README commands do that.
"""
import json
from pathlib import Path

import mcp_types as types
import pytest
from mcp.server.lowlevel import Server
from test_mcp_clients_support import serve_asgi

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.serialization import load

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"
SCHEMA = "http://json-schema.org/draft-07/schema#"
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WEATHER = {"New York": (33, "Cloudy", 82), "Chicago": (36, "Light rain / drizzle", 82),
           "Los Angeles": (73, "Sunny / Clear", 48)}
TOOLS = [
    {"name": "echo", "title": "Echo Tool", "description": "Echoes back the input string",
     "inputSchema": {"$schema": SCHEMA, "type": "object", "required": ["message"],
                     "properties": {"message": {"type": "string", "description": "Message to echo"}}},
     "annotations": READ},
    {"name": "get-env", "title": "Print Environment Tool",
     "description": "Returns all environment variables, helpful for debugging MCP server configuration",
     "inputSchema": {"$schema": SCHEMA, "type": "object", "properties": {}}, "annotations": READ},
    {"name": "get-structured-content", "title": "Get Structured Content Tool",
     "description": "Returns structured content along with an output schema for client data validation",
     "inputSchema": {"$schema": SCHEMA, "type": "object", "required": ["location"], "properties": {
         "location": {"type": "string", "enum": list(WEATHER), "description": "Choose city"}}},
     "outputSchema": {"$schema": SCHEMA, "type": "object", "additionalProperties": False,
                      "required": ["temperature", "conditions", "humidity"], "properties": {
                          "temperature": {"type": "number", "description": "Temperature in celsius"},
                          "conditions": {"type": "string", "description": "Weather conditions description"},
                          "humidity": {"type": "number", "description": "Humidity percentage"}}},
     "annotations": READ},
    {"name": "get-sum", "title": "Get Sum Tool", "description": "Returns the sum of two numbers",
     "inputSchema": {"$schema": SCHEMA, "type": "object", "required": ["a", "b"], "properties": {
         "a": {"type": "number", "description": "First number"},
         "b": {"type": "number", "description": "Second number"}}},
     "annotations": READ},
    {"name": "gzip-file-as-resource", "title": "GZip File as Resource Tool",
     "description": "Compresses a single file using gzip compression.",
     "inputSchema": {"$schema": SCHEMA, "type": "object", "properties": {
         "data": {"type": "string", "format": "uri", "description": "URL or data URI of the file content"}}},
     "annotations": {**READ, "readOnlyHint": False, "openWorldHint": True}},
]


def everything(base):
    """The stand-in, on the SDK's Streamable HTTP server like the real one (sessions, SSE replies)."""
    calls = []

    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool.model_validate(tool) for tool in TOOLS])

    async def call_tool(context, params):
        name, arguments = params.name, params.arguments or {}
        calls.append(name)
        if name == "get-structured-content":
            temperature, conditions, humidity = WEATHER[arguments["location"]]
            weather = {"temperature": temperature, "conditions": conditions, "humidity": humidity}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(weather))],
                                        structured_content=weather)
        if name == "get-sum":
            text = f"The sum of {arguments['a']} and {arguments['b']} is {arguments['a'] + arguments['b']}."
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
        if name == "echo":
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Echo: {arguments['message']}")])
        raise AssertionError(f"the example should never call {name}")

    server = Server("mcp-servers/everything", version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)
    app = server.streamable_http_app()
    app.state.calls = calls
    return app


@pytest.fixture
def reference_server():
    with serve_asgi(everything) as base:
        yield base


def run_example(tmp_path, base, script, **treatment):
    loaded = parse_scenario(load(EXAMPLES / "everything-weather.yaml"), base=EXAMPLES,
                            variables={"PORT": base.rsplit(":", 1)[1]})
    return ConnectorRun(loaded, driver=ScriptedDriver(load(EXAMPLES / script)), root=tmp_path,
                        output=tmp_path / "runs", **treatment)


def events(run, kind):
    rows = [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]
    return [row for row in rows if row["event_type"] == kind]


@pytest.mark.parametrize(("script", "treatment"), [
    ("everything-weather.calls.json", {}),
    ("everything-weather.calls.json", {"client_profile": "muse-template-unofficial-2026-10"}),
    ("everything-weather.calls.json", {"output_schema": "strict"}),
    ("everything-weather-cli.calls.json", {"projection": "muse-cli"}),
    ("everything-weather-cli.calls.json", {"projection": "muse-cli", "client_profile": "muse-template-unofficial-2026-10"}),
])
async def test_the_shipped_reference_server_example_passes_every_documented_treatment(
        tmp_path, reference_server, script, treatment):
    run = run_example(tmp_path, reference_server, script, **treatment)
    assert await run.run() == 0
    result = json.loads((run.path / "result.json").read_text())
    assert result["status"] == "completed"
    assert {check["id"]: check["status"] for check in result["checks"]} == {
        "execution": "passed", "chicago-conditions": "passed", "approval.dispatches_match": "passed",
        "answer.relay_complete": "passed"}
    assert result["answer"]["final_answer"] == "Chicago is at 36 degrees right now, with Light rain / drizzle and 82% humidity."


async def test_the_reviewed_allowlist_keeps_the_risky_reference_tools_from_the_model(tmp_path, reference_server):
    run = run_example(tmp_path, reference_server, "everything-weather.calls.json")
    assert await run.run() == 0
    [catalog] = events(run, "catalog")
    [projection] = events(run, "projection")
    published = {tool["name"] for tool in catalog["tools"]}
    offered = {tool["name"] for tool in projection["tools"]}
    # The catalog is the server's full list; the model is offered only the scenario's allowlist.
    assert {"get-env", "gzip-file-as-resource"} <= published
    assert offered == {"echo", "get-structured-content", "get-sum"}
