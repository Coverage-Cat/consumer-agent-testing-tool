import asyncio
import json
from pathlib import Path

import pytest
from test_surface import scenario
from test_surface_agent import driver as agent_driver
from test_surface_p0_mcp_apps import repository_runs  # noqa: F401 (an autouse fixture)

from consumer_agent_testing_tool.surface import SurfaceRun, SurfaceScenario
from consumer_agent_testing_tool.surface_host import BrowserHost


async def test_browser_temp_paths_stay_short_in_a_long_workspace(tmp_path, monkeypatch):
    root = tmp_path / ("long-workspace-" * 12)
    for name in ("playwright", "esbuild", "@modelcontextprotocol/client", "@modelcontextprotocol/ext-apps"):
        (root / "tooling/payment-ui/node_modules" / name).mkdir(parents=True)
    launched = {}
    output = asyncio.StreamReader()
    output.feed_eof()

    async def spawn(*args, **kwargs):
        from types import SimpleNamespace
        launched.update(kwargs)
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    host = BrowserHost(root)
    await host.start()
    await host.reader
    assert launched["cwd"] == root
    assert launched["env"]["TMPDIR"] == ".cache/surface-host"
    assert (root / launched["env"]["TMPDIR"]).is_dir()
    assert Path(launched["env"]["HOME"]).is_relative_to(root)


@pytest.mark.parametrize("bridge,driver", [
    ("mcp-app", "scripted"), ("legacy-openai", "scripted"),
    ("no-ui", "scripted"), ("mcp-app", "agent")])
async def test_embedded_consumer_surface_end_to_end(bridge, driver):
    root = Path.cwd()
    if not (root / "tooling/payment-ui/node_modules/playwright").exists() \
            or not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace-local Playwright tooling/browser is not installed")
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0", bridge, cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 20)
        assert line, (await fixture.stderr.read()).decode()
        endpoint = json.loads(line)["endpoint"]
        data = scenario(endpoint=endpoint, allowed_origins=[endpoint.removesuffix("/mcp")]).model_dump()
        data["steps"][2]["ui"]["bridge"] = "mcp-app" if bridge == "no-ui" else bridge
        contract = SurfaceScenario.model_validate(data)
        planner, api = agent_driver() if driver == "agent" else (None, None)
        run = SurfaceRun(root, contract, BrowserHost(root), secrets={"access": "fixture-access"},
                         planner=planner)
        result = await run.run()
        report = json.loads((run.path / "result.json").read_text())
        link = next(check for check in report["checks"] if check["id"] == "mcp-apps-resource-link")
        server = report["run_lock"]["server"]
        assert server["server_info"]["name"] == "surface-preferences-fixture" and server["tools_sha256"]
        assert server["ui_resources"]["form"]["uri"] == "ui://test/preferences"
        if bridge == "no-ui":
            # The scenario's hard-coded URI renders, but no real host would find this UI.
            assert result == 1, report
            assert report["capabilities"]["negotiation"] == "unavailable"
            assert link["status"] == "failed" and link["actual"] is None
            assert report["failure_class"] == "assertion"
            assert not report["compatibility_pass"]
        else:
            assert result == 0, report
            assert report["capabilities"]["negotiation"] == "passed"
            assert report["capabilities"]["completion"] == "passed"
            assert report["capabilities"]["bridge"] == bridge
            assert link["status"] == "passed" and link["descriptor_source"] == (
                "tool-descriptor" if bridge == "mcp-app" else "openai-output-template")
            assert run.state["steps"]["verify"]["result"]["structuredContent"]["writes"] == 1
            # Only a scripted baseline run is compatibility evidence; a step menu is not.
            assert report["compatibility_pass"] == (driver == "scripted")
            assert report["driver"] == driver
            public = (run.path / "events.jsonl").read_text()
            assert "fixture-access" not in public
            if api:
                assert len(api.inputs) == 4
                assert "fixture-access" not in json.dumps(api.inputs)
    finally:
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()
