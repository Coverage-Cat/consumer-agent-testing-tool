> **Scope update, 15 October 2026:** The supported release is now
> [`local-consumer-v1`](release-scope.md). The full-spec gates and development
> capabilities discussed below are historical context, not current release prerequisites.
> The [restricted browser behavior](#restricted-browser-behavior),
> [transactional consent](#transactional-consent) and [replay and diff](#replay-and-diff)
> sections describe current behavior; the development extensions they mention
> remain outside the release.

# Release extensions and qualification

The broader release scope has experimental, separately testable extensions.
Implementation and local synthetic evidence do not close the original live
provider gates or establish real-Muse behavior. `consumer-agent-testing-tool release-audit`
is the machine-readable status check; the release remains unqualified until
every listed gate has current evidence.

The optional [Maritime adapter](worker-maritime.md) now implements a named
cloud lifecycle through the same worker coordinator. Its local HTTP-contract
and actual subprocess checks are separate from live provisioning and
independently scheduled resource cleanup qualification.

The [API scenario lane](api-scenarios.md) connects Inspect's native tools and
approval hooks to the shared REST fixture. It adds private event/clock policies,
durable control reconciliation and interruption. These are synthetic workflow
checks; they do not close OS-isolation or live evidence gates.

[Authentication scenarios](authentication-scenarios.md) add shared REST/portal
session states, clock-driven expiry, refresh, scope and selected-subject checks,
and atomic retirement of old grants. Actual Inspect and Chromium tests exercise
these synthetic states; OAuth providers remain a separately verified integration.

[Host measurements](host-measurements.md) add dispatch and browser timing,
domain approval processing and scenario-labeled approval burden. Reports retain
timing gaps and deduplicate resumed proposals; private labels stay out of model
context.

[Proposal validity](proposal-validity.md) records schema decisions at the provider
boundary, including calls later rejected before API or browser dispatch.
Target-side argument checks remain a separate measurement.

[Developer HTTP mappings](api-mappings.md) extend no-model inspection and contract
checks beyond the reference fixture. [Developer behavioral trials](api-behavior.md)
reuse Inspect with private lifecycle, consent and oracle controls. The declared
request map preserves source schemas, explicit effects and response projections.
[Curated](developer-api-continuation.md) and
[generated](generated-api-continuation.md) continuation now preserve retained
trials and reconcile uncertain writes. Optional upstream OpenAPI validation and
mock tools are described in [API checks](openapi-checks.md). Broader native
checkpoint restoration remains unfinished.

## Restricted browser behavior

The [embedded consumer renderer](embedded-browser-worker.md) can now select the
isolated worker as well. Direct HTTP writes require exact, durable one-use
grants; interrupted writes require read reconciliation. Runtime locks and
private frame redirect guards apply to that selected path.

```sh
node scripts/surface_fixture.mjs 8765
.venv/bin/consumer-agent-testing-tool browse examples/browser-policy.json
```

`browse` opens the portal and emits an upstream Playwright MCP accessibility
snapshot with a generation ID and actionable references. `--actions` supplies a
JSON list of reference-based actions. `--driver agent --goal "..." --config ...
--model-profile ...` uses native model tools with the same broker boundary.
The policy file also sets the observation bounds and the dialog default, and
can enable [WebMCP page tools](#webmcp-page-tools); see
`examples/browser-policy.json`.

### Agent driver and outcomes

The agent driver composes Inspect's `Task`, `react`, native tool execution,
approval hooks and evaluation logs. It does not maintain a separate browser
model/tool loop. Before the browser opens, the driver runs the provider
preflight and records it under `agent_runs` and `model_preflight_runs`. If the
preflight does not pass, the browser stays closed: an unsupported provider or
unverified required coverage blocks the run (exit 3), a failed check fails it
(exit 1), and a provider or credential error is a setup failure (exit 2).

An Inspect approval hook validates each proposal before dispatch; this permits
only facade dispatch, not a network mutation or a domain grant. A turn with
more than one call, or arguments that break the schema or the kind's own
argument list, is rejected, and the agent receives the rejection as an ordinary
tool error. Nothing runs. The rejection is recorded as `proposal_rejected`, and
an agent that never proposes a valid call afterwards fails with
`agent.tool_proposals` (class `interpretation`). In `browse`, a call to an
unknown tool comes back as Inspect's tool error before the hook sees it: it is
not recorded as `proposal_rejected` and does not count toward
`agent.tool_proposals`. Browser scenarios record it as a rejection. Takeover
stops the agent before another generation.

The policy sets `max_actions` (default 30), `max_model_turns` (40), and
`wall_seconds` (180, applied to the Inspect task). Browser startup and evidence
export are separate from that task deadline. Exhausting a limit fails the run
(class `interaction`, exit 1); Inspect operator and custom limits pause it
instead (exit 3).

A completed run, or one paused for takeover, prints the final observation and
`Browser evidence: <path>` on stdout. Any other outcome prints nothing on
stdout. A run that does not complete prints one line on stderr that names the
evidence directory:
`Browse <status> (<class>) at <check>: <reason>. Evidence: <path>`. A run that
ends with an error, rather than a pause, also prints the error itself as
`<class>: <message>`. Exit codes are 0 completed, 1 failed, 2 setup or reporting
failure, and 3 blocked or paused, as after a takeover. A host cleanup failure
after a completed or paused run marks it failed (exit 2).

Inspect records are buffered in memory, redacted, and written under the browser
run's `agent/r-.../inspect/` directory. The parent result identifies
`agent_evidence`; its manifest locks the prompt, effective tool schema, policy
and harness. Use Inspect's existing viewer with that `inspect` directory.
The CLI/skill runner shares the same log exporter. Model completion continues
to carry an explicit backend-observation gap unless the trusted runner obtains
independent state evidence.

### Observations and actions

A snapshot observation carries its `generation`, the snapshot `text` and the
task's `tabs`, plus `tab_events`, `modal` and `dialogs_handled` when they apply.
Snapshots longer than `snapshot_max_chars` (default 20,000; 1,000 to 2,000,000)
are cut at a line boundary. `truncated` then gives `offset`, `end`,
`total_chars` and `next_offset`, and a closing text line names the next
request. `{"kind": "snapshot", "offset": N}` returns the next slice of the same
generation; `kind=snapshot` without an offset takes a fresh snapshot. Paging is
not charged against `max_actions`, but each request is a model turn.

The agent conversation keeps the latest `history_snapshots` observations
(default 2; 1 to 20) in full, whether whole pages or slices. Older ones become
stubs that name their generation and character range. The run lock records the
strategy under `compaction` and the bounds under `observation_bounds`; each
compaction is a `history_compacted` event.

Each operation takes only its own arguments:

| Kind | Arguments |
| --- | --- |
| `snapshot` | `offset` (optional) |
| `navigate` | `url` |
| `tab` | `tab` |
| `click`, `scroll`, `download` | `ref`, `generation` |
| `type` | `ref`, `generation`, `text` |
| `select` | `ref`, `generation`, `value` |
| `press` | `ref`, `generation`, `key` (`Enter`, `Tab`, `Escape`, `Space` or an arrow key) |
| `wait` | `ref`, `generation`, `state` (`visible` or `hidden`) |
| `upload` | `file`; `ref` and `generation` are optional but go together |
| `dialog` | `accept`; `text` (optional) answers a prompt |
| `takeover` | No other arguments |
| `webmcp` | `tool`, `params`; only when the policy sets `webmcp: true` (see [WebMCP page tools](#webmcp-page-tools)) |

Model actions cannot supply CSS selectors, JavaScript, CDP, arbitrary
Playwright calls, or HTTP requests. Downloads and uploads use approved fixtures
only. A host-only `return_control()` method ends takeover; the agent cannot
grant itself control.

References are valid while their own element renders unchanged; unrelated live
text elsewhere on the page does not invalidate them. If the target changed,
nothing runs and the agent gets `status: stale-reference` with a fresh
snapshot. A reference from an older generation, or one never shown, is refused
without a new generation. References belong to the tab that showed them: if
that tab closed or control moved, the action returns `stale-reference` with a
fresh snapshot of the current tab.

Ordinary action errors are observations, not run failures. A refused or failed
action returns a `status` (`stale-reference`, `denied`, `invalid-action` or
`action-failed`) and a `message`, with a fresh snapshot when the page may have
changed. Nothing is retried, and upstream error text stays in developer
evidence. The result's `observations.action_refusals` counts refusals by status;
agent evidence records `browser_action_refused` and `proposal_rejected` events.
In a scripted `--actions` run, a refused action ends the run (exit 1), and an
operation or argument name the facade does not define is a contract violation
that ends the run (exit 2). In agent runs the tool schema rejects such names
before dispatch, as recoverable tool errors.

### Dialogs, file choosers and tabs

JavaScript dialogs and file choosers are modal states. A dialog in any tab
blocks. The observation has `status: dialog-open` and a `modal` with the
dialog's `type`, `message` and, for a prompt, `default_value`. Control moves to
that tab, announced by a `tab_events` entry `selected` with reason
`javascript-dialog`. Answer with `{"kind": "dialog", "accept": true}`, adding
`text` to answer a prompt, or with `accept: false`. Any other action while a
dialog is open does nothing and returns `invalid-action` with the `modal`.

The policy field `dialogs` (`agent`, `accept` or `dismiss`; default `agent`)
can answer dialogs instead, at most ten per action before the agent is asked.
It never pre-empts an answer the agent gives. `dialogs_handled` lists each
answer and who gave it.

A file chooser appears as `modal.type: file-chooser` beside the page. Respond
with `kind=upload` and an approved `file`, or with `kind=dialog` and
`accept: false` to cancel; other actions are refused until then.

Observations list `tabs` as objects with `id`, `url`, `title`, `active` and
`opener`. A tab also has `modal` for a dialog open in it while another tab is
current, and `blocked` (`url`, `reason`) when it shows a policy-blocked error
page. `tab_events` announce `opened`, `closed` and `selected` tabs. Tabs were
previously a list of IDs, so a diff against an older run shows this change;
`run_lock.facade` identifies the facade version.

### Network policy

Network policy is independently checked for every browser HTTP request.
Rules bind an exact origin, path, method set, and declared effect. Requests
without a matching rule are denied. A GET can be a write: method heuristics
do not authorize effects. Write requests require an exact request digest in a
host-provided decision file. One-use approval binds the complete URL/method/body,
principal, task, state version, amount, currency, destination, and expiry.
Policy classification is supplied by the developer of the test service.

Native mode shares the host network. Chromium runs with
`--webrtc-ip-handling-policy=disable_non_proxied_udp` and
`--disable-blink-features=SharedWorker`, and page WebSockets are closed. The
result's `locations.egress` states exactly what native mode enforces: "native
Chromium on the host network: page and dedicated-worker HTTP(S) requests and
their redirects are limited to declared browser destination rules; page
WebSockets are closed; SharedWorker is disabled; WebRTC non-proxied UDP is
disabled; WebSockets opened by dedicated workers and WebRTC TCP relays are not
enforced; no OS isolation". Blocking those two paths would need a filtering
proxy for all browser egress.

Playwright's route callback alone did not intercept every hop of a server
redirect in the tested build. A private Chromium response interceptor now checks
each redirect's `Location` before following it. Redirects may reach only
explicitly permitted read destinations; redirected writes require a separate
action. The response guard must be ready before a request starts. A new tab's
first GET navigation, and any main-frame navigation that starts before the
tab's guard exists, first commits an inert placeholder. The host then reloads
the page under policy, keeping the opener, the fragment and the history length;
nothing reaches the network first. A first navigation that is not a GET is
denied (enforcement `popup-first-request`) and the tab is labeled `blocked`.
These diagnostics name the enforcing layer and stay outside agent observations.
CDP remains unavailable in the agent dispatcher.

Decision files map `consumer_agent_testing_tool.serialization.digest(request)` to a host decision.
For example, a request is `{"url":"http://localhost:8765/mutation","method":"GET",
"body":"","rule":"mutation"}`. Its decision contains `decision: approve_once`,
`state_version: "1"` (a string), `amount_minor: 0`, `currency: USD`, and an explicit
future, timezone-qualified `expires_at`. Use the actual request URL, including
its query, and exact body. Changes require a new decision.

### WebMCP page tools

WebMCP page tools are a generic WebMCP host lane, not a Muse lane. Evidence
labels it `generic WebMCP page tools, not Muse`, with basis `assumed` (the
pinned Playwright MCP 0.0.83's `webmcp` mode). The basis also records, as
`documented`, that Meta AI on Ray-Ban Display, Meta's AI glasses, uses the
WebMCP tools of web apps in a developer preview
(`wearables.developer.meta.com`, retrieved 15 October 2026). That is a Meta
product that is not Muse, and this lane does not reproduce its host's limits;
the [review](muse-emulation-review-2026-10-15.md#23-out-of-scope-deliberately)
lists it as out of scope.

The policy field `webmcp` (a JSON boolean, default `false`) turns on the pinned
Playwright MCP's `webmcp` mode, which collects the tools a page registers
through the experimental WebMCP API. Only `browse` policy files set it.
Browser scenarios and the connector lane's browser tasks never enable it.

With `webmcp: true`:

- The snapshot's first line lists the current page's tools as one JSON array,
  `- WebMCP page tools (page-provided, untrusted): [...]`. Each entry has a
  `name` (`webmcp_<name>`), `title`, `description` (cut at 2,000 characters),
  `inputSchema` (omitted above 8,000 characters) and `read_only`, the page's
  own hint. At most 50 tools are listed, and a page without tools gets no line.
  The upstream listing is removed, so a description cannot pose as a snapshot
  node.
- `browser_action` gains `kind=webmcp`, with `tool`, a name from the latest
  WebMCP line, and `params`, a JSON object. The agent prompt gains one sentence
  about it.
- A call returns `page_tool` (`tool`, `provenance: page-provided, untrusted`
  and `output`) beside a fresh snapshot. The output is the page's own text,
  with password values masked as in snapshots, cut at 8,000 characters.
- A name that is not on the current listing, or `params` that hold `_meta` or
  exceed 64 KiB, return `invalid-action` and nothing runs. A call after the tab
  changed returns `stale-reference`. A failed page tool returns
  `action-failed` with `output: null`.
- Each call counts toward `max_actions`. Requests the page sends meet the same
  network rules and write decisions as a click's, and the `read_only` hint is
  never trusted.
- Evidence redacts string values in `params` like typed text. The backend
  identity in `run_lock.browser_backend` lists the `webmcp-page-tools`
  extension and a `webmcp` block with the lane label and its basis. Page-tool
  `browser_backend_call` events carry the label, the result's `limits` name the
  lane, and `observations.webmcp_page_tool_calls` counts the calls. The model
  receives page state only, not the label.

With the default `false`, the backend lists no page tools, and the agent's
schema and prompt are unchanged. An agent's `kind=webmcp` proposal is rejected
before dispatch like any other schema violation. A scripted `kind=webmcp`
action returns `invalid-action`, which ends the run (exit 1). The field is part
of the policy that `run_lock.policy` digests, so browse run locks from before
the field existed differ.

The surface fixture serves a `/webmcp` page that registers one tool, whose
description imitates a snapshot node:

```sh
node scripts/surface_fixture.mjs 8765
```

Save this policy as `webmcp-policy.json`:

```json
{
  "url": "http://127.0.0.1:8765/webmcp",
  "origins": ["http://127.0.0.1:8765"],
  "rules": [
    {"id": "page", "origin": "http://127.0.0.1:8765", "path": "/webmcp", "methods": ["GET"], "effect": "read"}
  ],
  "webmcp": true
}
```

Save these actions as `webmcp-actions.json`:

```json
[{"kind": "webmcp", "tool": "webmcp_add_to_cart", "params": {"sku": "A1"}}]
```

Then, in another terminal in this workspace:

```sh
.venv/bin/consumer-agent-testing-tool browse webmcp-policy.json --actions webmcp-actions.json
```

The final observation holds the tool's `page_tool` output and a snapshot that
shows `Cart: A1`.

### Evidence, backend and tests

Browser files are explicit workspace fixtures with extension and size limits;
downloads use generated names in the run directory and are not model inputs.
Download size is checked after completion; bounded storage at the OS/container
level remains part of the browser-isolation release gate.

The agent sees the page as rendered, including the text it typed in the current
attempt. Its view masks only registered credentials (environment credentials
and, in browser scenarios, the scenario's secrets) and password fields. In a
resumed browser scenario attempt, text the agent typed or entered in a prompt
in an earlier attempt (4 or more characters) is masked like a secret. Password
values, whitespace normalized and including CSS generated content after them,
appear as `[password masked]` in both the agent view and evidence. Recorded
evidence also redacts typed values of 4 or more characters and prompt answers,
and applies the usual [evidence redaction](sealed-event-provenance.md#redaction).
Runs write sealed public results, ordered events and an HTML report, including
browser version, model configuration and harness/dependency locks. The result's
`observations` block records `snapshot_max_chars`, `truncated_snapshots`,
`action_refusals` and the `dialogs` setting, plus `webmcp_page_tool_calls` when
the policy sets `webmcp: true`. Takeover returns a paused outcome,
not a successful completion. Browser observations alone never become a verified
service outcome.
The trusted runner can call `assert_backend_count(id, actual, expected, source=...)`
with a count read from an independent oracle. Failed assertions fail the final
report even when interaction completed; a missing count blocks verification.
Only specifically verified backend observations appear as service outcomes. The agent has
no oracle or assertion tool.
The scenario lane also records boolean backend assertions and explicit oracle
gaps. A completed interaction with missing independent verification remains
blocked in both the parent report and sealed browser playback; exports preserve
the same result. See [backend verification](oracle-reporting.md).
Browser and consumer-surface runs now seal core results before HTML rendering.
If rendering fails, [report recovery](report-recovery.md) builds a separately
sealed public copy without changing the original result or repeating an action.
Execution, verification, diagnostic capture and HTML status remain distinct.

The backend is pinned `@playwright/mcp` 0.0.83, using its public
`createConnection(config, contextGetter)` entry point and the official MCP SDK's
in-memory transport. The existing guarded browser context is supplied before
navigation. It opens no MCP listener or browser debugging port. Native MCP
navigation, snapshot, click, type, select, tab selection, key, dialog and
file-chooser tools operate behind the facade. The private transport also
rejects other tool names, extra arguments, selector targets, file-output
requests, and unsupported MCP methods. With `webmcp: true`, it also admits the
tools on the current page's WebMCP listing.
The upstream catalog is never added to the agent's toolset. Website-provided
WebMCP tools are disabled unless the policy enables
[WebMCP page tools](#webmcp-page-tools).

Reference focus/scroll, reference-conditioned wait and approved file handling
are fixed host extensions over that same context: upstream MCP does not offer
their exact restricted contracts. Keypress first focuses the already validated
reference. Approved download clicks use MCP; the host saves the bounded result
under a generated fixture name. Upstream temporary output is kept in a fresh
private workspace cache directory and removed on orderly close. A process
crash can leave that directory; it is not a public run artifact. The output
budget is not a kernel disk quota.

The adapter requests the pinned upstream raw full snapshot, excludes code
generation and images, and keeps tool errors/console information in developer
events rather than the model response. Backend failure does not switch to a
direct-Playwright fallback. Version, negotiated protocol, browser build,
extension list, and dependency hashes are recorded. This version of MCP pins
Playwright `1.64.0-alpha-1790635538000`; the tested Chromium executable comes
from the separately pinned Playwright 1.63.0 workspace cache used by MCP Apps.
Both dependency versions are intentional and locked. Compatibility is tested,
not assumed to hold across upgrades.

This remains a surrogate for Muse's unpublished serializer and is not yet a
qualified OS isolation boundary. The local portal suite exercises duplicate
and missing names, dialogs, same-origin frames, shadow roots, dynamic list updates,
navigation, focus traps, canvas validation, stale controls and changed totals,
offscreen confirmation, session expiry/takeover, files, pending/cancelled UI,
duplicate submission and false success. Separate request-oracle checks cover
fetch, multi-hop redirects, cross-origin frame redirects, popup recovery and
redirected writes. These are deterministic fixture checks, not model trials.
`tests/test_browser_agent.py` additionally exercises the real Inspect lifecycle
with scripted model responses, including one Chromium workflow checked against
the independent oracle. These composition tests are not live inference evidence.
The portal fault implementation is packaged at
`consumer_agent_testing_tool/data/fixtures/browser-portal.mjs`; `scripts/browser_fixture.mjs`
is the workspace launcher.
The portal also serves pages for the connector lane's `browser` extension (see
[lane extensions](connector-extensions.md)): `/case/member-login`, a member
sign-in for host credential fill; `/case/checkout`, a checkout with a saved
card where every Place order charges; and the anti-automation barriers
`/case/captcha`, `/case/waf-block` (served with HTTP 403) and
`/case/account-locked`, which links to `/case/reset-password`. Nothing gets
past a barrier. The runner-only oracle returns `effects` (including `sign-in`
and `charge`), `requests` and `attempts`, with one attempt per CAPTCHA answer,
blocked-page load and locked-account sign-in. Sign-in values and barrier
inputs appear there only as SHA-256 digests.
Opt-in [private screenshot and trace capture](browser-diagnostics.md) pairs the
emitted snapshot with a masked viewport image and retains native Playwright
traces only when explicitly configured. It provides a local comparison report
and a verified command for the matching Playwright Trace Viewer. The agent never
receives those artifacts. Standard browser/surface exports exclude private
captures and rebuild redacted public evidence with a new seal.
The [browser scenario lane](browser-scenarios.md) integrates private scheduled
events, scripted transaction approval, delayed work, interruption and
response-loss reconciliation with the real Inspect/MCP browser loop.
Pending jobs and unresolved writes remain provisional in the independent oracle.
[Fixture-browser continuation](scenario-continuation.md#browser-continuation)
preserves native local storage and Inspect history, invalidates old references,
and gates uncertain mutations until visible reconciliation. General portal
session recovery and OS-isolated browser/target escape tests still require
implementation or qualification. The
agent has no shell or raw debugging channel in this lane. Actual Muse snapshot
serialization remains unknown.

## Transactional consent

`consent.py` is a durable SQLite one-use grant broker. The
[shared reservation fixture](reservation-fixture.md) extends the CLI/MCP business
logic with REST, a portal and opt-in transactional MCP tools. Confirm/cancel
validate current revision and amount, consume the grant, update inventory,
record the result and append an effect in one transaction. The runner reads an
independent Unix-socket snapshot. A lost HTTP response after commit can be
reconciled without creating a second effect. `transactions.py` now contains
only convenience helpers over this shared implementation.

Delayed operations retain their original grant and survive service-process
restart. Fixture-time completion rechecks state and authority. A concurrent stop
either prevents the commit or reports it as already completed; it never claims
to undo a confirmed reservation. Lifecycle events and independent state checks
also detect a fixture that falsely claims cancellation. These are application
jobs with deterministic fixture time, integrated with scenario hooks and Inspect
API/browser trials. [Scenario continuation](scenario-continuation.md) now retains
opt-in private history and resumes one original trial after public dispatch
reconciliation. The browser mapping restores the owned portal's local storage
and keeps sealed attempts separate. General portal sessions and arbitrary
Inspect checkpoint restoration remain open.

[Curated developer API continuation](developer-api-continuation.md) extends this
to external test services that implement retained leases. Resume preserves
the original state and budgets, retires prior grants and uses an explicit
read-only public receipt mapping to resolve uncertain writes.
[Generated API continuation](generated-api-continuation.md) additionally preserves
serialized requests and HTTP response checkpoints, reconstructing results through
a fresh native FastMCP worker without repeating the original request.
Both modes retain pending or unavailable independent outcomes after the model
finishes. Verification-only continuation rechecks the same state without model
preflight, another generation or public dispatch. It preserves prior proven
forbidden effects, original deadlines and attempt history.

Grant binding includes every envelope field and hashes of the operation schema
and effective arguments after defaults. Canonicalization is full RFC 8785:
UTF-16 key ordering and ECMAScript number serialization. Unsafe integers,
nonfinite numbers, unpaired surrogates, and remote schema references are
rejected. The binding's `numeric_domain` is `safe-integer-json-v1` when the
schema and arguments contain only integers, with digests unchanged from earlier
releases, and `rfc8785-number-v1` when either contains a non-integer number.
Adapters must reproduce the label and the RFC 8785 number form. Money in the
envelope stays in integer minor units. Key ordering, escapes, negative zero and
integer forms have cross-language Node/Python vectors; number serialization is
also checked against the RFC 8785 Appendix B vectors and the §3.2.3 example.

General surface tool policies may declare `consent` containing `envelope`,
`schema`, `reply_key`, `now`, and `expires_at`. Scenario references resolve in the
host; the answer must come from the supplied user/scripted replies and be
`approve_once`. The evaluated agent has no grant issuance tool. External
services must validate revision/idempotency themselves; host grant consumption
alone cannot make a remote service's write atomic.

## Replay and diff

```sh
consumer-agent-testing-tool replay runs/surface-ID --mode playback
consumer-agent-testing-tool replay examples/contract-tape.json --mode contract \
  --requests examples/contract-requests.json
consumer-agent-testing-tool replay runs/surface-ID --mode rerun --scenario examples/surface-preferences.yaml
consumer-agent-testing-tool diff runs/surface-BASELINE runs/surface-CANDIDATE
```

New surface and browser runs seal their public event/result/report files and
linked agent manifests, redacted artifacts and Inspect logs. Playback verifies
the seal and event ordering, performs no execution, and rejects missing linked
evidence, omitted hashes, filesystem links and escaping paths. Private host
checkpoints are excluded. Historical agent runs whose seals omit linked evidence
cannot be verified by the expanded check. A seal detects accidental or partial
tampering; it is not a signature from an independent authority.
An explicitly recorded renderer failure may omit HTML while preserving its
sealed core evidence. A missing successful report still fails verification.
Exports bind the source seal throughout construction and record the current
rendering harness separately from the original experiment's lock.
The shared [data-format contract](data-formats.md) preserves JSON Unicode and
numeric values when loading locks, results and replay requests. A numeric value
and its textual spelling remain different arguments.

Contract replay matches operation, step, arguments, relevant headers, body
digest, and fixture version against an isolated tape fixture. There is no
internet fallback. It does not substitute for the independent transactional
fixture's state oracle.

Behavioral reruns require a matching scenario/harness/model lock and remain
stochastic. A rerun may meet another server build; its identity is recorded
under the run lock's `server` entry. The operator must restore the original
external fixture state; the runner does not silently reset an arbitrary staging
system. Hosted model aliases and undisclosed server revisions limit
reproducibility. Different configurations require a new run and declared
`diff --treatment /model`, `--treatment /server` or another explicit lock path.
Comparisons make no invented causal diagnosis.
Implementation locks cover the package's code/resources and declared Python/Node
dependency locks. They do not establish the integrity of arbitrary local
modifications to installed third-party packages or an undisclosed remote server.

`diff` compares the outcomes, capabilities and milestone events of two sealed
surface or browser runs, and their run locks:

| Exit | Meaning |
| --- | --- |
| 0 | Comparable, with no outcome regression |
| 1 | An outcome regressed: a worse run status, or a check that passed in the baseline no longer passes or is missing |
| 3 | Not comparable: a run-lock change outside the declared `--treatment` paths, or a run that is unsealed or fails verification |

The output lists `regressions`, the changes in each section as RFC 6901 paths,
`unexplained_lock_changes`, and `normalization`. Both runs are normalized
identically before comparison, and `normalization` records the rules and how
many values each replaced:

- RFC 4122 UUIDs become `<uuid>`.
- Hexadecimal tokens of 16 or more characters that mix digits and letters become
  `<hex-id>`; in outcomes, only under identifier keys (`id`, `*_id`, `ids`,
  `uid`, `uuid`, `guid`).
- Under identifier keys, other tokens of 16 or more characters that mix letters
  and digits, such as ULIDs, become `<id>`.
- Values under service clock keys, such as `created_at`, `expires_at` or
  `timestamp`, become `<timestamp>`. Booking times such as `starts_at` stay exact.
- Values under digest keys (`*sha256`, `*digest`, `*hash`, `checksum`, `etag`,
  `integrity`) and the run locks are compared exactly.

Checks are aligned by their recorded `id`: a repeated ID becomes `id#2`, and a
row without one `#index`. Milestone events are aligned by step, then kind and
occurrence.

`--ignore POINTER` (repeatable) excludes part of the comparison. It takes an RFC
6901 pointer under `/outcomes`, `/capabilities` or `/milestones`, where `*`
matches one segment, for example `--ignore '/milestones/*/agent_observation'`.
Ignored sections, checks and fields appear as `<ignored>`, never count as
regressions, and are listed in the output. Run-lock differences are declared
with `--treatment` instead.

## Temporary workers

The [main CLI worker selector](worker-run.md) integrates `--execution worker`
with `inspect`, `check`, `doctor`, and `run`. It preserves independent model
and interface selection, checks the installed harness, and transports selected
inference/target credentials only to the trusted coordinator.
[Negotiated polling](worker-polling.md) supports jobs beyond one request timeout.
[Runtime archives](worker-runtime.md) deploy pinned browser and Node assets.
[Private continuation](worker-continuation.md) retains API/browser trials across
worker deletion with explicit collection and workspace admission controls.
[Python environment deployment](worker-python.md) installs explicit wheel bundles
without worker-side downloads. [Container deployment](worker-containers.md)
imports pinned images onto a preinstalled Docker engine and binds role selections
to verified deployment receipts. Live remote and named-cloud qualification remain open.
The separate explicit-bundle interface is also available:

```sh
consumer-agent-testing-tool --development-extensions worker bundle examples/worker-bundle.json --output runs/worker-bundle.zip
consumer-agent-testing-tool --development-extensions worker run worker-config.json runs/worker-bundle.zip
consumer-agent-testing-tool --development-extensions worker reap worker-config.json
```

Worker execution is independent of inference. A pinned archive contains only
explicit workspace files and a command; private filenames, traversal, changed
hashes, and oversized extracted bundles are rejected by the local reference
gateway.

`WorkerConfig` declares a provider identity, JSON command gateway argv, maximum
TTL/call duration, explicit credential environment names, and the expected
bundle digest. The controller invokes `provision`, `ready`, `seed`, `run`,
`export`, `cancel`, and `destroy`. Gateways must correlate requests by unique
trial ID, make provisioning idempotent, confirm readiness/destruction, enforce
the deadline independently, and export evidence before deletion. Credentials
in this gateway configuration are passed only to the selected provider gateway.
Main-CLI inference/target credentials use a separate private coordinator input.
Neither set is inherited wholesale or placed in the bundle.
Worker pricing is not inference pricing.

`python -m consumer_agent_testing_tool.worker_gateway WORKSPACE_WORKER_ROOT` implements the
protocol with actual local subprocess execution, fixture seeding, export,
deadline/cancellation, and directory deletion. It is a **local reference**, not
a cloud provisioning adapter or an adversarial isolation boundary.

The built-in `provider: ssh` adapter runs temporary workers on an explicitly
selected existing Linux/cloud host. It requires `ssh_target`, workspace-local
`ssh_identity_file` and `ssh_known_hosts_file`, and an `ssh_worker_root` under
`/tmp`. It transfers the pinned archive through the private protocol, uses strict
host-key checking, and disables ambient SSH configuration. It provisions worker
directories/processes, not cloud VMs. The host must already have Python and the
runtime dependencies required by the locked task.

A command gateway can also wrap another approved cloud SDK or worker service.
No provider has been silently selected, no remote resource has been provisioned,
and no cloud lifecycle gate has passed. A live provision/run/collect/destroy
proof is required before advertising qualified cloud execution. The periodic
`reap` control-plane job retries cleanup of
expired trials after a coordinator crash; configure it independently of the
coordinator for real cloud deployments.
Reaping is restricted to the original gateway configuration and matching trial
identity. Failed or unacknowledged destruction remains failed cleanup.

[Worker evidence collection](worker-evidence.md) now persists redacted exports
before deletion on normal, failed and reaped runs. Export replies require explicit
`complete` and `issues` fields as well as the matching trial and text file map.
Read-only export remains available after execution expiry. Partial or unavailable
diagnostics fail collection even when resource cleanup is confirmed.

[Reference-worker expiry](worker-expiry.md) now uses a separate process-group
guard with an immutable deadline, private disconnect pipe and kernel leases.
Coordinator/RPC loss, guard loss, suspended supervision and ordinary background
children have real process tests. This is a trusted-job lifecycle proof; the
reference runner is not an adversarial execution boundary or cloud deployment.

## Real-Muse calibration

```sh
consumer-agent-testing-tool --development-extensions calibrate cohort.json \
  --profile-sha256 PROFILE_DIGEST --surface-revision CURRENT_REVISION
consumer-agent-testing-tool --development-extensions calibrate examples/surface-preferences.yaml \
  --partner-packet --profile-sha256 PROFILE_DIGEST
```

Version 2 [observation packets](calibration-observations.md) compare paired
local/Muse task outcomes, independent backend state, surfaces, operation
sequences, arguments, request repetition, visible approvals, handoffs and
final-response consistency. Each dimension has its own observed denominator,
unknown count and descriptive agreement interval. Task outcome and consistency
also report false local passes/failures.

Packets bind scenario/trial identity, disjoint calibration/held-out splits,
source artifact hashes, observation/expiry dates, profile and surface revision.
Model, policy, browser and discovery visibility remain separate. Changed
profiles, changed surface revisions and expired cohorts are stale. Version 1
remains supported with explicit outcome-only comparison limits.

The importer checks artifact integrity and the operator's authorization
declaration, not external authenticity or hidden Muse context. A usable cohort
is not general Muse certification or a statistically sufficient release sample.
Synthetic unit packets test the analyzer and never count as Muse observations.
Without access, the partner packet supplies the scenario, staging instructions,
capture checklist, and unanswered host-specific questions. No automatic tunnel,
login bypass, or private infrastructure inspection is attempted.

## Evidence gates

The recorded ledger, `release/release-gates.json` (started from the committed
policy in `docs/release-gates.json`), links each passed gate to a dated artifact
under `release/evidence/` with a hash,
matching gate identity/basis, passing checks, and expiry. Test reports cannot
satisfy live inference, isolation, cloud, or authorized-observation gates.
Evidence also binds the current source digest; changing harness code invalidates
previously attached results. `scripts/record_release_tests.py JUNIT_PATH` records
actual named test results for the transactional-consent and replay/diff gates
only. It cannot attest the live gates or the separate muse-calibration gate.
Run `scripts/record_regression.py JUNIT_PATH` first. The deterministic-gate
recorder requires that exact, unexpired, accepted complete regression and its
successful unchanged-source process record. It checks report/log hashes and
every recorded test identity, then rechecks its inputs before publishing.
Old source, substituted reports, incomplete collections and missing run records
cannot renew gate evidence. The resulting artifacts retain the report,
run-record and regression hashes.
This ledger is deliberately conservative; developer attestation remains the
trust boundary for provenance of external evidence.
The [external qualification contract](external-qualification.md) additionally
requires gate-specific coverage, referenced configurations/run locks and
hashed observations. Muse qualification recomputes the comparison from its
cohort and source artifacts. A passing summary, explicitly synthetic report
or incomplete platform scope cannot satisfy these gates.

Local tests:

```sh
.venv/bin/pytest tests/test_release_extensions.py tests/test_browser_broker.py \
  tests/test_browser_portals.py tests/test_surface.py tests/test_surface_browser.py \
  --basetemp=.cache/pytest-extensions
```

The tests include negative detectors: changed/expired/revoked/wrong-account
grants, concurrent consumption, response loss, corrupted replay, request
mismatch, stale calibration, mock live-gate evidence, worker failure/cleanup,
and a browser GET mutation denied before the independent service counter changes.
