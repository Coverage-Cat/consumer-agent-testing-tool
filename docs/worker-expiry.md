# Reference-worker expiry and supervisor loss

The local reference gateway now runs each command under an independent
process-group guard. The [original reproduction](release-evidence/worker-expiry-reproduction-2026-10-13.log)
killed the RPC supervisor and observed the task still progressing after its
deadline. The guard now stops ordinary task descendants when that supervisor
disappears, when the original deadline expires, or when cancellation is requested.

The RPC supervisor and guard hold separate kernel file leases. A terminal receipt
is written before termination, but export and deletion cannot rely on that receipt
alone: both leases must be released before the gateway accepts terminal state.
The RPC supervisor uses a completion pipe and retains ownership of the guard PID
until it has sent the process-group kill, then reaps the guard.

The guard keeps its control and lease descriptors out of the command's
environment and inherited descriptors. It runs no model loop or task scheduler.
It exists only to enforce the selected reference command's lifetime.

## Exercised failures

- **Coordinator SIGKILL:** the already running RPC/guard still enforces expiry.
  The reaper later collects diagnostics without rerunning the task.
- **RPC supervisor SIGKILL or call timeout:** the private pipe closes and the
  guard stops its own process group.
- **RPC supervisor SIGSTOP:** the guard still expires the task. Collection stays
  blocked while the suspended supervisor retains its lease; elapsed time alone
  cannot authorize deletion.
- **Guard SIGKILL:** a live RPC supervisor detects the guard's exit, kills the
  remaining group before reaping its leader, and records a failed outcome.
- **Normal completion:** the requested command's exit code is retained and
  remaining ordinary background children are stopped before terminal export.

These are actual process-signal tests, including child heartbeat observations
before and after the fault. They are separate from the prior exception-injected
journal-recovery tests.

## Trial and deadline contracts

Provisioning fixes a finite deadline within one hour and a local monotonic
cutoff. Repeating provisioning does not renew it. The guard uses the earlier
remaining cutoff; moving wall time backwards while a task runs cannot extend
its monotonic deadline.

The guard polls at bounded intervals and permits a 100 ms completion handshake
before forcing termination itself. This is an operational deadline, not a
hard-real-time scheduling guarantee. Runtime evidence records that grace, the
guard source hash, Python version, platform and `isolation: none`.

Repeated run requests retrieve the prior terminal receipt, or fail while
execution is still active. They never run the same trial again. Seeding is
immutable after a successful seed; identical requests are idempotent. Invalid
replacement archives are validated before any program file changes. An
unconfirmed interrupted seed commit requires a new trial. Bundles cannot
overwrite the reserved lifecycle files.

Destruction removes the job directory. Small private control-lock and retired-ID
markers remain so a deleted trial identity cannot be provisioned or executed
again. They contain no task artifacts. This bookkeeping is not a claim that
every byte of controller metadata has been erased.

## Scope and limits

The tests exercise macOS and the existing Linux ARM64 test VM. The standalone
probe also runs against the fresh installed macOS package. Linux-container
checks use the current workspace gateway/guard source and a resolved image
digest; they are not fresh Linux package-installation evidence.

The SSH adapter sends both gateway and guard source in its existing explicit
`python -c` program. Its program/base64-bundle layout is locally exercised;
no remote SSH host or cloud provider was contacted for this proof.

The reference gateway remains **a trusted-job process runner, not hostile-code
isolation**. Deliberate session/process-group escape, malicious changes to its
same-user control files, simultaneous loss of both supervisors, kernel failure
and power-loss durability are not qualified. Process-group expiry does not
establish browser/network isolation or a named cloud provider's independent
resource cleanup. Those release gates remain separate.

## Expiry/runtime checkpoint — 13 October 2026

Focused expiry/runtime checks passed 51 tests; the source-bound complete
regression passed 1,224 tests without skips. The standalone probe now uses a
five-second setup/execution deadline with a twenty-second workload, requires
a progressing child heartbeat, and waits through the declared deadline before
checking termination. Linux checks include deliberately delayed readiness.

The [installed artifact proof](release-evidence/runtime-final-installed-2026-10-13.json)
preserves five actual supervision cases.
[Both Linux variants](release-evidence/runtime-final-linux-2026-10-13.json)
also retain their positive controls, terminal outcomes and cleanup results.
The [original Linux proof](release-evidence/worker-expiry-linux-2026-10-13.json)
and [original macOS proof](release-evidence/worker-expiry-macos-2026-10-13.json)
remain as historical evidence.
