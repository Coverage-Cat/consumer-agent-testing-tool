"""P3.4 WebMCP page tools in the browser facade (the "generic WebMCP page tools, not Muse" lane): browser_action
kind=webmcp {tool, params} is offered and dispatched only when the browser policy sets webmcp; its output is the
page's own untrusted text beside a fresh snapshot, it is charged like any action, and the result names the lane
among its evidence limits. Connector-lane browser tasks never enable it (test_ext_browser_tasks.py)."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput

from consumer_agent_testing_tool.browser import (
    WEBMCP_LIMIT,
    BrowserBroker,
    BrowserPolicy,
    run_browser_agent,
)
from consumer_agent_testing_tool.browser_agent import (
    INSTRUCTIONS,
    WEBMCP_INSTRUCTIONS,
    BrowserTraceRedactor,
    action_parameters,
    agent_evidence,
    argument_problem,
)
from consumer_agent_testing_tool.errors import LabError, LimitExhausted
from consumer_agent_testing_tool.providers import CapturingAPI

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "src/consumer_agent_testing_tool/data/surface_host/browser.mjs"
LANE = "generic WebMCP page tools, not Muse"
QUERY = "synthetic-entered-query"   # what the agent sends a page tool: hidden from evidence like typed text

FACADE_SCRIPT = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {RestrictedBrowser, validate} = await import(pathToFileURL(process.argv[1]).href);
const refused = (action, message) => assert.throws(() => validate(action),
  error => error.observable === 'invalid-action' && message.test(error.message));
refused({kind: 'webmcp', tool: 'webmcp_add'}, /requires params/);
refused({kind: 'webmcp', tool: 'webmcp_add', params: ['A1']}, /params must be a JSON object/);
refused({kind: 'webmcp', tool: 'webmcp_add', params: null}, /params must be a JSON object/);
refused({kind: 'webmcp', tool: 7, params: {}}, /tool must be a string/);
validate({kind: 'webmcp', tool: 'webmcp_add', params: {sku: 'A1'}});

function facade(webmcp, reply = async () => ({lane: 'lane', text: 'added A1 for hunter2-pass'})) {
  const calls = [];
  const mcp = {pageToolNames: new Set(['webmcp_add']), callPageTool: async (name, params) => {
    calls.push([name, params]);
    return reply(name, params);
  }};
  const browser = new RestrictedBrowser(null, null, '/tmp', () => {}, mcp, null);
  let generation = 4;
  Object.assign(browser, {policy: {webmcp, timeout_seconds: 1, dialogs: 'agent'}, page: {tab: 1}, generation,
    sync: async () => {}, modal: async () => null, passwords: async () => new Set(['hunter2-pass']),
    snapshot: async (status = {}) => ({generation: ++generation, ...status, text: '- paragraph [ref=e2]: Cart: A1'})});
  browser.shown = browser.page;
  return {browser, calls};
}
const add = {kind: 'webmcp', tool: 'webmcp_add', params: {sku: 'A1'}};
const report = {};
// Off (the default policy): refused before anything runs.
let {browser, calls} = facade(undefined);
let result = await browser.action(add);
assert.equal(result.status, 'invalid-action');
assert.match(result.message, /kind=webmcp is not available/);
assert.equal(calls.length, 0);
// On: the page's output (password values masked as in snapshots), then a fresh snapshot.
({browser, calls} = facade(true));
result = await browser.action(add);
assert.deepEqual(calls, [['webmcp_add', {sku: 'A1'}]]);
assert.equal(result.status, undefined);
assert.deepEqual(result.page_tool, {tool: 'webmcp_add', provenance: 'page-provided, untrusted',
  output: 'added A1 for [password masked]'});
assert.equal(result.generation, 5);
assert.match(result.text, /Cart: A1/);
report.called = result.page_tool;
// Names not on the latest listing and params the backend would refuse do nothing.
for (const action of [{...add, tool: 'webmcp_delete_account'}, {...add, params: {_meta: {cwd: '/'}}},
                      {...add, params: {blob: 'x'.repeat(70000)}}]) {
  result = await browser.action(action);
  assert.equal(result.status, 'invalid-action');
}
assert.equal(calls.length, 1);
// A tool on another tab's listing: the tab changed, so nothing runs and a fresh snapshot follows.
browser.page = {tab: 2};
result = await browser.action(add);
assert.equal(result.status, 'stale-reference');
assert.equal(calls.length, 1);
// A page-reported failure is an ordinary outcome with no output; a long output is bounded.
({browser, calls} = facade(true, async () => { throw Error('Playwright MCP webmcp_add failed'); }));
result = await browser.action(add);
assert.equal(result.status, 'action-failed');
assert.equal(result.page_tool.output, null);
({browser, calls} = facade(true, async () => ({lane: 'lane', text: 'y'.repeat(9000)})));
result = await browser.action(add);
assert.equal(result.page_tool.output.length, 8001);
console.log(JSON.stringify(report));
"""


def test_the_facade_runs_page_tools_only_when_the_policy_enables_them():
    result = subprocess.run(["node", "--input-type=module", "-e", FACADE_SCRIPT, str(FACADE)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["called"]["output"] == "added A1 for [password masked]"


class PageHost:
    """The facade's request surface for one page that registers WebMCP tools (browse_action only)."""

    def __init__(self):
        self.calls = []

    async def request(self, method, arguments):
        assert method == "browse_action"
        self.calls.append(arguments)
        observation = {"generation": len(self.calls) + 1, "text": "- paragraph [ref=e2]: Cart: A1"}
        if arguments["kind"] == "webmcp":
            observation["page_tool"] = {"tool": arguments["tool"], "provenance": "page-provided, untrusted",
                                        "output": '{"added": "A1"}'}
        return observation


def broker_for(tmp_path, **policy):
    return BrowserBroker(tmp_path, BrowserPolicy(url="http://127.0.0.1:8765", origins=["http://127.0.0.1:8765"],
                                                 rules=[], **policy), host=PageHost())


class Scripted(ModelAPI):
    def __init__(self, responses):
        super().__init__("test/browser")
        self.responses, self.inputs = iter(responses), []

    async def generate(self, input, tools, tool_choice, config):
        self.inputs.append({"messages": [message.model_dump(mode="json") for message in input],
                            "tools": [tool.model_dump(mode="json") for tool in tools]})
        return next(self.responses)


async def agent_run(broker, *responses, refused=False):
    evidence = agent_evidence(broker)
    api = Scripted([*responses, ModelOutput.from_content("test/browser", "Added the item; unverified.")])
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    if refused:  # A proposal the schema refuses, never followed by a valid one, fails the run (interpretation).
        with pytest.raises(LabError, match="violates the facade schema"):
            await run_browser_agent(model, broker, "Add item A1 to the cart.", {"generation": 1, "text": "Snapshot"},
                                    evidence=evidence)
    else:
        await run_browser_agent(model, broker, "Add item A1 to the cart.", {"generation": 1, "text": "Snapshot"},
                                evidence=evidence)
    return api, evidence


def page_tool_call(**params):
    return ModelOutput.for_tool_call("test/browser", "browser_action",
                                     {"kind": "webmcp", "tool": "webmcp_add_to_cart", "params": params})


def kinds_offered(api):
    schema = api.inputs[0]["tools"][0]["parameters"]
    return schema["properties"]["kind"]["enum"], schema["properties"]


async def test_the_agent_is_offered_kind_webmcp_only_when_the_policy_enables_it(tmp_path):
    # Off: the schema, its description and the instructions are those of every other run, and a proposal of the
    # kind is refused before dispatch.
    broker = broker_for(tmp_path / "off")
    api, _ = await agent_run(broker, page_tool_call(sku="A1"), refused=True)
    enum, properties = kinds_offered(api)
    assert "webmcp" not in enum and "tool" not in properties and "params" not in properties
    assert "webmcp" not in properties["kind"]["description"]
    assert api.inputs[0]["messages"][0]["content"] == INSTRUCTIONS and broker.host.calls == []
    assert enum == ["snapshot", "navigate", "tab", "click", "type", "select", "press", "scroll", "wait", "upload",
                    "download", "dialog", "takeover"]  # Exactly the schema runs had before kind=webmcp existed.
    assert action_parameters({}).model_dump() == action_parameters({}, False).model_dump()
    # On: offered and dispatched; the model reads the page's output, labeled as the page's.
    broker = broker_for(tmp_path / "on", webmcp=True)
    api, evidence = await agent_run(broker, page_tool_call(sku="A1", query=QUERY))
    enum, properties = kinds_offered(api)
    assert enum[-1] == "webmcp" and properties["tool"]["type"] == "string" and properties["params"]["type"] == "object"
    assert "webmcp: tool, params" in properties["kind"]["description"]
    assert api.inputs[0]["messages"][0]["content"] == f"{INSTRUCTIONS} {WEBMCP_INSTRUCTIONS}"
    assert broker.host.calls == [{"kind": "webmcp", "tool": "webmcp_add_to_cart", "params": {"sku": "A1", "query": QUERY}}]
    reply = json.loads(next(message["content"] for message in api.inputs[1]["messages"] if message["role"] == "tool"))
    assert reply["page_tool"] == {"tool": "webmcp_add_to_cart", "provenance": "page-provided, untrusted",
                                  "output": '{"added": "A1"}'}
    # What the agent sent the page tool is hidden from evidence like typed text; the tool name stays.
    texts = [path.read_text() for path in [*broker.path.rglob("*.json*"), *evidence.path.rglob("*.json")]]
    assert all(QUERY not in text for text in texts) and any("webmcp_add_to_cart" in text for text in texts)


def test_page_tool_arguments_are_checked_and_redacted_like_entered_text():
    assert argument_problem({"kind": "webmcp", "tool": "webmcp_x"}) == "kind=webmcp requires params"
    assert argument_problem({"kind": "webmcp", "tool": "webmcp_x", "params": {}, "ref": "e1"}) == \
        "kind=webmcp does not take ref"
    redactor = BrowserTraceRedactor()
    cleaned = redactor.clean({"arguments": {"kind": "webmcp", "tool": "webmcp_x",
                                            "params": {"query": QUERY, "n": 2}}, "echo": f"searched {QUERY}"})
    assert cleaned["arguments"]["params"] == {"query": "[REDACTED]", "n": "[REDACTED]"}
    assert cleaned["arguments"]["tool"] == "webmcp_x" and QUERY not in json.dumps(cleaned)


async def test_page_tool_calls_count_against_the_action_budget_and_name_their_lane(tmp_path):
    broker = broker_for(tmp_path / "budget", webmcp=True, max_actions=2)
    first = await broker.action({"kind": "webmcp", "tool": "webmcp_add_to_cart", "params": {"sku": "A1"}})
    assert first["page_tool"]["output"] == '{"added": "A1"}' and broker.count == 1
    await broker.action({"kind": "snapshot"})
    with pytest.raises(LimitExhausted, match="action budget"):
        await broker.action({"kind": "webmcp", "tool": "webmcp_add_to_cart", "params": {"sku": "B2"}})
    result = broker.finish("completed")
    assert WEBMCP_LIMIT in result["limits"] and LANE in WEBMCP_LIMIT and "assumed" in WEBMCP_LIMIT
    assert result["observations"]["webmcp_page_tool_calls"] == 1  # The refused third call was never sent.
    # The default policy's result is unchanged: no lane line, no count.
    plain = broker_for(tmp_path / "plain")
    await plain.action({"kind": "snapshot"})
    result = plain.finish("completed")
    assert not any("WebMCP" in line for line in result["limits"]) and "webmcp_page_tool_calls" not in \
        result["observations"]


@pytest.mark.parametrize("webmcp", [True, False])
async def test_page_tools_through_the_real_backend(webmcp):
    from consumer_agent_testing_tool.surface_host import BrowserHost
    if not (ROOT / "tooling/payment-ui/node_modules/@playwright/mcp").exists() \
            or not list((ROOT / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace-local Playwright MCP tooling/browser is not installed")
    fixture = await asyncio.create_subprocess_exec("node", "scripts/surface_fixture.mjs", "0", cwd=ROOT,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    host = BrowserHost(ROOT)

    async def refuse(name, arguments):
        raise AssertionError(f"Unexpected host callback {name}")
    host.callback, host.observe = refuse, (lambda row: None)
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 20)
        assert line, (await fixture.stderr.read()).decode()
        origin = json.loads(line)["endpoint"].removesuffix("/mcp")
        await host.start()
        opened = await host.request("browse_open", {
            "url": origin + "/webmcp", "origins": [origin], "timeout_seconds": 10, "webmcp": webmcp,
            "rules": [{"id": "page", "origin": origin, "path": "/webmcp", "methods": ["GET"], "effect": "read"}]})
        called = await host.request("browse_action", {"kind": "webmcp", "tool": "webmcp_add_to_cart",
                                                      "params": {"sku": "A1"}})
        if not webmcp:
            assert called["status"] == "invalid-action" and "Cart: A1" not in json.dumps(called)
            return
        assert "webmcp_add_to_cart" in opened["text"].split("\n")[0]
        assert called["page_tool"]["provenance"] == "page-provided, untrusted"
        assert '"added": "A1"' in called["page_tool"]["output"] and LANE not in json.dumps(called["page_tool"])
        assert called["generation"] == opened["generation"] + 1 and "Cart: A1" in called["text"]
        unknown = await host.request("browse_action", {"kind": "webmcp", "tool": "webmcp_unknown", "params": {}})
        assert unknown["status"] == "invalid-action" and unknown["generation"] == called["generation"]
    finally:
        await host.close()
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()
