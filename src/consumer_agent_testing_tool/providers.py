"""Small configuration and trace extension around Inspect's provider integrations."""
import asyncio
import json
import math
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from inspect_ai.model import (
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelAPI,
    get_model,
    modelapi,
)
from inspect_ai.tool import ToolInfo, ToolParams
from inspect_ai.util import JSONSchema
from tenacity import wait_exponential_jitter

from .config import LIVE_ADAPTERS, SUPPORT_NOTES, ModelProfile, credential_names
from .errors import LabError, Unsupported
from .serialization import recordable

MESSAGE_CHARACTERS = 300
# Provider identifiers (error codes, provider names) are short; anything longer is echoed input.
IDENTIFIER_CHARACTERS = 100
# https://dev.meta.ai/docs/pricing-rate-limits, checked 2026-10-15.
META_STEERING_CONTEXT = {
    "value": True, "basis": "documented", "source": "https://dev.meta.ai/docs/pricing-rate-limits",
    "checked_at": "2026-10-15",
    "detail": "Meta documents unbilled steering context (a system prompt and related scaffolding) "
              "injected into every prompt; it is added server-side and absent from captured requests",
}
META_STEERING_GAP = "Meta-injected steering context is not captured; the recorded model input is incomplete"
# Muse Spark Open's lineage (P1.8), recorded with any profile that serves it, so evidence never reads it as the
# model Muse runs (Muse launched on Muse Spark 1.3: the security post and launch post, checked 2026-10-15).
SPARK_OPEN = re.compile(r"(?:^|/)muse[-_]spark[-_]open\b", re.IGNORECASE)
SPARK_OPEN_LINEAGE = {
    "model": "Muse Spark Open", "basis": "documented", "source": "https://dev.meta.ai/models/muse-spark-open",
    "checked_at": "2026-10-15",
    "quote": "Muse Spark Open is derived from the Muse Spark model we released in August, with additional safety "
             "post-training to make it more robust against misuse as a standalone model without system guardrails.",
    "detail": "An open-weights derivative of the August Muse Spark release (Meta's dated posts name Muse Spark 1.2 "
              "that month), not the Muse Spark 1.3 model Muse launched on: agent behavior on it is surrogate "
              "evidence about a related model, never Muse's own model."}
SPARK_OPEN_GAP = ("The model is Muse Spark Open, derived from the August Muse Spark release; Muse runs Muse Spark "
                  "1.3, so model behavior here is not Muse's own model's")
# https://dev.meta.ai/docs/tool-calling: other names and forced tool choices return HTTP 400.
META_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_.-]+")
# How prior reasoning reaches the next turn; never as text inside assistant content.
REASONING_REPLAY = {
    ("openrouter", "chat-completions"): "structured reasoning_details, or the reasoning field it arrived in, "
                                        "on assistant messages; an observation gap records any replay as "
                                        "assistant text",
    ("meta", "responses"): "Responses reasoning items with encrypted content (store=false)",
    ("meta", "chat-completions"): "none; Meta Chat Completions redacts reasoning for external keys",
}
THINK_REPLAY_GAP = "Prior reasoning was replayed as <think> text inside assistant content"
# Stop reasons Inspect makes from provider errors (context length, content policy) and their labels.
STOP_FAILURES = {"model_length": "context length exceeded", "content_filter": "content-policy block"}
# Routing refusals name the profile fields that decide them.
FAILURE_HINTS = {
    "no endpoint satisfies the routing/ZDR policy": "check routing.only and routing.zdr in the model profile",
    "routing policy: no allowed provider": "routing.only must name a provider that serves this model",
    "no endpoint available for the model and routing policy": "check model_id and routing.only",
}


def credential(profile: ModelProfile):
    if profile.auth.mode != "api_key":
        return None
    names = credential_names(profile)
    values = {os.environ[name] for name in names if name and os.environ.get(name)}
    if len(values) > 1:
        raise LabError(f"Conflicting credential aliases: {', '.join(names)}")
    if not values:
        raise LabError(f"Missing credential: set {' or '.join(name for name in names if name)} in the "
                       "coordinator environment", category="authentication")
    return next(iter(values))


def bounded(text, limit=MESSAGE_CHARACTERS):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class ProviderResponseError(RuntimeError):
    """An HTTP-200 provider body that is not a usable completion; ``provider_error`` keeps its
    status and message. ``malformed`` marks bodies that are neither a completion nor an error."""

    def __init__(self, message, *, malformed=True):
        super().__init__(message)
        self.malformed = malformed
        self.provider_error = {"status": None, "message": message, "metadata": None}


def checked_responses(on_response):
    """Inspect parses an HTTP-200 chat-completions body without checking it (OpenRouter keeps only
    a retry class): keep the provider's status and message, and refuse bodies without choices."""
    # Inspect's helpers import the optional OpenAI SDK; scripted runs do not need it.
    from inspect_ai.model._openai import (
        OpenAIResponseError,
        classify_error_body,
        http_status_from_error_code,
    )

    def checked(response):
        error, choices = response.get("error"), response.get("choices")
        if error is None and isinstance(choices, list) and choices:
            return on_response(response)
        if error is not None and not (isinstance(error, dict) and isinstance(error.get("message"), str)):
            raise ProviderResponseError("Malformed in-body provider error: "
                                        + json.dumps(error, default=str)[:MESSAGE_CHARACTERS])
        kept = ({"status": http_status_from_error_code(error.get("code")), "message": error["message"],
                 "metadata": error.get("metadata")} if error is not None else
                {"status": None, "message": "Response carried no choices"})
        try:
            on_response(response)  # OpenRouter raises its documented retry classes first.
        except Exception as exc:
            exc.provider_error = kept if error is not None else {
                "status": None, "message": "Response carried no choices (provider warm-up)"}
            raise
        # Other adapters: Inspect's error-body vocabulary decides whether the error is retryable.
        retry = classify_error_body(error.get("code"), error.get("type")) if error is not None else None
        failure = (OpenAIResponseError("rate_limit_exceeded" if retry.kind == "rate_limit" else "server_error",
                                       kept["message"]) if retry else
                   ProviderResponseError(kept["message"], malformed=error is None))
        failure.provider_error = kept
        raise failure
    return checked


def _causes(exc):
    # Inspect, tenacity and the SDKs keep the original failure as the explicit cause.
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__
    return chain


def _fields(item):
    """(status, code, message, metadata) carried by one SDK or Inspect exception."""
    status = getattr(item, "status_code", None)
    code = getattr(item, "code", None)
    kept = getattr(item, "provider_error", None)
    if isinstance(kept, dict):  # an HTTP-200 body kept by checked_responses
        return kept.get("status"), code, kept.get("message"), kept.get("metadata")
    message = metadata = None
    if isinstance(body := getattr(item, "body", None), dict):
        message, metadata = body.get("message"), body.get("metadata")
    if isinstance(response := getattr(item, "response", None), dict):
        if isinstance(error := response.get("Error"), dict):  # botocore ClientError
            code, message = error.get("Code"), error.get("Message")
            status = status or response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        elif "message" in response:  # Inspect's OpenRouterError keeps the error object
            status, message, metadata = response.get("code"), response.get("message"), response.get("metadata")
    if message is None and type(item).__name__ not in ("ModelGenerateError", "RetryError"):
        # ModelGenerateError's text embeds the whole request; its cause carries the provider message.
        message = getattr(item, "message", None) if isinstance(getattr(item, "message", None), str) else str(item)
    return status, code, message, metadata


def _label(chain, status, code, text):
    names = {type(item).__name__ for item in chain}
    # A malformed body's text is arbitrary JSON or HTML; it must not drive the classification.
    if any(getattr(item, "malformed", False) for item in chain) or "JSONDecodeError" in names \
            or "unexpected non-chatcompletion response" in text:
        return "malformed provider response"
    if status in (408, 504) or any(isinstance(item, TimeoutError) or "timeout" in type(item).__name__.lower()
                                   for item in chain):
        return "provider timeout"
    if status == 401 or code in ("ExpiredTokenException", "UnrecognizedClientException", "InvalidClientTokenId") \
            or names & {"AuthenticationError", "NoCredentialsError", "TokenRetrievalError"}:
        return "missing/expired credential"
    if status == 402:
        return "insufficient credits"
    if status == 403 or code == "AccessDeniedException" or "PermissionDeniedError" in names:
        return "content-policy block" if "moderation" in text or "flagged" in text else \
            "permission/model access denied"
    if status == 429 or code in ("ThrottlingException", "TooManyRequestsException", "rate_limit_exceeded") \
            or "RateLimitError" in names:
        return "rate limit"
    if "content" in text and ("policy" in text or "filter" in text):
        return "content-policy block"
    if status == 404 or "no endpoints found" in text or "no allowed providers" in text:
        if "data policy" in text or "zero data retention" in text or "zdr" in text:
            return "no endpoint satisfies the routing/ZDR policy"
        if "no allowed providers" in text:
            return "routing policy: no allowed provider"
        if "tool" in text or "schema" in text:
            return "unsupported tool/schema feature"
        if "parameter" in text:
            return "no endpoint supports the requested parameters"
        if "no endpoints found" in text:
            return "no endpoint available for the model and routing policy"
        return "invalid model/deployment/region"
    if status == 413 or any(phrase in text for phrase in (
            "context length", "context window", "maximum context", "too many tokens", "input length",
            "prompt is too long", "input is too long")):
        return "context length exceeded"
    if ("tool" in text or "schema" in text) and any(
            word in text for word in ("unsupported", "not support", "invalid", "only")):
        return "unsupported tool/schema feature"
    if code in ("ValidationException", "ResourceNotFoundException") or \
            ("model" in text or "deployment" in text) and any(
                word in text for word in ("invalid", "unknown", "not found", "does not exist", "not a valid")):
        return "invalid model/deployment/region"
    if status in (500, 502, 503) or code == "server_error" or "InternalServerError" in names:
        return "provider unavailable"
    if status in (400, 422):
        return "invalid request"
    if any(isinstance(item, ConnectionError) or "connect" in type(item).__name__.lower() for item in chain):
        return "network failure"
    return "unclassified provider failure"


def provider_failure(exc, redactor=None):
    """Label a provider failure by HTTP status and error metadata; keep a bounded, redacted message."""
    from .evidence import Redactor

    chain = _causes(exc)
    status = code = message = metadata = None
    for item in chain:
        found = _fields(item)
        status = status if isinstance(status, int) else found[0]
        code = code if isinstance(code, str) else found[1]
        message = message or (found[2] if isinstance(found[2], str) and found[2].strip() else None)
        metadata = metadata or (found[3] if isinstance(found[3], dict) else None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    failure = {"class": _label(chain, status, code if isinstance(code, str) else None,
                               (message or "").lower()),
               "type": type(exc).__name__, "status": status}
    clean = (redactor or Redactor.environment()).text
    if isinstance(code, str):
        failure["code"] = bounded(clean(code), IDENTIFIER_CHARACTERS)
    if any(isinstance(getattr(item, "provider_error", None), dict) for item in chain):
        failure["in_body"] = True
    if message:
        failure["message"] = bounded(clean(message))
    if metadata:
        if isinstance(metadata.get("provider_name"), str):
            failure["provider_name"] = bounded(clean(metadata["provider_name"]), IDENTIFIER_CHARACTERS)
        failure["metadata"] = clean(json.dumps(metadata, default=str, sort_keys=True))[:2 * MESSAGE_CHARACTERS]
    return failure


def describe_failure(failure):
    text = failure["class"] + (f" (HTTP {failure['status']})" if failure.get("status") else "")
    if failure.get("message"):
        text += f": {failure['message']}"
    if failure.get("provider_name"):
        text += f" [provider: {failure['provider_name']}]"
    if hint := FAILURE_HINTS.get(failure["class"]):
        text += f" ({hint})"
    return text


def classify_provider_error(exc):
    return provider_failure(exc)["class"]


def stop_failure(api):
    """The described provider error behind the latest model_length/content_filter stop, if recorded."""
    failure = getattr(api, "stopped_by_provider", None)
    return describe_failure(failure) if isinstance(failure, dict) else None


def converted_error(response, output):
    """The provider error behind an output that Inspect converted from an error response."""
    from inspect_ai.model._openai import http_status_from_error_code

    body = response.get("error", response) if isinstance(response, dict) else None
    body = body if isinstance(body, dict) else {}
    message = body.get("message")
    error = ProviderResponseError(message if isinstance(message, str) and message.strip() else output.completion,
                                  malformed=False)
    error.code = body.get("code") if isinstance(body.get("code"), str) else None
    error.provider_error["status"] = http_status_from_error_code(body.get("code"))
    return error


def replayed_as_text(request):
    """Whether a serialized chat-completions request carries reasoning inside assistant content."""
    messages = request.get("messages") if isinstance(request, dict) else None
    return any(isinstance(message, dict) and message.get("role") == "assistant"
               and "<think" in json.dumps(message.get("content"), default=str)
               for message in messages or [])


def meta_request_problem(tools, tool_choice):
    """Requests the Meta Model API rejects with HTTP 400, refused before dispatch instead."""
    names = [tool.name for tool in tools
             if not META_FUNCTION_NAME.fullmatch(tool.name) or tool.name.count(".") > 1]
    if names:
        return ("Meta Model API function names must match [A-Za-z0-9_.-]+ and contain at most one dot; "
                f"refused before dispatch: {', '.join(names)}")
    if tools and tool_choice != "auto":
        # Without tools no tool_choice is sent; Inspect drops tools for "none" before this boundary.
        return "Meta Model API accepts only tool_choice auto; another tool choice was refused before dispatch"
    return None


def route_confirmation(returned, pinned):
    """OpenRouter returns a display name ('Google AI Studio'); compare it with the pinned slug's base."""
    base, _, variant = pinned.partition("/")
    name = "-".join(returned.lower().split()) if isinstance(returned, str) else None
    record = {"normalized_provider": name, "pinned_base": base, "variant": variant or None}
    if name and name in (pinned, base):
        # The response names a provider, never its quantization or endpoint variant.
        record.update(status="passed", variant_status="not pinned" if not variant else
                      "confirmed" if name == pinned else "variant unverified")
    else:
        record.update(status="failed", variant_status=None)
    return record


def generation(profile):
    extra = None
    effort = None
    if profile.provider == "local-glimmer" and profile.generation.reasoning_strength:
        extra = {"chat_template_kwargs": {"reasoning_strength": profile.generation.reasoning_strength}}
    elif profile.provider == "openrouter":
        extra = {"provider": profile.routing}
        if profile.generation.reasoning_strength:
            extra["reasoning"] = {"effort": profile.generation.reasoning_strength}
    elif profile.provider == "meta":
        effort = profile.generation.reasoning_strength
    return GenerateConfig(
        max_tokens=profile.generation.max_output_tokens,
        temperature=profile.generation.temperature,
        seed=profile.generation.seed,
        timeout=profile.limits.timeout_seconds,
        attempt_timeout=profile.limits.timeout_seconds,
        max_retries=profile.limits.max_retries,
        max_connections=profile.limits.max_concurrency,
        internal_tools=False, cache=False, fallback_models=[],
        extra_body=extra, reasoning_effort=effort,
    )


class ProviderRetryWait(wait_exponential_jitter):
    """Inspect's retry hook: backoff with server hints, without a retry loop."""

    def __init__(self):
        super().__init__(initial=3, max=30 * 60, jitter=3)

    def __call__(self, retry_state):
        delay = super().__call__(retry_state)
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        response = getattr(exc, "response", None)
        headers = (response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
                   if isinstance(response, dict) else getattr(response, "headers", {}))
        value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        if value is not None:
            try:
                hint = float(value)
            except (TypeError, ValueError):
                try:
                    hint = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    hint = 0
            if math.isfinite(hint) and hint > 0:
                delay = max(delay, hint)
        return delay


class BoundedModel(Model):
    """An admission deadline around Inspect's complete native generate call."""

    async def generate(self, *args, **kwargs):
        # Inspect's retry stop condition checks elapsed time after an attempt.
        # This also bounds backoff and a stalled request, including SDK cleanup.
        try:
            async with asyncio.timeout(self.config.timeout):
                return await super().generate(*args, **kwargs)
        except LabError:
            raise
        except Exception as exc:
            if evidence := getattr(self.api, "evidence", None):
                failure = provider_failure(exc, evidence.redactor)
                evidence.manifest["execution_failure"] = {
                    "class": "setup/infrastructure", "code": 2,
                    "detail": f"Provider request failed: {describe_failure(failure)}"}
                evidence.event("provider_request_failed", {
                    **failure, "timeout_seconds": self.config.timeout},
                    provenance="complete Inspect generate call, after retry/deadline enforcement")
                evidence.save()
            raise


@modelapi(name="consumer-agent-testing-tool-capture")
class CapturingAPI(ModelAPI):
    """Records the exact Inspect adapter input and available serialized provider payload.

    No planning, scheduling, tool execution, or inference retry loop lives here.
    """
    def __init__(self, delegate, evidence, routing=None, request_rules=None):
        super().__init__(delegate.model_name, base_url=delegate.base_url)
        self.delegate, self.evidence = delegate, evidence
        self.seen_results = set()
        self.proposals = {}
        self.provider_responses = []
        self.proposed_tool_count = 0
        self.routing = routing
        self.request_rules = request_rules
        # The provider error behind the latest output when Inspect turned it into a stop reason.
        self.stopped_by_provider = None

    def connection_key(self):
        return self.delegate.connection_key()

    def max_tokens(self):
        return self.delegate.max_tokens()

    def should_retry(self, ex):
        return self.delegate.should_retry(ex)

    def retry_wait(self):
        return ProviderRetryWait()

    async def generate(self, input, tools, tool_choice, config):
        for message in input:
            if isinstance(message, ChatMessageTool) and message.tool_call_id not in self.seen_results:
                self.seen_results.add(message.tool_call_id)
                self.evidence.event("tool_result", message.model_dump(mode="json"), audience="model",
                    parent_id=self.proposals.get(message.tool_call_id),
                    provenance="Inspect native tool result")
                if message.function == "skill" and not message.error:
                    self.evidence.event("skill_loaded", {"content": message.content,
                        "load_order": len(self.seen_results)}, audience="model",
                        provenance="Inspect skill loader result")
        payload = {"messages": [m.model_dump(mode="json") for m in input],
                   "tools": [t.model_dump(mode="json") for t in tools],
                   "tool_choice": (tool_choice if isinstance(tool_choice, str)
                                   else tool_choice.model_dump(mode="json")),
                   "generation": config.model_dump(mode="json"),
                   "interface_profile": self.evidence.profile}
        serialized = json.dumps(payload, ensure_ascii=False)
        # Credentials only: an environment contact detail (EMAIL) may be the user's own data. The payload is
        # JSON, so a credential holding a quote or backslash appears there only in its escaped form.
        forms = {form for secret in self.evidence.redactor.credentials
                 for form in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1])}
        if any(form in serialized for form in forms):
            self.evidence.manifest["execution_failure"] = {
                "class": "policy/approval", "code": 1,
                "detail": "Known coordinator credential found in model-visible payload"}
            self.evidence.event("action_denied", {
                "policy": "credential_in_model_context",
                "enforcing_layer": "coordinator provider boundary before network dispatch"})
            raise LabError("Known coordinator credential found in model-visible payload",
                           code=1, category="policy/approval")
        if self.request_rules and (problem := self.request_rules(tools, tool_choice)):
            self.evidence.manifest["execution_failure"] = {"class": "unsupported", "code": 3, "detail": problem}
            self.evidence.event("action_denied", {
                "policy": "provider_request_rules", "detail": problem,
                "tools": [tool.name for tool in tools], "tool_choice": payload["tool_choice"],
                "enforcing_layer": "coordinator provider boundary before network dispatch"})
            self.evidence.save()
            raise Unsupported(problem)
        self.stopped_by_provider = None
        observation = self.evidence.event("observation_emitted", payload, audience="model",
                                          provenance="Inspect provider boundary")
        request = self.evidence.event("model_request", {"observation_event": observation,
            "effective_surface": payload["tools"]}, parent_id=observation, audience="model")
        try:
            result = await self.delegate.generate(input, tools, tool_choice, config)
        except Exception as exc:
            self.evidence.event("provider_error", provider_failure(exc, self.evidence.redactor),
                                parent_id=request)
            raise
        output, call = result if isinstance(result, tuple) else (result, None)
        if isinstance(output, Exception):
            self.evidence.event("provider_error", provider_failure(output, self.evidence.redactor),
                                parent_id=request)
        # Inspect turns context-length and content-policy errors into model_length or
        # content_filter output whose text is the error; keep the provider's message.
        stopped = bool(call and call.error) and not isinstance(output, Exception)
        if stopped:
            reason = next((choice.stop_reason for choice in output.choices
                           if choice.stop_reason in ("model_length", "content_filter")), None)
            failure = provider_failure(converted_error(call.response, output), self.evidence.redactor)
            failure.update(type="converted provider error", converted_stop_reason=reason,
                           **({"class": STOP_FAILURES[reason]} if reason else {}))
            self.stopped_by_provider = failure
        if call:
            self.provider_responses.append(call.response)
            self.evidence.event("provider_serialization", call.model_dump(mode="json"),
                                parent_id=request, provenance="upstream ModelCall")
            if replayed_as_text(call.request) and THINK_REPLAY_GAP not in self.evidence.manifest["observation_gaps"]:
                self.evidence.manifest["observation_gaps"].append(THINK_REPLAY_GAP)
            # An error body serves no completion, so it neither confirms nor refutes the route.
            if self.routing and not isinstance(output, Exception) and not stopped:
                actual_provider = (call.response or {}).get("provider")
                allowed = self.routing["only"]
                route = route_confirmation(actual_provider, allowed[0])
                self.evidence.event("provider_route_verified", {
                    "requested": allowed, "returned_provider": actual_provider, **route,
                    "zdr_requested": self.routing["zdr"],
                    "zdr_basis": "OpenRouter request policy; downstream retention is not independently observable",
                }, parent_id=request)
                if route["status"] != "passed":
                    raise LabError(f"OpenRouter response did not confirm the pinned provider {allowed[0]!r} "
                                   f"(returned {actual_provider!r})")
                gap = (f"OpenRouter confirmed provider {route['pinned_base']!r} but cannot confirm the pinned "
                       f"variant {route['variant']!r}; responses do not identify endpoint variants")
                if route["variant_status"] == "variant unverified" and \
                        gap not in self.evidence.manifest["observation_gaps"]:
                    self.evidence.manifest["observation_gaps"].append(gap)
        else:
            if self.routing:
                raise LabError("OpenRouter serialized response unavailable; cannot confirm pinned provider")
            self.evidence.manifest["observation_gaps"].append(
                "Provider serialized request unavailable; Inspect boundary input captured")
        if not isinstance(output, Exception):
            captured = output.model_dump(mode="json")
            # model_dump writes NaN and Infinity as null; record what the model proposed instead.
            for choice, row in zip(output.choices, captured["choices"], strict=True):
                for call, dumped in zip(choice.message.tool_calls or [], row["message"]["tool_calls"] or [],
                                        strict=True):
                    dumped["arguments"] = recordable(call.arguments)
            if self.stopped_by_provider:
                # Inside the one terminal event per request that timing and usage summaries pair.
                captured["provider_error"] = self.stopped_by_provider
            captured["usage_field_provenance"] = {
                "schema_version": 1, "kind": "inspect-model-usage-fields",
                "explicit_fields": sorted(output.usage.model_fields_set) if output.usage is not None else [],
                "basis": "Explicit Inspect ModelUsage fields; the upstream adapter may already have "
                         "applied defaults. These are not independently verified billing records.",
            }
            response = self.evidence.event("model_response", captured, parent_id=request)
            from .proposal_metrics import record
            record(self.evidence, response, observation, output.message.tool_calls or [], tools)
            self.proposed_tool_count += len(output.message.tool_calls or [])
            limit = self.evidence.manifest.get("tool_call_limit")
            if limit is not None and self.proposed_tool_count > limit:
                self.evidence.manifest["execution_failure"] = {
                    "class": "interaction", "code": 1, "detail": "Scenario tool-call budget exceeded"}
                self.evidence.event("action_denied", {
                    "reason": "Scenario tool-call budget exceeded", "limit": limit,
                    "proposed_count": self.proposed_tool_count,
                    "enforcing_layer": "provider boundary before Inspect tool dispatch",
                    "batch": [recordable(asdict(call)) for call in output.message.tool_calls or []],
                }, parent_id=request)
                raise LabError("Scenario tool-call budget exceeded", code=1, category="interaction")
            for call in output.message.tool_calls or []:
                # Non-finite arguments must not turn an ordinary invalid proposal into a setup failure.
                self.proposals[call.id] = self.evidence.event("action_proposed",
                    recordable(asdict(call)), parent_id=request,
                    provenance="Inspect native tool proposal", audience="model")
        self.evidence.save()
        return result


def model_lineage(profile: ModelProfile) -> dict | None:
    """What a profile's model is derived from, when a primary source says (Muse Spark Open: SPARK_OPEN_LINEAGE)."""
    model_id = getattr(profile, "model_id", None)
    return dict(SPARK_OPEN_LINEAGE) if isinstance(model_id, str) and SPARK_OPEN.search(model_id) else None


def record_model_context(profile: ModelProfile, evidence):
    """Make the profile's support label, model lineage and provider-side prompt additions part of the run lock."""
    lock = evidence.manifest["run_lock"]
    lock["model_support"] = {"label": profile.support, "note": SUPPORT_NOTES[profile.support]}
    if replay := REASONING_REPLAY.get((profile.provider, profile.api)):
        lock["reasoning_replay"] = replay
    if lineage := model_lineage(profile):
        lock["model_lineage"] = lineage
        if SPARK_OPEN_GAP not in evidence.manifest["observation_gaps"]:
            evidence.manifest["observation_gaps"].append(SPARK_OPEN_GAP)
    if profile.provider == "meta":
        lock["provider_disclosures"] = {"meta_steering_context": META_STEERING_CONTEXT}
        if META_STEERING_GAP not in evidence.manifest["observation_gaps"]:
            evidence.manifest["observation_gaps"].append(META_STEERING_GAP)


def replay_reasoning_fields(api):
    """OpenRouterAPI replays reasoning that arrived without reasoning_details as <think> text inside
    assistant content; return it in the reasoning field it arrived in, as the generic adapter does."""
    from inspect_ai.model._providers.openrouter import (
        _requires_reasoning_content,
        reasoning_to_openrouter_reasoning_details,
    )

    convert = api.messages_to_openai
    fields = ("reasoning", "reasoning_content")

    async def converted(input):
        if _requires_reasoning_content(api.model_family()):  # already replayed as a field
            return await convert(input)
        messages, extras = [], []
        for message in input:
            extra = {}
            if message.role == "assistant" and not isinstance(message.content, str):
                kept = []
                for part in message.content:
                    if part.type == "reasoning" and part.internal in fields \
                            and reasoning_to_openrouter_reasoning_details(part) is None:
                        extra[part.internal] = part.reasoning
                    else:
                        kept.append(part)
                if extra:
                    message = message.model_copy(update={"content": kept or ""})
            messages.append(message)
            extras.append(extra)
        return [{**param, **extra} for param, extra in zip(await convert(messages), extras, strict=True)]

    api.messages_to_openai = converted


def resolve_model(profile: ModelProfile, evidence=None):
    if profile.provider not in LIVE_ADAPTERS:
        raise Unsupported(f"{profile.provider} is specified/unverified; configuration templates "
                          "are available but its live adapter gate has not passed. "
                          "Live adapters: bedrock, meta, openrouter")
    if profile.provider == "bedrock" and profile.generation.streaming:
        raise Unsupported("Bedrock streaming has not passed its separate transport gate; "
                          "the current MVP preflight verifies non-streamed Converse")
    if evidence:
        record_model_context(profile, evidence)
    config = generation(profile)
    secret = credential(profile)
    if evidence and secret:
        evidence.redactor.add([secret])
    rules = None
    if profile.provider == "bedrock":
        from botocore.tokens import FrozenAuthToken
        delegate = get_model(f"bedrock/{profile.model_id}", base_url=profile.base_url, config=config,
                              streaming=profile.generation.streaming, region_name=profile.region,
                              read_timeout=profile.limits.timeout_seconds,
                              connect_timeout=min(15, profile.limits.timeout_seconds), memoize=False).api
        # Inspect owns the bounded retry policy. Prevent a second, multiplicative
        # SDK retry budget underneath it.
        delegate.session.set_config_variable("max_attempts", 1)

        class ExplicitTokenProvider:
            def load_token(self, **kwargs):
                # IAM runs must not silently become bearer runs because of ambient keys.
                return FrozenAuthToken(secret) if secret and kwargs.get("signing_name") == "bedrock" else None

        delegate.session.register_component("token_provider", ExplicitTokenProvider())
        if profile.auth.mode == "api_key":
            class NoIAM:
                async def load_credentials(self):
                    return None
            # Bearer-only mode must not discover IAM credentials, SSO or IMDS.
            delegate.session.register_component("credential_provider", NoIAM())
        elif profile.auth.profile:
            delegate.session.set_config_variable("profile", profile.auth.profile)
        if profile.auth.mode == "aws_credentials":
            # Modeled auth can prefer bearer based on ambient configuration even
            # with our token provider disabled. Select the requested signer through
            # the SDK's event extension point; the SDK still owns credential refresh.
            delegate.session.register("choose-signer.bedrock-runtime.*", lambda **kwargs: "v4")
    elif profile.provider == "local-glimmer":
        if profile.generation.streaming:
            raise Unsupported("Local streaming has not passed the adapter gate")
        delegate = get_model(
            f"openai-api/local-glimmer/{profile.model_id}", base_url=profile.base_url,
            api_key=secret or "local-no-secret", config=config, responses_api=False, emulate_tools=False,
            strict_tools=False, stream=False, max_retries=0,
            client_timeout=profile.limits.timeout_seconds, memoize=False).api
    elif profile.provider == "openrouter":
        if profile.generation.streaming:
            raise Unsupported("OpenRouter streaming has not passed its separate transport gate")
        # Inspect's dedicated provider replays reasoning_details structurally; the generic
        # adapter would replay prior reasoning as <think> text inside assistant content.
        delegate = get_model(
            f"openrouter/{profile.model_id}", base_url=profile.base_url,
            api_key=secret, config=config, responses_api=False, emulate_tools=False,
            strict_tools=False, stream=False, max_retries=0,
            client_timeout=profile.limits.timeout_seconds, memoize=False).api
        replay_reasoning_fields(delegate)
    elif profile.provider == "meta":
        if profile.generation.streaming:
            raise Unsupported("Meta Model API streaming has not passed its separate transport gate")
        delegate = get_model(
            f"meta/{profile.model_id}", base_url=profile.base_url, api_key=secret, config=config,
            responses_api=profile.api == "responses", strict_tools=False, stream=False, max_retries=0,
            client_timeout=profile.limits.timeout_seconds, memoize=False).api
        rules = meta_request_problem
    else:
        raise Unsupported(f"{profile.provider} is specified/unverified; configuration templates "
                          "are available but its live adapter gate has not passed")
    if profile.provider != "bedrock":  # the OpenAI-compatible adapters' chat-completions bodies
        delegate.on_response = checked_responses(delegate.on_response)
    if evidence:
        delegate = CapturingAPI(delegate, evidence, profile.routing if profile.provider == "openrouter" else None,
                                request_rules=rules)
    else:
        delegate.retry_wait = ProviderRetryWait
    return BoundedModel(delegate, config)


def _stops(output, api):
    if failure := getattr(api, "stopped_by_provider", None):
        # A provider error that Inspect turned into this stop reason; name it with its message.
        raise LabError(f"Provider preflight: {describe_failure(failure)}", category="setup/infrastructure")
    if any(choice.stop_reason == "content_filter" for choice in output.choices):
        raise LabError("Provider preflight: content-policy block", category="setup/infrastructure")
    if any(choice.stop_reason == "model_length" for choice in output.choices):
        raise LabError("Provider preflight: stopped at a length limit (context window or max_output_tokens)",
                       category="setup/infrastructure")


async def preflight(profile: ModelProfile, evidence):
    if profile.provider == "local-glimmer":
        from .local_runtime import inspect_runtime
        await inspect_runtime(profile, evidence, credential(profile))
    model = resolve_model(profile, evidence)
    tool = ToolInfo(name="fixture_probe", description="Echo nested probe data.", parameters=ToolParams(
        type="object", properties={"payload": JSONSchema(type="object", properties={
            "count": JSONSchema(type="integer"), "label": JSONSchema(type="string")},
            required=["count", "label"], additionalProperties=False)},
        required=["payload"], additionalProperties=False))
    # Meta accepts only tool_choice "auto"; an explicit instruction replaces the forced choices.
    auto_only = profile.provider == "meta"
    choices = ("auto", "auto") if auto_only else ("any", "none")
    prompt = ('Call fixture_probe exactly once with payload {"count":7,"label":"probe"} using native tool '
              'calling. After the tool result, do not call any tool again; reply with its receipt as plain text.'
              if auto_only else 'Call fixture_probe with payload {"count":7,"label":"probe"} '
              'using native tool calling. After the result, return its receipt.')
    messages = [ChatMessageUser(content=prompt)]
    try:
        async with asyncio.timeout(profile.limits.timeout_seconds * 2):
            output = await model.generate(messages, tools=[tool], tool_choice=choices[0])
            _stops(output, model.api)
            calls = output.message.tool_calls or []
            if len(calls) != 1 or calls[0].function != "fixture_probe" or \
                    calls[0].arguments != {"payload": {"count": 7, "label": "probe"}}:
                raise Unsupported("Provider did not produce the required native nested tool call")
            call = calls[0]
            if not call.id:
                raise Unsupported("Provider omitted native call ID")
            messages += [output.message, ChatMessageTool(
                content='{"receipt":"probe-accepted-7"}', tool_call_id=call.id,
                function="fixture_probe")]
            continued = await model.generate(messages, tools=[tool], tool_choice=choices[1])
            _stops(continued, model.api)
            if continued.message.tool_calls or "probe-accepted-7" not in continued.completion:
                raise Unsupported("Provider failed tool-result continuation/correlation")
    except LabError:
        raise
    except Exception as exc:
        failure = provider_failure(exc, evidence.redactor)
        raise LabError(f"Provider preflight: {describe_failure(failure)}",
                       category="setup/infrastructure") from exc
    checks = [{"id": "native-tool-continuation", "status": "passed", "model": profile.model_id,
               "returned_model": continued.model, "api": profile.api, "region": profile.region,
               "auth_mode": profile.auth.mode, "tool_choice": list(choices)}]
    if profile.provider == "local-glimmer":
        from .local_runtime import stress_context
        raw_messages = [choice["message"] for response in model.api.provider_responses if response
                        for choice in response.get("choices", [])]
        separated = any(isinstance(message.get("reasoning_content"), str)
                        and message["reasoning_content"] for message in raw_messages)
        checks.append({"id": "native-reasoning-separation",
                       "status": "passed" if separated else "observation-gap",
                       "basis": "Provider reasoning_content field; no prose/regex extraction"})
        checks.append(await stress_context(profile, model, evidence, credential(profile)))
    return checks
