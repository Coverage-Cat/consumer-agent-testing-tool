# Private browser diagnostics

The browser agent receives text snapshots and reference-based actions. Developer
capture is a separate, opt-in channel. Configure it in the browser policy:

```json
{
  "diagnostics": {
    "enabled": true,
    "retain_raw_trace": true,
    "mask_selectors": ["[data-customer-summary]", "#private-canvas"],
    "max_screenshots": 100,
    "max_bytes": 52428800
  }
}
```

Add this object to the policy's existing URL, origins and request rules.
`enabled` defaults to false; `retain_raw_trace` independently defaults to false.
Raw traces require both options. Selector masks are trusted developer
configuration; the evaluated agent cannot supply or change them.

For each new snapshot generation, capture records a viewport PNG and the exact
redacted snapshot, its generation, and content hashes. A continued slice of the
same generation (`kind=snapshot` with an offset), a refused action without a
new snapshot and an open-dialog observation add no capture. Inputs, textareas,
selects, contenteditable elements and `[data-sensitive]` regions are masked
automatically, including inside frames. Additional selectors mask other page
content.
Capturing does not disable animations or change the agent's permitted actions.
Capture adds work and can change timing. Its configuration is hashed in the run
locks; comparisons with capture disabled must declare that configuration change.

The host compares accessibility snapshots immediately before and after the
screenshot. If the page or generation changed, it reports an observation gap
instead of pairing the image with an unrelated snapshot. This is sequential
capture, not an atomic view of a running page: canvas, video, animation or
purely visual changes can occur without changing the accessibility tree.

Masked screenshots are **not certified free of personal data**. Names, images,
canvas text and other content outside the masks can remain visible. Raw
Playwright traces can contain DOM, network bodies, cookies and unmasked timeline
images. Neither type is supplied to the model or included in standard exports.
Source-file attachment is disabled.

## Local review

Capture writes to `runs/browser-ID/diagnostics.private/`. The directory is
owner-only and finalized media, observation, manifest and index files have
owner-only permissions. The manifest records capture status, hashes, pairing
information and failures. The public run records only a summary and the private
manifest/index hashes; it does not embed the images or trace.

```sh
consumer-agent-testing-tool browser-diagnostics runs/browser-ID
consumer-agent-testing-tool browser-diagnostics runs/browser-ID --trace-command
```

The first command verifies the public seal, private hashes and snapshot pairing,
then reports the local HTML index. That index displays each screenshot beside
the exact emitted snapshot. Page text is HTML-escaped and the index has no script
execution or remote resources.

The second command returns an `argv` array for the existing local Playwright
Trace Viewer. Run that command to serve the verified trace on an ephemeral
loopback port. It selects the workspace Playwright package matching the
recorded backend version, uses no `npx`, downloads nothing, and does not upload
the trace to a hosted viewer. A missing/mismatched package is an error.
The viewer is a developer tool; its process is not an agent browser tool.

The local `report.html` links the private index under "Viewers and linked
evidence". It links a captured trace file, and names
`browser-diagnostics <run> --trace-command`, only when the private manifest
still matches the digest `result.json` pins. Otherwise it says the trace was
recorded as captured but is not linked. A static page cannot start the Trace
Viewer itself.

```sh
consumer-agent-testing-tool report runs/browser-ID
consumer-agent-testing-tool export runs/browser-ID --output exports/browser-ID
consumer-agent-testing-tool replay exports/browser-ID --mode playback
```

Standard exports rebuild the public result/report and redacted linked Inspect
logs, then generate a new seal. They exclude private diagnostics, raw files,
fixture downloads and host checkpoints. The exported diagnostic summary
explicitly says `excluded`, and the exported report says the diagnostics were
excluded from the export; neither implies the images were pixel-redacted.
The original run and its private evidence remain unchanged.

## Limits and failures

`max_screenshots` limits attempts and `max_bytes` limits retained PNG/ZIP media.
These are not kernel disk quotas. Raw trace intermediates can exceed the
retained-media limit before finalization; use a bounded worker filesystem where
that is required. Completed oversized media is discarded and reported as
`limit-exceeded`. JSON evidence and the HTML index are outside the media budget.

Call `await broker.stop_diagnostics()` or `await broker.close()` before
`broker.finish(...)` when embedding the Python broker. The CLI already follows
this lifecycle. A trace that was requested but never finalized cannot pass
diagnostic verification.

Capture, finalization and report errors preserve the independent execution
outcome and backend counts. The result has a separate `reporting_status`;
missing requested diagnostic evidence fails reporting instead of inventing
a successful capture. Private diagnostic errors never enter the browser tool's
model-facing response.

On orderly finalization, temporary trace files are removed. A crash may leave
intermediates in the owner-only private directory; these are not treated as a
completed trace. Retention/deletion is controlled by the developer's run
directory lifecycle. These captures do not establish browser OS isolation,
complete visual redaction, or real-Muse fidelity.
