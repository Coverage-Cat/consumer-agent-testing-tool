# Local behavior capabilities

These interfaces implement the [architecture recommendations](behavior-architecture.md).
They are opt-in surrogate treatments and local harness components. They do not
change the supported targets' default behavior or qualify an external assistant.

## Observations and calibration

`behavior-observation-v3.schema.json` describes a pair with `local` and `observed`
sides. Each side retains its subject/profile identity, account/session cohort
and prior-context treatment. Keep these artifacts and their sources private.
The public research summary is a separate, allowlisted view.

Completion, disposition, exposure, infrastructure validity and verdict are
independent fields. A decisive verdict requires reached exposure, healthy
infrastructure and references to hashed source artifacts. Partial operation
traces retain their existing per-dimension unknowns. Internal visibility is
never inferred from successful task output.

The calibration reader accepts versions 1, 2 and 3. A v3 cohort uses
`evidence_kind: observed` or `synthetic`. Surface-mismatched pairs must declare
`comparison: control`; they do not contribute to matched agreement or false-pass
counts. Unknown measurements remain outside those denominators.

`behavior_observations.migrate_pair` creates a new artifact with explicit
identity and assessment metadata. It refuses to overwrite the destination.
It neither modifies historical sources nor rewrites seals.
The new artifact records the original pair's path and digest.

## Actions and messages

Enable the connector/browser action journal with:

```yaml
extensions:
  behavior:
    principal: synthetic-owner
    dispatch_guard: true
    scopes:
      reservation:
        - {service: booking-connector, action: create_booking}
        - {service: "https://fixture.example", action: checkout}
```

The journal lives in the run's private directory. Its redacted events join the
sealed evidence. Scope bindings use a private HMAC key; raw arguments and
principal values are not written into those bindings.

Each separately authorized invocation receives a distinct intent ID.
The synchronous dispatch gate runs after awaited approval hooks. The host-only
`BehaviorExtension.revoke(service, action)` revokes that scope across routes.
Browser scopes use the rule's origin and rule ID; connector scopes use the
connector and tool names. The optional `scopes` map joins routes under one
revocation scope. Revoke any member to block every declared member, including
session writes. Undeclared routes retain separate scopes.

`ActionJournal` can be shared by controlled adapters. It records proposal,
authorized dispatch, transport response, independent reconciliation and effect
identity separately. `perform_action` can recover once after a declared
non-commitment read, only when `allow_recovery=True` and the grant is unchanged.
The connector lane's existing uncertainty behavior remains blocking.
Boolean reconciliation results update the journal as predicate observations,
with no invented backend transaction ID.

Delivery records preserve message/task IDs, reply links, source and timestamp
precision. Accepted, served and acknowledged are separate observations.
Dispatch-guard presence is an explicit treatment. A guard veto proves nothing
about whether an agent read a Stop, and revocation does not undo an in-flight
or committed effect.

## Session and credential treatments

Browser tasks remain isolated by default. For a bounded conversation lease:

```yaml
extensions:
  browser:
    # Supply the usual URL, origins and network rules.
    session:
      mode: conversation
      principal: synthetic-owner
      lifetime_seconds: 1800
```

Close a task before spawning its continuation. Cookies and local storage stay
in an in-memory lease keyed by run, principal and allowed origins. The lease
rejects another principal or origin scope. Expiry starts a fresh authenticated
workflow; it does not extend the lease or restore authority. `disconnect()`
clears retained state. Conversation history, indexed memory and session storage
are not part of the lease. Encrypted cross-run checkpoints are not implemented.

`CredentialJournal` records receipt, surrogate withholding/exposure, authorized
use, requested synthetic disclosure and other egress without storing values.
The content-trust extension records adapter receipt and private-fill attempts;
an attempted fill alone leaves consumption unknown.

`SyntheticCredentialFixture` generates its own values. It requires an independent
receipt before use, consumes a link only on POST, and permits an explicit
owner-requested synthetic disclosure. It cannot import a real credential.
Production credential filtering and redaction remain unchanged.

## Clocks, controls and claims

`VirtualClock` and `WallClock` expose UTC instants and monotonic elapsed time.
Existing fixture-clock scheduler events now identify their clock. `DurableSchedule`
records a due attempt before work, skips missed slots, counts effects separately
from reads, and requires independent cleanup evidence. Expiry and action-count
limits remain distinct.

`HttpAvailabilityObserver` samples both a served route and an authenticated
boundary. `AvailabilityWindow` bounds the duration and sample gap. An outage is
sticky, and a batch that encounters it persists its pause across restart.
Recovery requires a new bounded batch. Virtual time is never labeled wall time.
An observer that has not collected a sample yet does not permanently pause the
batch. The polling interval leaves room for both bounded HTTP requests.

`BehaviorCase` declares exposure prerequisites and independent controls.
`assess_case` requires control receipts from another trial.
`validate_fixture` rejects markers present in benign content and contradictory
price, cancellation or payment disclosures. Artifact readers verify source
hashes; collectors remain responsible for the truth of their annotations.

`answer.annotations` accepts exact text spans with optional `task_id`, `action_id`,
`object_id`, `at` and `phase` (`final`, `intermediate`, `quoted`, `unknown`).
Claim rules accept the same identity/time fields. Only unique, non-overlapping
complete sentences bind, so a span cannot split off a negation or hedge.
The scorer retains later corrections and cancellations in the full answer,
except statements explicitly bound to a different identity or nonfinal phase.
Intermediate and quoted statements do not assert a final outcome;
unbound scoped claims remain unjudged. Missing annotations retain legacy
lexical scoring. No semantic model is called.

## Bounded execution and publication

See the [local runner guide](../tools/behavior-sandbox/README.md).
The runner enforces one writer, one message slot per case and a hard end.
An uncertain send cannot be repeated on restart. Capability blockers close
their cases without replacement tasks. Read-only inspection never opens a writer.
Independent receipt reconciliation, outcome observation and pending cleanup
can finish after the send deadline; they cannot create another send.

The summary contains only fixed family names, aggregate dispositions/verdicts,
method and limitations. It contains no identities, exact times, hosts, prompts,
paths or raw evidence. Excluded and incomplete cases remain visible in counts;
an in-flight case is incomplete, not a capability blocker.
This does not alter sealed-run export.
