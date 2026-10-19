import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import digest


async def test_snapshot_references_escapes_and_network_write_gate():
    root = Path.cwd()
    if not list((root / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace Chromium is unavailable")
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    broker = None
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 15)
        assert line, (await fixture.stderr.read()).decode()
        origin = json.loads(line)["endpoint"].removesuffix("/mcp")
        policy = BrowserPolicy(url=origin + "/portal", origins=[origin], rules=[
            {"id":"portal", "origin":origin, "path":"/portal", "methods":["GET"], "effect":"read"},
            {"id":"mutation", "origin":origin, "path":"/mutation", "methods":["GET"], "effect":"write"}])
        broker = BrowserBroker(root, policy)
        snapshot = await broker.open()
        assert "Synthetic portal" in snapshot["text"]
        for action in ({"kind": "evaluate", "script": "fetch('/mutation')"},
                       {"kind": "click", "selector": "#save"}):
            with pytest.raises(LabError):
                await broker.action(action)
        match = re.search(r'button "Save"[^\n]*\[ref=([^\]]+)\]', snapshot["text"])
        assert match, snapshot
        with pytest.raises(LabError, match="Stale"):
            await broker.action({"kind": "click", "ref": match[1], "generation": 0})
        await broker.action({"kind": "click", "ref": match[1], "generation": snapshot["generation"]})
        # Independent target-side oracle: even a GET mutation was blocked before reaching the service.
        async with httpx.AsyncClient() as client:
            result = (await client.get(origin + "/browser-oracle")).json()
        assert result["browserWrites"] == 0
        assert any(event["kind"] == "network_denied" for event in broker.events)
        assert (await broker.action({"kind": "takeover"}))["status"] == "human-takeover"
        with pytest.raises(LabError, match="takeover"):
            await broker.action({"kind": "snapshot"})
        snapshot = await broker.return_control()
        request = {"url": origin + "/mutation", "method": "GET", "body": "", "rule": "mutation"}
        broker.decisions[digest(request)] = {
            "decision": "approve_once", "state_version": "1", "amount_minor": 0,
            "currency": "USD", "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
        match = re.search(r'button "Save"[^\n]*\[ref=([^\]]+)\]', snapshot["text"])
        await broker.action({"kind": "click", "ref": match[1], "generation": snapshot["generation"]})
        async with httpx.AsyncClient() as client:
            for _ in range(20):
                result = (await client.get(origin + "/browser-oracle")).json()
                if result["browserWrites"] == 1:
                    break
                await asyncio.sleep(.05)
        assert result["browserWrites"] == 1
        assert any(event["kind"] == "network_authorized" for event in broker.events)
        with pytest.raises(LabError, match="already consumed"):
            await broker.authorize("browser_request", request)
        broker.finish("completed")
        assert playback(broker.path)["result"]["run_lock"]["browser"]
    finally:
        if broker:
            await broker.close()
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()


def test_browser_failure_reports_redact_private_values(tmp_path):
    policy = BrowserPolicy(url="http://localhost:8765", origins=["http://localhost:8765"], rules=[])
    broker = BrowserBroker(tmp_path, policy, host=SimpleNamespace(), secrets=["private-value"])
    broker.finish("failed", "private-value failed")
    report = playback(broker.path)
    assert "private-value" not in json.dumps(report)
    assert report["result"]["status"] == "failed"
    assert report["result"]["verified_service_outcome"] is None


def test_missing_backend_observation_cannot_pass(tmp_path):
    policy = BrowserPolicy(url="http://localhost:8765", origins=["http://localhost:8765"], rules=[])
    broker = BrowserBroker(tmp_path, policy, host=SimpleNamespace())
    broker.assert_backend_count("confirmations", None, 1, source="unavailable fixture oracle")
    broker.finish("completed")
    result = playback(broker.path)["result"]
    assert result["status"] == "blocked" and result["verified_service_outcome"] is None
    with pytest.raises(LabError, match="unique"):
        broker.assert_backend_count("confirmations", 1, 1, source="changed observation")
