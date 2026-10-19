# Developer API continuation

Curated developer API trials can retain Inspect history and resume the same
test state. The private control adapter must implement `retained-trial-v1`.
Generated FastMCP trials use the same lease protocol with a separate
[native result restoration path](generated-api-continuation.md).

```sh
consumer-agent-testing-tool run scenarios/inventory.yaml --surface api-tools \
  --interface-profile api-tools-surrogate-v1 --model-profile YOUR_PROFILE \
  --trials 1 --resumable

consumer-agent-testing-tool run scenarios/inventory.yaml --surface api-tools \
  --interface-profile api-tools-surrogate-v1 --model-profile YOUR_PROFILE \
  --trials 1 --resume runs/r-REPLACE
```

The descriptor, mapping, scenario, model and implementation must match.
Continuation retains the original owner lock and cumulative time/turn/tool
budgets. Keep the original private directory; exports cannot be resumed.
Completed and fixed-sample release trials cannot be continued.

## Verify a deferred outcome

An agent's final response does not finish the retained trial while the independent
oracle reports pending work or cannot supply the required backend observations.
The host suspends the lease and retains the finished Inspect trajectory. Run the
same `--resume` command to check the outcome again.

For this verification-only continuation, the host validates the original lock,
reacquires the same state and reads the private oracle. It does not generate
another model response, perform model preflight, replay a public write, or reset
the target. Generated trials still regenerate and compare their native catalog.
The original deadline includes time spent waiting between invocations.

The manifest's `outcome_verification.status` is `pending`, `unavailable`, or
`terminal`. Terminal means the observations are final, not that the assertions
passed. A terminal failed backend assertion still fails the trial. Missing
observations remain gaps. Each invocation preserves the prior attempt's result
and the original Inspect logs.

The target owns background execution and callbacks. This command does not advance
the fixture clock, manufacture a callback, start a scheduler, or supply private
oracle answers to the model. If further agent interaction is required, interrupt
the trajectory before its final response and use the ordinary continuation path.
Without `--resumable`, no retained lease is promised.

## Retained state protocol

Use the existing [private Unix API control protocol](api-behavior.md).
Additionally advertise `retained-trial-v1`, `acquire_retained`, `resume` and
`suspend`. Normal trials continue to use `acquire` and `release`.

| Command | Arguments | Exact required result |
| --- | --- | --- |
| `acquire_retained` | `lease_id`, `initial_state_sha256`, `expires_at`, `initial_state` | Echo lease ID, digest and expiration; add nonempty `state_id`, `clock`, `user_profile`, `reset_completed: true`, `grants_retired: true` |
| `resume` | Original `lease_id`, `state_id`, digest and expiration | Echo those values; add `clock`, original `user_profile`, `state_preserved: true`, `grants_retired: true` |
| `suspend` | Original `lease_id`, `state_id`, `expires_at` | Echo those values; add `suspended: true`, `grants_retired: true` |
| `release` | Empty object | Existing `released: true`; retire authority and release the retained trial |

The adapter must persist the lease independently of the connection, binding it
to the full run/task/target/principal identity. Preserve business state,
idempotency records, event receipts and oracle state. Reject competing owners
and conflicting resets. `state_id` identifies the seeded state and must change
after replacement/reset. A disconnect retires outstanding authority but does
not discard or reset retained state.

Before accepting resume, atomically acquire exclusive ownership and retire
every unconsumed grant from the prior connection. Do not undo committed actions
or receipts. Preserve the original synthetic user profile. The absolute UTC
`expires_at` comes from the original host deadline and cannot be extended.
Enforce expiry and cleanup independently of the coordinator. Host acknowledgment
checks do not independently prove arbitrary adapters enforce these invariants.

The host persists intent before acquisition. A missing acquisition acknowledgment
stops without another reset and requires external inspection/cleanup. A lost
suspension acknowledgment can resume the same known state after confirmed
exclusive reacquisition. Private events must remain idempotent by stable event
ID. A lost clock-event reply permits only the clock named by that exact pending
event, followed by reconciliation with the durable scenario clock.

Lease IDs and checkpoints remain private. These acknowledgments establish
lifecycle state, not successful public business outcomes.

## Public receipt mapping

Retained responses are delivered without dispatching again. Interrupted reads
can be requested afresh. Proposals interrupted before dispatch are retired;
the agent must read current state and propose again under fresh consent.

For uncertain writes, add a `recovery` entry to the curated mapping:

```yaml
recovery:
  buy_item:
    lookup_tool: lookup_receipt
    arguments:
      key: key
```

An optional [`poll` policy](public-receipt-polling.md) waits through identity-matched
HTTP 202 receipts using a cumulative read budget, a fixed interval and the
original trial deadline. The default remains one lookup per resume.

Both tools must exist. The original tool must be a write; the lookup must be
declared read-only. Map every lookup input to a required original argument.
The lookup requires an unprojected HTTP 200 response contract returning exactly:

```json
{
  "run_id": "original-host-run-id",
  "task_id": "original-scenario-id",
  "principal": "original-test-principal",
  "operation": "orders.submit",
  "arguments_sha256": "digest-of-original-effective-arguments",
  "response": {"status": 200, "body": {"status": "completed"}}
}
```

Compute the digest with `consumer_agent_testing_tool.serialization.digest(arguments)`.
Scope the receipt to the authenticated principal/trial and persist it atomically
with the original outcome. The host matches all identity fields, validates the
original response contract, applies its projection and marks the response
reconciled. The private oracle still determines backend success. A public receipt
alone cannot make an unchanged backend pass.

Missing mappings, absent/mismatched receipts and unacknowledged responses keep
the trial stopped before further behavioral inference or mutation. Saved writes
are never retried. HTTP 202 remains provisional when the oracle reports pending
work. Recovery lookups use the same bounded, authenticated, nonredirecting
transport and consume the original wall-time budget.

## Scope

Verification uses local HTTP/Unix transports, scripted native Inspect calls and
an independent SQLite order ledger. Public pending receipts use the explicit
[polling contract](public-receipt-polling.md). Arbitrary portal/native checkpoint
restoration, other asynchronous job contracts and broader OS isolation remain
separate work.
