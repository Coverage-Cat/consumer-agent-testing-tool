# Scripted surrogate CI suites

A suite groups connector scenarios and catalog readiness checks under explicit Muse or Instinct treatments.
It uses scripted calls only. It has no model configuration, provider preflight or paid agent trial.
The result describes compatibility with CATT's surrogate and the declared checks, not hosted-assistant
admission or model quality.

## Validate and plan offline

From the repository root:

```sh
uv run consumer-agent-testing-tool ci validate examples/ci/booking.suite.yaml --jobs 4 --json
uv run consumer-agent-testing-tool ci plan examples/ci/booking.suite.yaml --jobs 4
uv run consumer-agent-testing-tool ci run examples/ci/booking.suite.yaml --jobs 4
```

Validation reads every scenario, calls file, document and readiness manifest before any service starts.
It checks target treatments, required outcome checks, script references and service isolation declarations.
Planning prints one JSON document. An invalid input exits 2 with structured diagnostics that omit input
snippets. Inspect a malformed file locally; a CI diagnostic does not echo its contents.

The versioned contracts are [ci-suite-v1](../schemas/ci-suite-v1.schema.json) and
[ci-plan-v1](../schemas/ci-plan-v1.schema.json). Input paths resolve from the suite file and must stay inside
the working directory. The complete expanded suite is limited to 512 cases.

## Execute and read results

`ci run` validates the entire suite, then executes the selected cases with at most `--jobs` workers.
Each case gets a separate worker directory and an owned service process when its service declares a command.
The coordinator stops the worker and service process groups on completion, timeout, SIGTERM or Ctrl-C.
Service commands must stay in the foreground; process groups do not contain daemons that detach themselves.

`timeout_seconds` bounds a case's startup and execution. `startup_seconds` separately bounds service readiness.
Cleanup can add up to ten seconds per process group; cleanup of concurrent cases runs concurrently.
The suite never retries a case. Interrupted cases are blocked or cancelled; queued cases remain `not-run`.
Completed cases retain their observations.
Programmatic cancellation propagates after cleanup and checkpointing; CLI signals return the incomplete
report with exit 3.

The default public outputs are `reports/ci/report.json` and `reports/ci/junit.xml`.
The JSON contract is [ci-report-v1](../schemas/ci-report-v1.schema.json). Use another empty directory with
`--report-dir`. The command can also print one JSON document:

```sh
uv run consumer-agent-testing-tool ci run examples/ci/booking.suite.yaml --jobs 4 --summary-json -
```

Progress goes to stderr. Read `exit_code`, then each case's `status`, `outcome`, `checks` and `diagnostics`.
Exit 0 means every scheduled expectation matched; 1 means a finding or expectation mismatch; 2 means a setup,
harness or reporting error; 3 means blocked or incomplete work. An error outranks other case results.
`compatibility_pass` is a separate, narrower claim; expected failures and readiness do not establish behavioral
compatibility. Readiness remains catalog-only evidence.

The JSON and JUnit reports use verified, redacted child evidence. JUnit retains skipped checks for blocked,
unsupported and unobserved outcomes. A case expectation and its underlying check status remain separate.
Private snapshots, child runs and raw logs are under `.cache/catt-ci/<report_id>/<case_id>/`.
`worker.log` holds worker output and `service/` holds service logs. Keep this directory out of artifacts.
To share a child run, use CATT's `export` command on its sealed `runs/<run_id>` directory.

CI reports contain diagnostics and check details, never raw worker/service output. Invalid input diagnostics
omit source snippets. Public and private output directories must be disjoint, and a public output directory
cannot contain unrelated files. Reserved `.catt-ci-*` atomic temporary files may remain after a crash;
upload only `report.json` and `junit.xml`. A write failure sets `fatal`, exits 2 and prints the in-memory
observations as JSON even without `--summary-json -`. CATT writes JUnit first, then publishes JSON. If a
write fails, it tries to publish both formats with the fatal error and remove any file it cannot replace.
Always require the command's exit status; a remaining file cannot establish success after a reporting error.

Suite inputs have a 4 MiB file limit. Aggregate reports have a separate 64 MiB read limit, including
`ci merge`, `ci diff` and `--failed-from`. Split unusually large suites if their reports exceed that bound.

## Cases and treatments

Each case names an existing connector scenario and a service. Connector cases also name a calls file.
Readiness cases set `kind: readiness` and name a shared surrogate readiness manifest. If that scenario has
several connectors, `connector` chooses the one whose endpoint and authentication readiness will use.

Every matrix declares `emulation_target`. Both targets support `native` and `cli`. Matrix defaults are
`projection: [native]`, `exposure: [in-prompt]`, `client_profile: [generic-sdk]` and
`output_schema: [strict]`. These override scenario treatments. Readiness only uses the target, projection
and client profile; leave its exposure and output-schema defaults.

Use [portable connector steps](connector-lane.md#scripted-driver) to share a trace across projections.
Native function names and explicit `exec` steps remain supported for tests that need those exact surfaces.
Search-only calls need an earlier explicit `tool_search`. The booking suite includes a separate discovery
trace; in-prompt hosts do not offer `tool_search`. Validation detects missing discovery, while actual search
ranking and dynamic queries need the live catalog.
CI accepts the shipped shared extensions. It rejects `user_simulator` and custom extension factories,
whose inference behavior the suite cannot establish.

## Service state

An owned service declares a command as an argv list and a readiness URL. Commands run without a shell.
`{port}` is a loopback port supplied to the command, readiness URL and scenario's `${PORT}`.
The service also receives `PORT`. Pass credentials through the environment.
Owned readiness URLs must use `{port}` in their port, and process-local scenario URLs must use `${PORT}`.
Existing services use their scenario's literal URLs or declared placeholder defaults.
Scripted HTTP(S) URL arguments also expand `${PORT}` and `${CASE}`, including service-specific names such
as `redirect_uri`. Ordinary non-URL strings are not interpolated.

Declare where the test's state lives:

| `state` | Required service contract |
| --- | --- |
| `process-local` | Each new service process starts with independent test state |
| `case-isolated` | The service author isolates external state by the supplied case identity |
| `shared` | Cases use the same state; use one worker and one shard |

An existing service omits `command` and `ready_url` and declares `state: shared`.
A fresh process or loopback address does not isolate an external database, insurance application or payment
provider. A remote URL requires the case's explicit `network: remote` declaration. This is configuration
validation, not a network sandbox; the service process and redirects can still make their own requests.

For external state, `{case}` in command arguments and readiness URLs, `${CASE}` in scenario URL fields, and
the service's `CATT_CASE_ID` contain a fresh namespace for this case attempt. `CATT_CASE_NAME` is the stable
expanded case ID; `CATT_RUN_ID` identifies the suite invocation. A `case-isolated` service must actually use
the fresh namespace for its database, queue or sandbox state.

## Deliberate negative cases

Declare every expected significant nonpassing finding by exact ID and status:

```yaml
expect:
  exit_code: 1
  checks:
    one-policy: failed
    answer.no_false_claims: failed
```

The exit code and complete set of required nonpassing checks and failed checks must match.
An additional permission failure, false claim or observation gap fails the expectation.
Setup/harness/reporting errors can never become expected passes. Exit 1 needs a failed check;
exit 3 permits blocking statuses and no failed check.

Expected IDs must name scenario checks, shared readiness rules or supported host checks. Optional scenario
checks count only when they fail.
These include `execution`, `answer`, `answer.no_false_claims`, `answer.relay_complete`,
`approval.dispatches_match`, `approval.disclosure_bound`, `client.output_schema`, `client.compatibility`,
and exact `reconcile.<tool>.<ordinal>` IDs (with the connector name when several connectors are declared).
Supported extensions also expose their named checks. A typo or unsupported expectation fails validation.
Expecting the generic `execution` or `answer` check also requires `failure_class` and `failure_stage`, taken
from the observed underlying outcome. A different cause cannot satisfy that expectation.

The case gate says `expected-failure` when the boundary behaves as declared. Its `outcome` still records
the underlying failure or block, and its compatibility flag stays false. A negative test does not establish
that an incomplete transaction succeeded.

## Selection

`--case ID` selects a source case or one expanded treatment. `--tag TAG` selects cases bearing any named tag.
Repeated case filters are a union, repeated tag filters are a union, and the two groups intersect.
Unknown filters and empty selections fail. The planner validates unselected inputs too.

`--shard I/N` distributes the sorted selection in round-robin order, using one-based indices. Every shard
shares the same scope digest; the plan names both the complete selection and its scheduled portion.
Shard count cannot exceed selected case count. A case definition binds the scenario, visible document
content, script, treatment, expectation and service configuration. Declared private values are masked before
hashing. Ordinary public values, including test cards, public contact fixtures and symbolic references, retain
their identity. An input that embeds a known environment credential is rejected; use environment-based
authentication or private host inputs.

Connector documents and `custom_connector.docs` bundles are validated and frozen before the worker uses them.
Unavailable script surfaces, non-allowlisted portable calls and projection-incompatible references fail
offline. Live input schemas, returned values, permission decisions and service outcomes still need a run.

`--failed-from REPORT` selects unresolved cases from a previous report with identical suite definitions.
It intersects any case/tag filters and can be combined with sharding. A report with no matching failures
exits 2 instead of producing an empty green gate. This is a new, explicitly requested invocation; each
owned service gets fresh state. Shared-service connector cases require manual reconciliation and selection.
The tool never retries a write automatically.

```sh
uv run consumer-agent-testing-tool ci plan examples/ci/booking.suite.yaml \
  --failed-from reports/previous/report.json
```

## Shards and build comparison

Distributed execution needs two public labels. `--build-id` identifies the service build; use a commit or
container digest from your build system. `--batch-id` identifies this CI invocation and attempt, such as
`run-123-attempt-2`. All shards of that invocation must share both labels.
In the GitHub Actions template, choose **Re-run all jobs** when rerunning a failed gate. **Re-run failed
jobs** leaves successful shards in the older attempt, so the current gate has missing coverage.
Artifact names include the attempt; the gate downloads only its attempt's artifacts.

```sh
uv run consumer-agent-testing-tool ci run examples/ci/booking.suite.yaml \
  --jobs 2 --shard 1/2 --batch-id local-001 --build-id build-a --report-dir reports/shard-1
uv run consumer-agent-testing-tool ci run examples/ci/booking.suite.yaml \
  --jobs 2 --shard 2/2 --batch-id local-001 --build-id build-a --report-dir reports/shard-2
uv run consumer-agent-testing-tool ci merge reports/shard-1/report.json reports/shard-2/report.json \
  --report-dir reports/merged --summary-json -
```

Merge verifies report digests, case definitions, selection, harness, build, batch and shard identities.
Duplicate or incompatible reports exit 2. Missing shards produce a report with explicit `not-run` rows and
exit 3. A failed shard stays failed. Merging a filtered selection does not claim full-suite compatibility.
Merged `seconds` is the longest reported shard duration; it is not the sum of distributed CPU time.

Fingerprints detect accidental inconsistency; they are not signed attestations. Build labels are supplied
by your build system. CATT does not infer or verify a remote service's binary revision. Keep artifact access
within your CI trust boundary, and never reuse a batch label across attempts.
Harness identity binds CATT's code, its own locks, Python, platform and installed core dependency versions.
The service's dependency changes belong to its build identity.

To compare builds, keep the tests, tool revision and dependency locks fixed:

```sh
uv run consumer-agent-testing-tool ci diff reports/baseline/report.json reports/candidate/report.json
```

The command prints [ci-diff-v1](../schemas/ci-diff-v1.schema.json) JSON. Exit 0 means no regression under
complete matching tests; 1 means a candidate finding, regressed observation or removed baseline case;
2 means an invalid report or harness error; 3 means incomplete or incomparable evidence. Changed case
definitions, changed harnesses, unbound builds and partial selections cannot produce exit 0. Existing
unexpected candidate failures also fail the comparison. Check-level differences remain visible even when
both runs already failed.

## Purchase boundaries

[purchase.suite.yaml](../examples/ci/purchase.suite.yaml) expands 14 scenarios across both targets and both
shared projections. It runs against the synthetic loopback purchase fixture:

```sh
uv run consumer-agent-testing-tool ci run examples/ci/purchase.suite.yaml \
  --jobs 4 --build-id local --report-dir reports/purchase
```

Use `--tag smoke` for the two complete purchase flows, or `--tag boundary` for the other cases.
The pack checks independent submission, charge, policy, document, payment-method and support-request state.
It covers declined submission, changed prices, unanswered payment approval, pending carrier work, UI fallback,
lost or schema-invalid write responses, false completion claims and misleading policy receipts.
Exact negative expectations ensure a new failure cannot hide behind an expected nonzero exit.

These are orchestration tests with synthetic secure-payment callbacks and a simulated clock. They neither
buy insurance nor exercise a real wallet or hosted assistant. The separate wallet and MCP Apps tests cover
local UI mechanics. The generated YAML remains an ordinary scenario format: copy a case, replace the
fixture's endpoint, tools and independent oracle with your service's sandbox equivalents, and review every
permission and state declaration. Regenerate this repository's pack with
`uv run python scripts/generate_ci_purchase_examples.py`; `--check` detects drift.

The [CI template](../examples/ci/README.md) shows bounded workers, complete shard aggregation, cached
dependencies and explicit opt-in for separate paid trials.

## Measure your workload

```sh
uv run python scripts/benchmark_ci.py --jobs 4 --repeats 3
```

The benchmark alternates one and four workers, includes validation through cleanup, and compares identical
observed check sets. It refuses a speed comparison if a run fails or its harness/check outcomes change.
Public samples are under `reports/ci-benchmark/`; private evidence stays under `.cache/catt-ci-benchmark/`.
Worker count is a concurrency bound, not a promised speedup. Startup, CPU, I/O and external service limits
determine useful parallelism. See [the measured workload](ci-benchmark.md).
