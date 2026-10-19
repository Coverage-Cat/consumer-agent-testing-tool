# Guard iframe requests before parsing starts

A complete regression exposed an intermittent embedded-app failure: the first
redirect response reached the browser, but its allowed destination was never
requested through the host broker. The run retained 1,523 passes and one
failure, with unchanged source and both Docker lanes enabled.

The [reproduction](release-evidence/iframe-startup-reproduction-2026-10-14.json)
delayed iframe interceptor installation by 100 ms. The app began fetching while
its HTML was parsed, before installation completed. Waiting for installation
inside the request handler was too late to reliably intercept that request's
redirect chain. Ten ordinary repetitions passed; the delayed case reproduced
the failure. The original failed run is retained.

The host now uses Chromium's private target protocol to pause new separate
iframe targets. It installs request/response interception and recursive child
attachment before allowing the iframe to execute. Same-process frames share
their parent's interceptor. Detached targets are retired; startup or protocol
failures close the context. An already-running, unguarded child is not silently
accepted.

The pinned Chromium protocol's non-flat child-session transport is confined
to the trusted host. It adds no agent tools and does not change destination
rules, redirect write denial, one-use grants or reconciliation. Unsupported
protocol behavior fails rather than falling back to an unguarded browser.
This is an implementation fix, not live-model or platform isolation
qualification; native rendering retains its existing limits.

The focused checks passed **70 browser cases**, including delayed ordinary
and nested iframes with and without write authority. The delay receipt confirms
that each separate iframe received the timing treatment. Another **9 protocol
and click checks** passed, covering startup ordering, errors, disconnects and
single-click behavior.

- [Browser checks](release-evidence/iframe-startup-focused-2026-10-14.xml)
- [Protocol checks](release-evidence/iframe-startup-protocol-2026-10-14.xml)
- [Original failed regression](release-evidence/qualification-audit-regression-2026-10-14.run.json)

Installed browser-host modules match the source bytes.
Four local gates pass; the eight external/review gates remain unverified.
