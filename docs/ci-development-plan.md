# CI utility development plan

This change makes the connector lane useful as a repeatable CI gate for an MCP service. It keeps Muse and
Instinct as explicit surrogate targets. Scripted runs establish compatibility with the declared calls and
observed outcomes; they do not establish hosted-assistant admission or model behavior.

## Problems in 0.4.0

- The CI template repeats individual commands and treats Muse-specific projections as the default matrix.
- Readiness needs a separate Python wrapper and a fixed port.
- Native and CLI calls files repeat the same intent with different result references.
- Consumers must build their own parallel execution, selection, sharding, aggregation and comparison.
- A useful CI result needs more than a process exit code: it must distinguish a service regression,
  incomplete observation, incorrect setup and a deliberately tested failure boundary.
- Safe reports exist for individual runs, but there is no aggregate contract for agents or CI systems.

## Implementation

1. **Declare and validate suites.** Add a versioned suite format and a `ci` command family. Validate the
   complete selection before starting a service or making a network request. Produce deterministic plans,
   structured diagnostics and JSON Schemas. Reuse connector scenarios and shared surrogate readiness.
   Reject empty selections and incompatible treatments.
2. **Run cases concurrently with bounded ownership.** Give each owned service a fresh process and port.
   Bound startup, cases, workers and cleanup. Handle cancellation and descendants. Keep shared or remote
   state explicit; process isolation does not isolate a service's external database or payment provider.
   Keep completed observations and identify cases that never ran.
3. **Keep the routine gate free of model inference.** Run scripted traces and readiness only. Reject
   inference configuration in the suite lane. Add projection-independent references derived only from the
   host-visible response, so the same trace can exercise native and CLI hosts without concealing lost or
   truncated output.
4. **Select and distribute work honestly.** Support case/tag selection, deterministic shards, selection
   from earlier failures, and complete shard aggregation. Detect duplicates, missing cases, changed case
   definitions and mismatched reports. Compare service outcomes across builds without presenting missing
   coverage or changed tests as a pass. Never retry state-changing calls automatically.
5. **Make results useful to developers and agents.** Emit versioned JSON and JUnit with case identity,
   treatment, timing, check-level findings, underlying run outcomes and safe diagnostic guidance.
   Validate deliberate negative cases against specific expected findings; setup errors cannot become
   passing expectations. Preserve blocked/unsupported distinctions. Keep private runs and service logs
   separate from uploadable reports, including on timeout or cancellation.
6. **Ship the integration.** Provide working workflows and suite examples for both surrogates, including
   purchase lifecycle and failure-boundary cases relevant to Coverage Cat-like services. Exercise the
   suite in this repository's CI. Document minimal setup, artifact handling, comparison, CI sharding and
   the distinction between compatibility tests and optional agent trials. Measure sequential and parallel
   execution on a reproducible workload.

## Review iterations

Commit each coherent implementation stage, then review the committed source through `claude -p`.
Use a clean review checkout and read-only tools so private run data is outside the review. Keep raw review
output in ignored storage.

Complete at least three review iterations: suite planning and portable scripts; execution and reporting;
then the integrated workflows, failure cases and documentation. Reproduce substantive findings, implement
worthy changes, and record which findings were fixed or rejected with evidence. Review feedback does not
override the task or repository instructions.

## Completion evidence

Completion requires all six implementation items and the review iterations above:

- An MCP-service developer can run a shipped suite with one documented command and parse its result
  without scraping terminal text.
- Muse and Instinct both run through native and shared CLI projections. No target is silently selected.
- Invalid late cases, unsupported configuration and empty selections produce no service effects.
- Scripted suite tests trap model-provider entry points and prove that the harness makes no model request.
- Concurrency overlaps real cases, gives owned services distinct state, and cleans up services and
  descendants on completion, timeout and interruption. Repeat timing-sensitive tests under CPU load.
- Purchase cases verify independent submission, payment, policy and support outcomes, including intended
  failure boundaries. Incorrect prices, permissions or claims cannot be hidden by a broad expected failure.
- Shard aggregation and build comparison reject incomplete, duplicate, stale or incompatible evidence.
- Public reports exclude host-private values, environment secrets, raw service output and private files,
  including malformed-input and interrupted-run paths.
- A repeatable benchmark reports measured wall time and concurrency, without claiming a universal speedup.
- Applicable local tests, lint, schema checks, repository checks and CI examples pass before a push.
- Each new regression test is shown to fail before its fix. The final committed implementation receives
  another review, and no material review finding remains unresolved.

Release evidence remains generated by the tagged release workflow. This development change does not
record or publish a release.
