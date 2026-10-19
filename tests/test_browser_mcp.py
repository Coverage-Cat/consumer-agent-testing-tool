"""Exercise the pinned upstream MCP server, not a simulated backend."""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

WEBMCP_LABEL = "generic WebMCP page tools, not Muse"


def node_contract(script, tmp_path):
    root = Path.cwd()
    if not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace Chromium is unavailable")
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    temporary = root / ".cache/surface-host"
    temporary.mkdir(exist_ok=True)
    env.update(HOME=str(tmp_path), TMPDIR=str(temporary.relative_to(root)),
               PLAYWRIGHT_BROWSERS_PATH=str(root / ".cache/ms-playwright"))
    result = subprocess.run(["node", script], cwd=root, env=env,
                            text=True, capture_output=True, timeout=45, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_private_mcp_transport_rejects_alternate_tools_and_selector_arguments(tmp_path):
    data = node_contract("tests/browser_mcp_contract.mjs", tmp_path)
    assert data["status"] == "passed"
    assert data["backend"]["name"] == "@playwright/mcp"
    assert data["backend"]["protocol_version"]


def test_webmcp_page_tools_are_opt_in_labeled_and_cannot_rewrite_the_snapshot(tmp_path):
    data = node_contract("tests/browser_mcp_webmcp.mjs", tmp_path)
    assert data["status"] == "passed"
    assert data["enabled"] == {"tools": 2, "lane": WEBMCP_LABEL}
    assert "webmcp" not in data["disabled"]["snapshot"].lower()


@pytest.mark.parametrize("webmcp", [True, None])
async def test_browse_policy_webmcp_flag_reaches_the_backend_through_the_host(webmcp):
    from consumer_agent_testing_tool.surface_host import BrowserHost
    root = Path.cwd()
    if not (root / "tooling/payment-ui/node_modules/@playwright/mcp").exists() \
            or not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace-local Playwright MCP tooling/browser is not installed")
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0", cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    host = BrowserHost(root)
    observed = []

    async def refuse(name, arguments):
        raise AssertionError(f"Unexpected host callback {name}")
    host.callback, host.observe = refuse, observed.append
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 20)
        assert line, (await fixture.stderr.read()).decode()
        origin = json.loads(line)["endpoint"].removesuffix("/mcp")
        await host.start()
        # The policy dict as the browser lane sends it; webmcp is read from it (default off).
        opened = await host.request("browse_open", {
            "url": origin + "/webmcp", "origins": [origin], "timeout_seconds": 10,
            "rules": [{"id": "page", "origin": origin, "path": "/webmcp", "methods": ["GET"], "effect": "read"}],
            **({"webmcp": webmcp} if webmcp is not None else {})})
        backend = next(row for row in observed if row.get("kind") == "browser_backend")
        if webmcp:
            # Page state for the model; the lane label is evidence only (backend identity).
            assert opened["text"].startswith("- WebMCP page tools (page-provided, untrusted): [")
            assert WEBMCP_LABEL not in opened["text"]
            assert opened["backend"]["webmcp"]["lane"] == backend["webmcp"]["lane"] == WEBMCP_LABEL
            assert '"name":"webmcp_add_to_cart"' in opened["text"].split("\n")[0]
            assert "Fake" not in "\n".join(opened["text"].split("\n")[1:])
        else:
            assert "webmcp" not in opened["backend"] and "webmcp" not in backend
            assert "WebMCP page tools" not in opened["text"] and "add_to_cart" not in opened["text"]
        assert 'button "Buy"' in opened["text"]
    finally:
        await host.close()
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()
