# Pending public receipts

Curated and generated API trials can wait for a public receipt while resuming
an uncertain write. The target supplies the receipt through an explicitly
declared read-only tool. The existing Inspect trajectory continues after the
original response is recovered.

Enable polling on the write's recovery mapping:

```yaml
recovery:
  buy_item:
    lookup_tool: lookup_receipt
    arguments:
      key: key
    poll:
      max_attempts: 10
      interval_seconds: 1
      wait_seconds: 10
```

For a generated adapter, put the same `recovery` entry under the target's
`openapi` configuration. Tool names must match the actual native catalog.
Every lookup argument still maps to a required argument of the original write.

## Public contract

Declare unprojected HTTP 200 and 202 response contracts on the lookup tool.
A pending HTTP 202 response contains exactly these fields:

```json
{
  "run_id": "original-host-run-id",
  "task_id": "original-scenario-id",
  "principal": "original-test-principal",
  "operation": "orders.submit",
  "arguments_sha256": "digest-of-original-arguments",
  "response": null
}
```

The host validates every identity field before waiting or issuing another read.
HTTP 200 uses the same identity fields and the original acknowledged response:

- Curated: `response: {"status": 200, "body": {"status": "completed"}}`.
- Generated: `response` contains `status`, `body_base64`, and `headers`, as
  specified in [generated continuation](generated-api-continuation.md).

The generated native output schema must represent both pending and acknowledged
responses. With the pinned FastMCP adapter, use a source schema whose `response`
property permits both an object and null. Distinct HTTP status declarations
alone do not ensure the generated output schema can represent both values.
The workbench preserves native validation and does not remove its constraints.

Only an identity-matched pending receipt authorizes automatic polling.
Malformed receipts, wrong-account results, redirects and other HTTP statuses
stop recovery. The host uses the configured lookup route and fixed interval;
services must implement this receipt contract on that route.

## Budgets and interruption

`max_attempts` is the cumulative number of admitted public reads for this
original write across all resumes. It must be an integer from 1 to 100.
`interval_seconds` is 0.05–30 seconds; `wait_seconds` is 0.05–60 seconds per
recovery invocation. Values must be finite. The original trial deadline also
applies, including time between invocations.

The host saves the attempt reservation and earliest next-poll time before
dispatch. A process exit cannot reset the budget or immediately repeat an
admitted read. When the invocation's wait allowance ends, the same retained
trial can be resumed later. Exhausting the cumulative read budget keeps the
write unresolved; another resume does not renew that allowance.

A successful public receipt is saved before result reconstruction. If the
process or native converter stops there, resume uses the saved response without
another lookup. Generated recovery restores the original native result through
FastMCP's network-denied conversion path.

These reads do not issue grants, repeat the original mutation, or ask the model
to choose a recovery action. The model receives the recovered original tool
result through its existing continuation checkpoint. Model/tool budgets remain
separate from the recorded host recovery-read budget.

## Verification and evidence

An acknowledged response is distinct from a verified backend outcome.
Independent pending work keeps the trial blocked even after receipt recovery
and the model's final response. [Outcome-only continuation](developer-api-continuation.md#verify-a-deferred-outcome)
can subsequently verify completion without another model call or public read.

Public events record admitted poll numbers, matching decisions and pending
status. The hash-bound polling checkpoint and raw saved response remain private
and are excluded from standard exports. The original response is still subject
to the selected adapter's projection, validation and model-context redaction.

Tests exercise real HTTP/Unix services, Inspect, native FastMCP conversion,
independent SQLite state, timeout, process exit and continuation. Fresh installed
checks exercise both curated and generated pending receipts. These are synthetic
workflow proofs; operating-system isolation, live inference and real-Muse
calibration remain separate evidence gates.
