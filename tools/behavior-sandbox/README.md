# Bounded local behavior runner

This wrapper composes the existing scripted connector runner. It makes no model
request. Its convenience adapter accepts loopback URLs and the `clock`,
`behavior` and `content_trust` extensions. Start fixtures separately using the
connector guide.

```sh
uv run python tools/behavior-sandbox/run.py run reports/batch.sqlite --manifest batch.json
uv run python tools/behavior-sandbox/run.py inspect reports/batch.sqlite
uv run python tools/behavior-sandbox/run.py summary reports/batch.sqlite
```

The manifest contains `batch` (the `behavior-batch-v1` schema) and `executions`,
a map from case ID to `scenario` and `script` paths relative to the manifest.
An optional `variables` map supplies scenario substitutions such as `PORT`.
The batch records a future `ends_at`, an ID, and cases with `id`, `family`,
`revision`, `fixture_revision` and `capabilities: ["scripted", "connector"]`.
The paths use the existing connector YAML and calls-file formats.

Each case has one dispatch slot. A crash during a send leaves it uncertain;
restarting does not resend it. The Python API can reconcile an independent
delivery receipt before observing the outcome. Completed or closed cases never
send again. Read-only reconciliation and pending cleanup can finish after the
send deadline. Inspect opens SQLite read-only and does not create a writer.

The Python `SandboxRunner` also accepts adapters implementing `provision`,
`ready`, `send`, `observe`, and `close`, with an explicit `capabilities` set.
Adapters must use isolated fixtures, return independent observation records,
and avoid hidden retries. `perform_action` in `behavior_journal` handles a
separate action's lost response and permits one recovery only after an
independent non-commitment read under an unchanged grant.

The connector adapter retains the sealed run as private assessment evidence.
It leaves the behavior verdict inconclusive until a collector supplies
case-specific exposure and control receipts. Passing connector checks alone
does not establish those prerequisites. `run` returns 3 for inconclusive or
blocked cases, 1 for contradicted behavior, 2 for setup/storage errors and 0
only when every behavior verdict is supported. `inspect` and `summary` return
0 when their read completes.

`summary` emits fixed family names, counts, method and limitations. It omits
case IDs, paths, identities, raw text and times. This view is separate from
CATT's faithful sealed-run export.
