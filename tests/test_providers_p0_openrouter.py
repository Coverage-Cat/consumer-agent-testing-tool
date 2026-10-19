"""OpenRouter via Inspect's dedicated provider (F8), failure labels (F11) and route confirmation."""
import json

import pytest
from aiohttp import web

from consumer_agent_testing_tool.config import ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.providers import preflight, resolve_model, route_confirmation

KEY = "router-p0-fixture-secret"
REASONING = " to=self<|message|>Use the probe with the exact payload."
ROUTING = {"only": ["together"], "allow_fallbacks": False, "require_parameters": True,
           "data_collection": "deny", "zdr": True}


async def serve(handler):
    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"


def router_profile(base_url, only=("together",)):
    return ModelProfile(provider="openrouter", api="chat-completions", model_id="meta/muse-spark-open",
                        auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"}, base_url=base_url,
                        routing={"only": list(only)}, limits={"timeout_seconds": 5, "max_retries": 0})


def evidence_for(tmp_path):
    profile = interface_profile("api-tools-surrogate-v1")
    return Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=profile, lock={})


def completion(message, finish, provider="Together"):
    return {"id": "fixture", "object": "chat.completion", "created": 1, "model": "meta/muse-spark-open",
            "provider": provider, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def recorded(evidence, kind):
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    return [json.loads((evidence.path / event["artifacts"][0]["path"]).read_text())
            for event in events if event["event_type"] == kind]


async def test_multi_turn_reasoning_is_replayed_structurally_not_as_assistant_text(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append(payload)
        if len(requests) == 1:
            return web.json_response(completion({
                "role": "assistant", "content": "Calling the probe.", "reasoning": REASONING,
                "reasoning_details": [{"type": "reasoning.text", "text": REASONING, "format": "unknown", "index": 0}],
                "tool_calls": [{"id": "probe-1", "type": "function", "function": {
                    "name": "fixture_probe", "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}})}}]},
                "tool_calls"))
        return web.json_response(completion({"role": "assistant", "content": "probe-accepted-7"}, "stop"))

    runner, base_url = await serve(handler)
    profile = router_profile(base_url)
    try:
        assert type(resolve_model(profile).api).__name__ == "OpenRouterAPI"
        checks = await preflight(profile, evidence_for(tmp_path))
    finally:
        await runner.cleanup()
    assert checks[0]["status"] == "passed"
    assistant = next(message for message in requests[1]["messages"] if message["role"] == "assistant")
    assert "<think" not in json.dumps(assistant["content"])
    assert "Use the probe" not in json.dumps(assistant["content"])
    assert assistant["reasoning_details"] == [
        {"type": "reasoning.text", "text": REASONING, "format": "unknown", "index": 0}]
    # Routing and privacy policy stay on every request, including the tool-result continuation.
    assert [request["provider"] for request in requests] == [ROUTING, ROUTING]
    assert requests[1]["messages"][-1]["tool_call_id"] == "probe-1"


@pytest.mark.parametrize("status,error,label", [
    (402, {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/settings/credits"},
     "insufficient credits"),
    (404, {"code": 404, "message": "No endpoints found matching your data policy (Zero data retention). "
                                   "Configure: https://openrouter.ai/settings/privacy"},
     "no endpoint satisfies the routing/ZDR policy"),
    (404, {"code": 404, "message": "No allowed providers are available for the selected model."},
     "routing policy: no allowed provider"),
    (404, {"code": 404, "message": "No endpoints found for meta/muse-spark-open."},
     "no endpoint available for the model and routing policy"),
    (400, {"code": 400, "message": "This endpoint's maximum context length is 131072 tokens. However, you "
                                   "requested about 150000 tokens."}, "context length exceeded"),
    (400, {"code": 400, "message": "meta/muse-spark-opn is not a valid model ID"},
     "invalid model/deployment/region"),
    (408, {"code": 408, "message": "Request timed out upstream"}, "provider timeout"),
    (502, {"code": 502, "message": "Provider returned error",
           "metadata": {"provider_name": "Together", "raw": "upstream overloaded"}}, "provider unavailable"),
    (503, {"code": 503, "message": "No instances available"}, "provider unavailable"),
    (401, {"code": 401, "message": "No auth credentials found"}, "missing/expired credential"),
    (429, {"code": 429, "message": "Rate limit exceeded"}, "rate limit"),
])
async def test_http_failures_get_specific_labels_and_keep_the_provider_message(
        tmp_path, monkeypatch, status, error, label):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    calls = []

    async def handler(request):
        calls.append(await request.json())
        return web.json_response({"error": error}, status=status)

    runner, base_url = await serve(handler)
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(LabError) as raised:
            await preflight(router_profile(base_url), evidence)
    finally:
        await runner.cleanup()
    message = str(raised.value)
    assert message.startswith(f"Provider preflight: {label} (HTTP {status}): ")
    assert error["message"][:40] in message
    if error.get("metadata"):
        assert "[provider: Together]" in message
    assert len(calls) == 1 and calls[0]["provider"]["allow_fallbacks"] is False
    recorded_errors = recorded(evidence, "provider_error")
    assert recorded_errors and recorded_errors[0]["class"] == label and recorded_errors[0]["status"] == status
    assert error["message"][:40] in recorded_errors[0]["message"]
    assert all(KEY not in path.read_text() for path in evidence.path.rglob("*.json"))


@pytest.mark.parametrize("body,label,fragment", [
    ({"error": {"code": 429, "message": "Rate limited upstream"}}, "rate limit", "Rate limited upstream"),
    ({"error": {"code": 502, "message": "Provider returned error"}}, "provider unavailable",
     "Provider returned error"),
    ({"error": {"code": 402, "message": "Insufficient credits"}}, "insufficient credits", "Insufficient credits"),
    ({"error": {"code": 404, "message": "No endpoints found matching your data policy"}},
     "no endpoint satisfies the routing/ZDR policy", "data policy"),
    ({"id": "warming", "object": "chat.completion", "created": 1, "model": "meta/muse-spark-open"},
     "provider unavailable", "warm-up"),
    ({"error": "plain string error"}, "malformed provider response", "plain string error"),
    ({"id": "empty", "object": "chat.completion", "created": 1, "model": "meta/muse-spark-open", "choices": []},
     "malformed provider response", "no choices"),
])
async def test_http_200_error_bodies_are_classified_without_crashing(tmp_path, monkeypatch, body, label, fragment):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    async def handler(request):
        await request.json()
        return web.json_response(body)

    runner, base_url = await serve(handler)
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(LabError) as raised:
            await preflight(router_profile(base_url), evidence)
    finally:
        await runner.cleanup()
    assert f"Provider preflight: {label}" in str(raised.value) and fragment in str(raised.value)
    assert "TypeError" not in str(raised.value)
    recorded_errors = recorded(evidence, "provider_error")
    assert recorded_errors[0]["class"] == label and recorded_errors[0]["in_body"] is True


async def test_connection_refused_is_a_network_failure(tmp_path, monkeypatch):
    import socket

    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with pytest.raises(LabError, match="Provider preflight: network failure"):
        await preflight(router_profile(f"http://127.0.0.1:{port}/v1"), evidence_for(tmp_path))


@pytest.mark.parametrize("returned,pinned,status,variant_status", [
    ("Together", "together", "passed", "not pinned"),
    ("Google AI Studio", "google-ai-studio", "passed", "not pinned"),
    ("Amazon Bedrock", "amazon-bedrock", "passed", "not pinned"),
    ("Nebius", "nebius/fp4", "passed", "variant unverified"),
    ("DeepInfra", "deepinfra/turbo", "passed", "variant unverified"),
    ("Other", "together", "failed", None),
    (None, "together", "failed", None),
])
def test_route_confirmation_normalizes_display_names(returned, pinned, status, variant_status):
    record = route_confirmation(returned, pinned)
    assert (record["status"], record["variant_status"]) == (status, variant_status)


async def test_variant_pins_pass_on_the_base_provider_and_record_the_unverified_variant(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    requests = []

    async def handler(request):
        requests.append(await request.json())
        if len(requests) == 1:
            return web.json_response(completion({"role": "assistant", "content": None, "tool_calls": [{
                "id": "probe-1", "type": "function", "function": {"name": "fixture_probe",
                "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}})}}]}, "tool_calls", "Nebius"))
        return web.json_response(completion({"role": "assistant", "content": "probe-accepted-7"}, "stop", "Nebius"))

    runner, base_url = await serve(handler)
    evidence = evidence_for(tmp_path)
    try:
        checks = await preflight(router_profile(base_url, only=("nebius/fp4",)), evidence)
    finally:
        await runner.cleanup()
    assert checks[0]["status"] == "passed"
    assert all(request["provider"]["only"] == ["nebius/fp4"] for request in requests)
    routes = recorded(evidence, "provider_route_verified")
    assert len(routes) == 2 and all(route["variant_status"] == "variant unverified" for route in routes)
    assert any("fp4" in gap for gap in evidence.manifest["observation_gaps"])


def probe_call(call_id="probe-1"):
    return [{"id": call_id, "type": "function", "function": {
        "name": "fixture_probe", "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}})}}]


async def replayed_assistant(tmp_path, first_message):
    requests = []

    async def handler(request):
        requests.append(await request.json())
        if len(requests) == 1:
            return web.json_response(completion(first_message, "tool_calls"))
        return web.json_response(completion({"role": "assistant", "content": "probe-accepted-7"}, "stop"))

    runner, base_url = await serve(handler)
    evidence = evidence_for(tmp_path)
    try:
        checks = await preflight(router_profile(base_url), evidence)
    finally:
        await runner.cleanup()
    assert checks[0]["status"] == "passed" and len(requests) == 2
    return next(message for message in requests[1]["messages"] if message["role"] == "assistant"), evidence


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_reasoning_without_details_is_replayed_in_its_field_not_as_assistant_text(
        tmp_path, monkeypatch, field):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assistant, evidence = await replayed_assistant(tmp_path, {
        "role": "assistant", "content": None, field: REASONING, "tool_calls": probe_call()})
    assert "<think" not in json.dumps(assistant["content"]) and "Use the probe" not in json.dumps(assistant["content"])
    assert assistant[field] == REASONING and "reasoning_details" not in assistant
    assert not any("<think>" in gap for gap in evidence.manifest["observation_gaps"])
    assert "reasoning field it arrived in" in evidence.manifest["run_lock"]["reasoning_replay"]


async def test_reasoning_replayed_as_assistant_text_is_recorded_as_an_observation_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    # The model wrote its reasoning into visible content; replaying that content is recorded, not hidden.
    assistant, evidence = await replayed_assistant(tmp_path, {
        "role": "assistant", "content": "<think>Use the probe.</think>Calling the probe.", "tool_calls": probe_call()})
    assert "<think>" in json.dumps(assistant["content"])
    assert "Prior reasoning was replayed as <think> text inside assistant content" in \
        evidence.manifest["observation_gaps"]


async def test_non_json_http_200_body_is_a_malformed_provider_response(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    async def handler(request):
        await request.json()
        return web.Response(text="this is not json", content_type="application/json")

    runner, base_url = await serve(handler)
    try:
        with pytest.raises(LabError, match="Provider preflight: malformed provider response: Expecting value"):
            await preflight(router_profile(base_url), evidence_for(tmp_path))
    finally:
        await runner.cleanup()


async def test_converted_input_length_rejection_is_not_masked_by_route_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    message = "The input length of 150000 tokens exceeds this endpoint's limit"

    async def handler(request):
        await request.json()
        return web.json_response({"error": {"code": 400, "message": message}}, status=400)

    runner, base_url = await serve(handler)
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(LabError) as raised:
            await preflight(router_profile(base_url), evidence)
    finally:
        await runner.cleanup()
    # Inspect returns this 400 as model_length output; an error body confirms no route either way.
    assert str(raised.value) == f"Provider preflight: context length exceeded (HTTP 400): {message}"
    assert recorded(evidence, "provider_route_verified") == []
    kept = recorded(evidence, "model_response")[0]["provider_error"]
    assert (kept["class"], kept["status"], kept["message"]) == ("context length exceeded", 400, message)


def test_failure_identifiers_and_messages_are_bounded():
    from consumer_agent_testing_tool.providers import (
        IDENTIFIER_CHARACTERS,
        MESSAGE_CHARACTERS,
        provider_failure,
    )

    class EchoingError(Exception):
        """A provider error whose fields echo request input."""

        def __init__(self):
            super().__init__("echoing provider error")
            self.status_code, self.code = 400, "invalid_request " + "c" * 500
            self.body = {"message": "m" * 2000,
                         "metadata": {"provider_name": "Together " + "p" * 500, "raw": "r" * 5000}}

    failure = provider_failure(EchoingError())
    assert len(failure["code"]) == len(failure["provider_name"]) == IDENTIFIER_CHARACTERS
    assert len(failure["message"]) == MESSAGE_CHARACTERS and len(failure["metadata"]) <= 2 * MESSAGE_CHARACTERS
