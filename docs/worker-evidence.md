# Worker evidence before deletion

Temporary worker runs now persist execution replies and collected diagnostics
before deleting the worker. Previously, a lost execution reply skipped export
entirely; a coordinator crash during deletion could also lose an export that
existed only in memory. The [reproduction](release-evidence/worker-export-reproduction-2026-10-13.log)
ran a real local subprocess, discarded its execution reply and observed
confirmed deletion with no saved logs.

A second [reproduction using the actual evidence writer](release-evidence/worker-run-export-reproduction-2026-10-13.log)
found that the former text-file allowlist omitted run manifests and linked
artifacts while reporting complete collection. The reference gateway now builds
complete public run exports using the shared redaction builders. The coordinator
verifies and saves manifests, notices, event artifacts, linked Inspect logs and
sealed browser/surface evidence before requesting deletion.

`worker.json` records execution, collection and cleanup separately:

- `execution` preserves a received integer exit code. A missing reply remains
  unknown; collected output does not invent successful execution.
- `collection` is `pending`, `complete`, `incomplete` or `failed`. The journal
  stores redacted `artifacts` and their `artifacts_sha256` before requesting
  destruction. When `artifacts.run_archive` is present, its file inventory covers
  the separately saved `evidence/runs/` tree.
- `cleanup` records the gateway's explicit destruction confirmation.
  Successful cleanup cannot turn failed execution or incomplete collection into
  a completed run.

Each lifecycle request and reply is written atomically. Failure or cancellation
triggers a cancellation request, collection and destruction. An export failure
is recorded before cleanup continues; this avoids retaining a remote resource
indefinitely while making the lost-evidence outcome explicit.

The periodic reaper uses the same collection sequence. If a prior complete
export with a matching digest is already saved, recovery preserves it and
revalidates its collected run files and links. It does not repeat execution or
depend on a deleted worker. Otherwise it attempts
collection before destruction. Recovery attempts other eligible workers even
when one reports failed cleanup or collection, then reports the failure.
Confirmed deletion with incomplete collection is a terminal evidence gap;
reaping cannot reconstruct deleted files.

The [main CLI worker path](worker-run.md) also records the names and digests of
selected coordinator credentials needed for redaction. A recovery export
requires the original values in the recovery environment. Missing or rotated
values fail collection before raw worker output is requested; cleanup still
proceeds with an explicit evidence gap. An already verified public export can
be reused without those credentials. Use the same `--output` directory for
execution and `worker reap` when the journal is outside the default `runs/`.

## Gateway contract

An `export` reply must include the matching `trial_id`, a `files` object mapping
names to UTF-8 text, a boolean `complete`, and an `issues` list of strings.
`complete: true` requires an empty issues list. Gateways that previously returned
only a file map must supply this explicit acknowledgement. An empty file map
is permitted for a gateway whose declared collection scope contains no files;
the coordinator trusts that gateway's declared collection scope. A legacy
third-party gateway returning only text diagnostics does not thereby qualify
complete Consumer Agent Testing Tool run exports.

The reference gateway also supplies `run_archive` when its job has a `runs/`
directory. Its `consumer-agent-testing-tool-worker-runs-v1` metadata contains archive SHA-256 and
size, each public file's SHA-256 and size, run roots/formats, and exporter source
identities. Collection requires the same exporter implementation on the
coordinator and worker. The worker Python must have the pinned Consumer Agent Testing Tool
installation available; this also applies to an explicitly selected SSH host.
A missing installation, unknown run layout or invalid artifact produces
incomplete collection, with the process diagnostics retained.

The coordinator requests archive chunks through the same `export` operation,
with `archive_sha256` and an aligned `offset`. Replies bind the trial, archive
and offset and carry `data_base64`. Each chunk contains at most 128 KiB; it is
never an unredacted run or an arbitrary file-read request. The worker caches one
immutable redacted archive so interrupted collection can retry after execution
expiry. Destruction removes that snapshot and any interrupted build directories.

The source and exported trees are bounded to 4,096 entries and 64 MiB; the
uncompressed ZIP transport is also limited to 64 MiB. Unsafe file types and
links fail collection. The receiver rejects path escapes, extra or duplicate
archive members, invalid lengths, changed content and missing linked artifacts.
It verifies the staged tree before atomically publishing `evidence/`. A failed
transfer keeps the text diagnostics and marks collection failed before cleanup;
it does not publish a partially verified run. Private checkpoints, browser
pixels and raw traces are excluded by the existing public-export builders.

Read-only terminal export must remain available after execution expiry.
The reference gateway refuses export until its process leases and terminal
receipt confirm that execution stopped.
It collects stdout, stderr and the exit record independently of the run archive.
Missing files, links, nonregular files, invalid UTF-8 and truncation produce an
incomplete result with retained partial diagnostics. Each diagnostic is bounded
to 256 KiB and their combined source content to 512 KiB. These are collection
bounds, not worker disk quotas or a complete adversarial filesystem boundary.

The subprocess exit status remains separate from the run's recorded verification
outcome. A worker process can exit zero after writing a failed scenario; the
export preserves that failed manifest and its positive violation counts.
Collection success is neither behavioral success nor release qualification.

Open `runs/worker-<trial>/evidence/runs/<run>/report.html` to inspect a collected
run. Its manifest and public artifacts remain next to that report; linked
Inspect logs can be opened with the existing Inspect viewer. `worker.json`
records transport/cleanup outcomes and the inventory needed to verify those
separately stored files.

This change verifies the local reference and coordinator protocol. It does not
qualify a cloud provider, independent cloud expiry, arbitrary remote work
finality, or external export authenticity. Journals are local integrity records,
not signatures from an independent authority.
[Reference-worker expiry](worker-expiry.md) now adds a separate process-group
guard and actual process-signal tests. Evidence recovery alone did not supply
that behavior. The reference runner remains outside hostile-code and live cloud
qualification.

## Verification

`tests/test_worker_evidence.py` exercises actual subprocess execution, lost
replies, execution expiry, cancelled execution paths, coordinator interruptions,
reaping, redaction and incomplete diagnostics. Faulty gateway responses test
typed completeness and trial identity. The fresh-install smoke runs the
installed gateway through success, lost execution replies and uncertain deletion.
These deterministic checks do not satisfy the live cloud-worker release gate.
Coordinator interruption tests inject an uncaught exception at the deletion or
export boundary and recover from the saved journal. `tests/test_worker_run_export.py`
adds actual evidence writers, a real Inspect log from a scripted model, binary
notices, sealed surface/browser links, large artifacts, corruption, transfer
interruption and recovery after deletion. The installed-wheel smoke now writes
and collects actual Consumer Agent Testing Tool runs with their notices and linked artifacts.
These tests do not simulate host power loss or qualify disk durability after a
machine failure.
