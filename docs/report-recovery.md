# Report failure and offline recovery

Browser and consumer-surface runs preserve a sealed result before generating
HTML. A failed renderer does not erase a completed action or turn its backend
assertions into unknowns.

The result distinguishes:

| Field | Meaning |
| --- | --- |
| `execution_status` | Whether the interaction completed, failed or paused |
| `verification_status` | Execution plus the run's declared checks, before reporting; their provenance remains explicit |
| `html_report.status` | Pending, completed or failed HTML generation |
| `diagnostic_reporting_status` | Whether requested diagnostic capture finalized |
| `reporting_status` | Combined HTML and diagnostic reporting state |
| `status` | Overall result; incomplete reporting cannot pass |

Renderer failures record their exception class, not exception messages that
could contain private values. Already established backend counts remain in
`verified_service_outcome`. Browser and consumer-surface CLI execution returns
code **2** for incomplete reporting; JSON evidence preserves the execution and
verification outcomes separately.

The current renderer is `consumer-agent-testing-tool-static-v3`. Pending or
failed descriptors written by `consumer-agent-testing-tool-static-v2`,
`consumer-agent-testing-tool-static-v1` and `surface-lab-static-v1` remain
recoverable.

## Report layout

Reports are static, escaped, offline HTML built only from redacted evidence.
They put the outcome first:

1. The verdict: overall, execution, verification and reporting status, whether
   the run is a compatibility pass, the failure stage, class and reason, the
   step where the run failed or stopped, and step progress.
2. The checks, then the host capability stages of a surface run.
3. The timeline: an index of steps (number, name, kind, status, events and
   model-visible events), then one section per step with its events in
   recorded order. A step is setup, a scenario step, a model turn, a browser
   generation, a connector tool call or the final answer. What the model
   received, and browser snapshots, appear in full, redacted; other events are
   summarized with a link to their redacted payload.
4. A connector run's **Connectors** table, when it declares several
   connectors.
5. Surface views, where the run recorded them: the published surface (the
   catalog), the exposed surface (what the projection or adapter offered the
   model, its flagged changes per tool, published tools it did not expose, the
   published-to-exposed diff and visibility), the observed input by step, and
   developer diagnostics (records the model never received, the last backend
   state observed independently, and the private browser diagnostics' status).
   A view a run did not record is named as absent, never inferred.
6. Metrics.
7. Viewers and linked evidence: each agent or model-preflight sub-run report
   with its status, its Inspect logs and the `inspect view --log-dir` command
   that opens them, and, in a local run, the private browser diagnostics and a
   captured Playwright trace ([browser diagnostics](browser-diagnostics.md)).
   Exported reports say that diagnostics were excluded.
8. Observation gaps and evidence limits, then the run lock and the recorded
   result, both collapsed.

The verdict's step row reads:

- **Failing step** for a failed run, **Stopped at step** for a blocked one.
  It names the step the failure stage names, its last attempt when a resumed
  run repeated the step; otherwise the first step with a failed check (for a
  blocked run, then a blocked check, then an observation gap). The check it
  names is that step's first check with the deciding status, so a failed check
  wins over an observation gap the same step recorded. Core lanes' checks in
  `state_asserted` events count.
- **Last recorded step** when nothing identifies the step.
- Nothing when the run failed only at reporting.

The observed-input table has the columns Characters, Bytes, Tokens and
Measured. A tool result shows the host's own count of the text the model
received; Bytes reads "not recorded" when evidence redaction changed the
recorded text. A projection counts its system prompt and tool definitions
(compact JSON), a core observation its messages, tools and `tool_choice`;
anything else shows the whole recorded record, labeled so. The model's own
proposals and `model_request` pointers are not listed as input.

Core run reports (`manifest.json`) follow the same order.

## Viewing retained evidence

```sh
consumer-agent-testing-tool report runs/browser-ID --format json
consumer-agent-testing-tool report runs/browser-ID
consumer-agent-testing-tool export runs/browser-ID --output exports/browser-ID
```

The JSON command reads the verified historical result. If its HTML is pending
or failed, the HTML command creates a separate redacted report bundle at:

```text
runs/browser-ID.reports/<source-seal-sha256>/report.html
```

The copy is sealed and records its source seal, the rendering harness
fingerprint, and `export.reporting_status: completed`. It retains the source
run's historical failure status; rendering a report is not a new successful
execution. Repeated viewing reuses a matching verified copy. To create a new
rendering with another tool build, export to a new destination.

`report` on an agent sub-run of a sealed run (`<run>/agent/<id>`) never
renders it again in place, which would break the parent's seal. It verifies
the parent's seal and returns the sealed sub-run report unchanged. A sub-run
sealed without a report is refused (exit 2, category `reporting`); export the
parent run for a verified rendering.

The original result, events, seal and timestamps remain unchanged. No model,
browser action, service call or payment setup is retried. Private checkpoints,
credentials, screenshots, traces and downloads stay excluded. Parent scenario
reports validate the child reference and link to a recovered browser copy when
needed; exported parent reports link to their own public child report.

## Integrity and limits

Missing HTML is accepted only when the verified result explicitly records a
supported pending/failed renderer. A completed report that goes missing remains
an integrity failure. Historical unsealed runs are not automatically trusted.
Unknown renderer descriptors, changed evidence, filesystem links in referenced
paths, mismatched cached copies, or a source seal changing during export fail
verification. Incomplete exports never publish their destination.

Result, report and seal files are individually replaced atomically. This is not
a multi-file filesystem transaction. A process exit after the initial core seal
and before rendering leaves a verified pending report that can be viewed later.
An interruption between subsequent file replacements can leave mismatched
hashes; the verifier refuses that state rather than generating a new seal over
unverified contents. Complete storage failure cannot guarantee a saved record.

Report recovery is separate from resuming agent execution. Whole-trial
Execution continuation is a separate opt-in capability for the owned
[API/browser scenarios](scenario-continuation.md); arbitrary target and native
Inspect checkpoint restoration remain unfinished.
