# Model latency and incomplete observations

Model latency now requires a unique `model_request` and matching terminal
`model_response` or `provider_error`, in event order and within the same
recorded clock epoch. The core event envelope records that epoch alongside
its monotonic timestamp.

The [synthetic reproduction](release-evidence/model-timing-reproduction-2026-10-14.json)
showed three misleading results: a missing response became zero seconds,
duplicate responses inflated the total, and clocks from different processes
were combined. These are observation gaps under spec §§15–16.

`metrics.model_seconds` is now null when any request is incomplete or
ambiguous. Missing clock identity, malformed ticks, backwards intervals,
duplicate request/terminal records, orphan responses and responses preceding
their requests also leave the total unknown. Historical records without clock
identity keep that limitation; the reader does not invent one.

`metrics.timing_coverage.model` separately retains the sum of valid observed
intervals, request count, successful-response count, provider-error count and
gap count. If no interval can be measured, `observed_seconds` is null too.
An explicitly recorded zero-length interval remains a measured zero.

Complete intervals from separate process epochs can be summed, but no
interval crosses between epochs. Concurrent calls are matched by request
identity. Their summed duration can exceed wall time. A timed provider error
establishes elapsed host time, not model success, billable usage or the
provider's internal timing.

This change uses the existing event and measurement machinery. It does not
add an inference loop, retry policy or network call. Total duration and token
usage have separate collection semantics.

A [separate follow-up reproduction](release-evidence/measurement-followups-2026-10-15.json)
found that incomplete token usage appeared as a complete total, and a
backwards wall clock produced a negative total duration. The
[measurement coverage follow-up](measurement-coverage.md) now addresses these
cases and has passed full regression and fresh-install verification.

The model-latency checkpoint passed **106 focused checks**, **1,578 regression tests without
skips**, and all **28 fresh-install stages**. The tests include actual cancellation
at the Inspect capture boundary and preservation of partial timing through
finish, redacted export and trial summary.

The installed wheel separately passed seven timing/export cases and recomputed
the three historical reproductions as unknown without changing their original
records. See [the installed proof](release-evidence/model-timing-installed-proof-2026-10-14.json).
This is synthetic local evidence;
live model, browser-isolation, cloud and Muse qualification remain open.
