"""MCP Apps host correctness: UI discovery, tool input, view calls and server identity (P0.7, I7, I8, M5)."""
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import compare
from consumer_agent_testing_tool.surface import SurfaceRun, SurfaceScenario
from consumer_agent_testing_tool.surface_host import BrowserHost, host_error


@pytest.fixture(autouse=True)
def repository_runs(monkeypatch):
    """The browser host needs the repository's own tooling, so fixture-backed runs are written under its runs/;
    the ones this test's SurfaceRuns created are removed afterwards (tmp_path runs go with their directory)."""
    created, construct = [], SurfaceRun.__init__

    def recording(self, *args, **kwargs):
        construct(self, *args, **kwargs)
        created.append(self.path)
    monkeypatch.setattr(SurfaceRun, "__init__", recording)
    yield
    runs = Path.cwd().resolve() / "runs"
    for path in created:
        if path.parent == runs and path.name.startswith("surface-"):
            shutil.rmtree(path, ignore_errors=True)


def result(run):
    return json.loads((run.path / "result.json").read_text())


def events(run):
    return [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]


def view_scenario(*callback_tools, **changes):
    data = scenario(**changes).model_dump()
    if callback_tools:
        data["steps"][2]["ui"]["callback_tools"] = list(callback_tools)
    return data


class SimHost(BrowserHost):
    """The real BrowserHost callback channel; only the Node sidecar is simulated like host.mjs."""

    def __init__(self, view_calls, *, save_error=False):
        self.pending, self.serial, self.process, self.reader = {}, 0, None, None
        self.callback, self.tasks = None, set()
        self.view_calls, self.save_error = view_calls, save_error
        self.replies, self.calls, self.saved, self.reads, self.rendered = [], [], False, 0, 0

    async def connect(self, scenario):
        return {"discovery": "passed", "negotiation": "passed"}

    async def send(self, value):
        self.replies.append(value)

    async def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        self.reads += name == "status"
        if name == "save":
            if self.save_error:
                return {"isError": True, "content": [{"type": "text", "text": "declined"}],
                        "structuredContent": {"error": "declined"}}
            self.saved = True
        return {"content": [], "structuredContent": {"status": "saved" if self.saved else "form_needed",
                                                     "token": "rotated-secret-canary", "reads": self.reads}}

    async def render(self, ui, source, path):
        self.rendered += 1
        completed = 0
        for index, (name, arguments) in enumerate(self.view_calls, 1):
            await self.handle_callback({"id": index, "name": name, "arguments": arguments})
            reply = self.replies[-1]
            if reply.get("abort"):  # host.mjs stops the step at once.
                raise LabError("UI step stopped by its callback", category="callback-interrupted")
            completed += "result" in reply and not reply.get("rejected") and reply.get("completion") is not False
        if ui["callback_required"] and not completed:
            raise LabError("UI completion callback timed out", code=1, category="interaction")
        return {"capabilities": {"rendering": "passed", "completion": "passed" if completed else "not-exercised"}}

    async def close(self):
        pass


async def test_view_reads_reach_the_service_every_time(tmp_path):
    host = SimHost([("status", {}), ("status", {}), ("save", {"value": "Test Shopper"})])
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(view_scenario("status", "save")), host,
                     secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert [reply["result"]["structuredContent"]["reads"] for reply in host.replies[:2]] == [2, 3]
    assert [name for name, _ in host.calls] == ["status", "prepare", "status", "status", "save", "status"]
    assert not any(key.startswith("ui:form:status") for key in run.state["operations"])


async def test_polling_reads_do_not_complete_a_step_that_submits_a_write(tmp_path):
    host = SimHost([("status", {}), ("status", {})])
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(view_scenario("status", "save")), host,
                     secrets={"access": "original-secret-canary"})
    assert await run.run() == 1
    assert [reply["completion"] for reply in host.replies] == [False, False]
    assert result(run)["failure_class"] == "interaction" and "save" not in [name for name, _ in host.calls]


async def test_identical_view_writes_are_one_logical_write(tmp_path):
    host = SimHost([("save", {"value": "Test Shopper"}), ("save", {"value": "Test Shopper"})])
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert [name for name, _ in host.calls].count("save") == 1
    assert all("result" in reply and not reply.get("rejected") for reply in host.replies)


async def test_rejected_view_write_reaches_the_view_as_an_error_result_and_fails_the_step(tmp_path):
    host = SimHost([("save", {"value": "Test Shopper"})], save_error=True)
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 1
    reply = host.replies[0]
    assert "error" not in reply and reply["rejected"] and reply["abort"]
    assert reply["result"]["isError"] is True  # A CallToolResult, not a JSON-RPC failure.
    report = result(run)
    assert report["failure_class"] == "tool/API" and "Service rejected save from UI form" in report["detail"]
    assert any(event["kind"] == "ui_callback" and event["is_error"] for event in events(run))


async def test_changed_view_write_pauses_the_run_visibly(tmp_path):
    host = SimHost([("save", {"value": "first"}), ("save", {"value": "different"})])
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 3
    assert host.replies[1]["abort"] and "Arguments changed" in host.replies[1]["error"]
    assert "Arguments changed for ui:form:save" in result(run)["detail"]
    assert [name for name, _ in host.calls].count("save") == 1


async def test_consent_gated_view_write_pauses_and_resumes_without_rerendering(tmp_path):
    from test_release_extensions import LATER, NOW, arguments

    from consumer_agent_testing_tool.transactions import TransactionFixture
    contract = scenario()
    fixture = TransactionFixture(tmp_path / "fixture.sqlite", "trial")
    contract.tools["save"].consent = {
        "reply_key": "save-approval", "now": NOW, "expires_at": LATER,
        "envelope": fixture.envelope("alice", "reservations.confirm", arguments()),
        "schema": {"type": "object", "properties": {"value": {"type": "string"}, "token": {"type": "string"}},
                   "required": ["value", "token"], "additionalProperties": False},
    }
    host = SimHost([("save", {"value": "Test Shopper"})])
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary"})
    assert await first.run() == 3
    assert host.replies[0]["abort"] and "Host approval required" in result(first)["detail"]
    assert "save" not in [name for name, _ in host.calls]
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path, replies={"save-approval": "approve_once"})
    assert await resumed.run() == 0
    assert host.rendered == 1 and [name for name, _ in host.calls].count("save") == 1
    assert "consent_consumed" in (first.path / "events.jsonl").read_text()


async def test_view_tool_outside_its_allowlist_is_refused_and_recorded(tmp_path):
    host = SimHost([("prepare", {})])
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 1
    assert "error" in host.replies[0] and not host.replies[0].get("abort")
    assert any(event["kind"] == "ui_callback_refused" for event in events(run))
    assert [name for name, _ in host.calls].count("prepare") == 1  # Only the scenario's own step.


class LinkHost(FakeHost):
    """Reports the UI resource the way host.mjs does, with a chosen tools/list descriptor link."""

    def __init__(self, descriptor_uri, catalog=None, sha256="a" * 64):
        super().__init__()
        self.descriptor_uri, self.catalog, self.sha256 = descriptor_uri, catalog, sha256

    async def connect(self, scenario):
        connected = await super().connect(scenario)
        if self.catalog is not None:
            connected.update(server_info={"name": "catalog-fixture", "version": "1.0.0"},
                             protocol_version="2025-11-25", server={"tools": {}}, instructions="Prefer reads",
                             tools=self.catalog)
        return connected

    async def render(self, ui, source, path):
        uri = ui["resource_uri"] or self.descriptor_uri
        self.observe({"kind": "ui_resource", "uri": uri, "tool": ui["source_tool"],
                      "source": "scenario" if ui["resource_uri"] else "tool-descriptor",
                      "descriptor_uri": self.descriptor_uri,
                      "descriptor_source": "tool-descriptor" if self.descriptor_uri else None,
                      "sha256": self.sha256, "encoding": "text"})
        self.tool_input = ui["tool_input"]
        return await super().render(ui, source, path)


@pytest.mark.parametrize("descriptor_uri,code", [("ui://test/preferences", 0), ("ui://test/other", 1), (None, 1)])
async def test_hard_coded_resource_uri_cannot_hide_a_broken_descriptor_link(tmp_path, descriptor_uri, code):
    run = SurfaceRun(tmp_path, scenario(), LinkHost(descriptor_uri), secrets={"access": "original-secret-canary"})
    assert await run.run() == code
    report = result(run)
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["layer"] == "host" and link["expected"] == "ui://test/preferences"
    assert link["actual"] == descriptor_uri and link["source"] == "scenario"
    assert link["status"] == ("passed" if code == 0 else "failed")
    assert report["compatibility_pass"] == (code == 0)
    assert report["run_lock"]["server"]["ui_resources"]["form"] == {"uri": "ui://test/preferences",
                                                                    "sha256": "a" * 64}


async def test_tool_input_carries_scenario_arguments_never_host_inputs(tmp_path):
    data = scenario().model_dump()
    data["tools"]["prepare"]["arguments_schema"] = {
        "type": "object", "properties": {"plan": {"type": "string"}, "shopper": {"type": "string"},
                                         "key": {"type": "string"}}, "additionalProperties": False}
    data["steps"][1]["arguments"] = {"plan": "basic", "shopper": {"$ref": "/fixture/name"},
                                     "key": {"$ref": "/secrets/access"}}
    host = LinkHost("ui://test/preferences")
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(data), host,
                     secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert host.tool_input == {"plan": "basic", "shopper": "Test Shopper", "key": "[REDACTED]"}
    assert host.calls[1][1]["token"] == "original-secret-canary"  # The service still gets host inputs.


async def test_automatic_link_check_alone_is_not_a_compatibility_pass(tmp_path):
    data = scenario().model_dump()
    for step in data["steps"]:
        step["checks"] = []
    bare = SurfaceRun(tmp_path, SurfaceScenario.model_validate(data), LinkHost("ui://test/preferences"),
                      secrets={"access": "original-secret-canary"})
    assert await bare.run() == 0
    report = result(bare)
    assert [(check["id"], check["status"], check["automatic"]) for check in report["checks"]] == [
        ("mcp-apps-resource-link", "passed", True)]
    assert not report["compatibility_pass"]  # The run asserted nothing about the service.
    authored = SurfaceRun(tmp_path, scenario(), LinkHost("ui://test/preferences"),
                          secrets={"access": "original-secret-canary"})
    assert await authored.run() == 0 and result(authored)["compatibility_pass"]


def test_link_check_id_is_reserved_for_the_host():
    data = scenario().model_dump()
    data["steps"][3]["checks"][0]["id"] = "mcp-apps-resource-link"
    with pytest.raises(ValidationError, match="reserved"):
        SurfaceScenario.model_validate(data)


@pytest.mark.parametrize("arguments", [
    {"all": {"$ref": "/secrets"}},  # The whole secret map.
    {"tok": {"$ref": "/steps/status/result/structuredContent/token"}},  # A token learned from a result.
    {"note": {"$ref": "/fixture/note"}},  # Ordinary input that embeds a supplied secret.
])
async def test_tool_input_never_carries_a_secret(tmp_path, arguments):
    data = scenario().model_dump()
    data["fixture"]["note"] = "use original-secret-canary here"
    data["tools"]["prepare"]["arguments_schema"] = {"type": "object", "additionalProperties": True}
    data["steps"][1]["arguments"] = arguments
    host = LinkHost("ui://test/preferences")
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(data), host,
                     secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert host.tool_input == dict.fromkeys(arguments, "[REDACTED]")
    assert "canary" not in json.dumps(host.tool_input)


async def test_tool_input_comes_from_the_recorded_call_so_a_resume_needs_no_earlier_reply(tmp_path):
    data = scenario().model_dump()
    data["tools"]["prepare"]["arguments_schema"] = {
        "type": "object", "properties": {"plan": {"type": "string"}}, "additionalProperties": False}
    data["steps"][1]["arguments"] = {"plan": {"$ref": "/replies/plan"}}
    data["steps"].insert(2, {"id": "confirm", "kind": "user", "reply_key": "confirm", "after": ["prepare"],
                             "description": "Confirm the plan"})
    data["steps"][3]["after"] = ["prepare", "confirm"]
    contract = SurfaceScenario.model_validate(data)
    host = LinkHost("ui://test/preferences")
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary"},
                       replies={"plan": "basic"})
    assert await first.run() == 3
    # The documented resume passes only the new reply; the sent plan is not resolved again.
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path, replies={"confirm": "yes"})
    assert await resumed.run() == 0
    assert host.tool_input == {"plan": "basic"} and [name for name, _ in host.calls].count("prepare") == 1


async def test_interrupted_read_only_ui_step_reconciles_from_its_completing_read(tmp_path):
    class ReadOnlyView(FakeHost):
        crash = True

        async def render(self, ui, source, path):
            self.rendered += 1
            await self.callback("status", {})
            if self.crash:
                self.crash = False
                raise RuntimeError("Browser host crashed after the read callback")
            return {"capabilities": {"rendering": "passed"}}
    data = scenario().model_dump()
    data["steps"][2]["ui"].update(callback_tools=["status"], actions=[])
    data["steps"][3]["checks"][0]["expected"] = "form_needed"
    contract = SurfaceScenario.model_validate(data)
    host = ReadOnlyView()
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary"})
    assert await first.run() == 2
    assert json.loads((first.path / "checkpoint.private.json").read_text())["ui_read_completions"] == {
        "form": "status"}
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path)
    assert await resumed.run() == 0
    assert host.rendered == 1 and resumed.state["steps"]["form"]["result"]["reconciled"] is True


async def test_invalid_descriptor_link_without_another_uri_is_a_failed_host_check(tmp_path):
    invalid = json.dumps("https://app.example.invalid/view.html")

    class InvalidLink(FakeHost):
        async def render(self, ui, source, path):  # As host.mjs reports a link no host can load.
            self.rendered += 1
            reason = f"Invalid UI resource URI on the tool definition: {invalid}"
            self.observe({"kind": "ui_resource", "uri": None, "tool": ui["source_tool"], "source": None,
                          "descriptor_uri": None, "descriptor_source": "tool-descriptor",
                          "descriptor_invalid": invalid, "bridge": ui["bridge"], "reason": reason})
            raise host_error({"error": f"UI resource link invalid: {reason}", "category": "tool/API"})
    data = scenario().model_dump()
    data["steps"][2]["ui"]["resource_uri"] = None
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(data), InvalidLink(),
                     secrets={"access": "original-secret-canary"})
    assert await run.run() == 1  # A server defect, not missing coverage (3).
    report = result(run)
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["status"] == "failed" and link["actual"] == invalid and link["expected"] is None
    assert report["failure_class"] == "assertion" and "Invalid UI resource URI" in report["detail"]
    assert "form" not in run.state["ui_attempts"]


def catalog(description="Read the preference state"):
    return [{"name": name, "description": description if name == "status" else name,
             "inputSchema": {"type": "object"}, "_meta": {"ui": {"resourceUri": "ui://test/preferences"}}}
            for name in ("status", "prepare", "save")]


async def test_run_lock_pins_server_identity_and_catalog_so_diff_tells_builds_apart(tmp_path):
    first = SurfaceRun(tmp_path, scenario(), LinkHost("ui://test/preferences", catalog()),
                       secrets={"access": "original-secret-canary"})
    same = SurfaceRun(tmp_path, scenario(), LinkHost("ui://test/preferences", catalog()),
                      secrets={"access": "original-secret-canary"})
    changed = SurfaceRun(tmp_path, scenario(), LinkHost("ui://test/preferences", catalog("Read state v2")),
                         secrets={"access": "original-secret-canary"})
    assert await first.run() == await same.run() == await changed.run() == 0
    server = result(first)["run_lock"]["server"]
    assert server["server_info"] == {"name": "catalog-fixture", "version": "1.0.0"}
    assert server["protocol_version"] == "2025-11-25" and server["tool_count"] == 3
    assert server["tools_sha256"] and server["instructions_sha256"] and server["capabilities_sha256"]
    assert server["tools_digest_basis"].startswith("tools/list as parsed by the MCP client SDK")
    assert compare(first.path, same.path)["lock_compatible"]
    difference = compare(first.path, changed.path)
    assert not difference["lock_compatible"]
    assert [item["path"] for item in difference["unexplained_lock_changes"]] == ["/server/tools_sha256"]
    assert compare(first.path, changed.path, treatments=["/server"])["lock_compatible"]
    recorded = {event["kind"]: event for event in events(first)}
    assert "tools" not in recorded["capabilities"] and len(recorded["server_catalog"]["tools"]) == 3


async def test_resume_refuses_a_changed_server_build(tmp_path):
    data = scenario().model_dump()
    data["steps"].insert(0, {"id": "reply", "kind": "user", "reply_key": "preference",
                            "description": "Provide a test preference"})
    data["steps"][1]["after"] = ["reply"]
    contract = SurfaceScenario.model_validate(data)
    first = SurfaceRun(tmp_path, contract, LinkHost("ui://test/preferences", catalog()),
                       secrets={"access": "original-secret-canary"})
    assert await first.run() == 3
    host = LinkHost("ui://test/preferences", catalog("Read state v2"))
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path, replies={"preference": "blue"})
    assert await resumed.run() == 2
    assert "tool catalog changed" in result(resumed)["detail"] and host.calls == []


async def test_behavioral_rerun_against_a_changed_build_is_allowed_and_diff_reports_it(tmp_path, monkeypatch):
    from consumer_agent_testing_tool import surface_host
    from consumer_agent_testing_tool.cli import parser
    from consumer_agent_testing_tool.extensions import execute_extension
    hosts = iter([LinkHost("ui://test/preferences", catalog()),
                  LinkHost("ui://test/preferences", catalog("Read state v2"))])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(surface_host, "BrowserHost", lambda *args: next(hosts))
    (tmp_path / "scenario.json").write_text(scenario().model_dump_json())
    (tmp_path / "secrets.json").write_text(json.dumps({"access": "original-secret-canary"}))
    assert await surface_host.run_from_cli(parser().parse_args(
        ["surface-run", "scenario.json", "--secrets-file", "secrets.json"])) == 0
    baseline = next((tmp_path / "runs").iterdir())
    assert await execute_extension(parser().parse_args(
        ["replay", str(baseline.relative_to(tmp_path)), "--mode", "rerun", "--scenario", "scenario.json",
         "--secrets-file", "secrets.json"])) == 0
    candidate = next(path for path in (tmp_path / "runs").iterdir() if path != baseline)
    difference = compare(baseline, candidate)
    assert [item["path"] for item in difference["unexplained_lock_changes"]] == ["/server/tools_sha256"]


async def test_close_does_not_wait_for_a_dead_reader():
    host = BrowserHost(Path.cwd())
    host.process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

    async def dead():
        return None
    host.reader = asyncio.create_task(dead())
    await host.reader
    started = time.monotonic()
    await host.close()
    assert time.monotonic() - started < 2 and host.process.returncode is not None


async def test_close_terminates_the_host_when_its_channel_is_broken():
    host = BrowserHost(Path.cwd())
    host.process = await asyncio.create_subprocess_exec(  # The child closes its end of the request pipe.
        sys.executable, "-c", "import os, time; os.close(0); time.sleep(30)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    host.reader = asyncio.create_task(host.read())
    await asyncio.sleep(1)
    with pytest.raises(LabError, match="Host channel failed during close|did not acknowledge close") as caught:
        await host.close()
    assert caught.value.category == "integration-failure"
    assert host.process.returncode is not None and host.reader.done() and not host.pending


async def test_cancelled_close_still_terminates_the_host():
    host = BrowserHost(Path.cwd())
    host.process = await asyncio.create_subprocess_exec(  # Reads requests and never answers.
        sys.executable, "-c", "import sys, time\nfor line in sys.stdin: pass\ntime.sleep(30)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    host.reader = asyncio.create_task(host.read())
    closing = asyncio.create_task(host.close())
    await asyncio.sleep(0.5)
    closing.cancel()  # As Ctrl-C does while the close acknowledgement is awaited.
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert host.process.returncode is not None and host.reader.done()


async def test_broken_host_channel_fails_requests_with_its_cause_not_a_timeout():
    host = BrowserHost(Path.cwd())
    host.process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; print('not json', flush=True); time.sleep(30)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    host.reader = asyncio.create_task(host.read())
    started = time.monotonic()
    with pytest.raises(LabError, match="Host transport closed unexpectedly") as caught:
        await host.request("connect", {})
    assert caught.value.category == "integration-failure"
    await host.close()
    assert time.monotonic() - started < 5


async def run_fixture(mode, edit, planner=None, **changes):
    root = Path.cwd()
    if not (root / "tooling/payment-ui/node_modules/playwright").exists() \
            or not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace-local Playwright tooling/browser is not installed")
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0", mode, cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 20)
        assert line, (await fixture.stderr.read()).decode()
        endpoint = json.loads(line)["endpoint"]
        data = scenario(endpoint=endpoint, allowed_origins=[endpoint.removesuffix("/mcp")], **changes).model_dump()
        edit(data)
        run = SurfaceRun(root, SurfaceScenario.model_validate(data), BrowserHost(root),
                         secrets={"access": "fixture-access"}, planner=planner)
        started = time.monotonic()
        code = await run.run()
        return code, run, result(run), time.monotonic() - started
    finally:
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()


def expect(selector, value):
    return {"kind": "expect_text", "selector": selector, "value": value}


@pytest.mark.parametrize("mode", ["ext-apps", "ext-apps-blob"])
async def test_official_style_server_without_ui_declaration_renders_with_real_tool_input(mode):
    def edit(data):
        data["tools"]["prepare"]["arguments_schema"] = {
            "type": "object", "properties": {"plan": {"type": "string"}},
            "required": ["plan"], "additionalProperties": False}
        data["steps"][1]["arguments"] = {"plan": "basic"}
        ui = data["steps"][2]["ui"]
        ui["resource_uri"] = None  # Discovered from the tool definition, as real hosts do.
        ui["actions"][0:0] = [expect("#tool-input", '{"plan":"basic"}'),
                              expect("#host-context", "light inline inline web sized locale zone prepare")]
    code, run, report, _ = await run_fixture(mode, edit)
    assert code == 0, report
    assert report["capabilities"]["negotiation"] == "passed" and report["capabilities"]["completion"] == "passed"
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["status"] == "passed" and link["source"] == link["descriptor_source"] == "tool-descriptor"
    rendered = run.state["steps"]["form"]["result"]
    assert rendered["resource_encoding"] == ("blob" if mode.endswith("blob") else "text")
    assert rendered["tool_input_keys"] == ["plan"]
    context = rendered["host_context"]
    assert (context["theme"], context["displayMode"], context["availableDisplayModes"], context["platform"]) == (
        "light", "inline", ["inline"], "web")
    # Fixed width, flexible height: the view reports its size (size-changed) up to maxHeight.
    dimensions = context["containerDimensions"]
    assert dimensions["width"] > 0 and dimensions["maxHeight"] > 0 and "height" not in dimensions
    assert context["locale"] and context["timeZone"] and context["tool_info"]
    capabilities = next(event for event in events(run) if event["kind"] == "capabilities")
    assert "extensions" not in capabilities["server"]
    assert report["compatibility_pass"]


async def test_result_metadata_alone_is_not_a_ui_link():
    def edit(data):
        data["steps"][2]["ui"]["resource_uri"] = None
    code, _, report, _ = await run_fixture("result-link", edit)
    assert code == 1, report
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["status"] == "failed" and link["source"] == "tool-result-meta" and link["actual"] is None
    assert report["capabilities"]["rendering"] == "passed" and not report["compatibility_pass"]


async def test_server_without_any_ui_link_is_unsupported_and_retryable():
    def edit(data):
        data["steps"][2]["ui"]["resource_uri"] = None
    code, run, report, _ = await run_fixture("no-ui", edit)
    assert code == 3, report
    assert report["failure_class"] == "unsupported" and report["detail"].startswith("UI unavailable")
    assert report["capabilities"]["negotiation"] == "unavailable" and report["resumable"]
    assert "form" not in run.state["ui_attempts"]  # Nothing was rendered or clicked.


async def test_view_gets_service_errors_as_results_and_the_run_fails_promptly():
    def edit(data):
        data["steps"][2]["ui"]["actions"] = [
            {"kind": "fill", "selector": "#name", "value": "Somebody Else"},
            {"kind": "click", "selector": "#save"}, expect("#status", "never shown")]
    code, run, report, elapsed = await run_fixture("ext-apps", edit, timeout_seconds=30)
    assert code == 1, report
    assert report["failure_class"] == "tool/API" and "Service rejected save from UI form" in report["detail"]
    assert elapsed < 20  # Stopped by the rejected write, not by the 30 s callback timeout.
    assert any(event["kind"] == "ui_callback" and event["is_error"] for event in events(run))


async def test_view_polling_reads_are_never_frozen():
    def edit(data):
        ui = data["steps"][2]["ui"]
        ui["callback_tools"] = ["save", "status"]
        ui["actions"][0:0] = [{"kind": "click", "selector": "#refresh"}, expect("#reads", "2"),
                              {"kind": "click", "selector": "#refresh"}, expect("#reads", "3")]
    code, run, report, _ = await run_fixture("ext-apps", edit)
    assert code == 0, report
    assert run.state["steps"]["verify"]["result"]["structuredContent"]["reads"] == 4


async def test_invalid_descriptor_link_fails_the_host_check_end_to_end():
    def edit(data):
        data["steps"][2]["ui"]["resource_uri"] = None
    code, run, report, _ = await run_fixture("invalid-link", edit)
    assert code == 1, report
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["status"] == "failed" and link["actual"] == '"https://app.example.invalid/view.html"'
    assert report["failure_class"] == "assertion" and "Invalid UI resource URI" in report["detail"]
    assert report["capabilities"]["negotiation"] == "unavailable" and not report["compatibility_pass"]
    assert "form" not in run.state["ui_attempts"]  # Nothing was rendered or clicked.


async def test_openai_template_alone_does_not_pass_an_mcp_app_link_check():
    def edit(data):
        data["steps"][2]["ui"]["resource_uri"] = None
    code, _, report, _ = await run_fixture("openai-template", edit)
    assert code == 1, report
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["status"] == "failed" and link["descriptor_source"] == "openai-output-template"
    assert link["actual"] == link["expected"] == "ui://test/preferences"
    assert "_meta.ui.resourceUri" in link["detail"] and report["capabilities"]["rendering"] == "passed"
