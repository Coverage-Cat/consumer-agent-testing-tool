"""Legacy initialization preserves document semantics and stays in the app frame."""
import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest
from test_browser_worker_scenario import selected_worker
from test_surface import scenario

from consumer_agent_testing_tool.surface import SurfaceRun, SurfaceScenario
from consumer_agent_testing_tool.surface_host import BrowserHost

APP = r"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<label>Name <input id="name" hidden></label><button id="save">Save</button>
<script>
window.bridgeAtParse = typeof window.openai?.callTool;
window.documentMode = document.compatMode;
document.querySelector('#name').value = window.openai.toolOutput.name;
window.resourceURI = window.openai.toolResponseMetadata.ui.resourceUri;
document.querySelector('#save').onclick = () =>
  window.openai.callTool('save', {value: document.querySelector('#name').value});
const child = document.createElement('iframe');
child.srcdoc = '<script>window.bridgeVisibility = typeof window.openai;<\/script>';
child.onload = () => {
  window.childBridge = child.contentWindow.bridgeVisibility;
  document.querySelector('#name').hidden = false;
};
document.body.append(child);
</script></body></html>"""


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("standards_mode", [True, False])
async def test_legacy_initialization_preserves_html_and_frame_scope(isolated, standards_mode):
    if isolated and os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1":
        pytest.skip("Explicit owned Linux browser worker VM proof is not enabled")
    root = Path.cwd()
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0", "legacy-openai", cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 20)
        assert line, (await fixture.stderr.read()).decode()
        endpoint = json.loads(line)["endpoint"]
        options = {"browser_worker": {
            **selected_worker().model_dump(), "destinations": {}}} if isolated else {}
        data = scenario(endpoint=endpoint, allowed_origins=[endpoint.removesuffix("/mcp")],
                        treatment="workaround", **options).model_dump()
        ui = data["steps"][2]["ui"]
        ui.update(bridge="legacy-openai",
                  observations={"mode": "documentMode", "bridge": "bridgeAtParse",
                                "child": "childBridge", "resource": "resourceURI"})
        with tempfile.TemporaryDirectory(prefix="legacy-frame-", dir=root / ".cache") as directory:
            resource = Path(directory) / "app.html"
            html = APP if standards_mode else APP.removeprefix("<!doctype html>")
            resource.write_text(html)
            ui["resource_file"] = str(resource.relative_to(root))
            run = SurfaceRun(root, SurfaceScenario.model_validate(data), BrowserHost(root),
                             secrets={"access": "fixture-access"})
            assert await run.run() == 0, json.loads((run.path / "result.json").read_text())
            rendered = run.state["steps"]["form"]["result"]
            assert rendered["observations"] == {
                "mode": "CSS1Compat" if standards_mode else "BackCompat",
                "bridge": "function", "child": "undefined",
                "resource": "ui://test/preferences"}
            assert rendered["rendered_resource_sha256"] == hashlib.sha256(html.encode()).hexdigest()
            assert run.state["steps"]["verify"]["result"]["structuredContent"]["writes"] == 1
    finally:
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()
