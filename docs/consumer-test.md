# Local consumer umbrella test

This document describes the original Python consumer helper and its historical
tests. For the general embedded MCP app host, use `consumer-agent-testing-tool surface-run` as
documented in the [README](../README.md#run-a-consumer-test).
The separate [checkout diagnostics](../tooling/payment-ui/README.md) exercise
the historical payment widgets without model inference.

## Existing application checkout

To exercise only the payment step of a user-authorized existing application:

```sh
.venv/bin/python -m consumer_agent_testing_tool.consumer_test \
  --origin "$TEST_ORIGIN" \
  --resume-uid APPLICATION_UID \
  --shopper examples/coveragecat-payment-ready-shopper.json --payment-checkout
```

The intake token and inference key are prompted without echo; do not put them
on the command line. A saved private `--checkpoint` can replace `--resume-uid`.
Set `TEST_ORIGIN` to the explicitly authorized test server; there is no default.
Checkpoints must contain the matching `origin` as well as the application UID
and token. Unbound legacy checkpoints are rejected until their issuing origin
has been verified and recorded privately.
This mode first fetches `consumer_intake_issues`, then exposes only status and
payment setup to the configured model. It requires a fresh `payment_needed` response for the
same application before accepting payment setup. It cannot prefill, patch,
select, bind or upload. An existing application's already-selected offer does
not require inventing a new selection decision.

The 6 October test reached `payment_collection_ready` with a Stripe SetupIntent
in the test environment. This Python host has no secure MCP payment-app renderer.
The service reported UI availability `unknown` and fallback reason
`host_capabilities_not_provided`, and returned a local payment link. No card was
sent or form completed. That result verifies setup and fallback, not a saved
payment method, collected premium or active policy.

## Initial shopping and review

This extension uses Inspect's native agent, the saved OpenRouter model profile,
and the official MCP client. The profile created in the README's Setup uses
Muse Spark Open `meta/muse-spark-open`, with ZDR enabled by default; no profile
is checked in. Both this helper and the
smoke probe accept `--config FILE` and `--model-profile NAME` and honor
`routing.zdr: false` if deliberately configured. See the README's
[routing availability and privacy settings](../README.md#openrouter-model-and-privacy-settings):
Together is now the default route and has passed a live native-tool probe with ZDR enabled.
Coverage Cat requests are restricted to `/api/consumer/mcp` on the explicit
`--origin`. It must be HTTPS, or HTTP on loopback, without a path, credentials,
query, or fragment. Redirects are disabled.
No production Coverage Cat connector, operator credential, browser or shell tool
is available to the evaluated model.

```sh
.venv/bin/python scripts/openrouter_smoke.py
.venv/bin/python -m consumer_agent_testing_tool.consumer_test \
  --origin "$TEST_ORIGIN" --stop-after-prefill
```

Both commands read the selected profile's credential environment variable
(`OPENROUTER_API_KEY` by default) or prompt without echo. Keys are never
written to configuration. Both use paid inference and require explicit user
authorization; only the consumer helper's `--status-only` mode avoids inference.
The fake shopper fixture is
`examples/coveragecat-fake-shopper.json`. Native nested tool calls and continuation
must pass the smoke probe.

The consumer test exposes exactly seven tools: `umbrella_consumer_prefill`,
`consumer_intake_issues`, `consumer_intake_patch`, `umbrella_consumer_select`,
`umbrella_consumer_bind`, `umbrella_consumer_attach`,
`umbrella_consumer_payment`. Documented dotted address fields are normalized to
the equivalent nested object; conflicting or changed shopper facts are rejected.

Each response creates a mode-0600 `consumer-checkpoint.json` inside its run
directory. This private resume file contains intake/selection credentials and
must not be shared. It is outside the normal export allowlist. Model observations
and evidence redact intake, polling, resume, review, selection and payment setup
tokens. The checkpoint contains no OpenRouter key.

After the human confirms the actual staged review and any estimated test
defaults, resume with:

```sh
.venv/bin/python -m consumer_agent_testing_tool.consumer_test \
  --origin "$TEST_ORIGIN" \
  --checkpoint runs/RUN_ID/consumer-checkpoint.json --confirm-review
```

The host stops at quotes. A separate `--decision FILE` must contain the user's
actual selection token, `um_uim_election` in the service's vocabulary, any required
real-user `credit_consent`, and carrier `intake` answers from the same selection
reply. Do not synthesize these choices. Monoline's construction and farming
answers were authorized as No for this fixture.

To read the current state once without inference or another submission:

```sh
.venv/bin/python -m consumer_agent_testing_tool.consumer_test \
  --origin "$TEST_ORIGIN" \
  --checkpoint runs/RUN_ID/consumer-checkpoint.json --confirm-review --status-only
```

The 6 October confirmed fake submission returned `no_quotes`; a status read
confirmed it. Checkout is not verified by that result. Credentials in returned
review-link queries/fragments are redacted before model context and export.

If declarations are requested, only these approved placeholder files are read:

- `/tmp/coveragecat-test-home-declarations.pdf`
- `/tmp/coveragecat-test-auto-declarations.pdf`

At payment, the host forces `payment_collection_requested: true`. This Python
host has no registered secure payment UI; it stops and reports the returned
`payment_url` and that fallback reason. Card fields are never exposed to the
model. Do not follow a production payment URL under this local-only test.

## Evidence limits

This is a consumer-flow extension with public tool-response evidence, not
independent confirmation of carrier/backend state or an issued policy. It does
not qualify the original local-weight/offline Glimmer gate or either live Bedrock
authentication gate. Model-generated summaries should be checked against the
structured service response.
