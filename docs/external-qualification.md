# External release qualification

The five live gates and the separate Muse-observation gate require identified
configurations, required checks and hashed supporting artifacts. Provenance and
the interpretation of external logs remain operator-attested.
For the concrete files, setup and observations needed before collection, see
[qualification inputs](qualification-inputs.md).

The [reproduction](release-evidence/qualification-audit-reproduction-2026-10-14.json)
showed that all six gates previously accepted an explicitly synthetic receipt
with one text-completion check and no observations or configuration. It did
not change the workspace ledger. The audit now delegates these gates to
[the qualification validator](../src/consumer_agent_testing_tool/qualification.py).

## Common receipt

The outer fields remain `gate`, `basis`, `status`, `source_sha256` and nonempty
passing `checks`. The ledger must reference the receipt's exact hash, matching
gate/basis and an unexpired, timezone-qualified expiry. Source must match the
current worktree.

External receipts additionally require `evidence_kind: live` for the five
live gates, or `evidence_kind: muse-observed` for Muse, and the versioned
`qualification` object below. Missing, explicitly synthetic, malformed or
incomplete qualification stays `unverified`. Audit validation never contacts
providers, loads credentials, executes inference or opens a browser.

Every reference has `path` and `sha256`. Paths must be canonical,
project-relative and contained within the project. SHA-256 values must have
64 lowercase hex digits. Referenced files must be nonempty and hash-matched.
Configuration, run-lock, cohort, profile and comparison documents must be
JSON objects no larger than 8 MiB. References are rechecked before acceptance.
Artifact contents are not copied into audit error messages.

## Live qualification

The structure is:

```json
{
  "schema_version": 1,
  "observed_at": "2026-10-14T12:00:00Z",
  "scopes": [
    {
      "id": "your-tested-configuration",
      "configuration": {
        "path": "qualification/configuration.json",
        "sha256": "REPLACE_WITH_FILE_HASH"
      },
      "run_lock": {
        "path": "qualification/run-lock.json",
        "sha256": "REPLACE_WITH_FILE_HASH"
      },
      "checks": []
    }
  ]
}
```

This template does not qualify: replace the hashes and fill every required
check. Scope IDs must be distinct and nonblank. Observation dates cannot be
in the future or at/after the ledger expiry.

Each check contains `id`, `status: passed`, `basis: live` or `test-report`,
and a nonempty `artifacts` array of references. Missing, duplicate or unknown
IDs fail validation. One report may support multiple checks, but an operator
must verify that it actually contains each claimed observation.

Every declared scope must independently cover the whole checklist. Partial
matrices from different platforms cannot be combined into one pass. A receipt
qualifies only its listed configurations, not unlisted platforms or models.

### Configuration and run lock

The configuration snapshot contains `platform`, `execution` (with `backend`),
the full `model` profile for inference/browser gates, and these extra fields:

| Gate | Required configuration |
| --- | --- |
| `local-glimmer-offline` | Local execution, `local-glimmer`, `offline: true`, runtime build/PID, memory budget, weight/runtime paths and hashes |
| `bedrock-bearer` | Local execution, Bedrock Converse, exact region/model, `api_key` auth with the bearer environment-variable reference |
| `bedrock-iam` | Local execution, Bedrock Converse, exact region/model, `aws_credentials` auth |
| `browser-isolation` | Local execution and valid model profile; `browser_runtime` with pinned `image: sha256:…`, `kernel`, `architecture`, `chromium` and `boundary` strings |
| `cloud-worker-lifecycle` | Worker execution; `cloud` with `provider: maritime`, exact `template_id`, `namespace` and TLS `api_url`; matching provider in the run lock |

The original run lock, extracted from its manifest, must include harness,
platform, execution, model, nonempty dependency inventory, dependency-lock
SHA-256 and nonempty source hashes. Platform and backend must match the
configuration. Model profiles use the existing validator and are compared
after resolving defaults; another model/auth profile cannot support the scope.

Loopback hosted-inference/cloud fixtures cannot qualify as live providers.
Local Glimmer remains a legitimate local model path. The cloud gate currently
validates the implemented Maritime adapter; command/SSH fixtures do not prove
named-provider provisioning.

Browser and cloud configuration fields are normalized review metadata. Keep
original runtime/provider records in the check artifacts; declarations alone
do not establish their truth.

### Required check IDs

**Shared model checks, all live:**

`native-tool-call`, `nested-arguments`, `call-result-correlation`,
`tool-result-continuation`, `local-tool-execution`, `model-identity`.

**Local Glimmer:** the shared checks plus live `artifact-runtime-identity`,
`reasoning-separation`, `fixture-adapters`, `representative-memory`,
`sustained-workflow`, `offline-enforcement`. It also requires
`secret-isolation` and `fallback-disabled`, allowing live or test-report basis.

**Both Bedrock gates:** the shared live checks plus `secret-isolation`,
`invalid-configuration`, `unsupported-capability`, `timeout-rate-limit`,
`fallback-disabled`, `offline-rejection`, allowing live or test-report basis.
Bearer additionally requires live `bearer-without-access-key-pair`;
IAM requires live `credential-chain-session`. This contract does not authorize
an AWS run. Bedrock remains deferred.

**Browser isolation:** live `agent-directed-attempt`, plus the following with
live or test-report basis:

`restricted-dispatch`, `direct-tcp`, `direct-udp`, `dns`, `dns-rebinding`,
`redirects`, `fetch`, `websockets`, `service-workers`, `webrtc`,
`child-processes`, `host-aliases`, `privileged-destinations`,
`positive-controls`, `bypass-controls`, `destination-observations`,
`enforcing-layer`, `secret-isolation`, `runtime-identity`.

An operator must establish an attributed attempt and destination observations.
Refusal or empty counters without positive controls do not satisfy these checks.

**Cloud lifecycle, all live:**

`provision`, `ready`, `seed`, `run`, `export-verified`, `cancel`, `destroy`,
`volume-cleanup`, `failed-run-cleanup`, `coordinator-loss`,
`independent-resource-expiry`.

Task timeout, a manual reaper invocation or local subprocess tests cannot
replace independently scheduled provider resource cleanup.

## Muse qualification

The `qualification` object contains:

- `schema_version: 1`.
- `observed_at`, matching the cohort's observation date.
- `cohort`: reference to a version 2 authorized `muse-observed` packet.
- `profile`: reference to the tested profile JSON. Its canonical digest,
  using Consumer Agent Testing Tool's `digest`, must equal the cohort's `profile_sha256`.
- `surface_revision`: the current declared revision.
- `study_plan`: reference to the operator-reviewed collection/analysis plan.
- `comparison`: reference to saved `calibrate` output.

The audit reuses the [existing analyzer](calibration-observations.md) at the
current audit time. It verifies cohort, pair and underlying source hashes,
authorization, profile/revision, expiry and split separation. The saved result
must match recomputation, including JSON scalar types.

Both calibration and held-out splits must cover all nine observable dimensions.
A narrower `primary_dimensions` list cannot bypass the full gate. The standalone
analyzer still supports version 1 outcome-only packets, but those cannot
qualify this full calibration gate.

This establishes completed collection and comparison, not agreement with Muse
or general compatibility. Disagreements remain visible. Study-plan quality,
prespecification, adequate sample size, scenario independence and authenticity
still require operator review; a hashed study-plan file does not prove them.

## Limits and verification

Validation rejects missing or contradictory declared evidence. It does not
authenticate a provider or Muse, detect a deliberately false live declaration,
or prove that arbitrary logs support an operator's interpretation.

Regression tests use synthetic data in isolated temporary ledgers to test
positive schema contracts and negative cases. They never publish a live
receipt to the workspace ledger. Implementing this validator does not promote
an external gate.
