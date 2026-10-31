# Consumer purchase behaviors

The local insurance examples test an application through review, offer selection,
carrier follow-up and policy retrieval. They use the same connector runner under
the Muse and Instinct surrogates. Independent service records determine submission,
charges, policies and support requests.

The fixture takes its workflow boundaries from
[Coverage Cat's consumer API](https://www.coveragecat.com/api/consumer/openapi.yaml),
reviewed on 31 October 2026. It implements a small synthetic subset, with simplified
fields and a fixture-only `confirm_estimates` checkpoint. It does not reproduce the
full API schema, underwriting, OCR, card processing or carrier operations.
No customer data, external request, paid inference, funded transaction or email is
needed. A pass describes the local surrogate run.

## Run the purchases

From the repository root:

```sh
for target in muse instinct; do
  for line in umbrella homeowners; do
    uv run consumer-agent-testing-tool connector-run "examples/connector/purchase-$line.yaml" \
      --emulation-target "$target" --driver scripted \
      --script "examples/connector/purchase-$line.calls.yaml" \
      --start "python -m consumer_agent_testing_tool.purchase_fixture --port {port} --line $line" \
      --ready-url "http://127.0.0.1:{port}/health" \
      --summary-json "reports/$target-$line.json"
  done
done
```

The [umbrella scenario](../../examples/connector/purchase-umbrella.yaml) starts with
prefill, reviews and submits once, selects the chosen offer, uploads both declarations,
requests secure payment setup, handles its synthetic callback and polls for a policy.
Its first prefill carries `sandbox: true`; follow-up calls keep the same intake.

The [homeowners scenario](../../examples/connector/purchase-homeowners.yaml) waits for
quotes, approves the exact purchase preview, answers a pending carrier question in the
same intake and polls through carrier follow-up. Payment and signing retain the customer
email channel. The public homeowners API has no equivalent sandbox switch; this
homeowners fixture is local synthetic data only.

The existing clock and scheduler advance the fixture by minutes or days. They do not
wait 48 wall-clock hours or establish a hosted assistant's scheduling capability.
The scripted user's approvals represent the chosen treatment. They do not establish
that a real shopper supplied consent.

The calls files use native tool names.
[End-to-end tests](../../tests/test_purchase_e2e.py) also translate those same steps into
the shared CLI command syntax and test both projections for both targets. This keeps
one purchase script per product. The secure-UI callback uses fictional IDs; actual
local wallet and MCP Apps mechanics remain in their existing suites below.

## Boundaries and failure controls

| Boundary | Local control and observation |
| --- | --- |
| Review before submission | Prefill creates a draft. Declined submission leaves the submission count at zero. A duplicate submit is refused. |
| Same intake, newest credential | Every accepted follow-up rotates its access token. A stale token or another intake's token is refused. Host arguments keep credentials out of model inputs and public evidence. |
| Offer and authority | Soft-credit consent and UM/UIM selection must accompany the umbrella choice. The price-change fault requires a new offer and approval; the old budget approval cannot authorize it. |
| Documents before payment | Both requested synthetic declarations must arrive. Incomplete, invalid or unsolicited uploads cannot reach payment setup. |
| Payment setup before fallback | `payment_needed` leads first to native setup. The unavailable-UI fault returns a specific fallback reason and an inert `.invalid` checkout URL. |
| Method, charge and policy | A method on file leaves charge and policy counts at zero. A later carrier transition records each effect once. A forged policy receipt cannot overrule the oracle. |
| Delayed approval | Background writes wait. An unanswered payment card sends no payment request; late answers pass through the same approval engine. |
| Homeowners carrier work | Early polls remain pending. Bind requires the exact preview token. The carrier question must receive a reply addressed to its returned message ID. |
| Lost or malformed acknowledgment | A submit can commit before its response is lost or malformed. The client does not retry it. Unreconciled effects remain blocked; strict schema failures remain failures. |
| Policy support | The service previews the exact request before confirmation. A pending cancellation request changes neither policy status nor refund count. False cancellation and refund claims fail. |

[Fixture tests](../../tests/test_purchase_fixture.py) exercise the state prerequisites.
[Pipeline tests](../../tests/test_purchase_e2e.py) exercise dispatch, approvals,
background work, independent checks and sealed evidence.
[Capture tests](../../tests/test_connector_capture_rotation.py) verify that multiple
follow-up tools can rotate one host-owned credential and that error responses cannot
replace it. [Claim tests](../../tests/test_answer_purchase_claims.py) cover insurance
wording and the separate `refunded` outcome.

These controls separate a tool receipt from a committed effect. They do not establish
real carrier timing or general language understanding. Answer detection remains
lexical and English-only. A refund requires its own oracle check; collecting a premium
does not prove money was returned. A missing capture on a pending response is recorded
as an observation gap; a later offer can supply it.

## Check modern MCP availability

An installed MCP SDK major version does not show which protocol a service supports.
Use [modern-readiness.yaml](../../examples/connector/modern-readiness.yaml) against the
endpoint you intend to test:

```sh
uv run consumer-agent-testing-tool check http://127.0.0.1:8765/mcp \
  --suite surrogate --emulation-target instinct \
  --manifest examples/connector/modern-readiness.yaml \
  --summary-json reports/modern-mcp.json
```

The manifest requires the observed `2026-07-28` revision. It uses modern discovery and
catalog access, with no purchase tool or model call. A usable legacy fallback fails
this requirement. A client restricted to older revisions is unsupported before
traffic. Authorization, reachability and malformed discovery errors cannot become
availability passes. Repeat with `--emulation-target muse`.

This extends shared readiness. The existing
[modern contract profile](../readiness-checks.md#mcp-2026-07-28-contract-profile)
still supplies deeper protocol checks; readiness does not claim conformance or
admission. The [MCP lifecycle](https://modelcontextprotocol.io/specification/2026-07-28/basic/lifecycle)
and [discovery contract](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
describe the dated protocol.

## Research ideas and existing coverage

The benchmark survey is a checklist for behavior selection. No external leaderboard
scores, datasets, grading system or reconstructed assistant runtime were imported.
Task completion, delegated authority, infrastructure health and observable coverage
remain separate.

| Source or benchmark family | Useful idea | CATT implementation or existing coverage |
| --- | --- | --- |
| [τ-bench family](https://github.com/sierra-research/tau2-bench) | Interactive tools, policy constraints and clarification | Scripted approval rules and user replies; new review, choice and bind boundaries. [User-simulator tests](../../tests/test_ext_user_simulator_replies.py) already cover that extension; model-based simulation still requires paid-inference authorization. |
| [ToolSandbox](https://github.com/apple/ToolSandbox) | Intermediate state and prerequisite dependencies | The purchase ledger and state-transition tests, including zero-effect refusals. Existing [behavior journal](../../tests/test_behavior_journal.py) covers interruption and revocation. |
| [AppWorld](https://appworld.dev), ThinkingBox-Bench | Grade underlying records rather than persuasive receipts | Existing connector oracles, now with separate submission, payment-method, charge, policy and refund records. No second outcome-grading engine. |
| [Gaia2 / Meta ARE](https://github.com/facebookresearch/meta-agents-research-environments) | Changing information and asynchronous work | Existing [scheduler](../../tests/test_ext_scheduler_e2e.py), [polling](../../tests/test_ext_scheduler_polling.py) and recovery controls; purchase cases add document review and carrier delays. |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo), AuthorizationBench, SovereignPA-Bench | Legitimate completion alongside trust and authority checks | Existing [content-trust suite](../../tests/test_ext_content_trust_suite.py), [permission rules](../../tests/test_permissions_rules.py) and [behavior observations](../../tests/test_behavior_observations.py). New purchase cases reuse their dispatch boundary and add honest pending outcomes. |
| MCP-Bench, Berkeley Function Calling Leaderboard | Tool discovery and selection | Existing [route tests](../../tests/test_ext_routes_run.py) and MCP contract tests. New [protocol availability tests](../../tests/test_surrogate_readiness_protocol.py) require observable discovery; a well-formed call is still insufficient evidence of purchase success. |
| MyPCBench, MCP-Persona, Persona2Web, PersonalWAB, PersonaLens; LongMemEval, LoCoMo, MemoryAgentBench | Preferences, retained context, stale facts and conflict handling | Existing [memory context](../../tests/test_ext_memory_context.py), [memory lifecycle](../../tests/test_memory_index.py) and [context-driven routes](../../examples/connector/routes-competing-connectors.yaml). No duplicate personal-account environment or new memory score. |
| ClawBench, WebArena family, OSWorld, WorkArena family, TheAgentCompany | Browser or desktop actions and submitted state | Existing [browser tasks](../../tests/test_ext_browser_tasks.py), [wallet checkout](../../tests/test_ext_wallet_run.py) and [MCP Apps](../../tests/test_surface_p0_mcp_apps.py). The purchase fixture adds orchestration boundaries without duplicating the UI runner. |
| Finished-assistant comparisons: Assistant Benchmark, PersonalAgentEval, rtrvr, SuperCLUE-XClaw, Personal Agent Bench | Reproducible errands and explicit coverage limits | Existing [behavior runtime and evidence rules](../behavior-runtime.md). Keep assistant, model, host treatment, case version and observation window distinct; do not import product rankings. |
| GAIA, AssistantBench, AgencyBench | Broader research and deliverable tasks | Outside this purchase change. Existing browser, API and file-handling paths remain available; a benchmark name alone does not justify another fixture. |

Coverage Cat's
[existing surface checkout notes](../coveragecat-sandbox-checkout.md) cover the older
review/prefill exercise and preserve its recorded submission failure. These new local
checks do not turn that historical failure into a pass.
