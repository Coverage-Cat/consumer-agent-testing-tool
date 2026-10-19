"""Worker selection, bounded HTTP broker, and explicitly enabled Linux VM proof."""
import asyncio
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import digest


def worker_policy(**changes):
    worker = {"image": "sha256:" + "a" * 64, "docker_socket": ".cache/test.sock",
              "docker_config": ".cache/docker", "destinations": {"http://portal.test": "127.0.0.1"}}
    worker.update(changes)
    return {"url": "http://portal.test/portal", "origins": ["http://portal.test"],
            "rules": [], "worker": worker}


@pytest.mark.parametrize("changes", [
    {"image": "mutable-tag"}, {"destinations": {"http://portal.test": "localhost"}},
    {"destinations": {}}, {"destinations": {"http://portal.test": "127.0.0.1",
                                           "http://extra.test": "127.0.0.1"}},
    {"max_response_bytes": 100_000_000}, {"max_request_bytes": 0},
    {"max_concurrent_requests": 0}, {"max_concurrent_requests": 33},
    {"max_requests": 0}, {"max_total_request_bytes": 0}, {"max_total_response_bytes": 0},
    {"probe_loopback_webrtc": True},
])
def test_invalid_worker_policy_fails_before_browser_launch(changes):
    with pytest.raises(ValidationError):
        BrowserPolicy(**worker_policy(**changes))


def test_worker_is_explicit_and_preserved_in_policy():
    policy = BrowserPolicy(**worker_policy())
    assert policy.model_dump()["worker"]["destinations"] == {"http://portal.test": "127.0.0.1"}
    assert BrowserPolicy(url="http://portal.test", origins=["http://portal.test"], rules=[]).worker is None


def test_worker_observation_cannot_substitute_an_image_or_probe_profile(tmp_path):
    broker = BrowserBroker(tmp_path, BrowserPolicy(**worker_policy()), host=SimpleNamespace())
    for event in [
        {"image": "sha256:" + "b" * 64, "webrtc_loopback_probe": False},
        {"image": "sha256:" + "a" * 64, "webrtc_loopback_probe": True},
        {"image": "sha256:" + "a" * 64},
    ]:
        with pytest.raises(LabError, match="identity"):
            broker.observe({"kind": "browser_worker", **event})
    assert broker.worker_runtime is None
    assert broker.events == []


def run_node(script, timeout):
    root = Path.cwd()
    home = root / ".cache/browser-worker-tests"
    home.mkdir(exist_ok=True)
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home))
    result = subprocess.run(["node", script], cwd=root, env=env, text=True,
                            capture_output=True, timeout=timeout, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_http_broker_pins_destinations_and_bounds_transport():
    assert run_node("tests/browser_http_contract.mjs", 20)["status"] == "passed"


def test_http_broker_aggregate_limits_precede_dispatch_and_survive_errors():
    assert run_node("tests/browser_http_budget.mjs", 20)["status"] == "passed"


@pytest.mark.skipif(os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1",
                    reason="Explicit owned Linux browser worker VM proof is not enabled")
def test_actual_worker_mcp_network_consent_and_files():
    result = run_node("tests/browser_worker_workflow.mjs", 120)
    assert result["status"] == "passed"
    assert result["consent"]["writes"] == 1
    assert result["bypasses"]["passed"]
    assert result["bypasses"]["unexpected_canary_hits"] == {"tcp": 0, "tcp6": 0, "udp": 0, "udp6": 0}


@pytest.mark.skipif(os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1",
                    reason="Explicit owned Linux browser worker VM proof is not enabled")
def test_actual_worker_browser_network_apis_and_changing_dns():
    result = run_node("tests/browser_worker_network.mjs", 120)
    assert result["status"] == "passed"
    assert result["webrtc_delivery_qualified"]
    assert result["enforced_canaries"]["hits"] == {"http": [], "websocket": [], "dns": [], "stun": 0}
    assert {row["address"] for row in result["positive_canaries"]["hits"]["http"]
            if row["path"].startswith("/dns-positive-")} == {"127.0.0.1", "127.0.0.2"}


@pytest.mark.skipif(os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1",
                    reason="Explicit owned Linux browser worker VM proof is not enabled")
async def test_python_browser_host_uses_worker_and_transaction_broker():
    import httpx
    root = Path.cwd()
    fixture = await asyncio.create_subprocess_exec(
        "node", "scripts/surface_fixture.mjs", "0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    broker = None
    try:
        line = await asyncio.wait_for(fixture.stdout.readline(), 15)
        assert line, (await fixture.stderr.read()).decode()
        origin = json.loads(line)["endpoint"].removesuffix("/mcp")
        connection = json.loads((root / ".cache/browser-vm/connection.json").read_text())
        worker = {key: connection[key] for key in ("docker_socket", "docker_config")}
        worker.update(image=json.loads((root / ".cache/browser-vm/worker-image.json").read_text())["image"],
                      destinations={origin: "127.0.0.1"})
        policy = BrowserPolicy(url=origin + "/portal", origins=[origin], worker=worker, rules=[
            {"id": "portal", "origin": origin, "path": "/portal", "methods": ["GET"], "effect": "read"},
            {"id": "mutation", "origin": origin, "path": "/mutation", "methods": ["GET"], "effect": "write"}])
        broker = BrowserBroker(root, policy)
        snapshot = await broker.open()
        assert "Synthetic portal" in snapshot["text"]
        reference = re.search(r'button "Save"[^\n]*\[ref=([^\]]+)\]', snapshot["text"])[1]
        await broker.action({"kind": "click", "ref": reference, "generation": snapshot["generation"]})
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(origin + "/browser-oracle")).json()["browserWrites"] == 0
        request = {"url": origin + "/mutation", "method": "GET", "body": "", "rule": "mutation"}
        broker.decisions[digest(request)] = {
            "decision": "approve_once", "state_version": "1", "amount_minor": 0,
            "currency": "USD", "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
        snapshot = await broker.action({"kind": "snapshot"})
        reference = re.search(r'button "Save"[^\n]*\[ref=([^\]]+)\]', snapshot["text"])[1]
        await broker.action({"kind": "click", "ref": reference, "generation": snapshot["generation"]})
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(20):
                writes = (await client.get(origin + "/browser-oracle")).json()["browserWrites"]
                if writes == 1:
                    break
                await asyncio.sleep(.05)
        assert writes == 1
        assert any(event["kind"] == "browser_worker" for event in broker.events)
        assert any(event["kind"] == "network_authorized" for event in broker.events)
        broker.assert_backend_count("writes", writes, 1, source="synthetic fixture oracle")
        await broker.close()
        broker.finish("completed")
        result = json.loads((broker.path / "result.json").read_text())
        identity = result["run_lock"]["browser_worker"]
        assert identity["image"] == worker["image"]
        assert identity["runtime"]["kernel"].startswith("Linux ")
        assert identity["runtime"]["docker_arch"] == "arm64"
        assert identity["webrtc_loopback_probe"] is False
    finally:
        if broker:
            await broker.close()
        if fixture.returncode is None:
            fixture.terminate()
        await fixture.wait()
