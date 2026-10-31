"""Modern MCP availability is an observed protocol requirement, separate from the SDK version."""
import json
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from test_protocol_modern import handshake_server, modern_server, serve

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.replay import playback

REVISION = "2026-07-28"


async def readiness(tmp_path, capsys, endpoint, target, *, profile="generic-sdk", revision=REVISION):
    manifest = tmp_path / "readiness.yaml"
    manifest.write_text(yaml.safe_dump({
        "version": 1, "kind": "surrogate-readiness", "protocol_revision": revision,
        "allow": ["lookup_record"],
    }))
    code = await execute(parser().parse_args([
        "check", endpoint, "--suite", "surrogate", "--emulation-target", target,
        "--client-profile", profile, "--manifest", str(manifest), "--output", str(tmp_path / "runs"),
        "--summary-json", "-", "--junit", str(tmp_path / "readiness.xml"),
    ]))
    summary = json.loads(capsys.readouterr().out)
    result = playback(Path(summary["trials"][0]["run"]))["result"]
    return code, result, summary


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_modern_availability_requires_discovery_and_catalog_without_tool_calls(
        tmp_path, monkeypatch, capsys, target):
    monkeypatch.chdir(tmp_path)
    requests = []
    runner, endpoint = await serve(modern_server(anonymous=True), requests)
    try:
        code, result, summary = await readiness(tmp_path, capsys, endpoint, target)
    finally:
        await runner.cleanup()
    assert code == 0, result.get("detail")
    assert {row["method"] for row in requests} == {"server/discover", "tools/list"}
    assert {row["version"] for row in requests} == {REVISION}
    check = next(row for row in result["checks"] if row["id"] == "protocol.revision")
    assert (check["status"], check["expected"], check["actual"]) == ("passed", REVISION, REVISION)
    assert result["server"]["protocol_era"] == "modern"
    assert result["server"]["protocol_method"] == "server/discover"
    assert result["server"]["server_info"] is None
    assert result["run_lock"]["protocol_revision"] == REVISION
    assert result["compatibility_pass"] is False
    assert summary["status"] == "completed"


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_legacy_fallback_is_usable_but_does_not_pass_a_modern_requirement(
        tmp_path, monkeypatch, capsys, target):
    monkeypatch.chdir(tmp_path)
    requests = []
    runner, endpoint = await serve(handshake_server("2025-11-25"), requests)
    try:
        code, result, summary = await readiness(tmp_path, capsys, endpoint, target)
    finally:
        await runner.cleanup()
    assert code == 1, result.get("detail")
    checks = {row["id"]: row for row in result["checks"]}
    assert checks["protocol.revision"]["status"] == "failed"
    assert checks["protocol.revision"]["actual"] == "2025-11-25"
    assert checks["catalog.projection"]["status"] == "passed"
    assert result["server"]["protocol_era"] == "handshake"
    assert result["server"]["protocol_method"] == "initialize"
    assert summary["status"] == "failed"
    assert not any(row["method"] == "tools/call" for row in requests)


async def test_legacy_only_client_is_a_blocker_before_any_network_request(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    requests = []
    runner, endpoint = await serve(modern_server(), requests)
    try:
        code, result, summary = await readiness(
            tmp_path, capsys, endpoint, "muse", profile="muse-template-unofficial-2026-10")
    finally:
        await runner.cleanup()
    assert code == 3, result.get("detail")
    assert summary["status"] == "blocked" and requests == []
    check, = result["checks"]
    assert check["id"] == "protocol.revision" and check["status"] == "unsupported"
    assert check["actual"] is None and "generic-sdk" in check["detail"]


@pytest.mark.parametrize("target", ["muse", "instinct"])
@pytest.mark.parametrize("failure", ["authorization", "unavailable", "invalid-discovery"])
async def test_unavailable_or_invalid_server_never_becomes_an_availability_pass(
        tmp_path, monkeypatch, capsys, target, failure):
    monkeypatch.chdir(tmp_path)
    requests = []

    async def handler(body):
        if failure == "invalid-discovery":
            return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        return web.Response(status=401 if failure == "authorization" else 503)

    runner, endpoint = await serve(handler, requests)
    try:
        code, result, summary = await readiness(tmp_path, capsys, endpoint, target)
    finally:
        await runner.cleanup()
    assert code in (2, 3), result
    assert summary["status"] in ("error", "blocked")
    assert result["compatibility_pass"] is False
    assert not any(row["id"] == "protocol.revision" and row["status"] == "passed"
                   for row in result["checks"])
    assert not any(row["method"] == "tools/call" for row in requests)


async def test_unknown_protocol_requirement_is_a_setup_error_before_network(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    requests = []
    runner, endpoint = await serve(modern_server(), requests)
    try:
        code, result, summary = await readiness(tmp_path, capsys, endpoint, "instinct", revision="2.0")
    finally:
        await runner.cleanup()
    assert code == 2 and result["failure_stage"] == "manifest"
    assert summary["status"] == "error" and requests == []
