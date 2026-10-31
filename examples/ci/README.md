# Scripted surrogate CI

From this repository's root, after `uv sync`:

```sh
uv run consumer-agent-testing-tool ci run examples/ci/booking.suite.yaml --jobs 4
uv run consumer-agent-testing-tool ci run examples/ci/purchase.suite.yaml \
  --jobs 4 --report-dir reports/purchase
```

Both commands start fresh synthetic loopback services, run explicit Muse and Instinct treatments through
native and CLI projections, and stop their owned processes. They need no model, key, Node or browser.
`booking.suite.yaml` includes shared readiness. `purchase.suite.yaml` covers 14 purchase and failure-boundary
scenarios; `--tag smoke` selects just the two complete purchases.

The public outputs are `report.json` and `junit.xml`. Read the JSON's `exit_code`, then each case's
`status`, `outcome`, `checks` and `diagnostics`. `--summary-json -` prints one JSON document on stdout.
Progress goes to stderr. Private runs, input snapshots and raw service logs stay under `.cache/catt-ci/`;
never upload that directory.

| Exit | Meaning |
| --- | --- |
| 0 | Every scheduled case matched its exact expectation |
| 1 | A finding or expectation mismatch |
| 2 | Input, startup, harness or reporting error |
| 3 | Blocked or incomplete work |

An expected negative case can pass its CI gate while its underlying check remains failed or blocked.
The report preserves both facts. A partial selection does not claim full-suite compatibility, and every
report says surrogate. These runs establish neither hosted-assistant admission nor model behavior.

## Add your service

1. Scaffold and review a scenario against your local MCP catalog. Declare independent state checks,
   the tool allowlist, private host arguments and scripted permissions.
2. Copy [booking.suite.yaml](booking.suite.yaml). Replace the service command, readiness URL, scenario,
   calls and manifest paths. An owned command is an argv list, runs without a shell and must stay in
   the foreground. Use its allocated `${PORT}` in scenario endpoints.
3. Keep `emulation_target: [muse, instinct]` and `projection: [native, cli]` for the shared matrix.
   A portable call uses `connector: NAME`, `tool: NAME` and `arguments: {...}`; `/responses/N` refers
   only to the response actually visible to the surrogate.
4. Declare `state: process-local` only when each service process starts with independent state.
   For an external database or queue, use `case-isolated` and isolate it with `CATT_CASE_ID`.
   Existing shared services require one worker and one shard. Remote endpoints require `network: remote`.
5. Run `ci validate SUITE --jobs 4 --json`, then `ci run SUITE --jobs 4`. All cases are validated before
   any service starts, including cases excluded by a filter.

The service command uses your current working directory and environment. Install its dependencies before
the suite runs; use the service's interpreter or executable path when it differs from CATT's environment.
Input paths resolve from the suite file and stay inside the working directory.

Copy a [purchase boundary](purchase.suite.yaml) when testing application submission, disclosure changes,
secure payment setup, pending policies, uncertain writes, false claims or support requests. Each uses
independent oracle state and exact expectations. The payment callback IDs and simulated clock belong to
the synthetic fixture; adapt them to your sandbox. The pack does not perform a real insurance purchase.

The [suite guide](../../docs/ci-suites.md) covers formats, service ownership, failure selection, sharding,
merge validation and build comparison. `ci diff` needs complete reports with `--build-id` labels and
matching tests/tool revisions. Changed tests or missing coverage cannot produce a green comparison.

## GitHub Actions

Copy [connector-lane.yml](connector-lane.yml) to your service repository's `.github/workflows/` and
replace each `TODO`. Pin `TOOL_REF` to a reviewed full commit SHA that includes `ci`; v0.4.0 predates it.
The template pins Actions, installs the locked tool checkout, caches dependency downloads, keeps checkout
credentials out of Git configuration, and uploads only explicit public JSON/JUnit paths.

| Job | When | Work |
| --- | --- | --- |
| `scripted` | Pull requests and manual runs | Two shards, each with at most two workers |
| `scripted-gate` | After every scripted matrix outcome | Merge all shards; require this check |
| `agent-trials` | Only with explicit opt-in and a model key | Paid native/CLI trials for both targets |

Every shard receives the commit SHA as its build ID and the CI run ID plus attempt as its batch ID.
Merge rejects duplicate, stale or mismatched shards. A missing shard remains unrun and exits 3. Do not
ignore exit 3 in a required gate. The gate also requires every shard job to succeed, even if an artifact
survived a later reporting or upload error. To rerun a failed gate, choose **Re-run all jobs**. **Re-run failed
jobs** mixes artifacts from different attempt IDs and merge rejects them.

Reduce the shard matrix to one job and omit merging for a small suite;
bounded local workers often avoid the cost of starting extra CI machines.

Paid trials are off by default and stay outside `ci run`. To request them once, select **Run paid model
trials** in the manual workflow form. To enable scheduled trials, set
`ENABLE_SCHEDULED_AGENT_TRIALS=true`. Both choices also need `OPENROUTER_API_KEY` and a committed model
profile. Adding a key alone does not enable inference. All four target/projection jobs incur charges.

A private CATT checkout from another repository needs a read-only credential with access to it. Supply it
only through your CI's trusted checkout step; do not expose it to untrusted pull requests. No credentials
are needed for the shipped local suites.

This repository's [CI workflow](../../.github/workflows/ci.yml) runs both suites on Linux and macOS.
For a single scenario, the existing `connector-run --start ... --junit ... --summary-json ...` flags remain
available. To share a sealed child run, upload the output of `consumer-agent-testing-tool export RUN`.
