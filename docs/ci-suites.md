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
```

Validation reads every scenario, calls file, document and readiness manifest before any service starts.
It checks target treatments, required outcome checks, script references and service isolation declarations.
Planning prints one JSON document. An invalid input exits 2 with structured diagnostics that omit input
snippets. Inspect a malformed file locally; a CI diagnostic does not echo its contents.

The versioned contracts are [ci-suite-v1](../schemas/ci-suite-v1.schema.json) and
[ci-plan-v1](../schemas/ci-plan-v1.schema.json). Input paths resolve from the suite file and must stay inside
the working directory. The complete expanded suite is limited to 512 cases.

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
CI accepts the shipped shared extensions. It rejects `user_simulator` and custom extension factories,
whose inference behavior the suite cannot establish.

## Service state

An owned service declares a command as an argv list and a readiness URL. Commands run without a shell.
`{port}` is a loopback port supplied to the command, readiness URL and scenario's `${PORT}`.
The service also receives `PORT`. Pass credentials through the environment.

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

## Selection

`--case ID` selects a source case or one expanded treatment. `--tag TAG` selects cases bearing any named tag.
Repeated case filters are a union, repeated tag filters are a union, and the two groups intersect.
Unknown filters and empty selections fail. The planner validates unselected inputs too.

`--shard I/N` distributes the sorted selection in round-robin order, using one-based indices. Every shard
shares the same scope digest; the plan names both the complete selection and its scheduled portion.
Shard count cannot exceed selected case count. A case definition binds the scenario, visible document
content, script, treatment, expectation and service configuration. Private values are masked before hashing,
so these public digests cannot be used to guess private inputs.
