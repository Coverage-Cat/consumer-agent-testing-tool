# Complete and partial measurements

Token counts and elapsed time describe recorded work. A missing observation
cannot establish zero, and a successful task does not establish complete
measurement coverage.

## Token counts

Each captured model response records which `Inspect ModelUsage` fields were
explicitly set before serialization. This matters because serialization can
include default zero values for fields that were never supplied. The record
describes the Inspect adapter's fields; an upstream adapter may itself apply
defaults, so it is not proof of provider billing.

| Report field | Meaning |
|---|---|
| `token_usage` | Complete counts for input, output and total tokens across every recorded request. Null if any required field or request/response association is missing or ambiguous. Optional counters appear only with complete coverage of their own. |
| `observed_token_usage` | Subtotals from explicit, valid counters on uniquely associated responses. These may cover only part of the run. |
| `usage_observations` | Recorded request count, association/provenance gaps, and per-field observed, missing and invalid counts. |
| `usage_trial_coverage` | Complete, partial and unobserved trials in an aggregate, including failed, blocked and infrastructure trials. |
| `token_field_coverage` | Each counter's coverage across trials and recorded requests, including trials with unknown request-history coverage. |

For example, if one request reports 12 tokens and another has no usage record,
the observed subtotal is 12 and the full total is null. Explicitly reported zero
is valid. Missing, negative, boolean, string and noninteger counts do not become
zero. Input, output, cache and reasoning counters retain their distinct adapter
semantics; the workbench does not derive a missing total by adding them.

Provider errors, cancellation, duplicate or orphan responses, and legacy
records without explicit-field provenance leave coverage incomplete. Historic
summaries are preserved as recorded; aggregation does not promote their old
totals into newly established complete measurements. Cost remains unknown.

## Total duration

`total_seconds` uses monotonic readings recorded at evidence creation and
finalization, with a shared process-clock identity. Wall timestamps remain
available for chronology and diagnostics. A clock adjustment cannot produce
a negative elapsed duration.

`timing_coverage.run` records the measured duration and the signed difference
between wall timestamps separately. A process restart, missing reading,
invalid tick or mismatched clock identity leaves the full duration null.
This implementation does not reconstruct the time between process epochs.

Trial summaries retain valid closed intervals from partially observed runs in
`observed_latency_seconds`. `latency_seconds` requires complete coverage for the
category. `partially_measured_trials` is a subset of `unmeasured_trials`: those
trials lack a complete total but retain a usable subtotal. Browser and approval
processing can overlap, so the categories must not be added together.

## Agent sub-runs

The agent sub-runs of `surface-run --driver agent` and `browse --driver agent`,
under each run's `agent/` directory, now compute these metrics too. A surface
sub-run is a scripted action menu: the model picks a step ID and the host
supplies every argument, so exposure, selection and argument validity hold by
construction. Its metrics are therefore labeled
`agent_interface: scripted action menu` and `tool_use_quality: not measured`.
Token usage and timing are measured as in other runs.

Trial-level success counts, `success_per_attempt` and exclusions are described
in [statistical decisions](statistical-invariants.md#trial-outcomes-and-exclusions).

## Verification

Verification passed **142 focused checks**, **1,614 regression tests without
skips**, and all **28 fresh-install stages**. The first main-workspace focused
attempt could not bind loopback fixture sockets in the sandbox; its failure
is retained separately from the passing retry on unchanged source.

The fresh wheel independently passed four capture/export cases, two trial
aggregate checks and three clock cases using its installed Inspect adapter.
See [the installed proof](release-evidence/measurement-coverage-installed-proof-2026-10-15.json).

The tests use synthetic model responses and local evidence. They do not qualify
live model performance, provider billing, browser isolation, cloud lifecycle
or actual Muse behavior.
