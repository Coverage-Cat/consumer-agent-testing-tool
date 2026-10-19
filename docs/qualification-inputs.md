# Qualification inputs for the local consumer release

The supported scope is now `local-consumer-v1`. No real-Muse observations,
cloud account, local Glimmer weights, Bedrock credentials, or browser-isolation
qualification packet is required. The older full-spec checklist below is retained
as historical development guidance; it is **not the current release checklist**.

The current release requires four source-bound proofs:

- **Regression:** the complete default test collection, with no failing or skipped cases.
- **Distribution:** a fresh offline installation of core/OpenRouter and consumer Node
  dependencies, their original project notices, verified wheel/source archives, and
  synthetic browser, mapped HTTP, consent and replay workflows.
- **Transactional consent:** current regression evidence for exact request grants,
  denied/stale authority, response loss, continuation and independent effect checks.
- **Replay/diff:** current regression evidence for sealed playback, mismatch rejection,
  semantic comparison, and explicit unknown observations.

These checks are run by the repository tooling, rather than supplied as user
qualification packets. See [release scope](release-scope.md) and the
[README](../README.md). This release does not bundle browsers, system libraries,
model weights, or cloud images, and makes no claim that their complete native
subcomponent inventory has been reviewed.

---

## Historical full-spec qualification inputs (superseded)

# Inputs needed to qualify a release

The release audit has not passed. Eight gates still need the work or evidence
described below. Supplying configuration makes a test possible; a gate passes
only after the required observations have been collected and reviewed.
The source-bound regression must also pass for the release being evaluated.

Keep supplied files inside this workspace. Reference credentials through the
configured environment variables; credentials do not belong in a public
qualification receipt. No additional shopper data or payment details are
needed for these general testing gates.

The handoffs below describe usable starting material, not requests to enable
accounts or spend money now. Notice recovery and evidence review can continue
locally. Live runs require the selected runtime or service, a defined test
scope, and any necessary authorization. The suggested bundles are organizational
guidance; the linked contracts define the machine-validated formats.

## 1. Original dependency notices

**Missing input:** original license/notice material applicable to these exact
installed versions:

- `@stoplight/json-schema-merge-allof@0.7.8`
- `fnv-plus@1.3.1`
- `parse-multipart-data@1.5.0`
- `pretty-data@0.40.0`
- `proxy-agent-negotiate@1.1.0`

For each, the usable input is the original notice file plus its source
provenance: release/archive or commit identity, retrieval record, and evidence
that it applies to the installed bytes. A package's license label, a proposed
but unmerged notice, or a sibling project's license is insufficient.

**Usable handoff:** one folder per package/version containing the unchanged
notice, its release or commit reference, and a short record connecting that
source to the installed package. Unresolved packages must remain listed.

**Work enabled:** bind the notice to the installed dependency, include it in
the redistributed artifacts, then rerun distribution checks. If no applicable
notice can be recovered, changing or replacing that dependency is separate
implementation and compatibility work. This gate is not waiting for an
account or credential. See [the notice review](dependency-notice-review.md).

## 2. Native runtime dependency and notice review

**Missing input:** a reviewed inventory covering the native libraries, browser
payloads and fonts actually shipped or required by each claimed runtime.
The current observation contains 57 components and 138 artifact paths, with
116 unresolved coverage gaps.

For each component, supply the embedded dependency inventory, original notices,
and references tying both to the exact payload hashes. Available upstream
SBOMs are starting material to review. Also declare the installation, host and
worker-image scopes being qualified; reviewing the Python package alone does
not cover its browser or container image.

**Usable handoff:** a scope list and an inventory mapping each payload hash to
its embedded dependencies and applicable notices, supported by build/linkage
records. Include the compiler/runtime and deployed system-library evidence
for each claimed platform. Mark uncertain associations and missing records
explicitly so the reviewer can distinguish recovered notices from established
coverage.

**Work enabled:** reconcile the review against the observed and packaged
inventory without dropping unresolved payloads. This requires substantive
review, not changing an `unverified` label. Host files outside this workspace
remain outside the authorized scope; relevant evidence must be supplied here.
See [the inventory contract](native-runtime-inventory.md).

The [rpds-py follow-up](rpds-native-notices.md) has recovered 34 original
notice files from 17 exact dependency archives. Its remaining input is the
build/linkage and compiler/platform evidence needed to establish complete
native coverage; the recovered notices alone do not resolve that review.
Read-only load-command inspection identifies `libiconv.2.dylib` and
`libSystem.B.dylib` as two concrete platform dependencies needing supplied
payload/notice evidence. The referenced host paths were not opened.

The [broader SBOM follow-up](native-sbom-notices.md) now retains 630 original
notice/attribution files from the declared registry dependencies and OpenSSL,
and corroborates the 14 local Rust component names/versions from locked parent
source archives. Those recovered materials still need linkage, compiler,
platform and completeness review; supplying a second package-level SBOM alone
would not resolve those remaining inputs.
The binary metadata review names libSystem, libiconv, CoreFoundation,
CoreServices and Security across seven payloads/eight architecture slices.
Provide the actual deployed dependency evidence for the claimed runtime;
load-command names and an unexecuted architecture slice are not that evidence.

## 3. Local offline Glimmer

**Missing input:** an explicitly selected local runtime, verified weight and
executable files available inside the workspace, and a usable machine on which
to run them.

The configuration needs the exact served model ID, loopback endpoint, artifact
paths and SHA-256 values, running server PID/build, context size, slot count
and memory budget. Record the machine/platform and the runtime's enforced
offline configuration. Illustrative values in a template are not observations.

**Usable handoff:** the actual `model_profiles.local-glimmer` configuration
shown in the linked setup guide, the referenced files inside this workspace,
and a running server whose identity can be checked. File paths, hashes and
process information must describe that server; a hosted API key alone cannot
enable this test.

**Work enabled:** verify native tool calls, nested arguments, tool-result
continuation, reasoning separation, representative memory use and sustained
fixture workflows while outbound access remains denied. OpenRouter success
cannot establish this local/offline property. Weight downloads are a separate
setup action, not implied by the qualification request.
See [local Glimmer setup](local-glimmer.md).

## 4. Bedrock bearer authentication — deferred

**Missing input, if this path is re-enabled:** an exact enabled model and AWS
region, a bearer credential available through the selected environment-variable
reference, and an authorized test scope and inference budget.

**Usable handoff if re-enabled:** a Bedrock Converse model profile using
`api_key` authentication, the credential's environment-variable name, and the
allowed region/model and spending limit. Supply the credential through the
environment, not in the profile or evidence bundle.

**Work enabled:** make a real native tool call and continue from its result
using bearer authentication without an access-key pair. The run also needs
the required invalid-configuration, timeout and isolation checks. Passing the
IAM path or a simulated SDK transport does not qualify bearer authentication.
The existing instruction to skip Bedrock remains in effect.

## 5. Bedrock IAM authentication — deferred

**Missing input, if this path is re-enabled:** an exact enabled model/region
and a selected AWS credential-chain method, such as an authorized role/session,
with its intended session behavior and test budget.

**Usable handoff if re-enabled:** a Bedrock Converse profile using
`aws_credentials`, the selected role/profile or session mechanism, its region
and model permissions, and a bounded test budget. Describe the session behavior
to exercise, such as temporary-credential expiry or renewal.

**Work enabled:** independently verify signing, native tool calls and
continuation through that credential chain, recording which session behavior
was actually exercised. A bearer token is not this input. No AWS credentials
are being requested while Bedrock remains deferred.
Both authentication gates use the
[external qualification contract](external-qualification.md).

## 6. Browser isolation

**Missing input:** a precise support matrix and a selected live model for
agent-directed tests. Each claimed scope needs the platform/architecture,
kernel, pinned worker image, Chromium version and enforcing boundary, plus
the model configuration and any authorized inference budget.

**Usable handoff:** one row per supported runtime with those identities, the
matching worker image, and an executable model profile. Include access to
controlled destinations where attempted connections and positive controls
can be observed independently. State whether the claim covers only the tested
Linux ARM64 worker or additional platforms.

**Work enabled:** run the direct network/bypass probes, positive controls and
model-directed attempts for each scope. Retain destination-side observations
and the attributed denial or arrival. A refusal to attempt the action or an
empty traffic counter alone does not establish isolation.

The existing Linux ARM64 tests provide local deterministic evidence.
Qualifying additional platforms requires running their matrix, and live tests
may expose defects that need fixing. Native browser rendering does not inherit
the isolated worker's qualification.
See [the browser worker](browser-worker.md) and
[agent-directed canary](browser-canary.md).

## 7. Cloud worker lifecycle

**Missing input:** a staging configuration for the implemented Maritime
adapter: exact API origin, dedicated namespace, prepared template ID, compatible
installed runtime, provisioning-credential reference, and bounded resource
count, lifetime and spending authorization. Model and target credentials are
separate inputs when the selected trial needs them.

Also supply the independent cleanup deployment: where it runs, its schedule,
its access to retained journals/provider credentials, and how it survives loss
of the coordinator. A template name or task timeout does not provide this.

**Usable handoff:** the workspace's `worker-maritime.json`, the prepared
template/runtime identity, resource and spending limits, and the deployed
cleanup schedule with its durable-state location. Provide credential references
separately. The staging namespace must be available for provision/delete trials
and for a deliberate coordinator-loss test.

**Work enabled:** observe provision, readiness, seed, execution, verified export,
cancellation and deletion, including actual volume cleanup, failed runs and
coordinator loss. The independent mechanism must demonstrably remove expired
resources. A manual cleanup command or successful local subprocess run is not
that evidence. See [Maritime setup and cleanup](worker-maritime.md).

## 8. Real-Muse calibration

**Missing input:** authorized observations of actual Muse using the test
surface, paired with local surrogate trials for the same scenario identities.
Supply the current profile/revision, observation/expiry dates and collection
authorization, plus a study plan declaring scenario selection, repetitions,
calibration/held-out separation and analysis.

The version 2 cohort references hashed pair files and underlying transcripts
or screenshots, target requests and independent backend-state records.
Both splits must cover all nine observable dimensions: outcome, backend
state, surface, operation order, arguments, repetition, approvals, handoffs
and final-response consistency. Keep unobserved information explicitly unknown.

**Usable handoff:** a version 2 cohort with its paired observation files and
underlying artifacts, the tested profile/revision, and the collection study
plan. If observations have not been collected, start with the partner packet
described in the linked guide and arrange authorized collection. A surrogate
transcript relabeled as Muse is not an observation of Muse.

**Work enabled:** reproduce the comparison from the retained artifacts,
report disagreements and assess coverage. Adequate sample size, independent
scenarios, authentic observations and annotation quality still need review;
hashes and schema validity do not establish them. No hidden Muse trace or
real customer information is required.
See [the collection format](calibration-observations.md).

## Turning inputs into evidence

For live gates, retain each tested configuration, its original run lock and
the supporting observations for every required check. The receipt references
those files by workspace-relative path and hash and is bound to the release
source and an expiry. Muse additionally requires the cohort, profile, study
plan and saved comparison; the audit recomputes the comparison.

The [receipt schema and exact check IDs](external-qualification.md) describe
what the validator accepts. That validator checks evidence completeness and
consistency; an operator must still review whether the original observations
support the claim. Synthetic unit-test fixtures cannot replace these inputs.
