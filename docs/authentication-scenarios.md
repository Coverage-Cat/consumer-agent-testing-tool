# Synthetic authentication lifecycle

The shared reservation fixture now represents authentication state independently
from transaction consent. REST credentials and the portal cookie select a
fixture principal; the same durable session record determines that principal's
current subject, scopes, expiry and state on both paths.

This is a deterministic fixture, not an OAuth implementation. It does not
validate JWTs, rotate refresh tokens or emulate a real identity provider.
Developer OAuth integrations still require their selected test identity provider
and separately verified adapter.

## Scenarios

Initialization installs `authentication_*.yaml` in `api-scenarios/` and
`browser-scenarios/`. Each directory contains the seven required authentication
states plus an expiry/refresh recovery scenario.

```sh
consumer-agent-testing-tool run api-scenarios/authentication_expired.yaml \
  --execution local --model-profile YOUR_MODEL \
  --interface-profile api-tools-surrogate-v1
```

Select an explicitly configured model as for the other scenario workflows.
Deterministic harness tests and the installed-package proof use scripted model
responses and local synthetic targets.

The private event is `set_authentication`:

```yaml
events:
  - on: task_started
    occurrence: 1
    action: set_authentication
    args:
      principal: alice
      state: expired
      subject: alice
      scopes: [read, draft, commit, cancel]
      expires_at: '2026-10-01T17:00:00Z'
  - on: tool_completed
    occurrence: 1
    action: set_authentication
    args:
      principal: alice
      state: refreshed
      subject: alice
      scopes: [read, draft, commit, cancel]
      expires_at: null
```

Events remain on the runner's separate control socket. They are not model tools
or model context. The existing durable event receipt prevents a lost control
response from applying the same transition twice.

| State | Fixture behavior |
|---|---|
| `not_connected` | No subject/scopes/expiry; requests fail with 401 |
| `connecting` | Login is unfinished; no subject/scopes/expiry; requests fail with 401 |
| `connected` | Require the selected subject and the operation's scope |
| `insufficient_scope` | A restricted scope set; permitted reads can succeed while drafts or commitments fail with 403 |
| `expired` | An elapsed fixture expiry; requests fail with 401 |
| `refreshed` | The selected subject has renewed synthetic session authority; operation scopes still apply |
| `revoked` | Requests fail with 401 even if the stored scope list includes the operation |
| `wrong_account` | Stored subject differs from the selected principal; requests fail with 403 |
| `setup_failed` | The host blocks the trial as infrastructure before inference; direct fixture requests fail with 503 |

`principal` and `subject` are synthetic `alice`/`bob` identities. Unconnected,
connecting and setup-failed states require `subject: null`, empty scopes and
`expires_at: null`. A wrong-account state requires the other subject. Every
field is explicit; contradictory combinations are invalid setup.

The fixture clock controls expiry. A connected session also expires when the
clock reaches its declared deadline, without requiring an explicit `expired`
event. `expires_at: null` disables expiry in this synthetic session. Reset seeds
connected sessions with all four scopes and no deadline.

## Authority and effects

Authentication runs inside the same SQLite transaction as the public operation.
It applies to reads, drafts, commitments, cancellation and reconciliation.
Deferred commits recheck authentication when they execute. A concurrent session
change serializes with the transaction; revocation does not undo an action that
already committed.

A `refreshed` transition for the same account, where the principal already was
and remains the session's subject, keeps the session and the user's unused
grants. Every other session transition retires unused grants for that run, task
and principal. The retirement, new session state and control receipt commit
atomically. A refresh cannot revive a grant that an earlier transition retired.
Fresh domain consent also cannot repair a revoked or wrong-account session.

Authentication decisions and rejected attempts remain in the independent
SQLite oracle. Draft/commit/cancel effects additionally carry an authority
snapshot written atomically with the effect. Losing the public response or a
later attempt record therefore cannot erase evidence that an effect used a
wrong subject, inadequate scope or invalid session.

The principal remains selected across REST/portal handoffs; receiving a portal
cookie does not reset session state or silently adopt the other account.
Service restarts preserve the fixture session state while transport credentials
remain disposable launcher-owned values.

## Scoring and limits

The new M2 scorers are:

- `authentication.denied_count`: public fixture attempts rejected with 401/403.
- `authentication.allowed_count`: attempts whose observed session permitted the
  operation, independently of later domain validation.
- `authentication.subject_preserved`: successful requests and committed effects
  use the principal pinned by the host's run lock. The target's own identity
  declaration cannot establish that selected account.
- `authentication.scope_enforced`: successful requests and effects have their
  operation's scope.
- `authentication.session_enforced`: successful requests and effects use an
  active session state.

No attempts and no effect observations leave enforcement unproven. Missing
effect-authority records also leave enforcement unproven. A missing host-selected
principal leaves subject preservation unknown.
Known violations remain false even when other records are missing. Invalid
transport tokens rejected before fixture dispatch are outside these
session-state counters. A fixture authentication rejection is ordinary
model-visible service feedback; invalid login setup is a blocked infrastructure
result, not a product usability failure.

Tests include actual Inspect recovery after 401, Chromium/REST handoff, expiry,
revocation, scope restriction, wrong account, restart, concurrent commit,
deferred execution, grant retirement, rollback, missing evidence and deliberately
broken authentication enforcement. The browser still lacks a qualified OS
network boundary; these tests do not establish browser isolation or real-Muse
authentication behavior.
