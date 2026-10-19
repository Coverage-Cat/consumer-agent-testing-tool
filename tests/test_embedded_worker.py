"""Embedded MCP Apps use the isolated worker and host-owned network authority."""
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_browser_worker_scenario import selected_worker
from test_surface import FakeHost, scenario
from test_surface_agent import driver as agent_driver

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.surface import Paused, SurfaceRun, SurfaceScenario
from consumer_agent_testing_tool.surface_host import BrowserHost

LIVE = pytest.mark.skipif(os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1",
                         reason="Explicit owned Linux browser worker VM proof is not enabled")


def worker(**changes):
    return {"image": "sha256:" + "a"*64, "docker_socket": ".cache/d.sock",
            "docker_config": ".cache/docker", "destinations": {}, **changes}


def grant(url="http://127.0.0.1:8765/mutation", **changes):
    return {"url": url, "method": "POST", "body_sha256": hashlib.sha256(b"synthetic-input").hexdigest(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), **changes}


def with_grants(grants, **changes):
    value = scenario(browser_worker=worker(), **changes).model_dump()
    value["steps"][2]["ui"]["network_grants"] = grants
    return SurfaceScenario.model_validate(value)


@pytest.mark.parametrize("bad", [
    {"$ref": "/steps/prepare/result/_meta/grant"}, {"$ref": "/model/grant"},
    {"url": "http://x"}, grant(body_sha256="bad"), grant(expires_at="2026-10-12"),
    grant(url="http://user:password@example.test/"), grant(url="http://example.test/#fragment"),
])
def test_embedded_network_grants_are_strict_trusted_inputs(bad):
    with pytest.raises(ValidationError):
        with_grants([bad])


@pytest.mark.parametrize("changes", [
    {"destinations": {"http://extra.test": "127.0.0.1"}},
    {"rules": [{"id": "escape", "origin": "http://extra.test", "path": "/", "methods": ["GET"], "effect": "read"}]},
    {"probe_loopback_webrtc": True},
])
def test_embedded_worker_cannot_expand_browser_destinations(changes):
    with pytest.raises(ValidationError):
        scenario(browser_worker=worker(**changes))


def test_native_renderer_cannot_claim_network_grants():
    value = scenario().model_dump()
    value["steps"][2]["ui"]["network_grants"] = [grant()]
    with pytest.raises(ValidationError, match="isolated"):
        SurfaceScenario.model_validate(value)


def test_network_observations_and_runtime_never_enter_planner_capabilities(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost())
    run.observe_host({"kind": "network_denied", "url": "https://private.test/?secret=fixture",
                      "runtime": {"private": "control"}, "rendering": "passed", "bridge": "mcp-app"})
    observation = run.observation(run.ready_steps())
    # Host capabilities are recorded for the report but are harness state, never planner input.
    assert run.state["capabilities"]["rendering"] == "passed"
    assert run.state["capabilities"]["bridge"] == "mcp-app"
    assert "capabilities" not in observation and "checks" not in observation
    assert "private.test" not in json.dumps(observation) and "control" not in json.dumps(observation)
    # The recorded capabilities reach the public report: network detail and runtime never do.
    recorded = json.dumps(run.state["capabilities"])
    assert "private.test" not in recorded and "control" not in recorded and "secret" not in recorded


async def test_nested_private_grants_resolve_and_remain_redacted_after_restart(tmp_path):
    value = grant()
    configured = with_grants([{"$ref": "/secrets/request_grant"}])
    first = SurfaceRun(tmp_path, configured, FakeHost(), secrets={
        "request_grant": value, "nested": {"values": ["nested-secret-canary", 42, None]}})
    first.persist()
    resumed = SurfaceRun(tmp_path, configured, FakeHost(), resume=first.path)
    from consumer_agent_testing_tool.browser_network import UIRequestGrant
    resumed.active_ui = configured.steps[2]
    resumed.active_network_grants = [
        UIRequestGrant.model_validate(resumed.resolve(configured.steps[2].ui.network_grants[0]))]
    request = {key: item for key, item in value.items() if key != "expires_at"}
    assert await resumed.callback("__surface_browser_request", request) == {"authorized": True}
    resumed.observe_host({"kind": "diagnostic", "message": "nested-secret-canary"})
    public = (resumed.path / "events.jsonl").read_text()
    assert value["body_sha256"] not in public and value["url"] not in public
    assert "nested-secret-canary" not in public


async def test_write_grants_bind_exact_request_expire_and_survive_restart(tmp_path):
    configured = with_grants([grant(), grant(expires_at=(datetime.now(UTC)-timedelta(seconds=1)).isoformat())])
    run = SurfaceRun(tmp_path, configured, FakeHost())
    run.active_ui = configured.steps[2]
    from consumer_agent_testing_tool.browser_network import UIRequestGrant
    run.active_network_grants = [UIRequestGrant.model_validate(row) for row in configured.steps[2].ui.network_grants]
    request = {key: value for key, value in grant().items() if key != "expires_at"}
    for key, value in [("url", "http://other.test/mutation"), ("method", "GET"), ("body_sha256", "b"*64)]:
        with pytest.raises(LabError, match="host grant"):
            await run.callback("__surface_browser_request", {**request, key: value})
    assert await run.callback("__surface_browser_request", request) == {"authorized": True}
    with pytest.raises(LabError, match="host grant"):
        await run.callback("__surface_browser_request", request)
    resumed = SurfaceRun(tmp_path, configured, FakeHost(), resume=run.path)
    resumed.active_ui, resumed.active_network_grants = run.active_ui, run.active_network_grants
    with pytest.raises(LabError, match="host grant"):
        await resumed.callback("__surface_browser_request", request)


def identity():
    return {"image": "sha256:" + "a"*64, "network": "none", "chromium_sandbox": True,
            "uid": 65532, "os_isolation": "qualification-pending", "webrtc_loopback_probe": False,
            "runtime": {"docker_version": "test", "docker_os": "linux", "docker_arch": "arm64", "kernel": "test"},
            "browser_version": "test"}


async def test_runtime_lock_is_verified_before_embedded_interaction_and_rerun(tmp_path):
    configured = with_grants([])
    first = SurfaceRun(tmp_path, configured, FakeHost())
    first.active_ui = configured.steps[2]
    await first.callback("__surface_browser_identity", identity())
    first.persist()
    resumed = SurfaceRun(tmp_path, configured, FakeHost(), resume=first.path)
    resumed.active_ui = configured.steps[2]
    with pytest.raises(LabError, match="changed"):
        await resumed.callback("__surface_browser_identity", {**identity(), "browser_version": "different"})
    with pytest.raises(LabError, match="differs"):
        resumed.require_run_lock({**first.run_lock, "browser_runtime": {**identity(), "browser_version": "different"}})
    candidate = SurfaceRun(tmp_path, configured, FakeHost())
    candidate.require_run_lock(first.run_lock)
    assert "browser_runtime" not in candidate.run_lock  # Not measured until acknowledged.
    candidate.active_ui = configured.steps[2]
    with pytest.raises(LabError, match="changed"):
        await candidate.callback("__surface_browser_identity", {**identity(), "browser_version": "different"})
    await candidate.callback("__surface_browser_identity", identity())
    assert candidate.run_lock == first.run_lock


async def test_interrupted_direct_write_needs_explicit_read_reconciliation(tmp_path):
    configured = with_grants([grant()])
    run = SurfaceRun(tmp_path, configured, FakeHost())
    run.state["ui_attempts"]["form"] = "pending"
    run.state["network_grants_used"] = {"form": [0]}
    with pytest.raises(Paused, match="read reconciliation"):
        await run.execute_step(configured.steps[2])
    assert run.host.rendered == 0 and run.host.calls == []


@pytest.mark.parametrize("confirmed", [True, False])
async def test_interrupted_network_write_reconciles_without_replaying_interactions(tmp_path, confirmed):
    data = with_grants([grant()]).model_dump()
    data["steps"][2]["ui"]["network_reconcile"] = {
        "tool": "status", "path": "/structuredContent/status", "expected": "saved"}
    configured = SurfaceScenario.model_validate(data)
    host = FakeHost()
    run = SurfaceRun(tmp_path, configured, host, secrets={"access": "test-access"})
    await run.invoke("save", {"value": "Test Shopper"}, "ui:form:save")
    host.saved = confirmed
    run.state["ui_attempts"]["form"] = "pending"
    run.state["network_grants_used"] = {"form": [0]}
    if confirmed:
        await run.execute_step(configured.steps[2])
        assert run.state["steps"]["form"]["status"] == "completed"
    else:
        with pytest.raises(Paused, match="unconfirmed"):
            await run.execute_step(configured.steps[2])
    assert host.rendered == 0
    assert [name for name, _ in host.calls] == ["save", "status"]


@LIVE
@pytest.mark.parametrize("bridge,agent", [("mcp-app", False), ("legacy-openai", False), ("mcp-app", True)])
async def test_embedded_worker_real_bridge_callback_and_private_inputs(tmp_path, bridge, agent):
    await run_live(tmp_path, bridge, agent)


@LIVE
@pytest.mark.parametrize("authorize", [True, False])
@pytest.mark.parametrize("mode", ["network-app", "network-autoload", "network-autoload-nested"])
async def test_embedded_worker_network_rules_redirects_and_one_use_writes(tmp_path, authorize, mode):
    await run_live(tmp_path, mode, False, network=True, authorize=authorize)


@LIVE
@pytest.mark.parametrize("authorize", [True, False])
@pytest.mark.parametrize("mode", ["network-autoload", "network-autoload-nested"])
async def test_iframe_interception_precedes_parser_requests(tmp_path, monkeypatch, authorize, mode):
    original = asyncio.create_subprocess_exec
    receipt = tmp_path / "startup-delay.txt"

    async def delayed(*args, **kwargs):
        if any(str(arg).endswith("/surface_host/host.mjs") for arg in args):
            kwargs["env"] = {
                **kwargs["env"],
                "NODE_OPTIONS": "--import=" + str(Path.cwd() / "tests/browser_startup_delay.mjs"),
                "SURFACE_TEST_STARTUP_RECEIPT": str(receipt),
            }
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    await run_live(tmp_path, mode, False, network=True, authorize=authorize)
    # Prove the scheduling treatment was applied to every separate iframe,
    # not simply that the unmodified workflow happened to pass again.
    assert receipt.read_text().splitlines() == ["delayed"] * (2 if mode.endswith("-nested") else 1)


async def run_live(tmp_path, mode, agent, *, network=False, authorize=False):
    root = Path.cwd()
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0", mode, cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 20)
        assert line, (await fixture.stderr.read()).decode()
        endpoint = json.loads(line)["endpoint"]
        origin = endpoint.removesuffix("/mcp")
        settings = selected_worker().model_dump()
        settings["destinations"] = {origin: "127.0.0.1"} if network else {}
        if network:
            settings["rules"] = [{"id": route, "origin": origin, "path": route, "methods": [method], "effect": effect}
                for route, method, effect in [("/asset", "GET", "read"), ("/redirect-read", "GET", "read"),
                    ("/redirect-write", "GET", "read"), ("/mutation", "POST", "write"),
                    ("/mutation", "GET", "write"), ("/nested", "GET", "read")]]
            # Distinct method rules still need distinct IDs.
            for index, row in enumerate(settings["rules"]):
                row["id"] += str(index)
        data = scenario(endpoint=endpoint, allowed_origins=[origin],
                        browser_origins=[origin] if network else [], browser_worker=settings).model_dump()
        ui = data["steps"][2]["ui"]
        ui["bridge"] = "legacy-openai" if mode == "legacy-openai" else "mcp-app"
        if network:
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(origin + "/denied-canary")).status_code == 200
            ui["network_grants"] = [grant(origin + "/mutation")] if authorize else []
            for selector, expected in reversed([
                ("#read", "asset loaded"), ("#write", "saved" if authorize else "Blocked"),
                ("#repeat", "Blocked"), ("#forbidden", "Blocked"), ("#redirect-write", "Blocked")]):
                ui["actions"][0:0] = [{"kind": "click", "selector": selector},
                    {"kind": "expect_text", "selector": "#network", "value": expected}]
            if mode.startswith("network-autoload"):
                # The first fetch starts while parsing the app, before the
                # host begins scripted interaction. It still needs the guard.
                ui["actions"].pop(0)
        contract = SurfaceScenario.model_validate(data)
        planner, api = agent_driver() if agent else (None, None)
        run = SurfaceRun(root, contract, BrowserHost(root), secrets={"access": "fixture-access"}, planner=planner)
        result = await run.run()
        report = json.loads((run.path / "result.json").read_text())
        assert result == 0, report
        assert report["run_lock"]["browser_runtime"]["image"] == settings["image"]
        assert report["capabilities"]["completion"] == "passed"
        assert run.state["steps"]["verify"]["result"]["structuredContent"]["writes"] == 1
        async with httpx.AsyncClient(trust_env=False) as client:
            oracle = (await client.get(origin + "/browser-oracle")).json()
        if network:
            assert oracle["browserWrites"] == int(authorize)
            assert oracle["assetReads"] == 1 and oracle["canaryHits"] == 1
            events = [json.loads(row) for row in (run.path / "events.jsonl").read_text().splitlines()]
            assert any(row.get("enforcement") == "host-ui-network-grant" for row in events)
            assert any(row.get("enforcement") == "chromium-response-interceptor" for row in events)
        public = (run.path / "events.jsonl").read_text() + (run.path / "result.json").read_text()
        assert "fixture-access" not in public
        if api:
            assert "fixture-access" not in json.dumps(api.inputs)
            assert "browser_runtime" not in json.dumps(api.inputs)
        return run
    finally:
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()
