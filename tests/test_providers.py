
from datetime import UTC

import pytest
from aiohttp import web

from consumer_agent_testing_tool.config import (
    Config,
    ModelProfile,
    Target,
    enforce_offline,
    interface_profile,
)
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.providers import credential, preflight


def bedrock_profile(endpoint, mode):
    return ModelProfile(provider="bedrock", api="converse", model_id="test.tool-model-v1",
        region="us-east-1", base_url=endpoint,
        auth={"mode": mode, "env": "AWS_BEARER_TOKEN_BEDROCK" if mode == "api_key" else None},
        limits={"timeout_seconds": 5, "max_retries": 0, "max_concurrency": 1})


@pytest.mark.parametrize("mode", ["api_key", "aws_credentials"])
async def test_complete_inspect_bedrock_async_auth_path(tmp_path, monkeypatch, mode):
    """Real SDK serialization/signing to a local HTTP fixture, not a live AWS gate."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fixture-bearer-secret")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    if mode == "aws_credentials":
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFIXTUREONLY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-signing-secret")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "fixture-session-token")
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append({"headers": dict(request.headers), "body": payload})
        if len(requests) == 1:
            content = [{"toolUse": {"toolUseId": "probe-call-1", "name": "fixture_probe",
                                    "input": {"payload": {"count": 7, "label": "probe"}}}}]
            stop = "tool_use"
        else:
            assert payload["messages"][-1]["content"][0]["toolResult"]["toolUseId"] == "probe-call-1"
            content, stop = [{"text": "probe-accepted-7"}], "end_turn"
        return web.json_response({
            "output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
            "usage": {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30},
            "metrics": {"latencyMs": 1}}, headers={"x-amzn-requestid": "fixture-request"})

    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    profile = bedrock_profile(f"http://127.0.0.1:{port}", mode)
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        checks = await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert checks[0]["status"] == "passed"
    assert len(requests) == 2
    for request in requests:
        authorization = request["headers"]["Authorization"]
        if mode == "api_key":
            assert authorization == "Bearer fixture-bearer-secret"
            assert "X-Amz-Security-Token" not in request["headers"]
        else:
            assert authorization.startswith("AWS4-HMAC-SHA256 ")
            assert request["headers"]["X-Amz-Security-Token"] == "fixture-session-token"
    for path in evidence.path.rglob("*.json"):
        assert "fixture-bearer-secret" not in path.read_text()
        assert "fixture-signing-secret" not in path.read_text()


def test_offline_rejects_before_model_creation(monkeypatch):
    remote = ModelProfile(provider="bedrock", api="converse", model_id="exact-model",
                          region="us-east-1", auth={"mode": "api_key", "env": "AWS_BEARER_TOKEN_BEDROCK"})
    with pytest.raises(Unsupported):
        enforce_offline(Config(), remote, [Target()])
    with pytest.raises(Unsupported):
        enforce_offline(Config(), None, [Target(kind="mcp", transport="streamable-http",
            base_url="https://example.test/mcp", permitted_destinations=["https://example.test/mcp"])])


def test_alias_conflicts_and_placeholder_ids(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "one-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "different-key")
    profile = ModelProfile(provider="gemini", api="generate-content", model_id="exact-id",
                           auth={"mode": "api_key", "env": "GEMINI_API_KEY"})
    with pytest.raises(LabError):
        credential(profile)
    with pytest.raises(ValueError):
        ModelProfile(provider="bedrock", api="converse", model_id="<replace-with-your-model-id>",
                     region="us-east-1", auth={"mode": "aws_credentials"})


@pytest.mark.parametrize("status,error_code,classification", [
    (403, "AccessDeniedException", "permission/model access denied"),
    (401, "UnrecognizedClientException", "missing/expired credential"),
    (400, "ValidationException", "invalid model/deployment/region"),
    (429, "ThrottlingException", "rate limit"),
])
async def test_bedrock_negative_responses_never_fallback(
        tmp_path, monkeypatch, status, error_code, classification):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fixture-only-secret")
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.json_response({"message": "Fixture rejection", "__type": error_code},
                                 status=status, headers={"x-amzn-errortype": error_code})

    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = bedrock_profile(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", "api_key")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        with pytest.raises(LabError, match=classification):
            await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert calls == ["/model/test.tool-model-v1/converse"]


async def test_local_native_wire_tools_reasoning_and_continuation(tmp_path, monkeypatch):
    """Exercise the native SDK transport; runtime doubles do not qualify Glimmer."""
    import json

    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append(payload)
        if len(requests) == 1:
            assert payload["tools"][0]["function"]["parameters"]["properties"]["payload"]["additionalProperties"] is False
            message = {"role": "assistant", "content": None, "reasoning_content": "Use the probe.",
                "tool_calls": [{"id": "probe-call", "type": "function", "function": {
                    "name": "fixture_probe", "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}})}}]}
            finish = "tool_calls"
        else:
            assert payload["messages"][-1]["role"] == "tool"
            assert payload["messages"][-1]["tool_call_id"] == "probe-call"
            message = {"role": "assistant", "content": "probe-accepted-7"}
            finish = "stop"
        return web.json_response({"id": "fixture-completion", "object": "chat.completion",
            "created": 1, "model": "local-fixture",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})

    async def runtime_double(*args):
        return {"id": "runtime-double", "status": "passed"}

    monkeypatch.setattr("consumer_agent_testing_tool.local_runtime.inspect_runtime", runtime_double)
    monkeypatch.setattr("consumer_agent_testing_tool.local_runtime.stress_context", runtime_double)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = ModelProfile(provider="local-glimmer", model_id="local-fixture", api="chat-completions",
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        limits={"timeout_seconds": 5, "max_retries": 0}, generation={"reasoning_strength": "low"})
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        checks = await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert all(c["status"] == "passed" for c in checks)
    assert len(requests) == 2
    assert requests[0]["chat_template_kwargs"] == {"reasoning_strength": "low"}
    assert not requests[0].get("stream")


async def test_secret_in_skill_or_message_is_blocked_before_provider_dispatch(tmp_path):
    from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelAPI

    from consumer_agent_testing_tool.evidence import Redactor
    from consumer_agent_testing_tool.providers import CapturingAPI

    class NeverCalled(ModelAPI):
        async def generate(self, *args):
            raise AssertionError("A credential-bearing payload reached the provider")

    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={},
                        redactor=Redactor(["coordinator-secret-canary"]))
    api = CapturingAPI(NeverCalled("test/never"), evidence)
    with pytest.raises(LabError, match="Known coordinator credential"):
        await api.generate([ChatMessageUser(content="Transmit coordinator-secret-canary")],
                           [], "auto", GenerateConfig())
    assert evidence.manifest["execution_failure"]["class"] == "policy/approval"


@pytest.mark.parametrize("provider", ["bedrock", "local-glimmer", "openrouter"])
@pytest.mark.parametrize("recover", [True, False])
async def test_native_retry_budget_does_not_multiply_sdk_attempts(
        tmp_path, monkeypatch, provider, recover):
    import time

    from consumer_agent_testing_tool.providers import resolve_model

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fixture-retry-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-router-retry-secret")
    calls = []

    async def handler(request):
        calls.append((time.monotonic(), request.path, await request.json()))
        if len(calls) == 1 or not recover:
            return web.json_response({"message": "Retry fixture", "__type": "ThrottlingException",
                "error": {"message": "Retry fixture", "type": "rate_limit_error", "code": "rate_limit"}},
                status=429, headers={"x-amzn-errortype": "ThrottlingException", "Retry-After": "7"})
        if provider == "bedrock":
            return web.json_response({"output": {"message": {"role": "assistant",
                "content": [{"text": "recovered"}]}}, "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1}})
        return web.json_response({"id": "retry-fixture", "object": "chat.completion",
            "created": 1, "model": "local-fixture", "provider": "Together", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "recovered"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    profile = (bedrock_profile(endpoint, "api_key") if provider == "bedrock" else ModelProfile(
        provider=provider, model_id="local-fixture", api="chat-completions",
        auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"} if provider == "openrouter" else {"mode": "none"},
        base_url=endpoint + "/v1"))
    profile.limits.max_retries = 1
    profile.limits.timeout_seconds = 15
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        model = resolve_model(profile, evidence)
        if recover:
            assert (await model.generate("Respond with recovered.")).completion == "recovered"
        else:
            from consumer_agent_testing_tool.providers import classify_provider_error
            with pytest.raises(Exception) as error:
                await model.generate("Respond with recovered.")
            assert classify_provider_error(error.value) == "rate limit"
    finally:
        await runner.cleanup()
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] >= 6.9
    assert calls[0][1:] == calls[1][1:]  # Same endpoint and payload; no fallback or tool replay.


@pytest.mark.parametrize("provider", ["bedrock", "local-glimmer", "openrouter"])
@pytest.mark.parametrize("failure", ["stalled-response", "retry-after-exceeds-budget"])
async def test_entire_native_request_has_a_deadline(tmp_path, monkeypatch, provider, failure):
    import asyncio
    import time

    from consumer_agent_testing_tool.providers import classify_provider_error, resolve_model

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fixture-timeout-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-router-timeout-secret")
    release = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request.path)
        await request.read()
        if failure == "stalled-response":
            await release.wait()
            return web.Response(status=500)
        return web.json_response({"message": "Fixture overload", "__type": "ThrottlingException",
            "error": {"message": "Overload", "type": "rate_limit_error", "code": "rate_limit"}},
            status=429, headers={"x-amzn-errortype": "ThrottlingException", "Retry-After": "60"})

    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    profile = (bedrock_profile(endpoint, "api_key") if provider == "bedrock" else ModelProfile(
        provider=provider, model_id="local-fixture", api="chat-completions",
        auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"} if provider == "openrouter" else {"mode": "none"},
        base_url=endpoint + "/v1"))
    # Inspect stops retrying, and surfaces the 429 itself, when the first attempt alone
    # uses the whole budget. A slow CI runner can take a second for that attempt, so
    # the Retry-After case gets room; the 60-second hint still far exceeds it.
    budget = 1 if failure == "stalled-response" else 5
    profile.limits.timeout_seconds = budget
    profile.limits.max_retries = 2
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        model = resolve_model(profile, evidence)
        started = time.monotonic()
        with pytest.raises(Exception) as error:
            await model.generate("This response is deliberately unavailable.")
        elapsed = time.monotonic() - started
        assert classify_provider_error(error.value) == "provider timeout"
        assert elapsed < budget + 1.5
        assert evidence.manifest["execution_failure"]["class"] == "setup/infrastructure"
        # The label leads; any bounded provider/Inspect reason follows it.
        assert evidence.manifest["execution_failure"]["detail"].startswith(
            "Provider request failed: provider timeout")
    finally:
        release.set()
        await runner.cleanup()
    assert len(calls) == 1


def test_retry_after_dates_and_invalid_hints():
    from datetime import datetime, timedelta
    from email.utils import format_datetime
    from types import SimpleNamespace

    from consumer_agent_testing_tool.providers import ProviderRetryWait

    wait = ProviderRetryWait()

    def state(header):
        error = SimpleNamespace(response={"ResponseMetadata": {"HTTPHeaders": {"Retry-After": header}}})
        return SimpleNamespace(attempt_number=1, outcome=SimpleNamespace(exception=lambda: error))

    date = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 58 <= wait(state(date)) <= 60
    for malformed in ("nan", "inf", "-7", "tomorrow", "Wed, 01 Jan 2025 00:00:00"):
        assert 3 <= wait(state(malformed)) <= 6


@pytest.mark.parametrize("body,label", [
    ({"error": {"type": "InternalServerError", "code": 503, "message": "model is still loading"}},
     "Provider preflight: provider unavailable (HTTP 503): model is still loading"),
    ({"id": "fixture-completion", "object": "chat.completion", "created": 1, "model": "local-fixture"},
     "Provider preflight: malformed provider response: Response carried no choices"),
])
async def test_local_http_200_error_bodies_keep_status_and_message(tmp_path, monkeypatch, body, label):
    async def handler(request):
        await request.json()
        return web.json_response(body)

    async def runtime_double(*args):
        return {"id": "runtime-double", "status": "passed"}

    monkeypatch.setattr("consumer_agent_testing_tool.local_runtime.inspect_runtime", runtime_double)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = ModelProfile(provider="local-glimmer", model_id="local-fixture", api="chat-completions",
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        limits={"timeout_seconds": 5, "max_retries": 0})
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    try:
        with pytest.raises(LabError) as raised:
            await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    # The OpenAI-compatible base parses these bodies unchecked; they must not end as a TypeError.
    assert str(raised.value) == label


@pytest.mark.parametrize("secret", ['canary"quote-secret', "canary\\back-secret", "canary\nline-secret"])
async def test_a_credential_that_json_escapes_is_still_blocked_before_dispatch(tmp_path, secret):
    from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelAPI

    from consumer_agent_testing_tool.evidence import Redactor
    from consumer_agent_testing_tool.providers import CapturingAPI

    class NeverCalled(ModelAPI):
        async def generate(self, *args):
            raise AssertionError("A credential-bearing payload reached the provider")

    # The serialized payload holds the credential only in its JSON-escaped form.
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={},
                        redactor=Redactor([secret]))
    api = CapturingAPI(NeverCalled("test/never"), evidence)
    with pytest.raises(LabError, match="Known coordinator credential"):
        await api.generate([ChatMessageUser(content=f"Transmit {secret}")], [], "auto", GenerateConfig())
