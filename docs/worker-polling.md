# Worker jobs beyond one request

Worker execution can outlast an individual gateway request. Readiness advertises
`run_protocol: poll-v1`; the controller submits one `run` request with
`mode: start`, then uses `mode: poll` to observe that same trial.

The worker configuration controls negotiation:

```json
{
  "provider": "your-selected-provider",
  "gateway": ["your-explicit-gateway-command"],
  "ttl_seconds": 900,
  "call_timeout_seconds": 30,
  "run_mode": "auto",
  "poll_interval_seconds": 1.0
}
```

`auto` selects polling when the gateway advertises this protocol. A gateway
without a polling declaration retains the blocking contract. `polling` requires
the declaration and rejects missing or unknown versions before execution.
`blocking` explicitly retains the original behavior. Main CLI worker placement
uses the same lifecycle and configuration.

The invocation deadline remains independent of each request timeout. Lost start
or poll replies can be followed by observation requests within that deadline.
The controller never resends an uncertain start, resubmits credentials on a
poll, or infers successful execution from elapsed time. A poll that reports no
admitted invocation fails the run instead of starting one.

## Replies and evidence

Every reply carries the original `trial_id`. Polling replies have one of these
states:

- `running`: execution or startup still owns a kernel lease. No exit code is
  permitted in this reply.
- `completed`: the worker has a terminal receipt and has released its process
  leases. An integer exit code is required; nonzero remains failed execution.
- `not_started`: no invocation was admitted. Observation has no side effects.

Repeated starts against an admitted trial return its existing state. Repeated
polls and starts after completion return the same terminal receipt. Neither can
execute the payload again. Malformed or contradictory replies fail the
controller's protocol check. The journal records the selected mode, each
request phase, observed state, lost replies and final outcome.

Collection and cleanup remain separate from execution. The existing verified
public export is collected before destruction, including after cancellation
or failure. Reaping after a lost coordinator still requires matching gateway
configuration and the original redaction credentials when output has not yet
been collected. It does not restart the evaluated job.

## Local reference supervision

The POSIX reference gateway runs as a dedicated single-thread process. In
polling mode it admits one invocation under its control lock, holds a supervisor
lease across startup, and forks a supervisor independently of the short RPC.
The child closes the RPC streams and starts a new session. It uses the same
pinned guard code and minimal evaluated-process environment as blocking mode.
The source-only SSH transport can use the same mechanism on its selected host.

The guard retains the original wall/monotonic deadline and independent
disconnect pipe. Loss of the daemon supervisor stops the owned process group;
loss of the short start or poll RPC does not end that supervisor. If the
coordinator disappears, the job remains bounded by its original deadline.
Suspended supervision does not establish terminal quiescence: export/deletion
wait until the leases are released even if the guard has written a receipt.

Cancellation marks the job under the control lock, then releases that lock
while waiting. This lets a still-launching supervisor observe the cancellation
and record a failed start. If the startup owner disappears before any guard
started, released leases plus the consumed admission marker establish a failed
dispatch, with no possibility of a later restart. Missing receipts after a
guard started remain unconfirmed termination.

These guarantees apply to trusted reference jobs and their ordinary process
groups. They do not qualify hostile process escape, simultaneous loss of all
supervision, machine failure, or a named cloud provider's lifecycle.

Polling removes the single-request duration limit; it does not add private
checkpoint continuation or deploy runtime assets. The configured total TTL
still limits the invocation, and the current reference maximum is one hour.

[Focused verification](release-evidence/worker-polling-final-2026-10-13.xml)
passed 140 checks, including actual macOS and Linux supervision, native
API/Docker CLI worker execution, lost replies, startup cancellation and failed
admission. These checks use local fixtures and synthetic inference.
