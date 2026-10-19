"""Meta Model API provider (F9/P0.9): runnable init profile, auto-only preflight, contributor opt-in."""
import json

import pytest
from aiohttp import web

from consumer_agent_testing_tool.cli import initialize, parser
from consumer_agent_testing_tool.config import ModelProfile, interface_profile, read_config
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.providers import preflight, resolve_model

KEY = "meta-fixture-model-api-key"


def usage():
    return {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 3}}


async def serve(handler):
    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"


def responses_fixture(requests, *, first_output=None):
    async def handler(request):
        payload = await request.json()
        requests.append({"path": request.path, "body": payload, "authorization": request.headers.get("Authorization")})
        if len(requests) == 1:
            output = first_output or [
                {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Use the probe."}],
                 "encrypted_content": "opaque-meta-reasoning"},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "fixture_probe",
                 "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}}), "status": "completed"}]
        else:
            output = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "Receipt: probe-accepted-7", "annotations": []}]}]
        return web.json_response({"id": f"resp_{len(requests)}", "object": "response", "created_at": 1,
                                  "model": "muse-spark-1.3", "status": "completed", "output": output,
                                  "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "usage": usage()})
    return handler


def evidence_for(tmp_path):
    profile = interface_profile("api-tools-surrogate-v1")
    return Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=profile, lock={})


async def test_init_meta_profile_resolves_and_passes_auto_only_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.delenv("META_API_KEY", raising=False)
    requests = []
    runner, base_url = await serve(responses_fixture(requests))
    path = tmp_path / "meta.yaml"
    try:
        initialize(parser().parse_args(["init", "--config", str(path), "--provider", "meta",
                                        "--model", "muse-spark-1.3", "--base-url", base_url]))
        profile = read_config(path).model_profiles["meta-dev"]
        assert (profile.api, profile.auth.env, profile.support) == ("responses", "MODEL_API_KEY", "specified/unverified")
        evidence = evidence_for(tmp_path)
        checks = await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert checks[0]["status"] == "passed" and checks[0]["tool_choice"] == ["auto", "auto"]
    assert [request["path"] for request in requests] == ["/v1/responses", "/v1/responses"]
    for request in requests:
        # Meta accepts only "auto": no forced choice is ever sent.
        assert request["body"].get("tool_choice", "auto") == "auto"
        assert request["authorization"] == f"Bearer {KEY}"
        assert request["body"]["model"] == "muse-spark-1.3" and request["body"]["store"] is False
    assert "do not call any tool again" in json.dumps(requests[0]["body"]["input"])
    replay = requests[1]["body"]["input"]
    # Reasoning carries across the tool turn as a structured item, never as assistant text.
    assert {"type": "reasoning", "encrypted_content": "opaque-meta-reasoning"}.items() <= next(
        item for item in replay if item["type"] == "reasoning").items()
    assert any(item["type"] == "function_call_output" and item["call_id"] == "call_1" for item in replay)
    lock = evidence.manifest["run_lock"]
    assert lock["provider_disclosures"]["meta_steering_context"]["value"] is True
    assert "steering context" in lock["provider_disclosures"]["meta_steering_context"]["detail"]
    assert lock["model_support"]["label"] == "specified/unverified"
    assert lock["reasoning_replay"].startswith("Responses reasoning items")
    assert any("steering context" in gap for gap in evidence.manifest["observation_gaps"])
    assert all(KEY not in item.read_text() for item in evidence.path.rglob("*.json"))


def test_init_meta_writes_the_meta_endpoint_and_a_resolvable_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", KEY)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    path = tmp_path / "meta.yaml"
    initialize(parser().parse_args(["init", "--config", str(path), "--provider", "meta",
                                    "--model", "muse-spark-1.3"]))
    profile = read_config(path).model_profiles["meta-dev"]
    assert profile.base_url == "https://api.meta.ai/v1"
    model = resolve_model(profile)
    assert type(model.api).__name__ == "MetaAPI" and model.api.responses_api is True


async def test_meta_chat_completions_preflight_sends_no_forced_tool_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append((request.path, payload))
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_chat_1", "type": "function", "function": {
                    "name": "fixture_probe", "arguments": json.dumps({"payload": {"count": 7, "label": "probe"}})}}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "probe-accepted-7"}, "stop"
        return web.json_response({"id": "chat", "object": "chat.completion", "created": 1, "model": "muse-spark-1.3",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}})

    runner, base_url = await serve(handler)
    profile = ModelProfile(provider="meta", api="chat-completions", model_id="muse-spark-1.3", base_url=base_url,
                           auth={"mode": "api_key", "env": "MODEL_API_KEY"},
                           limits={"timeout_seconds": 5, "max_retries": 0})
    try:
        checks = await preflight(profile, evidence_for(tmp_path))
    finally:
        await runner.cleanup()
    assert checks[0]["status"] == "passed"
    assert [path for path, _ in requests] == ["/v1/chat/completions"] * 2
    assert all(payload.get("tool_choice", "auto") == "auto" for _, payload in requests)
    assert all("max_completion_tokens" in payload for _, payload in requests)


# OpenRouter ":variant" suffixes and dated slugs still select the training-enabled model.
CONTRIBUTOR_IDS = [
    ("meta", "muse-spark-1.3-contributor", "muse-spark-1.3"),
    ("meta", "muse-spark-1.3-contributor-2026-09-02", "muse-spark-1.3-2026-09-02"),
    ("meta", "MUSE-SPARK-1.3-CONTRIBUTOR", "MUSE-SPARK-1.3"),
    ("openrouter", "meta/muse-spark-1.3-contributor", "meta/muse-spark-1.3"),
    ("openrouter", "meta/muse-spark-1.3-contributor:nitro", "meta/muse-spark-1.3:nitro"),
    ("openrouter", "meta/muse-spark-1.3-contributor:online", "meta/muse-spark-1.3:online"),
    ("openrouter", "meta/muse-spark-1.3-contributor-20260902", "meta/muse-spark-1.3-20260902"),
]


@pytest.mark.parametrize("provider,model_id,standard_id", CONTRIBUTOR_IDS)
def test_contributor_model_ids_require_explicit_training_opt_in(provider, model_id, standard_id):
    fields = {"provider": provider, "api": "responses" if provider == "meta" else "chat-completions",
              "model_id": model_id, "auth": {"mode": "api_key", "env": "MODEL_API_KEY" if provider == "meta"
                                              else "OPENROUTER_API_KEY"}}
    with pytest.raises(ValueError, match="allow_training_on_prompts"):
        ModelProfile(**fields)
    with pytest.raises(ValueError, match="allow_training_on_prompts"):
        ModelProfile(**fields, allow_training_on_prompts=False)
    assert ModelProfile(**fields, allow_training_on_prompts=True).allow_training_on_prompts is True
    with pytest.raises(ValueError, match="only to contributor-mode"):
        ModelProfile(**{**fields, "model_id": standard_id}, allow_training_on_prompts=True)
    assert ModelProfile(**{**fields, "model_id": standard_id}).allow_training_on_prompts is None


@pytest.mark.parametrize("provider,model_id", [
    ("meta", "muse-spark-1.3-contributor"), ("openrouter", "meta/muse-spark-1.3-contributor:nitro"),
    ("openrouter", "meta/muse-spark-1.3-contributor-20260902")])
def test_init_refuses_contributor_ids_unless_the_opt_in_is_written(tmp_path, provider, model_id):
    path = tmp_path / "contributor.yaml"
    arguments = ["init", "--config", str(path), "--provider", provider, "--model", model_id]
    with pytest.raises(ValueError, match="allow_training_on_prompts"):
        initialize(parser().parse_args(arguments))
    assert not path.exists()
    initialize(parser().parse_args([*arguments, "--allow-training-on-prompts"]))
    assert read_config(path).model_profiles[f"{provider}-dev"].allow_training_on_prompts is True
    assert "allow_training_on_prompts: true" in path.read_text()


@pytest.mark.parametrize("fields,match", [
    ({"base_url": "https://inference.example/v1"}, "api.meta.ai"),
    ({"generation": {"seed": 7}}, "seed"),
    ({"api": "messages"}, "dialect"),
])
def test_meta_profiles_reject_unreachable_or_dropped_settings(fields, match):
    with pytest.raises(ValueError, match=match):
        ModelProfile(**{"provider": "meta", "api": "responses", "model_id": "muse-spark-1.3",
                        "auth": {"mode": "api_key", "env": "MODEL_API_KEY"}, **fields})


def test_meta_streaming_is_gated_before_any_request(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    profile = ModelProfile(provider="meta", api="responses", model_id="muse-spark-1.3",
                           auth={"mode": "api_key", "env": "MODEL_API_KEY"}, generation={"streaming": True})
    with pytest.raises(Unsupported, match="streaming"):
        resolve_model(profile)


@pytest.mark.parametrize("name,tool_choice,refused", [
    ("booking.create", "auto", False), ("connectors.booking.create", "auto", True),
    ("booking create", "auto", True), ("booking_create", "any", True), ("booking_create", "none", True),
    (None, "none", False),
])
async def test_meta_function_names_and_tool_choice_are_checked_before_dispatch(
        tmp_path, name, tool_choice, refused):
    from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelAPI, ModelOutput
    from inspect_ai.tool import ToolInfo, ToolParams

    from consumer_agent_testing_tool.providers import CapturingAPI, meta_request_problem

    dispatched = []

    class Recording(ModelAPI):
        async def generate(self, input, tools, tool_choice, config):
            dispatched.append([tool.name for tool in tools])
            return ModelOutput.from_content("muse-spark-1.3", "ok")

    evidence = evidence_for(tmp_path)
    api = CapturingAPI(Recording("meta/muse-spark-1.3"), evidence, request_rules=meta_request_problem)
    tools = [ToolInfo(name=name, description="Synthetic booking tool.", parameters=ToolParams())] if name else []
    call = api.generate([ChatMessageUser(content="Book it.")], tools, tool_choice, GenerateConfig())
    if refused:
        with pytest.raises(Unsupported, match="Meta Model API"):
            await call
        assert dispatched == []
        assert evidence.manifest["execution_failure"]["class"] == "unsupported"
        events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
        assert [event["event_type"] for event in events] == ["action_denied"]
    else:
        await call
        assert dispatched == [[name] if name else []]


async def test_meta_rejections_keep_the_provider_message(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)

    async def handler(request):
        await request.json()
        return web.json_response({"error": {"message": 'only "auto" is supported for tool_choice',
                                            "type": "invalid_request_error", "code": "invalid_tool_choice"}},
                                 status=400)

    runner, base_url = await serve(handler)
    profile = ModelProfile(provider="meta", api="responses", model_id="muse-spark-1.3", base_url=base_url,
                           auth={"mode": "api_key", "env": "MODEL_API_KEY"},
                           limits={"timeout_seconds": 5, "max_retries": 0})
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(LabError, match="unsupported tool/schema feature") as error:
            await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert "HTTP 400" in str(error.value) and 'only "auto" is supported' in str(error.value)
    records = [json.loads(path.read_text()) for path in evidence.path.rglob("*.json")]
    assert any(record.get("status") == 400 and "tool_choice" in record.get("message", "")
               for record in records if isinstance(record, dict))


def test_meta_credential_aliases_are_named_and_must_agree(monkeypatch):
    profile = ModelProfile(provider="meta", api="responses", model_id="muse-spark-1.3",
                           auth={"mode": "api_key", "env": "MODEL_API_KEY"})
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    with pytest.raises(LabError, match="set MODEL_API_KEY or META_API_KEY"):
        resolve_model(profile)
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("META_API_KEY", "a-different-meta-key")
    with pytest.raises(LabError, match="Conflicting credential aliases"):
        resolve_model(profile)


def test_meta_custom_key_env_is_used_alone_and_aliases_stay_inference_only(monkeypatch):
    from consumer_agent_testing_tool.config import Config, Target

    monkeypatch.setenv("MY_META_KEY", KEY)
    monkeypatch.setenv("MODEL_API_KEY", "an-unrelated-default-alias-key")
    monkeypatch.delenv("META_API_KEY", raising=False)
    profile = ModelProfile(provider="meta", api="responses", model_id="muse-spark-1.3",
                           auth={"mode": "api_key", "env": "MY_META_KEY"})
    # The profile's own reference is honored; the default aliases are neither read nor in conflict.
    assert resolve_model(profile).api.api_key == KEY
    monkeypatch.delenv("MY_META_KEY")
    with pytest.raises(LabError, match="Missing credential: set MY_META_KEY in"):
        resolve_model(profile)
    # Either alias can reach Meta inference, so neither may double as a target credential.
    for name in ("META_API_KEY", "MODEL_API_KEY"):
        url = "https://mcp.example.test/mcp"
        with pytest.raises(ValueError, match="separate reference"):
            Config(targets={"remote": Target(kind="mcp", transport="streamable-http", base_url=url,
                                             permitted_destinations=[url], auth={"mode": "api_key", "env": name})})


def events_of(evidence):
    return [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]


def artifact(evidence, event):
    return json.loads((evidence.path / event["artifacts"][0]["path"]).read_text())


@pytest.mark.parametrize("body,label", [
    ({"error": {"code": 500, "message": "meta in-body upstream error"}},
     "provider unavailable (HTTP 500): meta in-body upstream error"),
    ({"error": {"code": 402, "message": "Insufficient balance for this team"}},
     "insufficient credits (HTTP 402): Insufficient balance for this team"),
    ({"id": "chat", "object": "chat.completion", "created": 1, "model": "muse-spark-1.3"},
     "malformed provider response: Response carried no choices"),
    ({"id": "chat", "object": "chat.completion", "created": 1, "model": "muse-spark-1.3", "choices": []},
     "malformed provider response: Response carried no choices"),
])
async def test_meta_chat_http_200_error_bodies_keep_their_message(tmp_path, monkeypatch, body, label):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    calls = []

    async def handler(request):
        calls.append(await request.json())
        return web.json_response(body)

    runner, base_url = await serve(handler)
    profile = ModelProfile(provider="meta", api="chat-completions", model_id="muse-spark-1.3", base_url=base_url,
                           auth={"mode": "api_key", "env": "MODEL_API_KEY"},
                           limits={"timeout_seconds": 5, "max_retries": 0})
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(LabError) as raised:
            await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    assert str(raised.value) == f"Provider preflight: {label}"
    assert "TypeError" not in str(raised.value) and len(calls) == 1
    errors = [artifact(evidence, event) for event in events_of(evidence) if event["event_type"] == "provider_error"]
    assert errors and errors[0]["in_body"] is True and label.split(": ", 1)[1] in errors[0]["message"]


async def test_meta_context_length_rejection_keeps_the_provider_message(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    message = "Input exceeds the maximum context length of 1048576 tokens"

    async def handler(request):
        await request.json()
        return web.json_response({"error": {"message": message, "type": "invalid_request_error",
                                            "code": "context_length_exceeded"}}, status=400)

    runner, base_url = await serve(handler)
    profile = ModelProfile(provider="meta", api="responses", model_id="muse-spark-1.3", base_url=base_url,
                           auth={"mode": "api_key", "env": "MODEL_API_KEY"},
                           limits={"timeout_seconds": 5, "max_retries": 0})
    evidence = evidence_for(tmp_path)
    try:
        with pytest.raises(LabError) as raised:
            await preflight(profile, evidence)
    finally:
        await runner.cleanup()
    # Inspect turns this 400 into a model_length stop; the label and message must survive it.
    assert str(raised.value) == f"Provider preflight: context length exceeded: {message}"
    events = events_of(evidence)
    responses = [event for event in events if event["event_type"] == "model_response"]
    # One terminal event per request, so timing and usage summaries still pair them.
    assert [event["event_type"] for event in events].count("model_request") == len(responses) == 1
    assert not any(event["event_type"] == "provider_error" for event in events)
    kept = artifact(evidence, responses[0])["provider_error"]
    assert (kept["class"], kept["code"], kept["converted_stop_reason"]) == (
        "context length exceeded", "context_length_exceeded", "model_length")
    assert kept["message"] == message


def test_meta_steering_disclosure_states_only_what_meta_documents():
    from consumer_agent_testing_tool.providers import META_STEERING_CONTEXT

    detail = META_STEERING_CONTEXT["detail"]
    assert "unbilled steering context" in detail and "every prompt" in detail
    assert "token usage" not in detail
