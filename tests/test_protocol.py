import json

import pytest

from consumer_agent_testing_tool.config import Target, interface_profile
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.protocol import check_contract, child_environment


@pytest.mark.parametrize("variant,diagnostic", [
    ("good", None), ("invalid_structured", "call:0:slots_list"),
    ("false_success", "backend.draft_count"), ("duplicate_draft", "backend.duplicate_effect_count"),
])
async def test_real_sdk_contract_and_broken_targets(tmp_path, variant, diagnostic):
    profile = interface_profile("mcp-contract-2025-11-25")
    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested", profile=profile, lock={})
    checks = await check_contract(Target(variant=variant), evidence)
    failures = [check["id"] for check in checks if check["status"] == "failed"]
    if diagnostic:
        assert diagnostic in failures
    else:
        assert failures == []
    assert any(check["id"] == "negotiation" and check["status"] == "passed" for check in checks)
    assert any(check["id"] == "abrupt-fixture-disconnect" and check["status"] == "passed"
               for check in checks)
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    assert any(event["event_type"] == "state_asserted" for event in events)
    coverage = evidence.manifest["protocol_coverage"]
    assert "fixture idempotency and independent backend state" in coverage["tested"]
    assert "experimental extensions" in coverage["not_applicable"]


def test_child_environment_is_allowlisted(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "super-secret-value")
    monkeypatch.setenv("AWS_PROFILE", "secret-profile")
    monkeypatch.setenv("HOME", "/privileged")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    assert set(child_environment()) == {"PATH", "LANG", "PYTHONIOENCODING"}


async def test_http_contract_rejects_reused_inference_secret_before_connect(monkeypatch):
    from consumer_agent_testing_tool.errors import LabError
    from consumer_agent_testing_tool.protocol import connect

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "same-provider-and-target-secret")
    monkeypatch.setenv("TARGET_AUTH", "same-provider-and-target-secret")
    target = Target(kind="mcp", transport="streamable-http",
        base_url="http://127.0.0.1:1/mcp", permitted_destinations=["http://127.0.0.1:1/mcp"],
        auth={"mode": "api_key", "env": "TARGET_AUTH"})
    with pytest.raises(LabError, match="must not reuse"):
        async with connect(target):
            raise AssertionError("Reused inference credentials reached a target connection")


async def test_native_http_fixture_session_and_cancellation(tmp_path):
    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested",
                        profile=interface_profile("mcp-contract-2025-11-25"), lock={})
    checks = await check_contract(Target(transport="streamable-http"), evidence)
    assert not [c for c in checks if c["status"] in ("failed", "unsupported")]
    assert any(c["id"] == "http-session-termination" for c in checks)
    assert any(c["id"] == "request-cancellation" and c["status"] == "passed" for c in checks)
    assert any(c["id"] == "abrupt-fixture-disconnect" and c["status"] == "passed" for c in checks)


async def test_custom_http_bearer_uses_target_credentials_only(tmp_path, monkeypatch):
    import asyncio
    import socket
    import sys

    from consumer_agent_testing_tool.cli import statuses
    from consumer_agent_testing_tool.protocol import stop_server

    monkeypatch.setenv("TARGET_FIXTURE_CREDENTIAL", "target-only-canary")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "provider-only-canary")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server = await asyncio.create_subprocess_exec(sys.executable, "-m", "consumer_agent_testing_tool.fixture_mcp",
        "--db", str(tmp_path / "fixture.sqlite"), "--run-id", "custom-http", "--seed",
        "--port", str(port), "--bearer-env", "TARGET_FIXTURE_CREDENTIAL",
        env=child_environment({"TARGET_FIXTURE_CREDENTIAL": "target-only-canary"}),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(.05)
        url = f"http://127.0.0.1:{port}/mcp"
        target = Target(kind="mcp", source_revision="fixture-v1", transport="streamable-http",
            base_url=url, permitted_destinations=[url],
            auth={"mode": "api_key", "env": "TARGET_FIXTURE_CREDENTIAL"},
            contract_cases=[{"tool": "slots_list", "arguments": {}, "effect": "read"}])
        evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                            profile=interface_profile("mcp-contract-2025-11-25"), lock={})
        checks = await check_contract(target, evidence)
        assert statuses(checks)[0] == 0
        assert any(c["id"] == "call:0:slots_list" and c["status"] == "passed" for c in checks)
        coverage = evidence.manifest["protocol_coverage"]
        assert "fixture idempotency and independent backend state" not in coverage["tested"]
        assert "independent target backend outcomes" in coverage["unverified"]
        assert all("provider-only-canary" not in path.read_text()
                   and "target-only-canary" not in path.read_text()
                   for path in evidence.path.rglob("*.json"))
    finally:
        await stop_server(server)
