"""MCP Apps host completion (P3.5) and dual-era negotiation (P3.1): generic-host evidence, not Muse."""
import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path

import pytest
from test_surface import FakeHost, scenario
from test_surface_p0_mcp_apps import (  # noqa: F401 (repository_runs is an autouse fixture)
    SimHost,
    events,
    expect,
    repository_runs,
    result,
    run_fixture,
    view_scenario,
)

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.surface import (
    APP_CONTEXT_LIMIT,
    APP_MESSAGE_QUEUED,
    MCP_APPS_HOST,
    SurfaceRun,
    SurfaceScenario,
)
from consumer_agent_testing_tool.surface_host import BrowserHost

# MCP Apps 2026-01-26 "Restrictive Default", plus the sandbox proxy's own required directives.
DEFAULT_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; media-src 'self' data:; connect-src 'none'; frame-src 'none'; "
               "object-src 'none'; base-uri 'self'")
PROXY = {"origin": "https://surface-app.invalid", "host_origin": "https://surface-host.invalid"}


def kinds(run, kind):
    return [event for event in events(run) if event["kind"] == kind]


def discovered(data):
    data["steps"][2]["ui"]["resource_uri"] = None  # Found from the tool definition, as real hosts do.


def test_scenario_digest_is_unchanged_until_negotiation_is_set():
    contract = scenario()
    # The digest a scenario had before the field existed: the same dump without it.
    before = hashlib.sha256(contract.model_dump_json(exclude={"version_negotiation"}).encode()).hexdigest()
    assert contract.digest() == before
    assert scenario(version_negotiation="auto").digest() != before


async def test_app_only_tool_steps_are_refused_before_any_call(tmp_path):
    class AppOnly(FakeHost):
        async def connect(self, scenario):
            return {**await super().connect(scenario),
                    "tool_visibility": {"prepare": {"model": False, "app": True, "declared": ["app"]}}}
    host = AppOnly()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 2
    report = result(run)
    assert report["failure_class"] == "setup/infrastructure" and report["failure_stage"] == "connect"
    assert "not visible to the model" in report["detail"] and host.calls == []


async def test_an_invalid_visibility_declaration_is_the_servers_defect_not_the_scenarios(tmp_path):
    class Invalid(FakeHost):
        async def connect(self, scenario):
            # host.mjs fails closed on a value that is not an array, and passes it on already serialized.
            return {**await super().connect(scenario),
                    "tool_visibility": {"prepare": {"model": False, "app": False, "declared": '"app"',
                                                    "invalid": True}}}
    host = Invalid()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 1
    report = result(run)
    assert (report["failure_class"], report["failure_stage"]) == ("tool/API", "connect")
    assert 'declares _meta.ui.visibility "app", which is not an array' in report["detail"]
    assert "UI callback tool" not in report["detail"] and host.calls == []  # No advice a view could follow.


async def test_view_calls_to_model_only_tools_are_refused_and_the_step_continues(tmp_path):
    class ModelOnly(SimHost):
        async def connect(self, scenario):
            return {**await super().connect(scenario),
                    "tool_visibility": {"status": {"model": True, "app": False, "declared": ["model"]}}}
    host = ModelOnly([("status", {}), ("save", {"value": "Test Shopper"})])
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(view_scenario("status", "save")), host,
                     secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert "error" in host.replies[0] and not host.replies[0].get("abort")
    refused = kinds(run, "ui_callback_refused")
    assert refused[0]["tool"] == "status" and "visibility" in refused[0]["reason"]
    # The model-visible steps still read status; the view's read never reached the service.
    assert [name for name, _ in host.calls] == ["status", "prepare", "save", "status"]


class ContextHost(FakeHost):
    """Reports a view's model-context updates the way host.mjs observes them."""

    def __init__(self, *updates):
        super().__init__()
        self.updates = updates

    async def render(self, ui, source, path):
        for update in self.updates:
            self.observe({"kind": "ui_model_context", "source": "ui/update-model-context", **update})
        return await super().render(ui, source, path)


async def test_latest_model_context_reaches_the_agent_as_app_context(tmp_path):
    from test_surface_agent import driver
    planner, api = driver()
    host = ContextHost({"content": [{"type": "text", "text": "Earlier context"}]},
                       {"content": [{"type": "text", "text": "Review form for Test Shopper"}],
                        "structuredContent": {"form": "preferences", "access_token": "secret-context-canary"}})
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret-context-canary"}, planner=planner)
    assert await run.run() == 0
    observations = [json.loads(row["messages"][-1]["content"]) for row in api.inputs[1:]]
    assert ["app_context" in observation for observation in observations] == [False, False, True]
    context = observations[-1]["app_context"]
    assert context["step"] == "form" and context["source"] == "ui/update-model-context"
    assert context["content"] == [{"type": "text", "text": "Review form for Test Shopper"}]
    assert context["structuredContent"]["form"] == "preferences"
    submitted = json.dumps(api.inputs)
    assert "secret-context-canary" not in submitted and "Earlier context" not in submitted


async def test_an_empty_model_context_update_clears_app_context(tmp_path):
    host = ContextHost({"content": [{"type": "text", "text": "Stale"}]}, {"cleared": True})
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 0
    assert "app_context" not in run.observation([])


async def test_oversized_app_context_is_withheld_from_the_model(tmp_path):
    host = ContextHost({"content": [{"type": "text", "text": "x" * (APP_CONTEXT_LIMIT + 1)}]})
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 0
    context = run.observation([])["app_context"]
    assert "content" not in context and str(APP_CONTEXT_LIMIT) in context["omitted"]


async def test_report_labels_the_lane_generic_mcp_apps_host_evidence(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    assert await run.run() == 0
    report = result(run)
    assert report["mcp_apps_host"]["evidence"] == MCP_APPS_HOST["evidence"] == "generic MCP Apps host; not Muse evidence"
    assert report["mcp_apps_host"]["version_negotiation"] == "legacy"
    assert any("not Muse evidence" in limit for limit in report["evidence_limits"])


async def test_negotiated_era_is_part_of_the_pinned_server_identity(tmp_path):
    class Modern(FakeHost):
        async def connect(self, scenario):
            return {**await super().connect(scenario), "server_info": {"name": "modern", "version": "2"},
                    "protocol_version": "2026-07-28", "protocol_era": "modern", "server": {"tools": {}},
                    "instructions": None, "tools": []}
    run = SurfaceRun(tmp_path, scenario(version_negotiation="auto"), Modern(), secrets={"access": "secret"})
    assert await run.run() == 0
    server = result(run)["run_lock"]["server"]
    assert (server["protocol_version"], server["protocol_era"]) == ("2026-07-28", "modern")


async def test_native_views_run_in_a_sandbox_proxy_under_the_restrictive_default_csp():
    code, run, report, _ = await run_fixture("ext-apps", discovered)
    assert code == 0, report
    sandbox = kinds(run, "ui_sandbox")[0]
    assert sandbox["proxy"] == PROXY and sandbox["renderer"] == "native"
    assert sandbox["csp"]["source"] == "restrictive-default" and sandbox["csp"]["policy"] == DEFAULT_CSP
    assert sandbox["metadata"] == "none" and sandbox["permissions"] == {"granted": [], "allow": None}
    rendered = run.state["steps"]["form"]["result"]
    # The official App SDK without an onteardown handler answers -32601; the host records it.
    assert rendered["teardown"] == {"status": "rejected", "tool_cancelled": False, "code": -32601}
    assert kinds(run, "ui_teardown")[0]["reason"] == "The UI step completed"
    size = rendered["view_size"]
    assert size["count"] >= 1 and 0 < size["applied_height"] <= 6000


def host_contract(data):
    origin = data["allowed_origins"][0]
    discovered(data)
    data["browser_origins"] = [origin]
    data["tools"]["poll"] = {"effect": "read"}
    ui = data["steps"][2]["ui"]
    ui["callback_tools"] = ["save", "poll", "status"]
    other = origin.replace("127.0.0.1", "localhost")
    ui["actions"][0:0] = [
        expect("#sandbox", "https://surface-app.invalid proxied clipboard-write=true camera=false"),
        expect("#visibility", "status:rejected poll:allowed"), expect("#display", "inline"),
        # The declared origin passes CSP; only the undeclared one is a violation.
        expect("#csp", f"violations:connect-src {other}")]


async def test_host_contract_csp_permissions_visibility_view_requests_and_teardown():
    code, run, report, _ = await run_fixture("host-contract", host_contract)
    assert code == 0, report
    origin = json.loads(run.scenario.model_dump_json())["allowed_origins"][0]
    sandbox = kinds(run, "ui_sandbox")[0]
    # Content-level _meta.ui wins over the resources/list entry's different declaration.
    assert sandbox["metadata"] == "resource-content" and sandbox["csp"]["source"] == "declared"
    assert sandbox["csp"]["domains"]["connectDomains"] == [origin] and sandbox["csp"]["rejected"] == []
    assert f"connect-src 'self' {origin};" in sandbox["csp"]["policy"] and "frame-src 'none'" in sandbox["csp"]["policy"]
    assert sandbox["permissions"] == {"granted": ["clipboardWrite"], "allow": "clipboard-write"}
    assert sandbox["prefers_border"] is True and sandbox["proxy"] == PROXY
    denied = kinds(run, "ui_callback_denied")
    assert [(row["tool"], row["reason"]) for row in denied] == [("status", "visibility excludes app")]
    # The measured visibility stays in the flat event row; "visibility" there is the envelope's audience.
    connected = kinds(run, "capabilities")[0]
    assert connected["visibility"] == "developer"
    assert connected["tool_visibility"] == {"status": {"model": True, "app": False, "declared": ["model"]},
                                            "poll": {"model": False, "app": True, "declared": ["app"]}}
    message = kinds(run, "ui_message")[0]
    assert message["content"] == [{"type": "text", "text": "Please confirm my saved preference"}]
    assert message["disposition"] == "recorded; not delivered to a conversation"
    link = kinds(run, "ui_open_link")[0]
    assert link["url"] == "https://example.com/help" and link["disposition"] == "recorded; not opened"
    assert [(row["requested"], row["granted"]) for row in kinds(run, "ui_display_mode")] == [("fullscreen", "inline")]
    assert kinds(run, "ui_teardown")[0] == {**kinds(run, "ui_teardown")[0], "status": "acknowledged",
                                            "tool_cancelled": False}
    assert any(row["data"] == '"teardown received"' for row in kinds(run, "ui_log"))
    # The latest ui/update-model-context is the app context a model would see next.
    context = run.observation([])["app_context"]
    assert context["structuredContent"] == {"form": "preferences", "step": "review"}
    assert context["content"] == [{"type": "text", "text": "Preference form open for review"}]
    rendered = run.state["steps"]["form"]["result"]
    assert rendered["view_requests"]["ui_model_context"] == 1 and rendered["teardown"]["status"] == "acknowledged"
    # Reads: two status steps and the view's one app-only poll; its status call never arrived.
    assert run.state["steps"]["verify"]["result"]["structuredContent"]["reads"] == 3
    assert report["compatibility_pass"]


async def test_agent_sees_the_views_model_context_as_app_context():
    from test_surface_agent import driver
    planner, api = driver()
    code, _, report, _ = await run_fixture("host-contract", host_contract, planner=planner)
    assert code == 0, report
    last = json.loads(api.inputs[-1]["messages"][-1]["content"])
    assert last["app_context"]["content"] == [{"type": "text", "text": "Preference form open for review"}]
    assert "fixture-access" not in json.dumps(api.inputs)


async def test_listing_metadata_is_the_csp_fallback():
    code, run, report, _ = await run_fixture("listing-csp", discovered)
    assert code == 0, report
    sandbox = kinds(run, "ui_sandbox")[0]
    assert sandbox["metadata"] == "resource-listing"
    assert sandbox["csp"]["domains"]["connectDomains"] == [run.scenario.allowed_origins[0]]


async def test_view_stopped_before_its_result_gets_tool_cancelled_then_teardown():
    code, run, report, elapsed = await run_fixture("early-call", discovered, timeout_seconds=30)
    assert code == 1, report
    assert report["failure_class"] == "tool/API" and "Service rejected save from UI form" in report["detail"]
    teardown = kinds(run, "ui_teardown")[-1]
    assert teardown["status"] == "acknowledged" and teardown["tool_cancelled"] is True
    received = kinds(run, "ui_log")[-1]["data"]
    assert "ui/notifications/tool-input,ui/notifications/tool-cancelled,ui/resource-teardown" in received
    assert "tool-result" not in received and elapsed < 20


async def test_legacy_openai_shim_records_view_requests_and_fails_loudly():
    def edit(data):
        ui = data["steps"][2]["ui"]
        ui["bridge"] = "legacy-openai"
        ui["actions"][0:0] = [expect("#shim", "light inline locale max state:null>Test Shopper "
                                     "globals:Test Shopper mode:inline modal:rejected unknown:absent")]
    code, run, report, _ = await run_fixture("legacy-openai", edit)
    assert code == 0, report
    sources = {row["kind"]: row["source"] for row in events(run)
               if row["kind"] in ("ui_model_context", "ui_message", "ui_open_link", "ui_display_mode")}
    assert sources == {"ui_model_context": "openai.setWidgetState", "ui_message": "openai.sendFollowUpMessage",
                       "ui_open_link": "openai.openExternal", "ui_display_mode": "openai.requestDisplayMode"}
    assert sorted(row["member"] for row in kinds(run, "ui_bridge_unsupported")) == ["requestModal", "someFutureMember"]
    rendered = run.state["steps"]["form"]["result"]
    assert rendered["view_size"]["applied_height"] == 321 and "teardown" not in rendered
    sandbox = kinds(run, "ui_sandbox")[0]
    assert sandbox["proxy"] is None and sandbox["metadata"] == "resource-content (openai/widgetCSP)"
    assert sandbox["csp"]["domains"]["connectDomains"] == [run.scenario.allowed_origins[0]]
    assert run.observation([])["app_context"]["structuredContent"] == {"draft": "Test Shopper"}


async def test_declared_csp_sources_that_could_widen_the_policy_are_refused():
    code, run, report, _ = await run_fixture("hostile-csp", discovered)
    assert code == 0, report
    sandbox = kinds(run, "ui_sandbox")[0]
    assert sandbox["csp"]["policy"] == (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://ok.example; img-src 'self' data:; font-src 'self'; media-src 'self' data:; "
        "frame-src 'none'; object-src 'none'; base-uri 'self'")
    assert {(row["field"], row["value"]) for row in sandbox["csp"]["rejected"]} == {
        ("connectDomains", '"*"'), ("connectDomains", "\"'unsafe-eval'\""),
        ("connectDomains", '"https://a.example; script-src *"'), ("connectDomains", '"https:"'),
        ("connectDomains", '"https://x.example/path"'), ("connectDomains", "7"),
        ("resourceDomains", '"https://one.example"')}
    # Only known Permission Policy features; a dedicated domain is recorded, never applied.
    assert sandbox["permissions"] == {"granted": ["camera"], "allow": "camera"}
    assert sandbox["domain"] == {"requested": '"abc.example"', "applied": False}


async def test_app_only_tools_cannot_be_called_from_tool_steps_end_to_end():
    def edit(data):
        host_contract(data)
        data["steps"][3]["tool"] = "poll"  # The verify step would call the view-only tool.
    code, run, report, _ = await run_fixture("host-contract", edit)
    assert code == 2, report
    assert report["failure_stage"] == "connect" and "poll is not visible to the model" in report["detail"]
    assert run.state["steps"] == {}


@contextlib.asynccontextmanager
async def fixture_endpoint(mode):
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
        yield json.loads(line)["endpoint"]
    finally:
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()


async def test_host_cancellation_tears_the_view_down_before_closing():
    root = Path.cwd()
    async with fixture_endpoint("host-contract") as endpoint:
        data = scenario(endpoint=endpoint, allowed_origins=[endpoint.removesuffix("/mcp")]).model_dump()
        host_contract(data)
        data["steps"][2]["ui"]["actions"] = []  # Nothing submits: the step waits for its callback.
        run = SurfaceRun(root, SurfaceScenario.model_validate(data), BrowserHost(root),
                         secrets={"access": "fixture-access"})
        task = asyncio.create_task(run.run())
        for _ in range(400):
            if run.state["capabilities"]["rendering"] == "passed":
                break
            await asyncio.sleep(0.05)
        task.cancel()  # As Ctrl-C does: the coordinator closes the host mid-render.
        assert await task == 3
    assert [(row["reason"], row["status"]) for row in kinds(run, "ui_teardown")] == [
        ("The host closed", "acknowledged")]
    assert any(row["data"] == '"teardown received"' for row in kinds(run, "ui_log"))
    assert result(run)["failure_class"] == "interrupted"


async def test_a_view_that_never_answers_teardown_does_not_hold_the_run():
    code, run, report, elapsed = await run_fixture("silent-teardown", discovered)
    assert code == 0, report
    # The host waits a bounded time for the response, records that none came, and closes the view.
    assert run.state["steps"]["form"]["result"]["teardown"] == {"status": "no-response", "tool_cancelled": False}
    assert elapsed < 30


async def test_a_view_that_navigates_its_sandbox_proxy_away_loses_the_bridge():
    def edit(data):
        discovered(data)
        data["steps"][2]["ui"]["actions"] = []  # Only the escaped document would call save.
    code, run, report, _ = await run_fixture("proxy-escape", edit, timeout_seconds=4)
    assert code == 1, report
    assert report["failure_class"] == "interaction" and "callback timed out" in report["detail"]
    # The data: document (opaque origin, no CSP) is no bridge peer: its tools/call never arrived.
    assert kinds(run, "ui_message_dropped")[0]["origin"] == "null"
    assert not [row for row in kinds(run, "host_action") if row["tool"] == "save"]


async def test_the_host_exits_promptly_when_its_coordinator_channel_closes_mid_render():
    root = Path.cwd()
    async with fixture_endpoint("silent-teardown") as endpoint:
        data = scenario(endpoint=endpoint, allowed_origins=[endpoint.removesuffix("/mcp")]).model_dump()
        discovered(data)
        data["steps"][2]["ui"]["actions"] = []  # The step waits for a callback that never comes.
        contract = SurfaceScenario.model_validate(data)
        host, seen = BrowserHost(root), []
        host.observe = seen.append
        try:
            await host.connect(contract)
            source = await host.call("prepare", {})
            ui = {**contract.steps[2].ui.model_dump(exclude={"network_grants", "network_reconcile"}),
                  "source_tool": "prepare", "tool_input": {}}
            rendering = asyncio.create_task(host.render(ui, source, root / ".cache/surface-host"))
            for _ in range(400):
                if any(row.get("rendering") == "passed" for row in seen):
                    break
                await asyncio.sleep(0.05)
            assert any(row.get("rendering") == "passed" for row in seen)
            started = time.monotonic()
            host.process.stdin.close()  # The coordinator is gone: no close request, only end of input.
            assert await asyncio.wait_for(host.process.wait(), 10) == 0
            # Teardown is bounded even for a view that never answers; the browser closes with the host.
            # The host forces its exit after 3 s; the bound that matters is the coordinator's 5 s.
            assert time.monotonic() - started < 4.5
            with pytest.raises(LabError, match="Host transport closed"):
                await rendering
        finally:
            await host.close()


async def test_auto_negotiation_reaches_a_2026_07_28_server():
    code, run, report, _ = await run_fixture("modern", discovered, version_negotiation="auto")
    assert code == 0, report
    server = report["run_lock"]["server"]
    assert (server["protocol_version"], server["protocol_era"]) == ("2026-07-28", "modern")
    assert server["server_info"]["name"] == "surface-preferences-fixture"
    # The server links its UI only because the request itself declared MCP Apps (2026-07-28 _meta).
    link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
    assert link["status"] == "passed" and report["mcp_apps_host"]["version_negotiation"] == "auto"
    connected = kinds(run, "capabilities")[0]
    assert (connected["protocol_era"], connected["version_negotiation"]) == ("modern", "auto")


async def test_the_default_handshake_cannot_reach_a_modern_only_server():
    code, _, report, _ = await run_fixture("modern", discovered)
    assert code == 3, report
    assert report["failure_class"] == "unsupported" and report["failure_stage"] == "connect"
    assert "version_negotiation: auto" in report["detail"] and report["resumable"]


async def test_auto_negotiation_falls_back_to_initialize_for_2025_servers():
    code, _, report, _ = await run_fixture("ext-apps", discovered, version_negotiation="auto")
    assert code == 0, report
    server = report["run_lock"]["server"]
    assert server["protocol_era"] == "legacy" and server["protocol_version"].startswith("2025-")


# M4: a view's resources/read and resources/list, and ui/message delivery.
def resource_view(data):
    discovered(data)
    ui = data["steps"][2]["ui"]
    ui["callback_resources"] = ["fixture://notes", "fixture://large"]
    # Read, withheld over the 5 MiB relay bound, denied (not named), then the filtered listing.
    ui["actions"][0:0] = [expect("#resources", "caps:{} read:Synthetic note large:error -32000 "
                                 "hidden:error -32000 list:fixture://notes,fixture://large")]


async def test_views_read_and_list_only_the_resources_their_step_names():
    code, run, report, _ = await run_fixture("resources", resource_view)
    assert code == 0, report
    reads = {row["uri"]: row for row in kinds(run, "ui_resource_read")}
    assert reads["fixture://notes"]["disposition"] == "proxied"
    assert reads["fixture://notes"]["contents"] == [{
        "uri": "fixture://notes", "mimeType": "text/plain", "encoding": "text", "characters": 14,
        "sha256": hashlib.sha256(b"Synthetic note").hexdigest()}]
    assert (reads["fixture://large"]["disposition"], reads["fixture://hidden"]["disposition"]) == (
        "withheld", "denied")
    assert reads["fixture://large"]["characters"] > 5 * 1024 * 1024 and "callback_resources" in reads[
        "fixture://hidden"]["reason"]
    listing = kinds(run, "ui_resource_list")[0]
    assert (listing["disposition"], listing["listed"], listing["shown"]) == (
        "proxied", 4, ["fixture://notes", "fixture://large"])
    # The denied resource never reached the server; evidence keeps digests, never content.
    assert run.state["steps"]["verify"]["result"]["structuredContent"]["resourceReads"] == {
        "notes": 1, "hidden": 0, "large": 1}
    assert "Synthetic note" not in (run.path / "events.jsonl").read_text()
    requests = run.state["steps"]["form"]["result"]["view_requests"]
    assert (requests["ui_resource_read"], requests["ui_resource_list"]) == (3, 1)
    # Without browser_origins nothing reaches the network, so nothing needs a network guard.
    assert kinds(run, "ui_sandbox")[0]["network"] == {"destinations": "none"}
    assert report["mcp_apps_host"]["network_writes"]["unconsented"] == 0


async def test_views_get_no_resources_unless_their_step_names_some():
    def edit(data):
        discovered(data)
        data["steps"][2]["ui"]["actions"][0:0] = [expect(
            "#resources", "caps:null read:error -32601 large:error -32601 hidden:error -32601 list:error -32601")]
    code, run, report, _ = await run_fixture("resources", edit)
    assert code == 0, report
    assert not kinds(run, "ui_resource_read") and not kinds(run, "ui_resource_list")


def test_callback_resources_are_exact_uris_for_the_mcp_app_bridge_and_stay_out_of_unset_digests():
    data = scenario().model_dump()
    assert "callback_resources" not in data["steps"][2]["ui"]  # Earlier scenario digests hold.
    for value, bridge in ((["fixture://notes", "fixture://notes"], "mcp-app"), (["notes"], "mcp-app"),
                          (["fixture://notes"], "legacy-openai")):
        changed = scenario().model_dump()
        changed["steps"][2]["ui"].update(callback_resources=value, bridge=bridge)
        with pytest.raises(ValueError, match="callback_resources"):
            SurfaceScenario.model_validate(changed)
    data["steps"][2]["ui"]["callback_resources"] = ["fixture://notes"]
    assert SurfaceScenario.model_validate(data).digest() != scenario().digest()


class MessageHost(FakeHost):
    """Reports a view's ui/message requests the way host.mjs observes them."""

    def __init__(self, *contents):
        super().__init__()
        self.contents = contents

    async def render(self, ui, source, path):
        for content in self.contents:
            self.observe({"kind": "ui_message", "source": "ui/message", "role": "user", "content": content})
        return await super().render(ui, source, path)


async def test_view_messages_reach_the_agent_once_as_app_messages(tmp_path):
    from test_surface_agent import driver
    planner, api = driver()
    host = MessageHost([{"type": "text", "text": "Please confirm my saved preference"}],
                       [{"type": "text", "text": "My key is secret-message-canary"}],
                       [{"type": "text", "text": "x" * (APP_CONTEXT_LIMIT + 1)}])
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret-message-canary"}, planner=planner)
    assert await run.run() == 0
    observed = [json.loads(row["messages"][-1]["content"]) for row in api.inputs[1:]]
    assert ["app_messages" in observation for observation in observed] == [False, False, True]
    first, masked, large = observed[-1]["app_messages"]
    assert first == {"step": "form", "source": "ui/message", "role": "user",
                     "content": [{"type": "text", "text": "Please confirm my saved preference"}]}
    assert "secret-message-canary" not in json.dumps(api.inputs) and "content" in masked
    assert large["content"] is None and str(APP_CONTEXT_LIMIT) in large["omitted"]
    # Delivered once, as a conversation message is: no later observation repeats them.
    delivered = [row for row in kinds(run, "agent_observation") if "app_messages" in row]
    assert len(delivered) == 1 and "app_messages" not in run.observation([])
    assert {row["disposition"] for row in kinds(run, "ui_message")} == {
        "queued for the agent's next observation as an app message"}


async def test_scripted_runs_record_view_messages_without_a_conversation(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), MessageHost([{"type": "text", "text": "Hello"}]),
                     secrets={"access": "secret"})
    assert await run.run() == 0
    assert kinds(run, "ui_message")[0]["disposition"] == "recorded; not delivered to a conversation"
    assert "app_messages" not in run.observation([])


async def test_agent_receives_the_views_ui_message_as_an_app_message():
    from test_surface_agent import driver
    planner, api = driver()
    code, run, report, _ = await run_fixture("host-contract", host_contract, planner=planner)
    assert code == 0, report
    last = json.loads(api.inputs[-1]["messages"][-1]["content"])
    assert last["app_messages"] == [{"step": "form", "source": "ui/message", "role": "user",
                                     "content": [{"type": "text", "text": "Please confirm my saved preference"}]}]
    assert kinds(run, "ui_message")[0]["disposition"] == "queued for the agent's next observation as an app message"


# M6: the native renderer's network handling for views that reach browser_origins.
def click(selector):
    return {"kind": "click", "selector": selector}


def native_network(data):
    discovered(data)
    data["browser_origins"] = list(data["allowed_origins"])
    data["steps"][2]["ui"]["actions"][0:0] = [
        click("#net-read"), expect("#network", "asset loaded"),  # Loopback, through a same-origin redirect.
        click("#net-write"), expect("#network", "saved"),
        click("#net-escape"), expect("#network", "Blocked"),  # A redirect to an origin only the CSP admits.
        click("#net-post-redirect"), expect("#network", "Blocked")]  # A redirect never carries a write.


async def test_native_views_reach_allowlisted_loopback_and_their_writes_are_unconsented_host_actions():
    code, run, report, _ = await run_fixture("native-network", native_network)
    assert code == 0, report
    origin = run.scenario.allowed_origins[0]
    assert kinds(run, "ui_sandbox")[0]["network"] == {
        "destinations": "browser_origins", "local_network_access": "granted", "redirects": "rechecked",
        "writes": "unconsented; recorded as host actions"}
    # Each write that reaches the server is recorded first, including one answered with a redirect.
    writes = [row for row in kinds(run, "host_action") if row.get("channel") == "ui-network"]
    body = hashlib.sha256(b"synthetic-input").hexdigest()
    assert [(row["operation"], row["method"], row["url"], row["consent"], row["body_bytes"], row["body_sha256"])
            for row in writes] == [("ui:form:network-write-1", "POST", origin + "/mutation", "none", 15, body),
                                   ("ui:form:network-write-2", "POST", origin + "/redirect-post", "none", 15, body)]
    assert report["mcp_apps_host"]["network_writes"]["unconsented"] == 2
    denied = [row for row in kinds(run, "network_denied") if row.get("enforcement") == "chromium-response-interceptor"]
    assert [(row["url"].endswith("/escaped") and not row["url"].startswith(origin), row["method"]) for row in denied] \
        == [(True, "GET"), (False, "POST")]
    # The server saw one direct write and no request at the other origin.
    service = run.state["steps"]["verify"]["result"]["structuredContent"]
    assert (service["browserWrites"], service["escapedRequests"]) == (1, 0)


async def test_app_messages_are_masked_by_the_observation_itself(tmp_path):
    from test_surface_agent import driver
    planner, _ = driver()
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret-message-canary"}, planner=planner)
    run.active_ui = run.scenario.steps[2]
    run.observe_host({"kind": "ui_message", "source": "ui/message", "role": "user",
                      "content": [{"type": "text", "text": "My key is secret-message-canary"}]})
    observation = run.observation([])
    assert observation["app_messages"][0]["step"] == "form"
    assert "secret-message-canary" not in json.dumps(observation)


async def test_native_network_writes_are_recorded_before_they_are_sent_and_survive_the_scrub(tmp_path):
    write = {"url": "http://127.0.0.1:8765/mutation", "method": "POST", "body_sha256": "0" * 64, "body_bytes": 0}

    class Writer(FakeHost):
        async def render(self, ui, source, path):  # As host.mjs asks before it lets a write through.
            self.recorded = await self.callback("__surface_network_write", write)
            with pytest.raises(LabError, match="Unexpected browser network write"):
                await self.callback("__surface_network_write", {"url": write["url"]})
            return await super().render(ui, source, path)
    host = Writer()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 0
    assert host.recorded == {"recorded": True}
    row = next(row for row in kinds(run, "host_action") if row.get("channel") == "ui-network")
    assert (row["operation"], row["tool"], row["effect"], row["consent"], row["url"]) == (
        "ui:form:network-write-1", None, "write", "none", write["url"])
    with pytest.raises(LabError, match="Unexpected browser network write"):  # Only during a UI step.
        await run.callback("__surface_network_write", write)
    # The finished run keeps the count in its scrubbed checkpoint, so a declined resume reports it too.
    resumed = SurfaceRun(tmp_path, scenario(), FakeHost(), resume=run.path)
    assert await resumed.run() == 0
    assert result(resumed)["mcp_apps_host"]["network_writes"]["unconsented"] == 1


# Follow-ups: what happens to a ui/message no model can read, and what reaches the view.
class AskingHost(FakeHost):
    """Asks the coordinator about each ui/message, as host.mjs does before it answers the view."""

    def __init__(self, count=1):
        super().__init__()
        self.count, self.answers = count, []

    async def render(self, ui, source, path):
        for index in range(1, self.count + 1):
            self.answers.append(await self.callback("__surface_app_message", {
                "kind": "ui_message", "source": "ui/message", "role": "user",
                "content": [{"type": "text", "text": f"Synthetic message {index}"}]}))
        return await super().render(ui, source, path)


async def test_a_view_message_no_model_turn_follows_is_refused_and_the_view_told(tmp_path):
    from test_surface_agent import driver
    planner, api = driver()
    data = scenario().model_dump()
    data["steps"] = data["steps"][:3]  # The UI step is the workflow's last: the driver asks the model nothing more.
    host = AskingHost()
    run = SurfaceRun(tmp_path, SurfaceScenario.model_validate(data), host, secrets={"access": "secret"},
                     planner=planner)
    assert await run.run() == 0
    assert host.answers == [{"queued": False}]  # host.mjs answers the view isError: true.
    assert [row["disposition"] for row in kinds(run, "ui_message")] == [
        "recorded; not delivered: no model turn follows the workflow's last step"]
    assert len(api.inputs) == 3 and "Synthetic message" not in json.dumps(api.inputs)
    assert not [row for row in kinds(run, "agent_observation") if "app_messages" in row]


async def test_view_messages_past_the_twenty_waiting_are_refused(tmp_path):
    from test_surface_agent import driver
    planner, api = driver()
    host = AskingHost(21)
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"}, planner=planner)
    assert await run.run() == 0
    assert host.answers == [{"queued": True}] * 20 + [{"queued": False}]
    assert kinds(run, "ui_message")[-1]["disposition"] == "recorded; not delivered: 20 app messages were already waiting"
    delivered = json.loads(api.inputs[-1]["messages"][-1]["content"])["app_messages"]
    assert [item["content"][0]["text"] for item in delivered] == [f"Synthetic message {index}" for index in range(1, 21)]


async def test_app_messages_no_model_call_read_reach_the_resumed_runs_first_observation(tmp_path):
    from test_surface_agent import driver
    planner, api = driver()
    contract, host = scenario(max_model_turns=3), AskingHost()  # The turn limit stops the loop after the UI step.
    run = SurfaceRun(tmp_path, contract, host, secrets={"access": "secret"}, planner=planner)
    assert await run.run() == 3
    assert host.answers == [{"queued": True}] and "Synthetic message" not in json.dumps(api.inputs)
    assert [(row["count"], row["requeued"], row["steps"]) for row in kinds(run, "app_messages_undelivered")] == [
        (1, True, ["form"])]
    planner, api = driver()
    resumed = SurfaceRun(tmp_path, contract, host, planner=planner, resume=run.path)
    assert await resumed.run() == 0
    first = json.loads(api.inputs[0]["messages"][-1]["content"])
    assert [item["content"] for item in first["app_messages"]] == [[{"type": "text", "text": "Synthetic message 1"}]]
    assert len(kinds(resumed, "app_messages_undelivered")) == 1 and "app_messages" not in resumed.observation([])


async def test_app_messages_still_queued_when_a_run_ends_are_recorded_as_undelivered(tmp_path):
    from test_surface_agent import driver

    class Rejecting(AskingHost):
        async def call(self, name, arguments):
            if name == "save":  # The view's write fails the step after its message was queued.
                self.calls.append((name, arguments))
                return {"isError": True, "structuredContent": {"error": "declined"}}
            return await super().call(name, arguments)
    planner, _ = driver()
    run = SurfaceRun(tmp_path, scenario(), Rejecting(), secrets={"access": "secret"}, planner=planner)
    assert await run.run() == 1
    assert kinds(run, "ui_message")[0]["disposition"] == APP_MESSAGE_QUEUED
    assert [(row["count"], row["requeued"], row["reason"]) for row in kinds(run, "app_messages_undelivered")] == [
        (1, False, "the run ended before a model read them")]


async def test_app_message_callbacks_come_only_from_a_ui_step(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    message = {"kind": "ui_message", "source": "ui/message", "role": "user", "content": []}
    with pytest.raises(LabError, match="Unexpected app message"):
        await run.callback("__surface_app_message", message)
    run.active_ui = run.scenario.steps[2]
    with pytest.raises(LabError, match="Unexpected app message"):
        await run.callback("__surface_app_message", {**message, "extra": True})
    assert await run.callback("__surface_app_message", message) == {"queued": False}  # Scripted.


def told(outcome):
    def edit(data):
        host_contract(data)
        data["steps"][2]["ui"]["actions"][0:0] = [expect("#message", outcome)]
    return edit


async def test_views_are_told_when_the_host_does_not_deliver_their_message():
    from test_surface_agent import driver
    # A scripted run has no conversation: the official SDK view's sendMessage result has isError: true.
    code, run, report, _ = await run_fixture("host-contract", told("isError"))
    assert code == 0, report
    assert kinds(run, "ui_message")[0]["disposition"] == "recorded; not delivered to a conversation"
    # An agent run queues it for the model's next observation (another step follows): a plain result.
    planner, _ = driver()
    code, run, report, _ = await run_fixture("host-contract", told("delivered"), planner=planner)
    assert code == 0, report
    assert kinds(run, "ui_message")[0]["disposition"] == APP_MESSAGE_QUEUED


def relay_view(data):
    discovered(data)
    ui = data["steps"][2]["ui"]
    ui["callback_resources"] = ["fixture://missing", "fixture://gone", "fixture://broken", "fixture://notes"]
    ui["actions"][0:0] = [expect("#resources", (
        "missing:error -32602 gone:error -32602 uri=fixture://gone broken:error -32001 twice:read,read "
        "first:fixture://missing next=page2 page:fixture://notes next=page3 long:error -32000 direct:denied"))]


async def test_view_resource_requests_reach_the_server_one_by_one_with_its_errors_and_cursors():
    code, run, report, _ = await run_fixture("resource-relay", relay_view)
    assert code == 0, report
    reads = {row["uri"]: row for row in kinds(run, "ui_resource_read")}
    # The bridge sends a legacy resource-not-found -32002 as -32602 (MCP 2026-07-28): both are recorded.
    assert (reads["fixture://missing"]["code"], reads["fixture://missing"]["view_code"]) == (-32002, -32602)
    gone, broken = reads["fixture://gone"], reads["fixture://broken"]
    assert (gone["code"], gone["data_characters"], "view_code" in gone) == (-32602, len('{"uri":"fixture://gone"}'), False)
    assert (broken["code"], broken["message"], "view_code" in broken) == (-32001, "Synthetic backend failure", False)
    # A listing is one server page with its nextCursor; an over-long cursor never reaches the server.
    listings = kinds(run, "ui_resource_list")
    assert [(row["disposition"], row.get("listed"), row.get("shown")) for row in listings] == [
        ("proxied", 2, ["fixture://missing"]), ("proxied", 2, ["fixture://notes"]), ("denied", None, None)]
    assert listings[2]["reason"] == "cursor longer than 2000 characters"
    # Each read reached the server, though it let clients cache the note; the view's own frame could
    # not use the host page's resource binding.
    assert [row["disposition"] for row in kinds(run, "ui_resource_read") if row["uri"] == "fixture://notes"] == [
        "proxied", "proxied"]
    assert run.state["steps"]["verify"]["result"]["structuredContent"]["resourceReads"]["notes"] == 2


def test_a_ui_step_names_at_most_100_callback_resources():
    data = scenario().model_dump()
    data["steps"][2]["ui"]["callback_resources"] = [f"fixture://note-{index}" for index in range(100)]
    SurfaceScenario.model_validate(data)
    data["steps"][2]["ui"]["callback_resources"].append("fixture://note-100")
    with pytest.raises(ValueError, match="at most 100 items"):
        SurfaceScenario.model_validate(data)


async def test_isolated_renderer_writes_never_take_the_unconsented_native_path(tmp_path):
    worker = {"image": "sha256:" + "a" * 64, "docker_socket": ".cache/d.sock", "docker_config": ".cache/docker",
              "destinations": {}}
    run = SurfaceRun(tmp_path, scenario(browser_worker=worker), FakeHost(), secrets={"access": "secret"})
    run.active_ui = run.scenario.steps[2]  # Its writes need a one-use host grant instead.
    with pytest.raises(LabError, match="Unexpected browser network write"):
        await run.callback("__surface_network_write", {"url": "http://127.0.0.1:8765/mutation", "method": "POST",
                                                        "body_sha256": "0" * 64, "body_bytes": 0})
    assert "unconsented_network_writes" not in run.state and not (run.path / "events.jsonl").exists()
