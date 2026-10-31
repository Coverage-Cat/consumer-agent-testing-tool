# Supported surrogate targets

Muse and Instinct are supported targets of the same connector testing contract,
`connector-surrogate-v1`. Neither is the default; scenarios and shared readiness
checks require an explicit target. Support means that CATT's
implementation is tested and packaged for both targets. Neither target is
calibrated against its hosted assistant, and no result certifies compatibility
or admission.

## Selection and public API

```sh
uv run consumer-agent-testing-tool targets
uv run consumer-agent-testing-tool targets --json
uv run consumer-agent-testing-tool connector-run scenario.yaml \
  --emulation-target instinct --projection cli --driver scripted --script calls.json
```

The Python API is
`consumer_agent_testing_tool.emulation.surrogate_contract("muse" | "instinct")`.
It returns an independent JSON-compatible object with `version`, `id`,
`emulation_target`, `status`, `fidelity`, `shared`, `additional_treatments`,
`exploratory_extensions` and `limits`. `targets --json` wraps both objects in
`{"schema_version": 1, "default_target": null, "targets": [...]}`. Every connector run seals its contract
in `run_lock.surrogate_contract`. Changing a target remains a recorded treatment
in replay, diff, JUnit and CI summaries.

The inference model is a separate choice. Scripted trials, capability listing
and surrogate readiness call no model provider. Agent trials require a model
profile and authorization for paid inference.

Starting with 0.3.0, new scenarios must declare `emulation_target`. To run an
older unlabeled scenario, add the field or supply the target on the command line.
Catalog scaffolding and shared readiness also require `--emulation-target`.
Scaffolding from a sealed run retains its recorded target; historical evidence
and published scenario digests keep their original interpretation.

| Treatment | Muse | Instinct |
| --- | --- | --- |
| Native provider functions (`native`) | Supported | Supported |
| CATT connector command parser (`cli`) | Supported | Supported |
| Exposure | `in-prompt`, `search-only`, disconnected connectors linked during a run | Same |
| MCP transport | `generic-sdk` | Same |
| Output schema handling | `lenient`, `strict` | Same |
| Shared extensions | clock, presentation, routes, memory, browser, custom_connector, wallet, scheduler, content_trust | Same |
| Custom extensions | Existing `LaneExtension` API; their implementations are not qualified | Same |
| User simulator | Exploratory, agent-only; excluded from decision contracts and deterministic qualification | Same |
| Additional Muse treatment | `muse-cli`, `muse-template-unofficial-2026-10`, app widget checks | Rejected before linking or inference |
| Default presentation | Existing Muse context | Plain text without an implicit character cap |

`cli` parses `exec` commands for `list-tools`, `describe-tool`, `call-tool` and
`cat`; it starts no shell. It shares the existing command implementation with
`muse-cli`, but has its own assumed provenance. Deferred discovery loads a
service's command namespace. Script references use `/results/N/stdout/...`;
native references use `/results/N/result/...`.

Historical Muse evidence, including records with omitted target values, and the
`muse-cli` flag retain their meaning. Switching a scenario that explicitly selects the unofficial
Muse client to Instinct also requires `--client-profile generic-sdk`.

Both targets accept registered developer extensions through `LaneExtension`,
including module discovery, configuration validation and the shared approval
and evidence hooks. Unknown extensions still fail setup. The published shared
extension list qualifies CATT's shipped implementations; it does not qualify
third-party implementations.

## Behavior and evidence

The host enforces the same reviewed tool allowlists, private argument overlays,
approval binding, changed-disclosure approvals, schema treatments and uncertain
write handling for both targets. Unknown settlement blocks a trial; it does not
authorize another write. Service oracles and declared read tools establish
outcomes independently of the answer.

Route tests cover connector discovery, consent before connection, competing
services, scripted phone fallback, website/HTTP routes and duplicate outcomes.
Phone transcripts are fixtures. A transcript with `books: true` contributes one
simulated booking; it is not evidence of a real phone call or a phone-specific
transaction consent flow.

Wallet tests use a local issuer and merchant. They cover approval, denial,
unanswered approval, merchant/amount/currency binding, expiry, single use,
changed amounts, repeated attempts, 3-D Secure, private billing fields, receipts,
card storage and uncertain settlement. A card is issued after approval and
remains hidden from the model. The fixture does not implement Stripe Link or
Instinct's live payment rails.

Muse retains the basis labels of its documented and unofficial treatments.
Instinct's shared algorithms, extension checks and event provenance are CATT
assumptions. Its public observations remain in the separate packaged
[source register](instinct-emulation.md). Native/CLI MCP is CATT's adapter; the
reviewed sources do not establish an Instinct MCP integration contract.

## Indexed memory lifecycle

Both targets support standing context, stale facts checked against live reads,
data minimization and an optional source-scoped fixture index:

```yaml
extensions:
  memory:
    index:
      account: taylor
      sources:
        hotel: {account: taylor, connector: cedar-stays}
        other-mail: {account: another-user}
      records:
        - {id: room, source: hotel, text: "Prefers the Garden double."}
        - {id: foreign, source: other-mail, text: "Other account's fixture note."}
      changes:
        - {source: hotel, action: unlink}
```

The index is a declared snapshot. Its changes are applied before each trial:

| Change | Future connector tool calls | Previously indexed context |
| --- | --- | --- |
| None, linked source for the active account | Available under normal permissions | Visible |
| `unlink`, or `linked: false` | Vetoed before approval and dispatch | Retained |
| `delete` | Available if still linked | Removed from standing context |
| Both | Vetoed | Removed |
| Source owned by another account | Vetoed | Hidden |

A connector can belong to only one indexed source. Changes can affect only the
active account. Records must reference declared sources, and IDs must be unique.
Bounds are 20 sources, 100 records totaling at most 8,000 characters, and 40
changes. `stale[].source` optionally binds a stale fact to the same lifecycle;
deletion cannot reintroduce it through standing memory. Unscoped standing text
and stale facts remain independent of the index.

`memory.index_ready` records the applied changes and counts, without record
text. `metrics.extensions.memory.index` records visibility and blocked calls.
`memory.source_access` checks that no service tool request used an unavailable
indexed connector, including calls made through the host by another extension.
It audits CATT's service-request ledger, including extension dispatch records.
This is a host boundary check, not an independent observation of network traffic;
custom extensions must record their own dispatches through `run.record_dispatch`.

These are local fixtures, not live OAuth revocations or backend deletion
requests. The index does not persist between trials, ingest a mailbox, or erase
information from a model's existing conversation. Independent host-side outcome
oracles remain available for verification. Muse uses its existing file-like
headings; Instinct uses neutral context headings.

## Shared readiness

```sh
uv run consumer-agent-testing-tool check http://127.0.0.1:8765/mcp \
  --suite surrogate --emulation-target instinct --projection cli \
  --manifest examples/connector/surrogate-readiness.yaml \
  --junit reports/readiness.xml --summary-json reports/readiness.json
```

This suite uses `server/discover` or the older `initialize` handshake, then
`tools/list`, and never `tools/call`. It checks
names, credential exposure, the reviewed allowlist, schema syntax and the actual
selected projection. It records inferred or declared tool classes for review.
The optional manifest has `version: 1`, `kind: surrogate-readiness`, `allow`,
`classes`, `host_arguments` (tool names mapped to host-owned field names) and an
optional dated `protocol_revision` requirement.
Validate it against `schemas/surrogate-readiness-v1.schema.json`.
Linked credentials use the workspace's `.cache/connector-links/`, shared with
`connector-run`. The Python `run_check` API accepts an explicit `store` path.

Exit codes are 0 for no required finding, 1 for a catalog finding, 2 for a
setup/reporting error and 3 for an unsupported projection or client limit.
Without an allowlist or class declarations those checks are explicitly not
evaluated. Schema syntax does not prove runtime argument validation, and
references are never fetched. Readiness does not establish behavior: run
scripted scenarios against the service's oracle too.

`protocol_revision: "2026-07-28"` requires observed modern discovery and catalog
access. Legacy fallback is a failure of that requirement, even with a usable
catalog. A client unable to request it is unsupported before traffic.
See [modern MCP availability](readiness-checks.md#modern-mcp-availability).

`muse-guidelines` remains the separate, pinned Meta-guidelines review. There is
no fabricated Instinct guidelines suite.

## Qualification

The `surrogate-parity` release gate requires exact module, test and parameter
identities for both targets and both shared projections. It includes stale and
fresh memory, account isolation, unlink/delete behavior, routing, payment
failures, readiness, browser consent, custom HTTP, scheduling and untrusted
content. A missing target, parameter, failure or skip cannot pass the gate.

The distribution gate runs installed-wheel bookings, readiness, sealed playback
and private exports for all four target/projection combinations. Verification
checks distinct run identities, artifact hashes, target/contract labels and
independent booking checks. A boolean or relabeled Muse run cannot qualify
Instinct.

CI runs the full supported collection and scripted connector examples. Release
evidence is generated only by the release workflow and is not committed during
implementation. Real-host calibration, Instinct's trusted network, Vault/TOTP,
Files and real communication delivery remain outside this shared contract.
