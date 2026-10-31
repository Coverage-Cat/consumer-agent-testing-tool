# Changelog

## Unreleased

## 0.4.0 - 2026-10-31

- Add synthetic umbrella and homeowners purchases for both surrogate targets.
  Independent records check review and submission, offer approval, declarations,
  secure-payment setup, carrier delays, policy retrieval and policy-support requests.
  Native and shared CLI regressions include denied and late approvals, changed
  prices, lost responses, malformed output and false completion receipts.
- Shared readiness can require a dated `protocol_revision`. The modern requirement
  checks `2026-07-28` discovery and catalog access without a tool or model call;
  legacy fallback cannot pass it. SDK versions and protocol availability remain distinct.
- Captures can accept results from several allowed tools, keeping a rotating
  intake credential current without exposing it to the model or public evidence.
- Check insurance completion and premium-collection wording in final answers.
  Add `refunded` as a separate assumed CATT outcome, so a payment or support
  request cannot substantiate a refund claim.
- Link the benchmark survey's useful behaviors to existing authorization, memory,
  scheduling, content-trust, browser and oracle suites. The new fixtures do not
  reproduce external benchmarks or certify hosted assistants.
- Update Python, Node, build and GitHub Actions dependencies and refresh notices.
  Resolve the AWS dependency constraint conflict; patch fast-uri, body-parser,
  qs, zod, FastMCP, Authlib and joserfc. The optional API validator uses a patched
  minimatch through a small compatibility adapter, which fresh initialization
  also stages. Repository and packaged lockfiles remain synchronized.

## 0.3.0 - 2026-10-30

- **Breaking:** Muse and Instinct have equal standing with no default target.
  Connector scenarios must set `emulation_target`, or callers must pass
  `--emulation-target muse` or `--emulation-target instinct`. Catalog scaffolding
  and `check --suite surrogate` require the flag. Bundled examples name their
  target, and historical evidence retains its original interpretation.
- Add explicit-selection checks to the surrogate-parity release gate and expose
  `default_target: null` in `targets --json`.
- Add opt-in behavior capabilities: neutral observation contracts, action and
  delivery journals, revocation checks, bounded browser sessions, clock and
  availability records, synthetic credential fixtures, exposure controls and
  scoped outcome claims.
- Add a durable local sandbox runner and aggregate public summaries, with
  architecture documentation and 13 generic behavior test families.
- Retain distinct source provenance and target-specific treatments. These
  capabilities test local surrogates; they do not certify a hosted assistant.
- Keep host OS and Python patch versions in release verification evidence so
  identical dependency notices do not change between the laptop and release runner.
- Patch supported dependencies: OpenTelemetry API 1.45.1, undici 8.12.3/7.30.1,
  and ip-address 10.7.5 address the reported parsing and denial-of-service advisories.

## 0.2.1 - 2026-10-22

- Sort the bundled notice archive by member name so it matches release generation.
  Original license and notice text is unchanged; the bundled SBOMs record the new
  version and lock digest. A regression check covers ordering, ZIP metadata and
  source contents.
- Includes the Instinct update below. The `v0.2.0` tag's publication stopped at
  the notice archive consistency check.

## 0.2.0 - 2026-10-22

- Muse and Instinct are supported connector surrogate targets, with a shared
  native/CLI contract, route discovery, memory, local wallet and bank-verification
  fixtures, and separate provenance. Muse-specific CLI/client treatments remain available.
- `targets --json` publishes capabilities; `check --suite surrogate` checks the
  catalog and projection for either target without calling a model or service tool.
- Indexed memory fixtures separate account isolation, future-access unlinking
  and stored-context deletion. Source-scoped stale facts obey the same lifecycle.
- A named surrogate-parity release gate requires exact cases for both targets;
  distribution verification checks distinct installed-wheel runs, readiness and exports.
- Package metadata identifies both supported surrogate targets. Inventory
  generation reads the package version, and tests check version, lock and archive
  consistency.
- Refresh dependency inventories and original notices for the locked dependency
  updates, including the Rolldown native binding.
- CI summaries report sealed setup/reporting failures as `status: error` with
  an error detail, consistent with exit code 2.
- Shared CLI reports use target-neutral provenance. Surrogate readiness detects
  escaped credentials and hides them even in schema keys. Indexed source names
  have the same constraints in the published schema and scenario loader.
- Connector run locks, CI summaries and JUnit now record the emulation
  target. Comparing older Muse evidence with a new run can report lock
  changes; published Muse scenario digests and older summary validity are
  preserved. Text presentation is also available as an assumed Muse treatment.
- Connector-only interface profiles are rejected by other execution commands
  instead of labeling a run with host behavior those commands do not implement.
- Update optional MCP Inspector to 2.11.0 and its locked MCP SDK and Vite
  dependencies to patched versions. Fresh `init` projects receive the same
  manifests as the repository.
- Pin certifi to 2026.10.19 and workflow uv to 0.13.4 to remove the emSign
  trust root affected by compromised intermediate CA keys.

## 0.1.0 - 2026-10-20

First public release.

- Consumer-agent surrogate testing for MCP connectors, MCP Apps, browsers and mapped HTTP APIs.
- Scripted, model-free trials; service-side checks; sealed evidence, exports, replay and diff.
- Task-owned streamed HTTP cleanup, with cleanup exceptions treated as test failures.
- GitHub CI, secret scanning, dependency update proposals and audited release automation.
- The MIT license excludes the Coverage Cat and CATT names and the logo; see `TRADEMARKS.md`.
- Optional Node API tools: a local stub replaces `postman-collection` and rejects Postman
  collections, and a maintained ref-parser replaces `json-schema-faker`'s old one. This removes
  Faker 5.5.3, uuid 8.3.2 and sprintf-js 1.0.3 and their advisories.

This is alpha software. Supported scope and exclusions are in the README. Release artifacts must
pass the release audit for their own commit.
