# Local consumer release scope

The current product boundary is `local-consumer-v1`. This scope supersedes the
earlier full-spec release plan at the user's request. It is a smaller product,
not a claim that missing external qualifications have passed.

## Supported paths

- The experimental Instinct target in `connector-run`: native MCP as an
  assumed adapter, text presentation and the selected extensions listed in
  [the Instinct spec](instinct-emulation.md). This adds no real-Instinct
  calibration, admission or private-protocol claim. It does not qualify
  Instinct's Vault, payment rails, Files or trusted-network protocol.
- The Muse connector surrogate lane: `connector-run` with connector-v1
  scenarios against a developer's MCP server over Streamable HTTP (https, or
  plain http on loopback). It covers the `native` and `muse-cli` projections;
  `in-prompt`, `search-only` and `disconnected` exposure; several connectors in
  one scenario; the `generic-sdk` and `muse-template-unofficial-2026-10` client
  profiles; account linking with an API key or OAuth 2.1; the permission
  engine's approval cards for a scripted user; outcome checks against an oracle
  URL or the server's read tools; answer checks; per-connector call quotas and
  the output-schema treatment; the scripted driver; and `--trials` with
  pass^k. `scaffold` writes a starting scenario from a live catalog, or a
  scripted calls file from a recorded agent trial (`--from-run`). See
  [connector lane](connector-lane.md).
- The lane extensions in the package: `clock`, `routes`, `memory`,
  `custom_connector`, `browser`, `wallet`, `scheduler`, `content_trust`,
  `presentation` and `user_simulator`, with their loopback test services:
  `connector_fixture` and its variants, `rest_fixture`, and the packaged browser
  portal. `user_simulator` is exploratory: scripted runs and decision contracts
  refuse it, so its trials never become deterministic release evidence. See
  [lane extensions](connector-extensions.md).
- Readiness checks: `check --suite muse-guidelines` (its opt-in
  `--drift-check` fetches the live guidelines over the network), `check
  --suite poppy` and `check --suite gadget`. The contract lane checks MCP
  targets with the per-revision profiles `mcp-contract-2025-11-25` (the
  default), `mcp-contract-2025-06-18`, `mcp-contract-2025-03-26` and
  `mcp-contract-2026-07-28`. See [readiness checks](readiness-checks.md).
- Local scripted or model-driven consumer MCP flows, including embedded MCP
  apps, app callbacks, form defaults, private host arguments and service checks.
  This is generic MCP Apps host evidence, not Muse evidence.
- Native browser interaction through the restricted Playwright MCP interface.
- Explicitly mapped HTTP APIs, synthetic fixtures, asynchronous operations and
  continuation of retained trials.
- Transaction-specific consent, independent backend assertions, redacted
  evidence, sealed playback, replay/diff, and measurement coverage.
- Remote inference configuration, including OpenRouter's explicit routing and
  ZDR settings. Live behavior of every provider/model is not a release claim.

The connector lane is a labeled Muse-like surrogate. Each Muse-specific
behavior carries a basis label, `documented`, `unofficial`, `assumed` or
`unknown`. The host records its labels in `run_lock.bases`; each extension
records its own in its run-lock entry or in `metrics.extensions.<name>` and its
events, and each run's `evidence_limits` says where. Unofficial sources are
unlicensed mirrors; the lane cites them, pinned by commit, and does not copy
them. No connector, readiness or extension result is Muse certification or a
claim of equivalence to Muse.

The browser's reference, origin, request and consent controls remain in place.
Testing those controls and normal browser behavior does not claim isolation
from the operating system or arbitrary hostile browser exploits.

## Excluded qualifications and development extensions

The keys in parentheses are the `EXCLUDED` entries in `release_scope.py`, which
`release-audit` reports. The developer CLI lane has no such key: the CLI
offers `--surface cli-skill` only after `--development-extensions`.

| Area | Release decision |
| --- | --- |
| Real-Muse calibration (`muse-calibration`, `calibration-analysis`) | No real-Muse account, observation cohort or equivalence claim; historical observation analysis is a development extension |
| Cloud worker lifecycle (`cloud-worker-lifecycle`) | Local execution only; no provision/run/destroy or independent cloud expiry claim |
| Local Glimmer (`local-glimmer-offline`) | No weights, tokenizer, serving process, memory budget or offline model qualification |
| Browser OS isolation (`browser-isolation`) | No required VM/container, isolation canary matrix or live-model escape attempts |
| Bedrock authentication (`bedrock-bearer`, `bedrock-iam`) | Previously deferred live qualification stays outside this release |
| Native host/image inventory (`native-runtime-inventory`) | OS, browser binaries, model artifacts and worker images are not shipped |
| Generated OpenAPI validators/mocks (`generated-openapi`) | Prism, Schemathesis and FastMCP generation remain development-only |
| Developer CLI lane, `run --surface cli-skill` | Development extension: a developer's own CLI in Docker images, with an oracle that returns the reservation fixture's snapshot schema. The supported CLI projection is `connector-run --projection muse-cli` |

Default CLI entry points reject excluded execution before provider or worker
dispatch. `--development-extensions` deliberately opts into the retained
experiments. Their source, synthetic tests and historical records remain useful,
but are not supported release features. Container-based shell execution is also
development coverage; its platform tests are not part of this consumer release.
The connector lane needs neither Docker nor a development flag.

The Python `development-extensions` dependency group replaces the old `api`,
`local` and `bedrock` installation extras. The supported optional inference
extra is `openrouter`; it also supplies the OpenAI SDK that the `meta` adapter
uses. Default initialization stages only the protocol and browser Node
manifests, the mapped-API example, and the API and browser scenarios the
supported CLI can run. The hostile browser canary, the CLI/skill scenarios and
templates for providers without a live adapter require `--development-extensions`
before `init`. `init` does not stage the connector examples: they are in the
source tree and the source archive under `examples/connector/`, and the
connector-v1 schema is in `schemas/` and in the package data.

## Deferred and not covered

These limits apply inside the supported paths. The
[implementation status](muse-emulation-review-2026-10-15.md#implementation-status-17-october-2026)
of the 15 October review lists every finding and roadmap item.

| Area | Status on 17 October 2026 |
| --- | --- |
| Live model qualification | No live Meta Model API, Muse Spark 1.3 or Muse Wonder run is recorded; the `meta` adapter is tested against local fixtures. Live Bedrock stays deferred |
| MCP conformance | The upstream suite stays pinned at 0.1.16, which defines server scenarios for 2025-06-18 and 2025-11-25 only; contract runs at other revisions, 2026-07-28 included, report upstream conformance as unsupported and not required. Pinning the 0.2.0-alpha line is deferred as a release decision |
| Connector runs | They do not resume. In the `browser` extension, a takeover the agent requests is handed back only by the scripted user, when `takeover.handback` is `on-steer`; there is no person or CLI handback, user-initiated takeover is not emulated, and each task starts with a fresh browser context |
| Connector approvals | Allow for this site is offered only on browser task write cards and lasts one trial; connector and checkout cards never offer it. A card that gets no answer is modeled as `no_response`, with no wall-clock timeout |
| Custom connectors | HTTP APIs with JSON bodies only; no sandboxed shell and no MCP URL as input. An OAuth token comes from the coordinator environment; the authorization-code flow is not run |
| Wallet | Single-use cards from the loopback fixture's own issuer, which can verify one billing postal code (`avs`); no Stripe test mode or `stripe/link-cli` |
| Background work | Third-party push has basis `unknown`; the scheduler polls instead. Rate limits and conditional reads are MCP tool results, not HTTP 429 or 304 |
| Older lanes | `run --surface api-tools`, `run --surface browser` and mapped APIs keep single-use approve-or-deny consent and their reservation vocabulary. Their `ask_user` is free text, and API and browser scenarios may give their own saved profile, but mapped APIs take persona names only, and a stale saved profile is scored only through `backend.read_performed`. The content-trust suite runs only in the connector lane |
| Embedded MCP apps | See [What the MCP Apps host implements](../README.md#what-the-mcp-apps-host-implements) for the host features that are still missing |

## Distribution resolution

All five unresolved project notices in the previous installation were
transitive dependencies of the optional Prism/http-spec stack:
`@stoplight/json-schema-merge-allof`, `fnv-plus`, `parse-multipart-data`,
`pretty-data`, and `proxy-agent-negotiate`.

The supported release does not install that stack. The fresh installation
exports only core/OpenRouter Python dependencies and installs the two consumer
Node lockfiles. Its generated project-level inventory must have no missing
original notices. The five old gaps remain historical facts; no substitute
copyright statements have been invented and no missing notice has been
relabeled as recovered.

The wheel contains project code and notices, not installed `node_modules`,
Python dependencies, browser runtimes or worker images. The source archive
retains development source and manifests. Neither artifact turns those
development dependencies into a qualified installation.

Native payload observations remain available with their original gaps.
The distribution gate verifies project notices and the actual supported
dependency installation; it does not claim a complete linked-library review
of the operating system or externally supplied runtimes.

## Gates and evidence

Only regression, distribution, transactional consent and replay/diff are
required. `release-audit` reports the scope and excluded capabilities explicitly.
It never turns an excluded gate into a pass. Source changes invalidate old
receipts; hashes, required checks and expiry still apply.

The default pytest collection is the local release collection. Development
test modules and isolated-only parameter variants are explicitly excluded in
`regression_scope.py`, not silently skipped. Native variants still run.
`pytest --development-extensions` collects the broader historical experiments.

The connector lane's, extensions' and readiness suites' tests are in the
default collection, so the regression gate covers them. In
`scripts/record_release_tests.py`, the transactional-consent gate also names
five tests of the connector lane's permission engine
(`tests/test_permissions_engine.py`), and the replay/diff gate names
`test_report_export_and_diff_work_on_connector_runs`. No extension or
readiness test is a named gate test. The distribution verifier's packaged
workflows run one connector-lane trial from the installed wheel: the scripted
`fixture-booking.yaml` quickstart against the packaged fixture, then playback
and a redacted export (`connector_lane_verified` in `dist/verification.json`).
They exercise no lane extension other than the built-in `clock`, and no
readiness suite.

The distribution verifier uses a fresh environment and fixed workspace caches,
with external networking denied during verification. It checks the installed
wheel, source archive, dependency content, notices, protocol, mapped HTTP,
native browser, embedded MCP UI, the scripted connector lane, consent,
continuation, replay and export.
No external provider, production quote, real payment, or cloud resource is used.

The retained verification is platform-specific: macOS with the configured
workspace Node and Chromium installations. Additional platforms would need
their own installation proof before being advertised as qualified.
