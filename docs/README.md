<p align="center">
  <img src="assets/catt-logo.svg" width="160" alt="CATT logo: an orange cartoon cat at a desk with CATT spelled in code on the monitor">
</p>

# Documentation index

Start with the [README](../README.md). Current pages describe the supported
release, `local-consumer-v1`, except the development extensions, which are
outside it. The background pages record the original specification and the
review that drove the connector lane.

## Current product

### Assistant connector surrogates (`connector-run`, `scaffold`)

- [Instinct emulation](instinct-emulation.md): primary-source research,
  the target's specification, supported treatments and gaps.
- [Shared surrogate contract](surrogate-contract.md): supported Muse/Instinct
  capabilities, selection API, indexed memory lifecycle, readiness and release gates.
- [Connector lane](connector-lane.md): connector-v1 scenarios, the
  `connector-run` flags, projections and exposure, several connectors,
  per-connector quotas, approvals and scripted-user rules, linking, client
  profiles and output-schema treatments, checks, exit codes and evidence, the
  scripted driver, `scaffold` (from a live catalog or a recorded run), and the
  loopback connector fixture.
- [Lane extensions](connector-extensions.md): how to write an `ext_<name>.py`
  extension against the stable run API, then a reference for each shipped
  extension: `clock`, `routes`, `memory`, `custom_connector`, `browser`,
  `wallet`, `scheduler`, `content_trust`, `presentation` and `user_simulator`.
- [Connector examples](../examples/connector/README.md): each shipped scenario,
  with the commands that start its fixture and run it, including one against
  the MCP project's "Everything" reference server.
- [CI templates](../examples/ci/README.md): a GitHub Actions workflow for a
  service's own repository, using `--start`, `--junit` and `--summary-json`
  ([CI outputs and services](connector-lane.md#ci-outputs-and-services)).
- [AGENTS.md](../AGENTS.md): the short guide for coding agents.

### Readiness checks (`check`)

- [Readiness checks](readiness-checks.md): `check --suite surrogate`, `muse-guidelines`,
  `poppy` and `gadget`, the `mcp-contract-2026-07-28` contract profile, and
  their behavior under `--offline`.

### Consumer MCP apps (`surface-run`)

- [Surface workflows](surface-workflows.md): the scenario contract, the scripted
  action menu agent driver, the MCP Apps host, resume, exit codes and evidence.
- [Coverage Cat sandbox checkout](coveragecat-sandbox-checkout.md): the extended
  checkout example and its recorded failure at submission.
- [Local consumer umbrella test](consumer-test.md): the older Python consumer
  helper, with its historical 6 October test results.

### Browser (`browse`, `run --surface browser`)

- [Restricted browser behavior](release-extensions.md#restricted-browser-behavior):
  the snapshot facade, actions, dialogs, tabs, network policy and `browse`
  outcomes.
- [Browser scenarios](browser-scenarios.md): browser trials against the packaged
  reservation portal, with private events, approvals and reconciliation.
- [Browser diagnostics](browser-diagnostics.md): opt-in private screenshots and
  Playwright traces.
- [Iframe startup](iframe-startup.md): how new iframes are guarded before their
  first request.

### HTTP APIs (`run --surface api-tools`, `inspect`, `check`)

- [API scenarios](api-scenarios.md): fixture API trials, events and approvals,
  the host prompt, free-text questions and saved profiles, agent mistakes and
  limits, and the final-answer scorer.
- [Developer HTTP mappings](api-mappings.md): the mapping contract, request
  headers, credential placement, inspection and contract checks.
- [Developer API behavioral trials](api-behavior.md): the private control
  adapter protocol, consent and oracle for developer APIs.
- [Developer API continuation](developer-api-continuation.md): retained leases,
  public receipts and deferred outcome verification.
- [Pending public receipts](public-receipt-polling.md): bounded receipt polling
  while an uncertain write is resumed.
- [Scenario continuation](scenario-continuation.md): `--resumable` API and
  browser trials.
- [Pending outcomes](pending-outcomes.md): why unfinished work cannot erase an
  observed violation.
- [Shared reservation fixture](reservation-fixture.md): the reference service
  behind the API and browser trials, with its fault variants.
- [Authentication scenarios](authentication-scenarios.md): synthetic session
  states, expiry, refresh, scope and subject checks.

### Evidence, replay and reports

- [Event provenance](sealed-event-provenance.md): the sealed event envelope,
  redaction scopes, export and dependency notices.
- [Report recovery](report-recovery.md): the report layout (verdict and
  failing step, per-step timeline, surface views, viewers), status fields and
  recovery when HTML rendering fails.
- [Replay and diff](release-extensions.md#replay-and-diff): sealed playback,
  contract replay, reruns and `diff`.
- [Backend verification](oracle-reporting.md): how independent oracle
  observations survive scoring, playback and export.
- [Forbidden effects across continuation](invariant-history.md): how a proven
  violation persists through resumes, exports and comparisons.
- [JSON and YAML data](data-formats.md): loading rules for configuration,
  locks and evidence.

### Measurements and decisions

- [Measurement coverage](measurement-coverage.md): complete and partial token
  and duration totals, and agent sub-run metrics.
- [Model timing](model-timing.md): model latency and incomplete request records.
- [Host measurements](host-measurements.md): dispatch, browser and approval
  timing at host boundaries.
- [Proposal validity](proposal-validity.md): argument validity measured at the
  provider boundary.
- [Statistical decisions](statistical-invariants.md): trial outcomes, exclusion
  policies, invariants and fixed-sample decision contracts.

### Model providers

- [Model providers](model-providers.md): adapters, support labels, credentials,
  preflight, OpenRouter routing, the Meta Model API and failure labels.

### Development extensions (outside `local-consumer-v1`)

- [Custom CLI](custom-cli.md): testing a developer CLI in separate sandboxed
  images.
- [Final CLI snapshots](shell-finalization.md): stopping evaluated processes
  before the CLI oracle's final snapshot.
- [Browser canary](browser-canary.md): the agent-directed forbidden-traffic
  canary, which needs the isolated browser worker.
- [Isolated browser worker](browser-worker.md): the Linux ARM64 container
  browser for `browse` and browser scenarios.
- [Isolated embedded apps](embedded-browser-worker.md): the same worker as an
  embedded-app renderer for `surface-run`.
- [Generated OpenAPI baseline](generated-api-baseline.md): FastMCP-generated MCP
  tools from an OpenAPI document.
- [Generated API continuation](generated-api-continuation.md): resuming
  generated FastMCP trials.
- [OpenAPI checks](openapi-checks.md): optional Spectral, Prism and
  Schemathesis validation.
- [Prism mocks](prism-mocks.md): stateless Prism mock targets.
- [Calibration observations](calibration-observations.md): comparing local runs
  with authorized Muse observations.
- [Local Glimmer](local-glimmer.md): local model setup and qualification.
- [Worker run](worker-run.md): `--execution worker` for `inspect`, `check`,
  `doctor` and `run`.
- [Worker polling](worker-polling.md): worker jobs that outlast one request.
- [Worker runtime](worker-runtime.md): pinned runtime archives for workers.
- [Worker continuation](worker-continuation.md): retained trials across worker
  deletion.
- [Worker Python](worker-python.md): worker Python environments from explicit
  wheel bundles.
- [Worker containers](worker-containers.md): pinned container images for
  workers.
- [Worker evidence](worker-evidence.md): collecting evidence before a worker is
  deleted.
- [Worker expiry](worker-expiry.md): reference-worker deadlines and supervisor
  loss.
- [Maritime worker](worker-maritime.md): the Maritime cloud worker adapter.

## Release and distribution

- [Release scope](release-scope.md): the `local-consumer-v1` boundary, its
  supported paths including the connector lane, excluded qualifications,
  deferred work and required gates.
- [Distribution verification](distribution.md): fresh-install checks,
  inventories and notices.
- [Qualification inputs](qualification-inputs.md): the four current release
  proofs, then the superseded full-spec checklist.
- [External qualification](external-qualification.md): receipts for live and
  Muse qualification gates outside this release.
- [Release extensions](release-extensions.md): extensions, consent, replay,
  workers and evidence gates, with development history.
- [Supplemental notices](native-notices.md): explicitly supplied notices for
  bundled binary and font payloads.
- [Native SBOM notices](native-sbom-notices.md): original notices from retained
  native SBOMs.
- [rpds-py notices](rpds-native-notices.md): notice recovery for the rpds-py
  native package.
- [Native runtime inventory](native-runtime-inventory.md): the native payload
  inventory, a gate excluded from this release.
- [Optional API notice review](dependency-notice-review.md): notices for the
  optional generated-API stack.
- [`release-gates.json`](release-gates.json): the committed release policy
  (gates, scope and exclusions). Recorded results are generated per release by
  the [release workflow](../.github/workflows/release.yml) and attached to the
  GitHub release; `release-audit` reads them from `release/`.
- [`release-evidence/`](release-evidence/): dated reviews, reproductions and
  live-run records that the pages cite. They are historical; current gate
  results are not committed.

## Background

- [Original specification](spec-v0.3.md): the 1 October 2026 specification; it
  does not define the current release scope.
- [Muse emulation review](muse-emulation-review-2026-10-15.md): the 15 October
  review of Muse-alike emulation gaps, with its roadmap and finding IDs, and an
  appended implementation-status table dated 17 October.
