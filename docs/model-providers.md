# Model providers

Model profiles in the configuration file select the inference adapter for
`doctor --model-preflight`, `run`, `connector-run --driver agent`, `surface-run --driver agent`
and `browse --driver agent`. This page describes the adapters, their checks
and how provider failures are reported. Setup commands are in the
[README](../README.md#setup).

## Adapters

| Provider | In `local-consumer-v1` | API | Key variable |
| --- | --- | --- | --- |
| `openrouter` | Live adapter: Inspect's `openrouter` provider | Chat Completions | `OPENROUTER_API_KEY` |
| `meta` | Live adapter: Inspect's `meta` provider; no live run recorded | Responses (default) or Chat Completions | `MODEL_API_KEY` or `META_API_KEY` |
| `bedrock` | Live adapter; live qualification deferred | Converse | `AWS_BEARER_TOKEN_BEDROCK`, or AWS credentials |
| `openai`, `anthropic`, `gemini`, `azure-openai`, `vertex-gemini`, `openai-compatible` | Configuration templates only | — | — |
| `local-glimmer` | Development extension | Chat Completions | Optional local key |

`init` refuses the template-only providers unless `--development-extensions`
comes before `init`, so no configuration it writes for the supported release
fails when the model is resolved. Runs refuse template profiles before any
request. No adapter supports streaming; `generation.streaming: true` is refused.

## Support labels

Each profile has a `support` label: `supported`, `MVP gate` or
`specified/unverified`. `init` writes `specified/unverified`. The label is a
declaration, not a check. `doctor --model-preflight`, `run`, and the agent drivers of
`connector-run`, `surface-run` and `browse` print a warning on stderr for any
profile not labeled `supported`:

```text
warning: model profile 'openrouter-dev' (openrouter meta/muse-spark-open) is specified/unverified: configuration template; its live adapter gate has not passed. Results are not qualified provider evidence.
```

The run lock records the label and its note as `model_support`. `supported`
declares that the profile has attached live evidence; a run does not verify it
again.

A run whose model is Muse Spark Open (a model id with `muse-spark-open`) also
records `run_lock.model_lineage`: basis `documented`, quoting
`https://dev.meta.ai/models/muse-spark-open` (checked 15 October 2026), which
says the model is derived from the Muse Spark model released in August. It
adds the observation gap "The model is Muse Spark Open, derived from the
August Muse Spark release; Muse runs Muse Spark 1.3, so model behavior here is
not Muse's own model's". Model preflights, `run` trials, agent sub-runs of
`connector-run`, `surface-run` and `browse`, and connector run locks all
record it.

## Credentials and training opt-in

Key variables are read from the coordinator environment only and never written
to evidence. For a Meta profile that names `MODEL_API_KEY` or `META_API_KEY`,
either variable may hold the key; if both are set, they must agree. A profile
whose `auth.env` names another variable reads only that variable. A target's
credential must use a variable that no provider or inference profile uses,
including both Meta aliases. Before each model request, the provider boundary
refuses a payload that contains a registered credential
(`credential_in_model_context`, exit 1).

Model IDs that contain the token `contributor` select a contributor mode, in
which the provider may train on your prompts and responses. OpenRouter
`:variant` suffixes (`...-contributor:nitro`) and dated slugs
(`...-contributor-20260902`) count. Such a profile needs
`allow_training_on_prompts: true`, which `init --allow-training-on-prompts`
writes. The setting is refused on any other model ID.

## Preflight

`run` probes the selected profile once per invocation, before any fixture
action or trial. The model must make one native tool call with nested
arguments, then continue from the tool result without calling a tool again.
`doctor --model-preflight` runs the same probe; without that opt-in, `doctor`
checks the environment and makes no model request, even with a saved profile.
`surface-run --driver agent` runs it before
connecting to the service, and `browse --driver agent` before opening the
browser. A provider error during the probe is a setup failure (exit 2). A
provider that cannot make the call or continue from its result is unsupported
(exit 3).

## OpenRouter

Each OpenRouter response must name the pinned provider. OpenRouter returns a
display name, which is compared after lowercasing and replacing spaces with
hyphens: `Google AI Studio` becomes `google-ai-studio`. A pin with a variant,
such as `nebius/fp4`, passes when the base provider matches. The run then
records `variant_status: variant unverified` and an observation gap, because
responses do not identify endpoint variants. A pin without a variant records
`not pinned`. Any other provider fails the request. The `provider_route_verified`
event records the requested and returned providers, `zdr_requested` and both
statuses. An error response confirms nothing, so it is not checked. The
endpoint catalog is not consulted to confirm a variant or quantization.

`run_lock.reasoning_replay` records how earlier reasoning reaches the next turn:
as structured `reasoning_details`, or in the `reasoning` or `reasoning_content`
field it arrived in. Any replay as `<think>` text inside assistant content is
recorded as the observation gap "Prior reasoning was replayed as <think> text
inside assistant content".

Routing and ZDR settings are described in the
[README](../README.md#openrouter-model-and-privacy-settings).

## Meta Model API

`init --provider meta` writes `api: responses` and
`base_url: https://api.meta.ai/v1`; `--api chat-completions` selects Chat
Completions. Meta credentials may reach only `api.meta.ai` or a loopback test
fixture. The adapter uses the OpenAI SDK from the `openrouter` extra.

The host refuses two kinds of request before dispatch, because Meta rejects them
with HTTP 400. Function names must match `[A-Za-z0-9_.-]+` and contain at most
one dot, and a request with tools must use `tool_choice: auto`. A refusal is an
`action_denied` event (`provider_request_rules`) and exits 3. The preflight
therefore uses `auto` with explicit instructions instead of forced tool choices.

`generation.seed` is refused with the Responses API, which has no seed
parameter. `generation.reasoning_strength` sets the reasoning effort. With
Responses, reasoning carries across turns as reasoning items with encrypted
content (`store: false`). With Chat Completions none is replayed, because Meta
redacts reasoning for external keys.

Meta documents unbilled steering context, a system prompt and related
scaffolding, that it injects into every prompt. It is added on Meta's servers
and is absent from captured requests.
`run_lock.provider_disclosures.meta_steering_context` records this with its
source, and every Meta run carries the observation gap "Meta-injected steering
context is not captured; the recorded model input is incomplete". The API is
pay-as-you-go; see [Meta's pricing](https://dev.meta.ai/docs/pricing-rate-limits).

The Meta adapter does not use Meta's server-side `tool_search` or deferred tool
loading, and does not support the Messages API. No live Meta Model API run has
been recorded; the adapter is tested against local fixtures.

## Failure labels

Provider failures are labeled from the HTTP status and error metadata. The CLI
shows `<label> (HTTP <status>): <message> [provider: <name>] (<hint>)`, with a
bounded, redacted copy of the provider's message. `provider_error` and
`provider_request_failed` events record the same fields: `class`, `status`,
`code`, `message`, `provider_name`, `in_body` and `metadata`.

| Label | Typical cause |
| --- | --- |
| `missing/expired credential` | HTTP 401, or an SDK credential error |
| `insufficient credits` | HTTP 402 |
| `permission/model access denied` | HTTP 403 |
| `content-policy block` | A moderation 403, or a content-policy or filter error |
| `rate limit` | HTTP 429 |
| `provider timeout` | HTTP 408 or 504, or a client timeout |
| `no endpoint satisfies the routing/ZDR policy` | HTTP 404 that names the data policy or ZDR |
| `routing policy: no allowed provider` | HTTP 404 "no allowed providers" |
| `no endpoint available for the model and routing policy` | HTTP 404 "no endpoints found" |
| `no endpoint supports the requested parameters` | HTTP 404 that names parameters |
| `unsupported tool/schema feature` | A tool or schema refusal |
| `invalid model/deployment/region` | HTTP 404, or an unknown model |
| `context length exceeded` | HTTP 413, or a context-length message |
| `invalid request` | Another HTTP 400 or 422 |
| `provider unavailable` | HTTP 500, 502 or 503 |
| `malformed provider response` | An HTTP 200 Chat Completions body with empty `choices` (for Meta and local Glimmer, also one without `choices`), a non-JSON body, or a body that is not a completion |
| `network failure` | A connection error |
| `unclassified provider failure` | Anything else |

The three routing labels add a hint that names the profile fields to check,
such as `check routing.only and routing.zdr in the model profile`.

OpenRouter's documented warm-up body, which has no `choices`, is retried within
the profile's `limits.max_retries` and then labeled `provider unavailable`.

For every OpenAI-compatible adapter (Meta, OpenRouter and local Glimmer), an
HTTP 200 Chat Completions body that carries an `error` object is classified by
that error. When Inspect turns a context-length or content-policy error into a
`model_length` or `content_filter` stop, the provider's message is kept: in the
preflight error, as `provider_error` on the `model_response` event, and in the
trial's "model output is incomplete" reason. The HTTP status of such a converted
error is known only when its body has a numeric code, as OpenRouter's does.
