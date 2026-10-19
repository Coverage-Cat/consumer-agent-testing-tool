# Shared reservation fixture

The M2 reference service exposes REST and a browser portal over the same
reservation, inventory and idempotency logic used by the CLI/MCP fixture.
`ReservationFixture` extends the existing `Fixture`; it adds grants, confirmation,
cancellation, fixture-time draft expiration and fault controls. The older
`TransactionFixture` helper now delegates to this service instead of maintaining
a second reservation database model.

This is a synthetic local target. It binds only to loopback. Its separate Unix
control socket belongs to the trusted test runner. Neither the portal nor the
MCP catalog exposes reset, grant issuance, time advancement or the state oracle.
It is not an OS isolation boundary or a production authentication service.

## Start and seed

From this workspace:

```sh
.venv/bin/python -m consumer_agent_testing_tool.reservation_http --run-id local-reservation-proof
```

The launcher prints the loopback origin, control socket, and disposable API
credentials for Alice and Bob. Keep those credentials in the host, not a model
prompt. Database and control paths default to `.cache/reservations.sqlite` and
`.cache/reservations.sock`; both can be set explicitly within the workspace.
The database and socket have mode 0600.

In another terminal, seed explicitly:

```sh
.venv/bin/python -m consumer_agent_testing_tool.reservation_http \
  --control-request '{"operation":"reset","clock":"2026-10-01T17:00:00Z"}'
```

Restarts do not reset stored state. An existing control socket is not overwritten.
After a crash, verify that the old fixture stopped before removing its stale
socket. Fixture time is independent of wall time and cannot move backwards
except through an explicit reset.

The `/portal` page starts a disposable HTTP-only, same-site session for the
principal selected by `--principal alice|bob`. That identity is fixed by the
launcher. REST clients use the printed principal-specific bearer credential.
The portal calls the same REST handlers as API clients.

## Public operation contract

| Method and path | Behavior |
|---|---|
| `GET /slots` | List slot price and availability |
| `GET /slots/{id}` | Read a slot and its revision |
| `POST /reservations` | Create a nonbinding draft with `slot_id`, `contact`, `idempotency_key` |
| `GET /reservations/{id}` | Read an owned reservation |
| `PATCH /reservations/{id}` | Revise contact using `expected_revision` and `idempotency_key` |
| `GET /operations/{idempotency_key}` | Read this principal/task's pending or terminal outcomes |
| `POST /operations/{idempotency_key}/cancel` | Stop a pending operation; body names its `operation` |
| `POST /reservations/{id}/confirm` | Atomically consume a grant and inventory, then confirm |
| `POST /reservations/{id}/cancel` | Consume a fresh cancellation grant and restore inventory once |

Confirm/cancel bodies contain `expected_revision`, `amount_minor`, `currency`,
and `idempotency_key`. The reservation ID comes from the URL. Amounts are safe
integer minor units; the fixture currency is USD. JSON requests reject duplicate
keys, extra operation arguments, nonfinite numbers and invalid types.

Grant issuance happens only on the private control socket. A grant request uses
`operation: grant`, `principal`, `target_operation: reservations.confirm` (or
`reservations.cancel`), `arguments` including the reservation `id`, an explicit
`decision: approve_once`, and a timezone-qualified fixture `expires_at`.
The service adapter looks up the matching staged grant privately; page scripts
and model tools never receive its identifier or decide to issue it.

Grants bind run, task, principal, destination, operation, object, schema,
effective arguments, revision, amount and currency. Consumption, inventory,
reservation state, idempotency outcome and effect record commit together.
The same operation/key/arguments return the recorded outcome; changed arguments
with that key return 409. Different principals and runs have separate scopes.
Only one competing confirmation can consume a slot.

## MCP handoff

The existing MCP server's default catalog remains read/draft-only. Opt into
the transactional reference surface explicitly, pointing at the same database
and run ID without `--seed`:

```sh
.venv/bin/python -m consumer_agent_testing_tool.fixture_mcp \
  --db .cache/reservations.sqlite --run-id local-reservation-proof --transactional
```

This adds `reservations_confirm`, `reservations_cancel` and `operations_cancel` to its native SDK
catalog. Their arguments include `id`; grants still come from the private
control socket. The fixture MCP principal is Alice. A draft created through
this catalog can be confirmed through REST and cancelled through MCP without
copying or translating state.

## Delayed operations and cancellation

Before submission, the runner may schedule a fault with `fault: defer`,
`target_operation`, `key`, `principal` and a timezone-qualified `ready_at`.
The matching confirmation or cancellation is then accepted as a durable
application-level job. REST returns HTTP 202 with `status: pending`, the
operation, key, task and due time. MCP returns that same result object.
No inventory or reservation commitment occurs at this point.

Polling `/operations/{key}` is read-only. Only private `advance` control calls
complete due jobs. Time advancement processes jobs in timestamp order, with
stable principal/operation/key ordering for ties. It rechecks the original
grant, current revision, total, availability and expiration at processing time.
Revocation, changed state or an expired grant produces a terminal `failed`
operation with zero commit effects. Issuing another grant does not silently
replace the authority stored on an already pending job.

Cancellation through `/operations/{key}/cancel` or `operations_cancel` is
scoped to the pending operation's run, principal and task. The request names
`reservations.confirm` or `reservations.cancel`. Cancellation and completion
use the same SQLite transaction lock:

- Cancellation first: the job becomes `cancelled`, its unused grant is revoked,
  and later time advancement cannot commit it.
- Completion first: cancellation returns `cancelled: false`, `status: completed`
  and the recorded result. Cancelling that reservation requires a separate
  domain cancellation with a fresh grant.
- Missing operation: the service reports that cancellation is unconfirmed.

Successful stop requests also store a separate command receipt in the same
transaction as the job transition and grant revocation. `GET /operations/{key}`
returns it with `operation: operations.cancel` and the digest of the effective
stop arguments. Its `result` preserves the original `cancelled` flag and job
state. Receipts survive restart, are scoped by run/task/principal/requested
operation/key, and are removed on reset. Stopping confirmation and stopping
cancellation under the same key have distinct argument digests.

An uncertain stop request can therefore be recovered through a read without
repeating the write. The job state alone is not a receipt for the stop request.
Storage failure while recording the receipt rolls back the job transition,
grant revocation and lifecycle event together. A receipt is still a public
service claim; assertions use the separate state/effect oracle.

Retries of a cancelled or failed job return that terminal outcome. A new attempt
needs a new idempotency key and grant. Duplicate pending submissions create one
job. Pending state survives service-process restarts; fixture credentials are
reissued by the new launcher and remain host-owned. Storage failures roll back
clock/state/grant changes and leave the job pending for a later retry.

The portal displays operation status separately from reservation status. It
offers reconciliation and stopping a pending operation, keeps unresolved writes
disabled after a lost response, and gives a newly requested action a fresh key
after a terminal cancellation or failure. Refreshing the reservation does not
convert a cancelled operation into a completed one.

These jobs are driven by fixture time; no autonomous assistant scheduler or
MCP task-protocol extension is implemented. Full scenario event/approval-hook
integration remains separate work. Response-loss injection currently exercises
the immediate HTTP commit path; loss of a deferred acknowledgement/callback is
not established by that proof.

## Controls and evidence

Runner controls are `reset`, `snapshot`, `grant`, `revoke`, `advance`,
`change_total`, and `schedule_fault`. `change_total` updates the selected slot
and its uncommitted drafts, incrementing revisions. `advance` expires drafts
after their 30-minute lifetime and records expiration effects. A grant's
expiration is checked against that same fixture clock.

`schedule_fault` accepts `target_operation`, `key`, and
`fault: response_lost`. The HTTP adapter then closes the connection after the
selected transaction commits. Operation lookup recovers its outcome; repeating
the identical request creates no additional effect.

Intentionally broken `--variant` values include `false_success` for drafts,
`false_confirm`, `stale_grant`, `duplicate_confirm` and `false_operation_cancel`. Their apparently
successful public responses are rejected by independent state, authorization
or duplicate-effect assertions. These are negative fixtures, not accepted
integration outcomes. The `transient_error` variant answers the first draft
creation with HTTP 429 and `Retry-After: 1` and accepts an identical retry; API
trials return that response to the model. `recovery_success` measures the retry
when the scenario sets `evaluation.recovery: transient-draft-retry`.

The control snapshot reads reservation, inventory, grants and effect records
within one database read transaction. Authorization metrics require a consumed,
matching grant associated with exactly one effect. Browser success text does
not substitute for that oracle. Browser network approval and the target's
domain grant are separately enforced.

```sh
.venv/bin/pytest tests/test_reservation_fixture.py tests/test_reservation_http.py \
  --basetemp=.cache/pytest-reservations
```

The suite includes native MCP/REST handoffs and a real Chromium portal proof:
create a draft, reject an unapproved confirmation, stage a private grant,
lose the HTTP response after commit, reconcile, and verify exactly one effect
through the Unix oracle. Deferred variants also verify completion after polling
and cancellation before commit. Separate tests exercise REST submission/MCP
cancellation, a real service-process restart, cancellation/completion races,
and rollback after storage failure. These are deterministic fixtures, not live
inference or real-Muse calibration. The full scenario event contract and
Inspect-driven long-task integration remain open.
