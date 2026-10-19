# Consumer Agent Testing Tool review: Muse-alike emulation gaps and improvements

**Date:** 15 October 2026. **Scope:** this workspace as of 15:40 PDT (source files last
modified 13:07, including the new Coverage Cat checkout example). **Method:** [section 1](#1-how-this-review-was-done).
Findings marked *repro* were reproduced by an independent verifier who was told to refute them.
Muse claims marked **official** were re-fetched from Meta pages and quoted verbatim.

## Summary

**What works.** Consumer Agent Testing Tool's core is careful and unusual in agent test tooling:

- backend checks against an independent oracle;
- RFC 8785 consent bound to the effective arguments;
- durable one-use grants;
- lost-response reconciliation and resumable trials with no mutation replay;
- deliberately defective fixture variants;
- sealed evidence with separate execution, verification and reporting status;
- a browser facade that gives the agent no JavaScript, selectors or CDP;
- explicit `unknown` constraint records.

Under the release runner, all 1,031 default tests pass.

**Verdict.** The supported "Muse-alike agent" does not test tool use. In `surface-run --driver
agent`, the model sees only `perform_action(id)` over the scenario steps that are ready
(`src/consumer_agent_testing_tool/surface_agent.py:28-33`). It never sees the MCP server's tools, schemas or
results. Every shipped example has at most one ready step, yet runs report
`compatibility_pass: true`.

The root cause is a scoping decision, not a small bug. `local-consumer-v1` moved the CLI/skill
lane to development-only, although:

- the spec made that lane the MVP default;
- Meta says Muse's model was trained for "zero-shot tool calling using CLIs and skills";
- Meta says built-in connectors are thin CLIs plus SKILLs.

**What changed since the spec.** After the spec's 1 October research snapshot, Meta published
a concrete, testable connector contract. Consumer Agent Testing Tool covers little of it.

- [Connector guidelines](https://platform.muse.ai/docs) define tool classes Read / Write /
  Sensitive write and default to "Ask on first use". Sensitive writes are asked "Every use" and
  can never be "Always allow". Changed details need fresh approval. Transaction states must be
  distinct, duplicates prevented, and checkout completed.
- [Help Center](https://www.meta.com/help/artificial-intelligence/1385290430137537/) lists five
  grant choices.
- The [security post](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse)
  describes:
  - surrogate credentials swapped in at the network boundary;
  - a browser sub-agent that pauses for takeover;
  - single-use wallet cards;
  - untrusted-content labeling.

Conversely, no source shows Muse rendering third-party MCP Apps widgets, which is the lane
Consumer Agent Testing Tool headlines.

**Top five actions**

1. **Fix result integrity** ([P0](#p0-result-integrity-and-redaction)).
   - Label the step menu honestly.
   - Keep agent mistakes and limit exhaustion in success denominators.
   - Make a failed assertion exit 1.
   - Make unimplemented scorers fail validation instead of silently blocking.
   - Let developer-API decision contracts pass.
2. **Fix redaction.** `TOKENIZERS_PARALLELISM=false` blocks every agent request. An empty
   pagination token destroys the sealed report. Typing "2" corrupts the browser snapshot the
   model sees.
3. **Build a Muse connector surrogate lane** ([P1](#p1-muse-connector-surrogate)):
   - the real catalog from the developer's server;
   - a supported CLI/skill projection;
   - Muse's permission classes and grant scopes, with approval cards;
   - OAuth or API-key linking with surrogate credentials;
   - a hosted Muse-family model.
4. **Add the behavioral half of a Muse guidelines check.** MCPJam's Muse grader is passive by
   design: behavior "observable only by exercising the tools against a test account, which a
   passive readiness run must not do". Consumer Agent Testing Tool's oracle-backed trials are exactly that half.
5. **Re-scope the embedded MCP Apps lane** as generic-host evidence for ChatGPT, Claude and
   others. Fix the defects that make conformant servers report "UI capability unavailable".

Findings are split into four tracks, so that generic-host defects don't crowd out the Muse
work:

- [A, result integrity](#track-a-result-integrity);
- [B, evidence and redaction](#track-b-evidence-and-redaction);
- [C, Muse fidelity](#track-c-muse-fidelity);
- [D, generic MCP Apps host and browser robustness](#track-d-generic-mcp-apps-host-and-browser-robustness).

## 1. How this review was done

- **Code review.** Six reviewers read the subsystems in full:
  - the agent loop and providers;
  - the MCP / MCP Apps host;
  - the browser;
  - consent, auth and developer APIs;
  - evidence, replay and release;
  - the product surface, traced against `docs/spec-v0.3.md`.
- **Research.** Four researchers searched the web (Bing, WebFetch, curl) and GitHub (`gh`).
  Their topics were the Muse product, the developer platform and models, protocol standards,
  and comparable tools.
- **Adversarial verification.** Eleven verifiers re-checked 60 code claims and 29 external
  claims; the results are in [Appendix B](#appendix-b-verification-log).
  - Code verifiers reproduced most claims with in-process scripts under `/tmp`; no repo files
    were changed.
  - Source verifiers fetched primary pages themselves.
  - One code claim was refuted; 15 code claims and 7 source claims were narrowed.
- **Completeness critic.** A final agent searched again for missing pieces, which added the
  route-selection, custom-connector, wallet, background-work, memory, content-trust and
  presentation items. It also corrected prioritization.
- **Tests.** The default collection was run twice ([Appendix C](#appendix-c-test-runs)). Plain
  `pytest` fails 119 tests (environment artifacts); under the release runner, all 1,031 pass.
- **Not done.** No live model inference, Docker lanes, Coverage Cat calls or cloud workers.
  Statements about live behavior rest on retained evidence under `runs/` and
  `docs/release-evidence/`.

## 2. What Muse-alike emulation requires as of 15 October 2026

Muse launched on 8 September. It runs on iOS, Android, the web and WhatsApp, has had a Mac app
since 17 September and an iPad app since 7 October, and serves adults in the US and Canada. It
launched on Muse Spark 1.3; Muse Wonder "will soon power experiences in Muse".

**Source labels**

- **Official:** Meta pages, quoted verbatim.
- *Unofficial:* unlicensed GitHub mirrors of files from inside Muse VMs, such as
  `sneakers-the-rat/muse-skills` and `rallat/muse-skills`. They change quickly: the MCP CLI
  template first appeared on 15 October. The Connector Terms prohibit reverse engineering, and
  `muse.ai/robots.txt` forbids automated collection. Any recommendation that rests on them must
  be an optional, labeled, SHA-pinned profile, never a default, and their content must never be
  shipped.
- *Press:* news coverage, cited for context only.

### 2.1 Capability matrix

Status legend: **Missing**, **Dev-only** (behind `--development-extensions`), **Partial**,
**Present**. The Proposal column refers to items in [section 3](#3-roadmap).

| # | Muse capability and evidence | Consumer Agent Testing Tool today | Status | Proposal |
|---|---|---|---|---|
| 1 | **Tool exposure and model-written arguments.** **Official:** built-in connectors are CLIs plus SKILLs; the model was trained for "zero-shot tool calling using CLIs and skills". *Unofficial:* MCP servers are reached through a CLI (`list-tools`, `describe-tool`, `call-tool`), and "Adding a server creates no new model tool". **Official:** the Meta Model API offers deferred tool loading through `tool_search`. | MCP: a step menu only (I1). The model writes arguments only against the built-in fixture or a hand-written HTTP mapping. The CLI/skill lane is dev-only (`cli.py:65-66`). | **Missing** for MCP; **Dev-only** for CLI/skills | P1.1, P1.2 |
| 2 | **Connector discovery and route choice.** **Official:** "Approved connectors are discoverable by the Muse agent and in Settings > Connectors" (§7.1); users can connect "by asking Muse". *Press:* "When no API is available, Muse can access the service via the browser instead." *Unofficial:* there is a connector search tool, and probes cover trigger-positive, trigger-negative, routing and duplicate protection. | No lane offers competing routes. Exposure treatments exist only in the dev-only lane. `mixed-surrogate-v1` (spec §7) is unimplemented. | **Missing** | P2.1 |
| 3 | **Tool classes and approvals.** **Official:** <ul><li>Read / Write / Sensitive write; "A tool that combines reads and writes must be classified as a write" (§3.2);</li><li>"default to Ask on first use"; sensitive writes are asked "Every use" and "cannot be set to Always allow" (§3.3);</li><li>Help Center: Allow once, Allow for this task, Allow for this site, Always allow, Deny, plus the modes "Ask for some actions" and "Always ask";</li><li>Sentinel offers one-time, session, task, time-bounded or perpetual grants;</li><li>"A dialog is presented to the user directly within the client UI — not via their conversation".</li></ul> | `approve_once` / `deny` only (`consent.py:116-118`). Grants last a fixed 300 fixture seconds. Reads and drafts are never prompted, and there is no sensitive tier. MCP-lane consent is optional and untyped. No approval card artifact. | **Missing** | P1.3 |
| 4 | **Re-approval and disclosure.** **Official:** "If any details (e.g. the price, recipient, scope) change after a write is approved, require a new fresh write" (§3.4). "Before commitment … the seller, full price and currency, recurring charges, key terms, and cancellation or refund policy" (§4.7). | Stale-total re-approval exists for the reservation fixture only (F4). Disclosure is never checked. | **Partial** | P1.3, P1.7 |
| 5 | **Credentials and linking.** **Official:** "code in the runtime cell or a worker only ever sees a “surrogate” token, minted by authd", and "Sentinel will replace any surrogate tokens with the real credential … at the network boundary". Submissions provide "OAuth client credentials or an API key" (§5.4). Read-only OAuth is suggested where possible (§3.1). | Targets support only a static Bearer token from an environment variable. The MCP consumer lane has no auth at all. There is no OAuth, and no API key in a custom header, query or path. | **Missing** | P1.4 |
| 6 | **Custom connectors.** **Official:** "Muse can also write its own custom connectors for other services you care about if they have their own APIs or CLIs"; "Meta doesn't review custom connectors". *Unofficial:* credentials come through a `request_api_access` flow; API keys and the OAuth code flow are supported, while cookies, passwords and signed requests are declined. | A curated mapping, a private control adapter and a loopback target are required (F4). Generated OpenAPI is dev-only. Nothing tests whether an agent can build a client from the docs. | **Missing** | P2.2 |
| 7 | **Transaction rules.** **Official:** complete tasks "including … checkout where needed"; support guest checkout; "Preserve progress so Muse can resume" (§1.1). "Distinguish quoted, pending, confirmed, failed, and cancelled actions … check their status before retrying. Prevent duplicate transactions" (§4.7). | Strong for the reservation fixture: idempotency, reconciliation, duplicate detection. Not portable to developer targets (F4). | **Partial** | P1.7 |
| 8 | **Honest final answers.** **Official:** "Report this only after the provider confirms both the reservation and payment"; "Do not fabricate … successful outcomes" (§4.3). *Press:* CNET reported a "$5 discrepancy" between Muse's quoted and charged totals. | `answer.no_false_booking_claim` is registered but never computed (I5). | **Missing** | P1.6 |
| 9 | **Egress control.** **Official:** "Sentinel is the sole permission authority for connector actions and network egress", with L4/L7 checks, SSRF protection and tainted-process egress; there is an Allowed-websites list. | Exact browser request rules, redirect guards and destination allowlists. OS isolation is dev-only. No taint model. Native-mode leaks exist (B7). | **Partial** | P2.3 |
| 10 | **Browser sub-agent.** **Official:** accessibility tree only, no JavaScript, "paused during user takeover or credential fill"; "Muse uses a special browser sub-agent to complete each task"; checkout detection; "Take control of the browser" and "Stop the task". *Unofficial:* `browser.spawn_task` / `steer_task` / `peek_task`; "The browser task cannot see this conversation." | The facade is solid. The agent runs standalone. Takeover is a flag with no handback (F7). Scenarios run only against the built-in portal. Robustness defects in track D. | **Partial** | P2.3, P0.8 |
| 11 | **Payments.** **Official:** "a single-use card number is issued … We present a human in the loop approval for every one of these events"; Link's wallet; Shop Pay and PayPal announced; 1Password "coming soon". | No wallet or payment fixture. Coverage Cat mocks a SetupIntent through a tool callback. | **Missing** | P2.4 |
| 12 | **Background and scheduled work.** **Official:** "Muse continues to work on a schedule and in response to relevant events"; reminders and scheduled tasks under "Upcoming"; calendar connectors "can update Muse automatically". No third-party push is documented. | A fixture clock and events for the built-in fixture. No agent-side scheduler (spec §12 says so explicitly). | **Missing** | P2.5 |
| 13 | **Memory.** **Official:** editable `Memory.md`, `Soul.md` and `Identity.md`, plus memory import. Guidelines §4.2: "Use the minimum data required for each tool". | A one-line "saved user profile". `stale_memory` is a dev-only goal sentence. | **Missing** | P2.6 |
| 14 | **Content trust.** **Official:** "When data enters the model's context from any external source, it's labeled as untrusted input"; injection classifiers run outside the cell; the email connector filters "one-time tokens, password reset links, and login magic links". Guidelines §3.2 ban manipulative instructions in tool descriptions *and responses*. | Prompt wording only. Hostile scenarios warn the agent in advance (F6). Email and code masking happens only as a redaction side effect (E5). | **Partial** | P2.7, P1.7 |
| 15 | **Presentation and channels.** **Official:** "in the Muse app or directly in WhatsApp"; Muse renders its own cards, widgets and artifacts. | Final answers are never evaluated. No text-only or voice profile. | **Missing** | P2.8 |
| 16 | **Muse MCP client behavior.** *Unofficial template:* Streamable HTTP POST only; versions 2025-11-25, 2025-06-18 or 2025-03-26; capabilities `{}`; one session per invocation; no redirects; port 443; 45 s timeout; no retries; allowlisted tools; header auth. | The Node host uses the legacy handshake, an exact endpoint pin and no redirects, but keeps a persistent session and declares the UI extension. The contract lane rejects 2025-06-18 and 2025-03-26 servers (F10). | **Partial** | P1.5 |
| 17 | **Model layer.** **Official:** Spark Open is "derived from the Muse Spark model we released in August", gated, under the Muse Model License. The Meta Model API requires `tool_choice: "auto"` and at most one dot in function names. Its `tool_search` is not available on Chat Completions. Reasoning carries across turns only on Responses. Contributor mode trades price for permission to "use your content to train future Meta models". | Spark Open goes through OpenRouter's *generic* adapter, so reasoning is replayed as text (F8). The `meta` provider is offered by `init` but cannot run (F9). | **Partial** | P1.8 |
| 18 | **Admission readiness.** **Official:** documentation topics (§5.4); "a dedicated test account" and "how to test checkout without real charges" (§5.5); three review stages (§6); no new money-movement tools (§4.6). | Nothing. MCPJam added a passive Muse grader on 7 October. | **Missing** | P1.7 |
| 19 | **Personal Agent Protocol ("Poppy").** **Official:** Meta and Sierra announced it on 6 October. Sierra published draft 0.1 on 9 October and reported that, at its demo, Muse "found Rocket's agent through Poppy". The draft specifies `poppy.json`, CIMD, DPoP and MCP 2025-06-18 or later. | Nothing. | **Emerging** | P3.2 |
| 20 | **Real-Muse observation.** Must be authorized and manual, given `robots.txt` and the Terms. | Calibration is dev-only, with an importer and a partner packet. | Keep manual | — |

### 2.2 Not a Muse requirement: embedded MCP Apps

Three facts point the same way:

- The guidelines mention MCP once ("In your API or MCP documentation …") and never mention UI,
  widgets or templates.
- None of the 14 clients in the MCP extension support matrix is from Meta.
- MCPJam's grader states "Muse adds no MCP extension: no `_meta` keys, no UI templates, no
  manifest". That is MCPJam's reading of the guidelines, not a statement from Meta. A search of
  an hourly mirror found no `ui://`, `resourceUri` or `outputTemplate`, but seven uses of Muse's
  own `widget.create_list_widget`.

Keep the lane, label it **generic MCP Apps host evidence**, and fix the defects that produce
false "blocked" results or false passes (P0.7). The remaining conformance work (P3.5) matters
for ChatGPT- or Claude-style hosts, not for Muse. For Muse, test that results carry the fields
Muse's own widgets need (P2.8).

### 2.3 Out of scope, deliberately

The following are not omissions:

- Mac computer use acts on local apps, not third-party integrations.
- Muse Code is a different product.
- Meta AI glasses' WebMCP host is a Meta product but not Muse.
- A2A, AP2 and Visa TAP have no documented Muse use.

The official gadget SDK is a real Muse surface, but its token terms say it is "not a
developer platform"; keep it optional (P3.3).

## 3. Roadmap

Effort: **S** is days, **M** is one to three weeks, **L** is longer.

### P0: result integrity and redaction

| Item | Effort | Change | Acceptance |
|---|---|---|---|
| P0.1 | S | Label `surface-run --driver agent` a "scripted action menu" in `result.json`, the report and README, with the observation gap "service tool surface not exposed". Remove `checks` and `capabilities` from model observations (`surface.py:517-519`). | No "surrogate behavior" or `layers.agent.exercised` for step menus. |
| P0.2 | S | Count parallel calls, schema-invalid arguments, unknown tools and limit exhaustion as failures. Exclude only harness failures before the first model call. Report successes per attempt. Stop auto-adding `isolation.no_forbidden_traffic` in lanes that never compute it. | Agent mistakes stay in the `task_success` denominator. Shipped `stale_total` produces valid trials. |
| P0.3 | S | Failed assertion exits 1, setup error exits 2, blocked exits 3 (spec §18). Update `tests/test_surface.py:154-163`. Print the failure stage and reason on stdout. | CI can tell a broken service from a paused run. |
| P0.4 | S | Reject unimplemented scorer IDs at validation until P1.6 lands. Remove `run_id` from the developer-API run lock. | The scorer registry has no unimplemented entries. A developer-API contract can return "passed". |
| P0.5 | S | Redaction fixes: <ul><li>ignore environment values under 8 characters, booleans and numbers;</li><li>route every secret through `Redactor.add`;</li><li>register captures before logging `service_response`;</li><li>carry run secrets into export;</li><li>add structured PII keys;</li><li>redact evidence, never the model's own observation.</li></ul> | Repro scripts for E1-E5 pass. |
| P0.6 | S | Make plain `pytest` hermetic: set a workspace-local `basetemp` in `tests/conftest.py`. | `pytest` gives 1,031 passed. |
| P0.7 | M | MCP Apps: decide UI availability from the client capability and the tool's `_meta.ui.resourceUri`; send the real tool input. Fix `scripts/surface_fixture.mjs:90-91` and `tests/test_surface_browser.py:36-39`. State in README which parts of the host contract are implemented. | The official ext-apps example servers render. |
| P0.8 | M | Browser: raise the pipe limit and bound snapshots; return ordinary errors to the model; surface dialogs and file choosers as actionable modal states; announce popups. | A 1,200-link page, `confirm()` and a file input all work. |
| P0.9 | S | Wire Inspect 0.3.273's native `meta` provider, or remove it from `init`. Refuse `-contributor` model IDs without explicit opt-in. | No configuration that `init` writes fails at `resolve_model`. |

### P1: Muse connector surrogate

One new interface profile, `muse-connector-surrogate-2026-10`, assembled mostly from existing
parts.

| Item | Effort | Change | Acceptance |
|---|---|---|---|
| P1.1 | M | Import the real catalog: MCP `tools/list` with descriptions, schemas and annotations, or OpenAPI. Record `serverInfo`, the negotiated version, a catalog digest and UI resource hashes in the run lock. | `diff` can tell two server builds apart. |
| P1.2 | L | Offer two labeled projections. (a) Native function tools, a generic baseline. (b) A Muse CLI projection: `<connector> list-tools [--names-only] / describe-tool / call-tool`, raw JSON, non-zero exit on `isError`, an allowlist of reviewed tools, and deferred loading through a host search tool. Bring the CLI/skill lane into the supported scope with a generic oracle (the `api_control` protocol), without a Docker requirement for the default profile. | Shipped examples offer more than one valid next action, and the agent composes arguments from the server's own schemas. |
| P1.3 | M | Permission engine with approval cards as evidence. <ul><li>Classify tools as Read, Write or Sensitive write from annotations, docs and developer declarations.</li><li>Ask on first use; the five grant scopes; every-use prompts for sensitive writes; re-approval on changed details.</li><li>Approve with edits, no response or timeout, and the global modes.</li><li>Cards are shown outside the model's context and their disclosure is checked (§4.7).</li></ul> | Prompt counts per class match the §3.3 table. Each executed call matches its approved card. |
| P1.4 | L | Linking and surrogate credentials. <ul><li>OAuth 2.1 with PKCE, PRM and CIMD or DCR, against a local IdP fixture or the developer's staging IdP.</li><li>API keys in a header, query or path.</li><li>A `request_api_access`-style flow.</li><li>The host transport swaps surrogate tokens for real ones.</li><li>Expired, revoked and wrong-account states on developer targets.</li></ul> | No credential in any model request. Auth scenarios score against developer targets. |
| P1.5 | S | A Muse MCP client compatibility profile (unofficial, SHA-pinned): <ul><li>the three legacy versions;</li><li>POST only, one session per invocation;</li><li>no redirects, port 443, 45 s timeout, no blind retry;</li><li>per-connector quotas;</li><li>output-schema strictness as a recorded treatment.</li></ul> The contract lane accepts 2025-06-18 and 2025-03-26 servers. | Servers that need streams, persistent sessions or redirects are flagged. |
| P1.6 | M | Answer scorers. <ul><li>Deterministically extract the claimed status (quoted, pending, confirmed, failed or cancelled), IDs and totals, and compare them with the oracle.</li><li>A per-scenario relay-completeness rubric covering seller, limits, fees, recurring charges and cancellation terms.</li><li>Any judge output is labeled "judged" and its prompt recorded.</li></ul> | A false "confirmed" fails the trial. |
| P1.7 | M | `check --suite muse-guidelines`. <ul><li>Static: a Read/Write/Sensitive sheet (import MCPJam's format); hidden writes; steering in descriptions and live responses; money-movement tools; §5.4 doc topics; test account; no-charge checkout; credentials in responses.</li><li>Behavioral, against the developer's test account: a Cedar Stays-style flow, fees added at checkout, re-approval on a changed price, a status check before retry with no duplicates, guest checkout, cancel and refund.</li><li>Pin the policy by hash and fetch date, and check weekly for drift.</li></ul> | The report says "documented constraint, not admission". |
| P1.8 | M | Muse-family models. <ul><li>Spark 1.3 and Wonder on the Meta Model API through Responses or Messages, so `tool_search` works and reasoning carries over.</li><li>Record that Meta injects steering context.</li><li>Move OpenRouter to Inspect's `openrouter` provider for structured reasoning replay.</li><li>Keep Spark Open labeled as derived from the August release.</li></ul> | No `<think>` text in replayed assistant content. |
| P1.9 | S | Compute the metrics for surface runs (exposure, selection given exposure, argument validity), and add pass^k and per-scenario summaries. | Metrics present in every agent run. |

### P2: orchestration realism

| Item | Effort | Change |
|---|---|---|
| P2.1 | M | Route selection and fallback. Scenarios declare candidate routes (the developer's connector, a competing connector, a website, a phone stub), each in-prompt or search-only. Families: trigger-positive, trigger-negative, disconnected connector (offer to connect, then fall back), and duplicate protection across routes. This implements `mixed-surrogate-v1`. |
| P2.2 | L | A custom-connector lane. The input is the developer's public docs (OpenAPI, `llms.txt`, an MCP URL). The agent gets a sandboxed shell and HTTP through an egress proxy, requests credentials without ever seeing them, then verifies with a harmless read. Score docs sufficiency, first-call success and auth-scheme compatibility. |
| P2.3 | L | A browser sub-agent. <ul><li>A task brief with no conversation and no secrets; a final review handed back before any commit.</li><li>A checkout-detection approval.</li><li>Host credential fill while the agent is paused.</li><li>Handback through the CLI, and cookies kept across resume.</li><li>Anti-automation fixtures (CAPTCHA, WAF, account lock) where the expected behavior is to stop or request takeover, never bypass.</li></ul> |
| P2.4 | M | A wallet and checkout. <ul><li>A single-use card bound to merchant, amount (with tolerance), currency and expiry, approved on every purchase and filled by the host.</li><li>3DS and AVS fixtures.</li><li>Assert: no reusable card stored, no preselected trial or auto-renew, a receipt issued.</li><li>Stripe test mode or `stripe/link-cli`.</li></ul> |
| P2.5 | M | A scheduler on the fake clock. <ul><li>Jittered recurring jobs.</li><li>Background approvals that stay pending until answered late or never.</li><li>Polling efficiency (ETag, since-cursors), 429 with Retry-After, quotes or holds that expire.</li><li>Push from third parties labeled unknown.</li></ul> |
| P2.6 | S | Memory fixtures. Seed `Memory.md`, `Soul.md` and `Identity.md` with deliberately stale facts and score re-reading without coaching. Report data minimization (§4.2). Replace the fixed persona enum with scenario-defined profiles. |
| P2.7 | M | A content-trust stage. Tag provenance, optionally run a classifier, and filter one-time codes, reset links and magic links into opaque references. Report false positives. Add an unprompted, AgentDojo-style injection suite across the API, MCP and browser lanes. |
| P2.8 | S | A presentation profile. Hide UI resources; render results as text or JSON; check the fields list widgets need (title, price with currency, status, IDs, links); add WhatsApp-like text-only and voice limits. |
| P2.9 | M | An optional, labeled natural-language user simulator. Record its model, prompt and seed (spec §14), and match scripted replies semantically. |

### P3: optional or emerging

- **P3.1:** dual-era MCP 2026-07-28 support (generic) in the host and the contract lane, with
  conformance `0.2.0-alpha`. Muse's unofficial client negotiates only legacy versions, so this is
  not a Muse blocker.
- **P3.2:** a Poppy draft 0.1 lane that validates `poppy.json`, CIMD client metadata, DPoP
  sessions, browser-session join and the operations extension. Label it "no production Muse
  evidence".
- **P3.3:** a device lane on the gadget SDK's FakeVm.
- **P3.4:** a WebMCP lane, labeled Meta AI glasses rather than Muse.
- **P3.5:** finish the generic MCP Apps host (M-track items).

## 4. Findings

Severity rubric: false pass > misattribution > crash or blocked > fidelity gap > developer
experience. "Locked by" names a test that asserts the current wrong behavior, so the fix and
the test change land together. IDs are prefixed by track: I (A), E (B), F (C), M and B (D).

### Track A: result integrity

| ID | Sev. | Finding | Evidence |
|---|---|---|---|
| I1 | Critical | The agent driver is a one-choice step menu, but is reported as an exercised agent pass. Repro: an exhaustive search of all three shipped examples finds at most one ready step. The retained run's tool enums were `["prefill"]`, then `["followup"]`. | `surface_agent.py:28-33,119-121`; `surface.py:503-521`; `host.mjs:56-59`. Locked by `tests/test_surface_agent.py:54-55`. |
| I2 | High | Parallel calls, schema-invalid arguments and hallucinated tools become `setup/infrastructure`; turn, tool and deadline limits become "blocked". Both leave the denominator. A required observation gap outranks a failed backend check. Repro: 30 passes plus 30 such trials scored 30/30, "passed threshold". | `errors.py:4`; `api_scenario.py:375-385`; `scenario_session.py:177-190`; `metrics.py:173-180`; `decision.py:84-85` |
| I3 | High | Shipped `stale_total` API and browser scenarios can never yield a valid trial. They auto-add `isolation.no_forbidden_traffic`, which only the CLI lane computes. | `api_scenario.py:151-157`; `mapped_scenario.py:163-169` |
| I4 | High | A failed assertion gives "blocked" and exit 3, while setup errors exit 1. This inverts spec §18. | `surface.py:440-442,564-581`. Locked by `tests/test_surface.py:154-163`. |
| I5 | High | `answer.no_false_booking_claim` is accepted, never computed, and always an observation gap, so any scenario that uses it can never pass. | `scenario.py:18`. Locked by `tests/test_mapped_scenario.py:471-481`. |
| I6 | High | Developer-API decision contracts can never pass, because the per-trial `run_id` sits in the run lock. Repro. | `api_control.py:59,101`; `decision.py:63-65` |
| I7 | High | The UI resource URI comes from the scenario or the tool result, never the tool definition, so a hard-coded URI hides a broken descriptor link (false pass). | `host.mjs:77` |
| I8 | Medium | Run locks omit `serverInfo` and catalog or UI hashes, so different server builds compare as "same-configuration". | `surface.py:228-234`; `replay.py:222` |
| I9 | Medium | `diff`'s exit code ignores outcome regressions. Its output is dominated by generated IDs (about 120 KB for two passing runs). | `replay.py:186-198`; `extensions.py:27` |
| I10 | Medium | Surface and browse agent runs compute no metrics (`scenario_id` is unset), while README advertises "Record tokens and timing". | `surface_agent.py:47-56`; `evidence.py:242-244`; `README.md:20` |
| I11 | Medium | A fractional number anywhere in a consent-bound schema fails the trial mid-run as infrastructure. | `consent.py:31-32,96-98` |
| I12 | Medium | `application/problem+json` responses are rejected and blamed on the developer's API, so the agent never sees a 409 or 422 body. | `api_transport.py:37-38` |
| I13 | Medium | The `transient_error` 429 is treated as terminal infrastructure, so recovery is never exercised. | `api_scenario.py:437-438` |
| I14 | Medium | No native tool-call preflight in `surface-run` or `browse` agent mode, so a provider failure appears after a live write and is blamed on the agent. | `surface_host.py:140-149`; `browser.py:284-300` |

### Track B: evidence and redaction

| ID | Sev. | Finding | Evidence |
|---|---|---|---|
| E1 | High | Environment values of 4 or more characters under secret-like names become secrets. Repro: with `TOKENIZERS_PARALLELISM=false`, the credential guard refuses the first model request, because schemas contain `"additionalProperties": false`. The run is misreported as policy/approval. | `evidence.py:52-54`; `providers.py:194-203` |
| E2 | High | `learn_secrets` and other call sites append directly to `Redactor.secrets`. Repro: an empty or one-character `next_page_token` corrupts `run_id`, sealing fails, and a passing run ends unsealed with no report. | `surface.py:316-325,570-578`; `surface_agent.py:98`; `api_scenario.py:113,236` |
| E3 | Medium | Values captured from keys such as `session` or `sid` are logged before registration and survive export. Repro. | `surface.py:373-380`; `replay.py:120` |
| E4 | Medium | Exports keep `full_name`, `phone_number`, `birthday`, `license_number` and address fields. Today's examples are synthetic. | `evidence.py:21-30,89` |
| E5 | Critical | Typed text is redacted from the model's *own* browser snapshot. Repro: typing "2" turns `[ref=e12]` and `$125.00` into `[REDACTED]` fragments. Every email is shown as `[EMAIL]` (a declared, Muse-like OTP filter would be the right tool; see P2.7). | `browser.py:161-166`; `evidence.py:79-89` |
| E6 | High | Two evidence formats: playback and diff accept only sealed runs; metrics exist only for core runs. | `replay.py:72-74`; `evidence.py:241-244` |
| E7 | Medium | Sealed `report.html` is JSON inside `<pre>`. It shows no trajectory, no model-visible input and no sub-run link. | `sealed_report.py:33-41` |
| E8 | Medium | `checkpoint.private.json` (raw results and live tokens) is kept indefinitely, even after completion. | `surface.py:265-266` |
| E9 | Low | Each evidence directory copies the 3.3 MB `notices.zip`: 580 copies, 2.32 GB under `runs/`. | `evidence.py:170-185` |
| E10 | Low | Nesting of about 1,000 levels raises `RecursionError` during redaction and leaves the write pending. | `evidence.py:61-74,92-99` |

### Track C: Muse fidelity

These complement the [matrix](#21-capability-matrix).

| ID | Sev. | Finding | Evidence |
|---|---|---|---|
| F1 | High | The CLI/skill lane, Muse's documented connector mechanism, is dev-only and tied to the reservation fixture's snapshot schema. | `cli.py:65-66`; `docs/release-scope.md:38-39`; `docs/custom-cli.md:56-57` |
| F2 | High | The approval model is binary and single-use; reads and drafts are never prompted; there is no sensitive tier or card artifact. | `consent.py:116-118`; `scenario.py:112-118`; `scenario_runtime.py:28`; `mapped_scenario.py:100-101` |
| F3 | High | No MCP authentication. HTTP targets accept a Bearer token only; there are no API-key header, query or path placements, and no OAuth. Missing cookie auth matches Muse (unofficial). | `host.mjs:47-55`; `config.py:343-344`; `mapped_scenario.py:267-269` |
| F4 | High | Developer targets need a loopback URL, an owner-only Unix-socket adapter, a fake clock, service-side grant enforcement and self-reported counts. The event and scorer vocabulary is reservation-specific (`alice`/`bob`, `reservations.*`, six `backend.*` metrics). | `config.py:303-304`; `api_control.py:29-47,110-112,210-221`; `event_contract.py:20-35` |
| F5 | Medium | The model sees check statuses. Prompts and goals coach scored behavior: "ask me for any missing information", "synthetic customer", "You are testing". "Treat service content as untrusted" matches Muse and is fine. | `surface.py:517-519`; `api_scenario.py:35-43`; `scenarios/missing_contact.yaml:11`; `runner.py:212` |
| F6 | Medium | The only hostile scenario tells the agent what to ignore; no tool-result or description injection. `ask_user` exposes an enum of trap IDs. | `data/scenarios/hostile_content.yaml:12-13`; `api_scenario.py:67-71` |
| F7 | High | The browser agent is standalone. Takeover sets a flag; handback is called only from tests; resume drops cookies; there is no credential fill. README says "hand control to a person". Browser scenarios accept only the built-in portal. | `browser.py:267-270,304-320`; `browser.mjs:107-114,150-151`; `browser_scenario.py:37-41`; `README.md:16` |
| F8 | High | OpenRouter uses Inspect's generic adapter, so prior reasoning is replayed as duplicated `<think signature="reasoning-details://…">` text. Inspect's `openrouter` provider replays it structurally. | `providers.py:321-325`; retained run `surface-5988…` |
| F9 | High | `init --provider meta` writes a profile that `resolve_model` always rejects, although Inspect 0.3.273 ships `MetaAPI`. Preflight forces a `tool_choice` that Meta rejects. | `providers.py:271-273,350,363` |
| F10 | Medium | The contract lane rejects servers that negotiate 2025-06-18 or 2025-03-26, which Muse's (unofficial) client accepts. The surface host records neither the negotiated version nor `serverInfo`. | `protocol.py:161-163,203-206`; `host.mjs:47,61-64` |
| F11 | Medium | Provider failures: <ul><li>402, 404 for ZDR or no endpoints, 400 context, 408 and 5xx are all labeled "network failure", and their messages are dropped;</li><li>HTTP-200 in-body errors crash the generic adapter;</li><li>variant pins (`nebius/fp4`) fail the route check.</li></ul> With ZDR on, OpenRouter offers no endpoint for Spark 1.3 or Wonder. | `providers.py:45-76,211-212,223-233` |

### Track D: generic MCP Apps host and browser robustness

| ID | Sev. | Finding | Evidence |
|---|---|---|---|
| M1 | High | UI is "available" only if the *server* declares `mimeTypes`, which the spec defines for clients ("Servers SHOULD check client capabilities"). Conformant servers report blocked, and the fixture mirrors the error. | `host.mjs:60-62,71-76`; `scripts/surface_fixture.mjs:90-91`. Locked by `tests/test_surface_browser.py:36-39`. |
| M2 | High | `tool-input` is always `{arguments:{}}`, breaking a MUST; `hostContext` is empty. | `host.mjs:137-141` |
| M3 | High | `_meta.ui.csp`, permissions and domain are ignored. No CSP, no sandbox proxy, and one iframe with `allow-same-origin`. | `host.mjs:152-168` |
| M4 | High | `ui/message`, `update-model-context`, `open-link` and `resources/read` return -32601; there is no teardown and `size-changed` is dropped. Repro with the official App SDK. | `host.mjs:138-143` |
| M5 | High | View calls are deduplicated per tool per UI step (polling reads freeze). `isError` becomes a JSON-RPC failure. `Paused` is swallowed by `handle_callback`, so consent-gated view writes become unresumable. | `surface.py:393-425`; `surface_host.py:89-95` |
| M6 | Medium | Native renderer: widget writes get no consent; redirects bypass `browser_origins`; Chromium Local Network Access silently drops allowlisted loopback requests. | `host.mjs:163-170` |
| M7 | Medium | Legacy handshake only, so 2026-07-28 servers can't be tested, although `config.py:538` registers that profile. No `_meta.ui.visibility` enforcement. `blob` resources are rejected. The `window.openai` shim has three members. | `host.mjs:47,80,106-114`; `protocol.py:203-206` |
| B1 | High | The host pipe keeps asyncio's 64 KiB line limit. Repro: a page with 1,200 links kills the session; MCP results over 64 KiB do too. | `surface_host.py:48-53,63` |
| B2 | High | Any text change invalidates every reference. Repro: a 250 ms ticker livelocks the agent. | `browser.mjs:163-166` |
| B3 | High | Every error other than a stale reference ends the run (navigation denied, MCP action failure, unsupported key). | `browser_agent.py:185-187` |
| B4 | High | JavaScript dialogs are dismissed, and the resulting MCP modal state fails the post-click snapshot (5 of 5 trials). A file input wedges the session permanently. | `browser.mjs:91`; `browser_mcp.mjs` |
| B5 | High | Popups fail on first load and present as "This site can't be reached"; tabs expose IDs only. | `browser.mjs:61-70,94,129`. Locked by `tests/test_browser_portals.py:268-292`. |
| B6 | High | No limit on snapshot size or history: `max_output=0`, `compaction=None`, `truncation="disabled"`. | `browser_agent.py:238-264` |
| B7 | Medium | Native egress leaks: worker WebSockets and WebRTC STUN reach undeclared hosts, while reports claim egress is limited to declared rules. | `browser.mjs:42-51`; `browser.py:249` |
| B8 | Low | Password values are serialized into snapshots; latent until credential fill exists. | `browser_mcp.mjs:20-28` |
| B9 | Low | Deferred fixture jobs fail if they become ready after the fixed 300 s grant, or after a `refreshed` session event. | `reservation_jobs.py:82-88,179-180` |

## 5. Developer experience and maintainability

- **Onboarding.** Testing one's own MCP server means copying a step-graph YAML and hard-coding
  every argument. Testing an HTTP API needs a mapping plus a private stateful adapter (F4). A
  portal gets no outcome checks (F7). Add:
  - `consumer-agent-testing-tool scaffold <mcp-url|openapi>`;
  - record-to-scenario;
  - a "what you can test today" table at the top of the README.
- **Defaults.**
  - `defaults.interface_profile` is the dev-only `muse-cli-skills-public-2026-09`, so `run`
    fails without `--interface-profile` (`config.py:413`).
  - `init` stages scenarios the supported CLI cannot run.
  - `doctor` checks Docker rather than Node and Chromium (`cli.py:218-225,477-501`).
  - The profile `support` label is never enforced (`config.py:92`).
- **README accuracy.** Node is not pinned (no `engines` field). The "included OpenRouter
  profile" is git-ignored and not shipped. Python 3.12 and 3.13 are unproven: every record is
  3.14.4.
- **Scope.**
  - 5,087 lines of dev-only modules (27% of 19,137) ship in the wheel: workers, calibration,
    qualification, generated OpenAPI. Move them to a separate package or exclude them.
  - `release-audit` is an end-user command that works only inside this checkout; move it to
    `scripts/` and use pytest markers instead of test-name lists.
- **Docs.** 66 files and 13,423 lines with no index (`verification.md` alone is 3,856 lines),
  plus stale test counts. Add `docs/README.md` that separates current from historical pages.
  Remove `docs/release-evidence` (855 files, 24 MB) from the sdist.
- **Dependencies.**
  - Two Playwright copies (1.63.0 and 1.64.0-alpha) with mismatched browser revisions.
  - Inspector 2.9.0, while 2.11.0 is current.
  - Conformance 0.1.16, which has no 2026-07-28 scenarios.
- **Reports.** Show, in this order:
  - the verdict and failing step;
  - a per-step timeline with model-visible content inline;
  - the published, exposed, observed and diagnostic views (spec §8);
  - metrics;
  - links to Inspect View and the Playwright trace.

## 6. Prior art

| Project | Use |
|---|---|
| [MCPJam Inspector](https://github.com/MCPJam/inspector) | Its Muse readiness grader (PR #6026, 7 October) is passive by design. It has four lanes, a suggested Read/Write/Sensitive sheet, a hash-pinned policy corpus and weekly drift checks. Import its sheet format, and position Consumer Agent Testing Tool as the behavioral half. |
| [modelcontextprotocol/conformance](https://github.com/modelcontextprotocol/conformance) `0.2.0-alpha` | Auth scenarios and the 2026-07-28 requirement set. |
| [ext-apps basic-host](https://github.com/modelcontextprotocol/ext-apps) | Sandbox proxy, CSP and handler wiring for P3.5. |
| [facebookincubator/muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk) | The only official Muse wire protocol; FakeVm tests. |
| [meta-model-cookbook](https://github.com/meta-models/meta-model-cookbook), [meta-oss-cookbook](https://github.com/meta-models/meta-oss-cookbook) | Meta Model API, Spark Open and Glimmer guidance. Note: its "the preview is free" line is stale; the API is pay-as-you-go. |
| [tau2-bench](https://github.com/sierra-research/tau2-bench), [AgentDojo](https://github.com/ethz-spylab/agentdojo), [snyk/agent-scan](https://github.com/snyk/agent-scan) | User simulation and pass^k; unprompted injection; tool-poisoning lint. |
| [BrowserGym](https://github.com/ServiceNow/BrowserGym), [WebArena](https://github.com/web-arena-x/webarena) | Reproducible browser tasks. |
| [stripe/link-cli](https://github.com/stripe/link-cli), [Shopify/ucp-cli](https://github.com/Shopify/ucp-cli) | Account-free payment and commerce fixtures. |
| [Personal Agent Protocol](https://github.com/personal-agent-protocol/personal-agent-protocol) | Draft 0.1 discovery and sessions. |
| [stevysmith/mcplane](https://github.com/stevysmith/mcplane) | Submission preflight. Reported rejection causes include an unreachable server, a reviewer who cannot sign in, and a missing privacy policy. |

Avoid `muse-spark-app/muse-spark` and `Miron47486/Meta-Muse-Spark-1.3-Free`; both look like
malware lures.

## Appendix A. Source register

All sources were fetched on 15 October 2026.

**Official.**
- [Connector guidelines](https://platform.muse.ai/docs) and
  [Connector Terms](https://platform.muse.ai/terms) (effective 18 September 2026).
  `muse.ai/platform` now redirects with a 307 to `platform.muse.ai`. MCPJam's text hash for the
  docs page, `60628e5a…`, matched on 15 October.
- [How We Built Safety Into Muse](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse)
  (8 September). Its footnote reads "Hatch is our internal name for Muse".
- Help Center articles:
  - [approvals](https://www.meta.com/help/artificial-intelligence/1385290430137537/)
  - [connectors](https://www.meta.com/help/artificial-intelligence/1687253048996149/)
  - [data](https://www.meta.com/help/artificial-intelligence/2225571704857152/)
  - [subscriptions](https://www.meta.com/help/subscriptions/1021145227643680/)
  - [Mac](https://www.meta.com/help/artificial-intelligence/1126304576638594/)
  - [browser](https://www.meta.com/help/artificial-intelligence/2124746764949121/)
- [Launch post](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)
  (8 September; modified 30 September) and
  [small business](https://about.fb.com/news/2026/09/introducing-muse-small-business/)
  (29 September).
- [Muse Spark Open](https://research.meta.ai/blog/muse-spark-open),
  [model page](https://dev.meta.ai/models/muse-spark-open) and
  [Muse Wonder](https://research.meta.ai/blog/muse-wonder).
- Meta Model API docs: [tool calling](https://dev.meta.ai/docs/tool-calling),
  [tool search](https://dev.meta.ai/docs/tool-search),
  [reasoning](https://dev.meta.ai/docs/reasoning) and
  [pricing](https://dev.meta.ai/docs/pricing-rate-limits).
- [Personal Agent Protocol](https://www.facebook.com/business/news/a-new-way-for-businesses-and-personal-agents-to-work-together)
  (6 October) and [Sierra's draft post](https://sierra.ai/blog/poppy) (9 October).
- The spec's `dev.meta.ai/docs/muse-glimmer/llama-cpp` link now redirects with a 308 to
  `/deploy`, so the evidence-registry entry `glimmer-llama` needs a refresh.

**Standards.**
- [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog).
- [MCP Apps 2026-01-26](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx).
- [Extension client matrix](https://modelcontextprotocol.io/extensions/client-matrix).
- [OpenRouter endpoints](https://openrouter.ai/api/v1/models/meta/muse-spark-open/endpoints).
  Spark Open has four endpoints, all ZDR-listed. Spark 1.3 and Wonder are Meta-only and not
  ZDR-listed.

**Unofficial** (unlicensed; cite, don't copy).
- [sneakers-the-rat/muse-skills](https://github.com/sneakers-the-rat/muse-skills) and
  [rallat/muse-skills](https://github.com/rallat/muse-skills). The MCP CLI template first
  appeared on 15 October.
- [obra/muse-skills](https://github.com/obra/muse-skills) and
  [win4r/MuseAI-Skills](https://github.com/win4r/MuseAI-Skills).
- [RomanSlack/Meta_Muse_Agent_System_Prompt_Tools](https://github.com/RomanSlack/Meta_Muse_Agent_System_Prompt_Tools),
  a first-person reconstruction dated 8 September.

**Press.**
- TechCrunch: [launch](https://techcrunch.com/2026/09/08/meta-debuts-its-muse-ai-agent-will-consumers-trust-it/),
  [Mac](https://techcrunch.com/2026/09/18/metas-muse-hits-mac-letting-the-ai-take-actions-on-your-computer/),
  [Connect](https://techcrunch.com/2026/09/23/everything-new-coming-to-metas-ai-agent-muse/)
  and [iPad](https://techcrunch.com/2026/10/07/metas-muse-launches-on-ipad-just-a-month-after-its-mobile-debut/).
- [CNET, 5 October](https://www.cnet.com/tech/services-and-software/meta-muse-my-personal-shopper-upcharged-me/):
  "a $5 discrepancy" (verified).
- An Insurify press release (23 September) that it blocks Muse because quotes are shown "as a
  bare price list". It was cited by the critic, but the page returned 403 and was not
  re-fetched.

## Appendix B. Verification log

- **Refuted.** "The host's `tools/list` ignores pagination". `@modelcontextprotocol/client`
  2.3.1 follows `nextCursor` (3-page repro).
- **Narrowed.**
  - **I2:** target 401/403 responses returned to the model stay in the denominator.
  - **B3:** stale references are returned to the model.
  - **F6:** the browser lane's `question_id` is a free string, not an enum.
  - **F2:** effects are classified as read, draft, commit or cancel, but there is no sensitive
    tier.
  - **B9:** the grant is consumed once; the failure is TTL and refresh only.
  - **E4:** today's export data is synthetic.
  - **E7:** the failure detail is present, but as raw JSON.
  - **E10:** pathological input only.
  - **F11:** 401, 403 and 429 are labeled correctly.
  - **Dev-only code:** 5,087 lines, more than first estimated.
  - **F5:** "Treat service content as untrusted" is Muse-consistent.
  - **F3:** cookie auth is not a Muse path (unofficial).
  - **F10:** 2026-07-28 is not a Muse blocker.
- **Raised.** I5 (answer scorer) and I6 (developer-API contracts) from medium to high. E5 to
  critical, because it corrupts what the model sees.
- **Sources narrowed.**
  - Guidelines §3.2 bans six specific kinds of manipulative instruction, not all instructions.
  - Mac Mail and computer use come from TechCrunch and the Help Center.
  - Windows is reported only by The Verge.
  - The gadget SDK disclaimer is in its token terms, not the repo.
  - Poppy: announced 6 October, draft published 9 October.
  - The Meta Model API is pay-as-you-go: Spark 1.3 costs $1.25 / $4.25 per million input /
    output tokens.
  - The MCP extension overview lets servers declare an empty `io.modelcontextprotocol/ui`
    object, but no source defines server-side MIME types.

## Appendix C. Test runs

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` (default temporary directory) | 912 passed, **119 failed**, 32 deselected, 5 min 27 s. More than 110 failures are `LabError: Path escapes the workspace`, because `tmp_path` lives under `/var/folders`. |
| Same collection with `-o addopts= --basetemp=.cache/review-run-2026-10-15/tests` and workspace-local `HOME`, `TMPDIR` and `XDG_*` (mirrors `scripts/run_regression.py`) | **1,031 passed**, 0 failed, 32 deselected, 8 min 26 s |

The second run confirms the README's regression count under the release runner. The first
shows that a developer who runs `pytest` directly gets 119 failures that are environment
artifacts, not product defects (P0.6).

## Implementation status (17 October 2026)

This section was added on 17 October 2026; the review above is unchanged. It
gives the status, in this workspace's code on that day, of every finding, every
roadmap item and the developer-experience items of section 5. It is built from
an item-by-item audit on 17 October, which compared the code with the
15 October baseline and re-ran many of the review's repros, then from the fix
and integration rounds that followed and a reading of the code. It was updated
the same evening, after a residual round of fixes and a relabeling of the
WebMCP lane. The updated rows are F2, F3, F5, F6, E7, M4, M6, P1.3, P1.5,
P1.8, P1.9, P2.3, P2.4, P2.6, P2.8, P2.9, P3.4, P3.5, scaffold,
record-to-scenario and reports.

- **fixed:** what the review asked for is in place.
- **partial:** part is in place; the row names what is missing.
- **open:** nothing has changed yet.
- **deferred:** postponed on purpose; the row gives the reason.

Of the 102 items, 71 are fixed, 24 partial, 6 open and 1 deferred. A fixed row
is not evidence of Muse equivalence. Behavior that models Muse is a Muse-like
surrogate with basis labels (`documented`, `unofficial`, `assumed` or
`unknown`), the MCP Apps lane is generic-host evidence, the WebMCP lane is
labeled "generic WebMCP page tools, not Muse" (Meta documents WebMCP for its
AI glasses, a Meta product that is not Muse), and the Poppy checks follow
draft 0.1.
The [README](../README.md#what-you-can-test-today) lists what each lane runs
today.

### Findings

| ID | Status | Evidence or reason |
|---|---|---|
| I1 | fixed | Step-menu runs are labeled lane `action-menu` and `scripted action menu`, with the gap "Service tool surface not exposed to the model"; `layers.agent.exercised` is false and they never set `compatibility_pass`. An agent that uses a server's real tools runs in `connector-run`. |
| I2 | fixed | Parallel calls, schema-invalid arguments and unknown tools return to the model as tool errors, exhausted limits fail the trial, and both stay in the `task_success` denominator; a failure outranks an observation gap, and `success_per_attempt` is reported. The default exclusion policy, `pre-model-harness-and-unscored`, still lists unscored trials outside the denominator. |
| I3 | fixed | `isolation.no_forbidden_traffic` is no longer added in lanes without a traffic oracle, and the shipped `stale_total` scenarios complete with an ideal agent. |
| I4 | fixed | A failed assertion exits 1, a setup error 2 and a blocked run 3, and `surface-run` prints the stage, class and reason. |
| I5 | fixed | `answer.no_false_booking_claim` is computed against the oracle in the fixture API, browser and CLI lanes; lanes that cannot compute it reject it at validation. |
| I6 | fixed | The per-trial `run_id` left the developer-API run lock, so identical trials share a lock and a decision contract can pass. |
| I7 | fixed | The UI link comes from the tool's `tools/list` definition, and the `mcp-apps-resource-link` check fails a run whose rendered URI differs from it. |
| I8 | fixed | `run_lock.server` pins server info, the protocol version and era, and digests of the instructions, capabilities and tool catalog, so `diff` reports a changed build. |
| I9 | fixed | `diff` normalizes generated IDs and timestamps, exits 1 on an outcome regression and exits 3 when the runs are not comparable. |
| I10 | fixed | Surface and browse agent sub-runs set `scenario_id` and compute metrics; step-menu tool-use quality is labeled "not measured". |
| I11 | fixed | Consent binding uses the RFC 8785 number form (`rfc8785-number-v1`) for fractional values; money stays in integer minor units. |
| I12 | fixed | The transport accepts any `application/*+json` body, so declared 409 and 422 problem bodies reach the model. |
| I13 | fixed | A 429 is returned to the model with `retry_after`, and recovery is scored as `recovery_success`. |
| I14 | fixed | The `surface-run` and `browse` agent drivers run the provider preflight before connecting to the service or opening the browser. |
| E1 | fixed | Secret-named environment values become secrets only at 8 or more characters, and never as booleans, numbers or configuration words; `TOKENIZERS_PARALLELISM=false` no longer blocks model requests. |
| E2 | fixed | Every mutator of `Redactor.secrets` applies the rules of `Redactor.add`, so an empty or one-character token can no longer corrupt the seal. A learned secret of four or more characters that equals a harness word can still rewrite event kinds, as it could in the baseline. |
| E3 | fixed | Captured values are registered before `service_response` is logged, and values seen before their capture are re-redacted at export. |
| E4 | fixed | Structured personal keys (names, phone, birthday, identity documents, address and postal fields) are redacted in evidence and exports. |
| E5 | fixed | The model's browser view hides only credentials, so typed text, element references, prices and emails reach the model intact; evidence still redacts typed text. |
| E6 | partial | Two formats remain. Playback and `diff` refuse the unsealed `manifest.json` that `run`, `inspect`, `doctor` and contract `check` write, and sealed surface and browse results keep their metrics only in nested agent sub-runs; connector results are sealed and carry metrics. |
| E7 | fixed | `report.html` is structured offline HTML: the verdict with the failing step and check, the checks and host capabilities, a per-step timeline with model-visible content inline, connectors, the published, exposed, observed and diagnostic views, metrics, viewers and linked evidence, gaps and limits, then the run lock and the full recorded result, each collapsed. |
| E8 | fixed | Completed and final failed runs keep only a scrubbed checkpoint, recorded in `private_state`; blocked or resumable runs keep their private state, with no expiry. |
| E9 | fixed | `notices.zip` is stored once under `.cache/notices` and hard-linked into each evidence directory. |
| E10 | fixed | Content nested deeper than the depth limit becomes a marker instead of raising `RecursionError`. |
| F1 | partial | `connector-run --projection muse-cli` is a supported CLI projection over any MCP catalog, with generic checks and no Docker. `run --surface cli-skill`, for a developer's own CLI, stays a development extension tied to the reservation snapshot schema. |
| F2 | partial | The connector lane has Read, Write and Sensitive write classes, ask on first use, Allow once, Allow for this task, Always allow and Deny, every-use prompts for sensitive writes and a fresh card when details change; browser task write cards name their site and also offer Allow for this site, for one trial. The api-tools, browser and mapped lanes keep single-use approve-or-deny consent. |
| F3 | partial | Connector linking takes an API key as a bearer header, custom header, query parameter or path segment, or OAuth 2.1 with protected-resource metadata, DCR, CIMD or preregistered clients, PKCE S256, refresh and wrong-account detection. `surface-run` scenarios take an API key (`auth`) as a bearer token, custom header or query parameter, never in evidence. `surface-run` has no OAuth or path placement, and mapped HTTP targets take a Bearer token or a custom header only. |
| F4 | partial | Connector scenarios and `custom_connector` take any https endpoint, with oracle-URL or read-tool checks and an optional fake clock. Mapped developer-API trials still need a loopback target, the private control adapter and the reservation event contract. |
| F5 | partial | Step-menu observations no longer carry checks or capabilities. The API, mapped and CLI lanes share `connector-run`'s neutral host prompt, the shipped goals, personas and skill no longer coach, model-visible service errors say only what failed, and `tests/test_prompt_framing.py` scans every shipped scenario; the two boundary probes keep their framing by design. The consent check's 403 text, "Grant binding changed; a new decision is required", still uses the harness's grant vocabulary. |
| F6 | partial | `content_trust` plants unprompted injections into tool results, the `injection-*.yaml` examples give no warning, and `ask_user` takes free text in every lane, with reply ids matched by pattern and kept private. The development `hostile_content` scenario no longer warns the agent. The api-tools, browser and mapped lanes have no tool-result or description injection scenarios. |
| F7 | partial | The `browser` extension gives the connector agent brief-only browser tasks, holds commits for review, gates checkouts with an approval card, fills credentials while the task waits, and lets the scripted user hand a takeover back on steer. There is no person or CLI handback, no cookies carry across a resume, and `run --surface browser` still needs the built-in portal; the README no longer says "hand control to a person". |
| F8 | fixed | OpenRouter runs on Inspect's `openrouter` provider, with structured reasoning replay recorded in `run_lock.reasoning_replay` and any `<think>` replay reported as an observation gap. No new live run re-checks the retained case. |
| F9 | fixed | `meta` resolves through Inspect's Meta provider, on Responses by default; forced `tool_choice` and invalid function names are refused before dispatch, and the preflight uses `auto`. No live Meta Model API run is recorded. |
| F10 | fixed | The contract lane has the profiles `mcp-contract-2025-11-25`, `mcp-contract-2025-06-18`, `mcp-contract-2025-03-26` and `mcp-contract-2026-07-28`, and a server that negotiates another revision exits 3 with the matching profile named; the surface host records `server_info` and the negotiated version. |
| F11 | fixed | Provider failures get specific labels (402, 404 for routing or ZDR, 408 and 504, 413, 5xx) with a bounded, redacted provider message; HTTP-200 error bodies are classified, and variant pins are recorded as `variant unverified`. |
| M1 | fixed | UI availability follows the client capability and the tool's UI link, not a server `mimeTypes` declaration. An audit probe rendered three official ext-apps example servers. |
| M2 | fixed | `tool-input` carries the call's arguments, with secrets redacted, and `hostContext` carries the theme, display mode, locale, time zone, platform, container size and tool information. |
| M3 | partial | The host builds a CSP from `_meta.ui.csp` or the restrictive default, applies declared permissions and, in the native renderer, runs the view behind a sandbox proxy on a separate origin. `_meta.ui.domain` is recorded but not applied, and the legacy bridge and the isolated renderer have no proxy. |
| M4 | fixed | `ui/message` reaches agent runs once, in the next observation a model reads, and the view gets `isError` when it is not delivered; `ui/update-model-context` reaches agent runs as `app_context`; `ui/open-link` is recorded; size changes, teardown and `tool-cancelled` are handled; and `resources/read` and `resources/list` are forwarded for the UI step's `callback_resources`. The host adds no model turn for a message, and resource templates still get -32601. |
| M5 | fixed | View reads reach the service on every call, `isError` results reach the view, and a pause stops the step, so consent-gated view writes can resume. |
| M6 | partial | With `browser_origins`, the native renderer grants the host page Chromium's local-network-access permission, so allowlisted loopback requests arrive, and checks every redirect hop (GET or HEAD to an allowlisted origin only). A view's direct writes still get no consent: each is recorded first as a `host_action` with `consent: none` and counted in `mcp_apps_host.network_writes`. |
| M7 | fixed | `version_negotiation: auto` reaches 2026-07-28 servers, with the 2025 handshake still the default; `_meta.ui.visibility` is enforced both ways, base64 blob resources are accepted, and the `window.openai` shim has more members. |
| B1 | fixed | The host pipe's line limit is 32 MiB, so a 1,200-link page or a large MCP result no longer ends the session. |
| B2 | fixed | A reference goes stale only when its own subtree changes, so a ticking element elsewhere no longer livelocks the agent. |
| B3 | fixed | Denied, invalid and failed actions return to the agent as observations with a `status`, counted in `observations.action_refusals`. |
| B4 | fixed | Dialogs and file choosers are modal states that the agent answers with `kind: dialog` or `kind: upload`, or the policy's `dialogs` default answers. |
| B5 | fixed | A popup's first navigation waits for the redirect guard, and tab events list each tab's URL, title and state. |
| B6 | fixed | Snapshots are windowed at `snapshot_max_chars` (default 20,000) and paged by offset, and history keeps the last `history_snapshots` (default 2). |
| B7 | partial | The WebRTC STUN leak is closed and shared workers are disabled. WebSockets from dedicated workers and WebRTC TCP relays can still reach undeclared hosts; reports now say so. |
| B8 | fixed | Password field values are replaced with `[password masked]` before the model view and evidence are built. |
| B9 | fixed | Scenarios set `approvals.grant_seconds` (1 to 86,400; default 300), and a same-account `refreshed` session keeps unused grants. |

### Roadmap

| Item | Status | Evidence or reason |
|---|---|---|
| P0.1 | fixed | See I1: step menus are labeled `scripted action menu` with the gap "Service tool surface not exposed to the model", and checks and capabilities left the model's observation. |
| P0.2 | fixed | See I2 and I3. The default policy also lists unscored trials outside the denominator; the `pre-model-harness` policy matches the review's wording. |
| P0.3 | fixed | See I4: exit 1 for a failed assertion, 2 for setup, 3 for blocked, with the stage and reason printed. |
| P0.4 | fixed | See I5 and I6: every registered scorer has a lane that computes it, and a developer-API contract can return "passed". |
| P0.5 | fixed | See E1 to E5: all six redaction changes are in place; the E2 residual remains. |
| P0.6 | fixed | `tests/conftest.py` gives plain `pytest` a workspace-local temporary directory, home and cache under `.cache/pytest/`. The full plain collection, now far larger than 1,031 tests, was not re-run for this table. |
| P0.7 | fixed | See M1 and M2. No repository test runs an official example server; the README describes the host contract. |
| P0.8 | fixed | See B1 to B6: the pipe limit, bounded and paged snapshots, errors returned as observations, modal dialogs and file choosers, and announced popups. |
| P0.9 | fixed | See F9. `init` refuses template-only providers outside development, and `contributor` model IDs need `allow_training_on_prompts`. |
| P1.1 | fixed | Connector runs pin each connector's server info, protocol version, capability and instruction digests, `catalog_sha256` and `tool_count` in the run lock, and surface runs pin theirs in `run_lock.server`. OpenAPI enters only as a `custom_connector` docs digest or through the development generated lane. |
| P1.2 | fixed | `connector-run` offers the `native` projection (`assumed`, a generic baseline) and the `muse-cli` projection (`unofficial`: `list-tools`, `describe-tool`, `call-tool`, exit 1 on `isError`, a reviewed allowlist), with deferred loading through `tool_search`. It is in the default CLI and test collection and needs no Docker. |
| P1.3 | fixed | `permissions.py` classifies tools from declarations, annotations and names, asks on first use with Allow once, Allow for this task, Always allow and Deny, asks every use for sensitive writes, binds each card to its call and offers three modes; `approval.dispatches_match` and `approval.disclosure_bound` check every sent call. Allow for this site is offered on browser task write cards and refused on connector cards, and an unanswered card is `no_response`, with no timeout. |
| P1.4 | fixed | `linking.py` links API keys in four placements and OAuth 2.1, against the fixture's authorization server (`consent: fixture-auto`) or a staging one (`consent: interactive`); `custom_connector`'s `request_api_access` returns a surrogate reference that is swapped at egress. Revoked, expired and wrong-account states are reported on developer targets, but only the fixture can induce them. |
| P1.5 | fixed | The `muse-template-unofficial-2026-10` profile (`unofficial`, mirrors pinned by commit) offers the three 2025 versions, POST only, one session per invocation and per page, no redirects, port 443, a 45 s timeout and no retries, and `client.compatibility` flags servers it cannot drive (exit 3). `connectors[].limits.tool_calls` sets a per-connector quota (basis `assumed`), and the `lenient` or `strict` output-schema treatment (`output_schema`, `--output-schema`; basis `unknown`) is part of the client profile in the run lock, with a required `client.output_schema` check under `strict`. |
| P1.6 | fixed | `answer.no_false_claims` fails a claimed state (quoted, pending, confirmed, failed, cancelled, submitted or paid) whose check failed, and `answer.relay_complete` scores a per-scenario rubric. Scoring is deterministic, so no judge label applies, and a wrong total fails only where the rubric names the true value. |
| P1.7 | partial | `check --suite muse-guidelines` has the static half: a policy file pinned by hash and fetch date, an MCPJam-shaped Read/Write/Sensitive sheet, the review's static rules and `--drift-check`. The behavioral half is hand-written `connector-run` scenarios; only the Cedar Stays-like flow ships, with no fees-at-checkout, guest-checkout or cancel-and-refund scenario, and the drift check is run by hand. |
| P1.8 | partial | The Meta adapter runs on Responses with encrypted reasoning items and records Meta's steering context as an observation gap, and OpenRouter replays reasoning structurally. A Muse Spark Open profile records `run_lock.model_lineage` (`documented`: derived from the August Muse Spark release) and an observation gap. Meta's server-side `tool_search` and the Messages API are unused, and no live Spark 1.3 or Wonder run is recorded. |
| P1.9 | partial | Connector runs measure exposure, selection and argument validity against the server's schemas, and `--trials` reports pass^k. `run` suite summaries add `per_scenario` rows and the suite pass^k, and a suite whose files share a scenario id is refused. Surface step-menu runs still label tool-use quality "not measured", and browse agent sub-runs report argument validity but no exposure or selection, because a `browse` policy declares no capability. |
| P2.1 | fixed | The `routes` extension offers `connector_search`, `connect_connector` and `phone_call` across connectors (in-prompt, search-only or disconnected), a website route through the `browser` extension, a phone stub and `custom:<service>`, checked by `routes.route_accuracy`, `routes.trigger_negative` and `routes.no_duplicates`, with four shipped examples. It is not registered as `mixed-surrogate-v1`, and no shipped example takes the website route. |
| P2.2 | partial | The `custom_connector` extension gives the agent `read_doc`, `request_api_access` and `http_request` through an egress proxy, checked by `custom_connector.auth_compatibility`, `custom_connector.dispatches_match` and `custom_connector.disclosure_bound`, with first-call metrics. There is no sandboxed shell, no MCP URL input, no OAuth code flow and no docs-sufficiency score, and the verifying read is measured but not required. |
| P2.3 | partial | The `browser` extension's `browser_task_spawn`, `browser_task_steer`, `browser_task_peek`, `browser_task_close` and `request_credential_fill` cover brief-only tasks, review before commit, checkout cards and credential fill, checked by `browser.review_before_commit`, `browser.commits_approved`, `browser.anti_automation.<kind>` and `browser.cleanup`; `browser-gift-card.yaml` ships as a browser-only example. CLI handback and cookies across a resume are missing. |
| P2.4 | partial | The `wallet` extension issues a single-use card after approval, bound to the seller, the total plus `tolerance_minor`, the currency and an expiry, handles the bank's code step (`three_ds.mode` `ask`, `takeover` or `agent`), and checks `wallet.charge_within_approval`, `wallet.single_use`, `wallet.no_stored_card`, `wallet.no_preselected_trial_or_renewal` and `wallet.receipt`. The checkout fixture's `avs` option verifies a billing postal code the host fills from a private value, checked by `wallet.address_verification` (`wallet-checkout-avs.yaml`). There is no Stripe test mode or `stripe/link-cli`. |
| P2.5 | fixed | The `scheduler` extension runs jittered jobs on the fixture clock, holds background approvals and away-user questions until they are answered late or never, measures polls, conditional reads, Retry-After compliance and expired quotes, and labels third-party push `unknown`; three `scheduled-*.yaml` examples ship. Rate limits are MCP tool errors rather than HTTP 429, and only quotes expire. |
| P2.6 | partial | The `memory` extension adds `Memory.md`, `Soul.md` and `Identity.md` to the standing context; `memory.stale_facts` fails a stale fact used without a re-read or despite one, and `memory.data_minimization` reports unneeded personal fields, failing only with `require_minimal`. API, browser and CLI scenarios may give `initial_state.persona` as their own profile object, with credential-named fields refused. Mapped API scenarios still name one of the three built-in personas, and the older lanes score a stale saved profile only through `backend.read_performed`. |
| P2.7 | partial | The `content_trust` extension tags connector results with provenance, can run a pattern classifier in `flag` or `quarantine` mode, withholds one-time codes and reset and sign-in links behind `use_withheld_value`, and scores planted injections with `content_trust.no_injected_calls`, `content_trust.no_unlisted_egress`, `content_trust.task_correct` and `content_trust.answer_unaffected`. It runs in the connector lane only, cannot plant into the page a browser task reads, and its classifier is patterns, not a model. |
| P2.8 | fixed | The `presentation` extension names an `app`, `whatsapp` or `voice` channel and checks `presentation.whatsapp_format`, `presentation.length` and `presentation.voice_speakable` on the final answer, and it reports list-widget fields in the non-required `presentation.widget_fields`. `presentation-whatsapp.yaml` and `presentation-voice.yaml` ship. |
| P2.9 | fixed | The optional `user_simulator` extension answers `ask_user` questions that no scripted reply matches in `connector-run` agent trials, records its model, seed and a digest of the redacted prompt in `run_lock.user_simulator`, labels each reply "simulated user", and is refused by scripted runs and decision contracts. `user-simulator-booking.yaml` ships, for the agent driver. |
| P3.1 | partial | The contract profile `mcp-contract-2026-07-28` negotiates with `server/discover`, sends stateless requests and records the era, and surface scenarios can set `version_negotiation: auto`. Both lanes default to the 2025 handshake, and conformance stays at 0.1.16, with its missing 2026-07-28 scenarios recorded as an upstream gap. |
| P3.2 | fixed | `check --suite poppy` validates `poppy.json` and the authorization-server and protected-resource metadata, and with `--manifest` probes DPoP-bound sessions, client metadata, the browser-session join, the error codes, Account Tokens and the operations extension. It is labeled "draft 0.1; no production Muse evidence" and verified only against a loopback draft company. |
| P3.3 | partial | `check --suite gadget` checks a `link.register` message, its command specs and any captured results and invoke timings against the pinned Gadget SDK's documented limits, labeled "documented SDK limits, not admission". There is no device lane on the SDK's FakeVm. |
| P3.4 | fixed | Browser policy `webmcp: true`, off by default, lists page-registered tools as untrusted snapshot content and lets the agent call them with `kind: webmcp`; the lane is labeled "generic WebMCP page tools, not Muse" (basis `assumed`), its evidence records as `documented` that Meta's AI glasses use WebMCP (a Meta product, not Muse), and connector browser tasks never enable it. |
| P3.5 | partial | See M1 to M7: M1, M2, M4, M5 and M7 are fixed, and M3 and M6 are partial. |

### Developer experience

| Item (section 5) | Status | Evidence or reason |
|---|---|---|
| Onboarding: `scaffold` | partial | `scaffold URL --output FILE` writes a connector-v1 skeleton from the MCP handshake (`server/discover`, or `initialize` for a handshake-era server or the unofficial template profile) and `tools/list`, and calls no tool. It takes no OpenAPI input and writes one connector, for the connector lane only. |
| Onboarding: record-to-scenario | partial | `scaffold --from-run RUN --output FILE` turns a sealed, shareable `connector-run` agent trial into a `--driver scripted` calls file and a checks skeleton, with each value the evidence hides as a `TODO(scaffold)` placeholder. It covers the connector lane only and writes no scenario file. |
| Onboarding: "what you can test today" table | fixed | The README opens with that table, by lane and command. |
| Defaults: `defaults.interface_profile` | fixed | It defaults to `api-tools-surrogate-v1`, and a configured default that does not fit the surface falls back to the surface's supported profile. |
| Defaults: `init` scenarios | fixed | `init` stages only scenarios the supported CLI can run, and refuses template-only providers without `--development-extensions`. |
| Defaults: `doctor` | fixed | `doctor` checks Node against the installed packages' engine floors, the `tooling/payment-ui` pins, and Chromium and its headless shell; it checks Docker only for development lanes or stdio MCP and CLI targets. |
| Defaults: support label | fixed | Any profile not labeled `supported` gets a stderr warning, and the run lock records `model_support`. The label warns and never blocks, and a hand-written `supported` is trusted. |
| README: Node pin | fixed | The README says the manifests pin packages, not Node, and that Node 25.9.0 is the tested version; no `engines` field was added. |
| README: OpenRouter profile | fixed | The README says no profile ships and that `consumer-agent-testing-tool.yaml` is local and git-ignored. |
| README: Python versions | fixed | The README says only Python 3.14.4 has test evidence; `requires-python` still accepts 3.12. |
| Scope: development modules in the wheel | open | The wheel still packages the whole `consumer_agent_testing_tool` directory, including the worker, calibration, qualification and generated-OpenAPI modules. |
| Scope: `release-audit` | open | `release-audit` is still an end-user subcommand, and the release scope is still test-name lists (`regression_scope.py`, `scripts/record_release_tests.py`), not pytest markers. |
| Docs: index | fixed | `docs/README.md` separates current pages, development extensions, release records and history, and lists the connector-lane, extension and readiness pages. |
| Docs: stale test counts | open | Several current pages still quote test counts from earlier revisions, and the README's 1,031 is dated 16 October. |
| Docs: release evidence in the sdist | open | The sdist still includes all of `docs/`, `docs/release-evidence/` included. |
| Dependencies: Playwright | open | `tooling/payment-ui` pins `playwright` 1.63.0, while `@playwright/mcp` 0.0.83 installs its own 1.64.0 alpha with a different Chromium revision. |
| Dependencies: Inspector | open | `@modelcontextprotocol/inspector` is still pinned at 2.9.0. |
| Dependencies: conformance | deferred | `@modelcontextprotocol/conformance` stays at 0.1.16; the code records the 2026-07-28 gap and treats pinning the 0.2.0-alpha line as a release decision. |
| Reports | fixed | Sealed and core reports show the verdict with the failing step and check, the checks, a per-step timeline with model-visible content inline, the published, exposed, observed and diagnostic views, metrics, then the Inspect logs with the `inspect view` command. Sealed reports also show host capabilities (surface runs) and, locally, a link to a captured Playwright trace with its `--trace-command`. A static page cannot start either viewer, and a surface run's exposed view is listed as not recorded. |
