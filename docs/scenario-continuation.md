# Scenario continuation

For external curated APIs, see the
[retained lease and public receipt contract](developer-api-continuation.md).
The fixture-specific restoration below remains separate.

The built-in API and browser scenario lanes can continue an interrupted trial in the same
run directory. Start with explicit private-history retention:

```sh
consumer-agent-testing-tool run api-scenarios/stale_total.yaml \
  --surface api-tools --interface-profile api-tools-surrogate-v1 \
  --model-profile local-glimmer --trials 1 --resumable
```

Continue the printed trial directory, using the same scenario and configuration:

```sh
consumer-agent-testing-tool run api-scenarios/stale_total.yaml \
  --surface api-tools --interface-profile api-tools-surrogate-v1 \
  --model-profile local-glimmer --trials 1 --resume runs/r-REPLACE
```

Use your configured model-profile name. A fresh inference preflight must pass
before continuation. A changed scenario, model configuration, target, interface,
implementation, or verified local runtime is rejected. Completed trials and
preregistered fixed-sample trials cannot be continued by this command.

An independently observed unauthorized or duplicate effect remains a failure
through continuation, even when a later oracle is unavailable or underreports.
The original observation event and attempt remain in the report. Browser
playback and exported summaries preserve the same failure. See
[invariant history](invariant-history.md).

The CLI also compares the complete recorded execution configuration with the
selected one before acquiring the trial or running model preflight. Changed
placement, sandbox settings or browser-worker settings are refused without
updating the saved trial. A missing execution record is not treated as permission
to adopt the current settings. The private checkpoint binds that execution
configuration too, so editing only the public manifest cannot rebind the trial.
This remains a trusted local-state contract, not authentication of a hostile host.
Main CLI worker continuation is still unsupported; this check does not add
private checkpoint transfer between workers.

## What is retained

`--resumable` stores the Inspect message history and its pending tool proposal
in `private/trial.private.json`. It contains unredacted model-visible text:
keep the original private run directory under your control. Ordinary runs do
not retain this conversation file. Standard exports exclude the private
journal and SQLite database and cannot be used as resumable runs.

The adapter retains the original trial ID, fixture database, scenario event
IDs, scripted clarification history, model-attempt count, tool-call count and
absolute deadline. Downtime counts against that deadline. If the deadline has
passed before the resumed attempt's first generation, the trial is blocked
(exit 3); if it passes during the agent's turns, the trial fails (class
`interaction`, exit 1). A lost inference response still consumes its attempt.
Resumable trials disable automatic provider retries; continuation is explicit.
Ordinary runs keep the provider's bounded retry policy.

Inspect continues to own the ReAct loop, native tools and approvals. The
adapter supplies a new Inspect sample from retained messages, reinstalls the
locked system prompt once, and appends a separately exported Inspect log
segment. It does not restore an arbitrary Inspect checkpoint, subagent state,
provider cache, arbitrary browser session, or external service. Interrupted in-flight
tool results are reconstructed from trusted dispatch records with recorded
reconciliation provenance.

## API writes and approvals

The runner records dispatch intent before sending a request and saves the
response before running post-tool scenario events.

| State at interruption | Continuation behavior |
|---|---|
| Before the first model request | Finish the durable event once; keep the original fixture |
| Tool response retained | Supply the retained response and finish its stable event checkpoint |
| Write response missing | Read the public operation by key and match the operation and argument digest |
| Public lookup absent, ambiguous or mismatched | Remain paused; no model request and no mutation replay |
| Read response missing | Report the interruption; the agent can request a fresh read |
| Approval interrupted before dispatch | Finish the private event, retire any grant, and require a fresh proposal |
| Invalid agent proposal | Refuse continuation; a restart cannot erase the violation |
| Final model message retained | Validate it and collect the oracle without generating again |

A pending deferred operation remains provisional. An uncertain request to stop
a pending job is reconciled through its own public `operations.cancel` receipt.
The receipt binds the requested operation and key; it is stored atomically with
the job transition and grant revocation. A job being cancelled or completed
does not by itself prove that this stop request reached the service. Missing,
ambiguous or mismatched receipts keep continuation paused. No stop request is
resent automatically.

The receipt distinguishes `cancelled: true` from work already completed or
failed. It records the service response, not an independent verification of its
claim: the runner still checks the private job/effect oracle. The deliberately
broken false-cancellation fixture cannot pass while its job remains pending.
This is the closed fixture's recovery contract, not a generic adapter for
arbitrary APIs.

Only one process may own the trial. Referenced public artifacts and Inspect
segments are hash-checked before resume. Private files must remain available,
and linked private paths are rejected. These checks assume trusted local
private state; they are not independent authentication of that state or an
adversarial filesystem snapshot.

Previous attempt outcomes remain in the event history. Resuming appends to one
trial; it does not create another sample for suite statistics. Earlier suite
reports remain historical and are not silently recomputed. Current backend
assertions come from the independent fixture oracle.

## Browser continuation

Use the same flags with the packaged browser scenario:

```sh
consumer-agent-testing-tool run browser-scenarios/stale_total.yaml \
  --surface browser --interface-profile muse-browser-public-2026-09 \
  --model-profile YOUR_CONFIGURED_PROFILE --trials 1 --resumable

consumer-agent-testing-tool run browser-scenarios/stale_total.yaml \
  --surface browser --interface-profile muse-browser-public-2026-09 \
  --model-profile YOUR_CONFIGURED_PROFILE --trials 1 --resume runs/r-REPLACE
```

This mapping restores the owned reservation portal's native local-storage state
and continues Inspect with a fresh browser snapshot. Native storage capture
runs only on the private host channel when retention is enabled. It is absent
from the model's tools, snapshots and public export. Entered text remains masked
in the resumed public browser trace. Cookies are excluded: the local fixture
issues fresh account-scoped credentials.

The same logical fixture can receive a different loopback port after restart.
The host rebinds the saved single-origin local storage to that new fixture
origin. It does not restore arbitrary origins, cookies, tabs, SSO/MFA sessions,
IndexedDB or private DOM state. The browser build and MCP backend must match
the first attempt.

Every resumed attempt starts in a new generation range, invalidating old
element references. The portal marks saved observations as restored and requires
a visible refresh or reconciliation before another transaction. The host also
blocks new mutations while an earlier request has an uncertain outcome—even
if a broken page enables the submit control. The agent can use the visible
reconciliation button; the host matches the public operation, key and argument
digest before clearing the uncertain request. Raw network records and oracle
answers are never supplied to the browser agent.
Server errors and missing JSON write receipts remain uncertain; receiving an
HTTP response alone does not prove that a mutation failed before commit.
The same public receipt check applies when a stop-pending response is lost.
Visible reconciliation reads the job status and its command receipt together;
without the matching command receipt, the host keeps that write unresolved even
when the page shows a terminal job state.

Interrupted pre-dispatch approvals are retired rather than dispatched.
Cumulative model/tool budgets and the original deadline remain in effect;
browser startup and downtime consume that deadline. Explicit `--resume`
acknowledges the prior pause for this synthetic fixture.

Each attempt has a separate sealed browser evidence directory: `browser/`,
then `browser-attempt-2/`, and so on. Earlier seals remain unchanged. Parent
reports and exports validate and include every segment. After an abrupt
process exit, the coordinator can seal valid retained events as an
**interrupted** attempt without fabricating a prior outcome or rerunning the
browser. The new attempt reads the independent fixture oracle.

## Verification and remaining scope

Tests exercise real Inspect calls, local HTTP/Unix fixtures, interruption before
and after approval, ambiguous reconciliation, exhausted budgets, private export
exclusion, CLI preflight, and separate-process exits after commit, after
response delivery, and after a final model message. The fresh installed-wheel
smoke also pauses and resumes an API transaction without creating a second
draft.

Browser tests also exercise restoration of entered form state, stale references,
unchanged sealed segments, a broken submit control, budget and build guards,
CLI continuation, and a separate-process exit after a confirmation commits.

Arbitrary developer APIs/portals, broader callback-loss combinations and
complete native Inspect checkpoint restoration remain open. This capability
does not close browser isolation, cloud lifecycle,
provider qualification or real-Muse observation gates.
