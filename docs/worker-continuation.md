# Private worker continuation

API and browser scenarios can opt into retained state with the ordinary worker
selector:

```sh
consumer-agent-testing-tool run scenarios/example.yaml --surface api-tools \
  --config consumer-agent-testing-tool.yaml --execution worker --trials 1 --resumable
```

After the coordinator stops, the controller collects public evidence and a
separate private checkpoint for each retained run, then destroys the worker.
The worker journal reports their collection and cleanup outcomes separately.
Raw checkpoints are never included in the public evidence archive.

Resume one collected checkpoint with the same scenario and configuration:

```sh
consumer-agent-testing-tool run scenarios/example.yaml --surface api-tools \
  --config consumer-agent-testing-tool.yaml --execution worker --trials 1 \
  --resume runs/worker-WORKER_ID/private/r-RUN_ID
```

Use the actual worker and run directory names reported by the previous
invocation. Public exports and an original local-run directory are not worker
checkpoints. A completed trial, expired deadline, blocked continuation, changed
scenario/model/profile, or changed execution configuration cannot be resumed.
Fixed-sample decision contracts cannot acquire an extra continuation attempt.

The new worker restores the existing Inspect history, private scenario state,
backend receipts, browser storage and evidence before starting the fixed
coordinator. It keeps the original run identity, deadline and cumulative
budgets. Each attempt adds its worker provenance to the same run. Bundled API
mapping, skill and OpenAPI paths are compared by their logical workspace paths
and pinned contents, so a different job directory alone does not change the
trial. Existing API lease and uncertain-write reconciliation rules still apply.

## Transfer and admission

Both ends must negotiate `checkpoint-chunks-v1`. A gateway can export retained
state only after proving the coordinator stopped. Transfers have bounded file
inventories and archive sizes, per-file hashes and an archive hash. Restoration
rejects traversal, links, special files, conflicting retries and mismatched
pins. Identical upload retries and commit recovery are supported; an incomplete
upload is not installed as a run.

Admission is recorded before the first start request. A lost start
acknowledgement causes polling, not another start. The controller serializes
ownership of the selected checkpoint and records consumed admissions in
`.consumer-agent-testing-tool-worker-claims/` at the workspace root. Restoring a backup copy of a
checkpoint within that workspace cannot reset its admission. Keep this
directory when recovering controller state.

This is a workspace-local guard, not distributed exactly-once execution:
copying state into an independent workspace or deleting admission records can
remove that protection. An uncertain admitted start must be investigated or
continued from the subsequent worker's collected checkpoint. Do not clear
admission records to retry it.

## Private data and deployment limits

Retention is explicit. Private capsules contain raw conversation history,
service receipts, browser state and other trial data. They use private directory
and file permissions but are not encrypted at rest. Protect the workspace and
use a trusted worker transport. Public export redaction does not sanitize these
private capsules. Selected provider credentials are supplied separately for
each invocation rather than becoming deployment inputs.

Python, adapter packages, container resources, model endpoints and developer
API control services must remain available on the selected worker. A retained
developer API lease does not migrate its external service or Unix socket.
Select that socket explicitly with `execution.worker.api_control_sockets`;
the exact selection is pinned to the execution configuration. Permissions,
ownership and symlink checks also apply outside the job directory.
Pinned browser and Node assets can be redeployed through the existing runtime
archive mechanism. These facilities do not provision a cloud provider or
establish hostile-worker isolation.

Reaping can retry an incomplete collection while the stopped job is still
available. Cleanup is attempted even if collection fails; a destroyed worker
with incomplete collection is not a recoverable checkpoint. Completed local
capsules can be verified without retrieving raw state again.

## Verification

The [current complete regression](release-evidence/worker-checkpoint-regression-2026-10-13.xml)
includes native SDK/Inspect, curated and generated API leases, and deployed
Chromium continuation. The
[installed-wheel proof](release-evidence/worker-checkpoint-installed-2026-10-13.json)
also verifies collection and deletion across two actual worker invocations.
These use synthetic local services and do not qualify hosted models, cloud
provisioning, hostile-worker isolation or real-Muse fidelity.
