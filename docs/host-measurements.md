# Host timing and approval burden

Scenario reports now measure CLI/API dispatch, browser actions and domain approval
processing at the host boundaries. These measurements use paired monotonic events
with a process clock identity. They are execution evidence, not estimates from
model text or declarations that a tool is installed.

`metrics` includes:

| Field | Measured scope |
|---|---|
| `model_seconds` | Unique model request/response or provider-error pairs within a recorded clock epoch. Missing or ambiguous pairs leave the total unknown; valid intervals remain in `timing_coverage.model`. |
| `tool_seconds` | CLI shell execution or API adapter dispatch. Generated API dispatch includes native response conversion. Private reset, oracle checks and result-only restoration are excluded. |
| `browser_seconds` | Browser action requests, including nested domain approval processing. Browser startup and private screenshot/trace capture are excluded. |
| `approval_wait_seconds` | Active host processing of a frozen domain proposal, including scripted approval hooks and private grant issuance. This is not a measurement of human response time. |
| `approval_burden` | Unique domain approval proposals, including denied proposals, and requests per completed task. Dispatch permission checks are not domain approvals. |
| `total_seconds` | Monotonic time from evidence creation to finalization within one recorded process clock. Missing readings or a restart leave the full duration unknown. |
| `timing_coverage` | Request/interval counts, observation gaps and the duration actually observed for each category. Model records distinguish successful responses from provider errors. |

Browser and approval durations overlap and must not be added together. Model
and total-run durations have their own scopes. Total duration uses a monotonic
clock; wall timestamp differences are retained separately as diagnostics.
A new process epoch cannot establish the elapsed duration across a restart.

An instrumented tool, browser or approval category with no calls is measured
zero. An uninstrumented category is null. Model timing requires recorded requests;
no model request leaves it unknown. A missing, duplicated, interrupted or invalid interval leaves
that category's total null; `observed_seconds` preserves only valid closed
intervals. Process restarts never join two different monotonic clock epochs.
Observed activity predating instrumentation is a gap, not retroactively measured
work. Historical runs do not acquire measurements by exporting them.
See [model timing](model-timing.md) for request pairing and legacy-clock limits.

Approval requests retain their logical proposal identity across a resumed
attempt. Reprocessing the same proposal adds observed processing time without
adding another request. A new proposal requiring a fresh decision is a separate
request. Interrupted approval processing remains a timing gap even if a later
attempt completes the decision.

Scenario authors can preregister classifications by the one-based ordinal of
each unique domain proposal:

```yaml
evaluation:
  capability: reservations.confirm
  approval_requests:
    "1": necessary
    "2": redundant
```

Only `necessary` and `redundant` are accepted labels. An omitted label is reported
as unknown. These private evaluation labels do not influence approval policy and
are not supplied to the model. Classification is the scenario author's judgment;
the tool does not infer necessity from an assistant's explanation.

Trial summaries retain requests from failed and blocked trials. The
requests-per-completed-task ratio uses only completed tasks and is null if any
completed task lacks approval evidence. `latency_seconds` requires complete
coverage for that field; `observed_latency_seconds` preserves the measured
subtotal, including valid closed intervals from incomplete runs, alongside
explicit complete and partial trial counts. `partially_measured_trials` is
a subset of `unmeasured_trials`, whose full totals remain unknown. Use one final
manifest per trial, including all its continuation attempts.

The deterministic tests exercise actual Inspect API and Chromium scenario
boundaries, redacted exports, lost control responses, legacy evidence and
incomplete clocks. Installed-package checks additionally exercise curated and
generated dispatch and continuation. Their inference and targets are synthetic.
These measurements do not establish live model performance, browser OS isolation,
cloud lifecycle qualification or agreement with actual Muse behavior.

See [measurement coverage](measurement-coverage.md) for token-count provenance,
partial aggregates and total-duration details.
