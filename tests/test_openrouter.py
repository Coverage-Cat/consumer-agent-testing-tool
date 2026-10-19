import json

import pytest
import yaml
from aiohttp import web

from consumer_agent_testing_tool.config import (
    Config,
    ModelProfile,
    Target,
    enforce_offline,
    interface_profile,
    read_openrouter_profile,
)
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import Evidence, run_lock
from consumer_agent_testing_tool.providers import preflight


@pytest.mark.parametrize("returned_provider", ["Together", "Other", None])
@pytest.mark.parametrize("zdr_setting", [None, True, False])
async def test_openrouter_native_tools_and_enforced_provider_route(
        tmp_path, monkeypatch, returned_provider, zdr_setting):
    monkeypatch.setenv("OPENROUTER_API_KEY", "owned-router-fixture-secret")
    requests = []
    expected_zdr = True if zdr_setting is None else zdr_setting

    async def handler(request):
        payload = await request.json()
        requests.append(payload)
        assert request.headers["Authorization"] == "Bearer owned-router-fixture-secret"
        assert payload["model"] == "meta/muse-spark-open"
        assert payload["provider"] == {"only": ["together"], "allow_fallbacks": False,
            "require_parameters": True, "data_collection": "deny", "zdr": expected_zdr}
        assert "reasoning" not in payload
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "probe-1", "type": "function", "function": {"name": "fixture_probe",
                "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}})}}]}
            finish = "tool_calls"
        else:
            assert payload["messages"][-1]["tool_call_id"] == "probe-1"
            message, finish = {"role": "assistant", "content": "probe-accepted-7"}, "stop"
        return web.json_response({"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "meta/muse-spark-open", "provider": returned_provider,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Exercise the user-editable settings file through both native tool turns.
    settings = {"provider": "openrouter", "api": "chat-completions",
        "model_id": "meta/muse-spark-open",
        "auth": {"mode": "api_key", "env": "OPENROUTER_API_KEY"},
        "base_url": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        "routing": {},
        "limits": {"timeout_seconds": 5, "max_retries": 0}}
    if zdr_setting is not None:
        settings["routing"]["zdr"] = zdr_setting
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({"model_profiles": {"router": settings},
                                  "defaults": {"model_profile": "router"}}))
    profile = read_openrouter_profile(path)
    interface = interface_profile("muse-cli-skills-public-2026-09")
    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested",
        profile=interface, lock=run_lock(profile=interface, model=profile.model_dump(mode="json")))
    try:
        if returned_provider == "Together":
            assert all(check["status"] == "passed" for check in await preflight(profile, evidence))
            assert len(requests) == 2
            records = [json.loads(p.read_text()) for p in evidence.path.rglob("*.json")]
            retained = json.dumps(records)
            assert f'"zdr_requested": {json.dumps(expected_zdr)}' in retained
            assert f'"zdr": {json.dumps(expected_zdr)}' in retained
            for request in requests:
                assert "owned-router-fixture-secret" not in json.dumps(request)
            for path in evidence.path.rglob("*.json"):
                assert "owned-router-fixture-secret" not in path.read_text()
        else:
            with pytest.raises(LabError, match="pinned provider"):
                await preflight(profile, evidence)
            assert len(requests) == 1
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("routing", [
    {"allow_fallbacks": True}, {"zdr": "false"}, {"zdr": 0}, {"zdr": None}, {"only": []},
    {"only": ["phala", "other"]}, {"require_parameters": False},
    {"data_collection": "allow"},
])
def test_router_rejects_invalid_zdr_and_unapproved_routing(routing):
    with pytest.raises(ValueError):
        ModelProfile(provider="openrouter", api="chat-completions", model_id="meta/muse-spark-open",
            auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"}, routing=routing)


@pytest.mark.parametrize("status,message,classification", [
    (401, "Invalid API key", "missing/expired credential"),
    (403, "Model access forbidden", "permission/model access denied"),
    (404, "Unknown model slug", "invalid model/deployment/region"),
    (404, "No endpoints found that support tool use", "unsupported tool/schema feature"),
    (400, "Invalid tool schema", "unsupported tool/schema feature"),
    (429, "Rate limit exceeded", "rate limit"),
])
async def test_openrouter_rejections_are_actionable_and_do_not_fallback(
        tmp_path, monkeypatch, status, message, classification):
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-negative-fixture-secret")
    calls = []

    async def handler(request):
        calls.append((request.path, await request.json()))
        return web.json_response({"error": {"code": status, "message": message}}, status=status)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = ModelProfile(provider="openrouter", api="chat-completions",
        model_id="meta/muse-spark-open", auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"},
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        limits={"timeout_seconds": 5, "max_retries": 0})
    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested",
        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        with pytest.raises(LabError, match=classification):
            await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert len(calls) == 1
    assert calls[0][1]["provider"]["only"] == ["together"]
    assert calls[0][1]["provider"]["allow_fallbacks"] is False


async def test_openrouter_offline_missing_key_and_streaming_reject_before_dispatch(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.providers import resolve_model
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    profile = ModelProfile(provider="openrouter", api="chat-completions",
        model_id="meta/muse-spark-open", auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"})
    with pytest.raises(Unsupported):
        enforce_offline(Config(), profile, [Target()])
    with pytest.raises(LabError, match="Missing credential"):
        resolve_model(profile)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-unused-fixture-secret")
    profile.generation.streaming = True
    with pytest.raises(Unsupported, match="streaming"):
        resolve_model(profile)
