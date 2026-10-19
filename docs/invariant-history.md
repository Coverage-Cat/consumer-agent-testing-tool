# Forbidden effects across continuation and comparison

One independently established unauthorized or duplicate effect fails the trial.
That observation cannot be erased by an interruption, a later unavailable oracle,
or a later oracle that reports a smaller count.

API, browser, curated and generated trials retain these observations in the
existing private continuation journal after every attempt. This includes attempts
that stop before the agent's final response. Each retained observation records
its count, attempt number and evidence event. Public manifests retain this
nonsecret failure evidence; current `backend_metrics` remain a separate
point-in-time observation.

The CLI/skill runner uses the same invariant recorder without adding a
continuation engine. It captures typed effect checks after normal scoring or
interrupted execution, and the CLI retains them alongside the execution error.
See [backend verification](oracle-reporting.md) for this reporting contract.

The zero-effect requirement is mandatory. Omitting a scenario assertion or
declaring a positive expected forbidden-effect count cannot turn a defective
integration green. The resulting check records zero as the required value and
preserves a conflicting scenario value as `declared_expected`. A self-test can
pass by expecting this diagnostic; the underlying integration report still fails.

Browser continuation applies the retained failure to both the parent report and
the current sealed browser playback. Its source names the original independent
observation event. A completed browser interaction therefore may still have a
failed verification result. Private oracle data never becomes a model input.

The [browser canary](browser-canary.md) also records proven forbidden traffic
in this invariant history. Its separate durable ledger accumulates destination
arrivals across attempts; the retained failure protects reports when a later
oracle is unavailable or underreports. This traffic check is distinct from the
unauthorized/duplicate business-effect summary counts described below.
Typed identity, scope, session and changed-grant violations use the same retained
history. [Statistical decisions](statistical-invariants.md) apply these and traffic
invariants before thresholds or exclusions.

## Summary and decision rules

For each input trial, effect counts use the maximum valid observation across:

- Current independent backend metrics.
- Assertion checks, including retained failures.
- The manifest's original invariant observations.

Only nonnegative integers are counts. Booleans, floats, strings, negative values
and missing values do not establish an observation or fill coverage. Repeated
snapshots of one effect are not added together. Counts from distinct trials are
summed.

Supply one final manifest per trial. Duplicate run IDs are rejected rather than
counted as independent samples; resuming a trial does not increase the statistical
sample size. A retained forbidden effect fails the decision's invariant gate
before its task-success threshold is considered. Trials that the
[exclusion policy](statistical-invariants.md#trial-outcomes-and-exclusions)
removes from the task-success denominator still contribute their proven
forbidden effects.

## Evidence and limits

Regression cases reproduce lost violations across actual Inspect API/browser
continuation and curated/generated developer adapters. Independent SQLite state
confirms the effects. Other cases cover invalid metric types, duplicate trial
identities, omitted assertions, and a statistical threshold that previously
passed despite a retained violation.

The installed proof pauses broken API/browser runs, resumes with a deliberately
underreporting oracle, and requires failed reports/playback plus preserved summary
counts after redacted export. This verifies the harness's handling of supplied
independent observations. It cannot authenticate an arbitrary developer oracle or
detect effects that no available evidence ever observed.
