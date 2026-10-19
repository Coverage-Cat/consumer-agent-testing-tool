# Observable calibration evidence

`calibrate` compares paired local and authorized Muse observations. Version 2
separates nine dimensions, their denominators and their unknown observations:
task outcome, independent backend state, surface used, operation sequence,
arguments, request repetition, visible approvals, handoffs and final-response
consistency. It does not run Muse, infer hidden policy or certify a release.

## Try the synthetic example

In a project initialized with `consumer-agent-testing-tool init --provider none`:

```sh
consumer-agent-testing-tool calibrate calibration-examples/cohort.json \
  --profile-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --surface-revision synthetic-fixture-v1
```

This requires no account, inference or network. Expected exit code is **3**:
the packet is explicitly synthetic and cannot qualify as Muse evidence. Its
held-out pair deliberately contains a local false success, different backend
state and an inconsistent Muse-side response. All nine dimensions have
observations; agreement on some dimensions cannot conceal failure on others.
The fixed example's expiry remains visible after its observation window ends.

## Collect a version 2 packet

Start with the installed example's JSON structure. The cohort contains:

- `schema_version: 2`, a cohort name and explicit `authorized: true`.
- `evidence_kind`: `synthetic` or `muse-observed`. Only actual authorized Muse
  observations may use the latter.
- Timezone-qualified `observed_at` and `expires_at`.
- `profile_sha256` and `surface_revision`, identifying the tested configuration.
- Separate `model`, `policy`, `browser` and `discovery` visibility judgments,
  each `observed`, `partial` or `unknown`.
- A nonempty `consistency_rubric` used by the operator to judge the final response
  against independent state.
- Optional `primary_dimensions`, defaulting to all nine.
- Paired observation references with scenario/trial IDs, a `calibration` or
  `held-out` split, project-relative artifact path and exact SHA-256.

Scenario sets must be disjoint across splits. A scenario/trial pair cannot
appear twice. Each referenced pair repeats the version, evidence kind, profile,
surface revision and scenario/trial identity; they must match the cohort.
Changing a cohort's label alone cannot relabel its pair artifacts.

Each pair has `local` and `muse` sides. A side lists hashed source artifacts,
with kinds `transcript`, `screenshot`, `target-requests` or `backend-state`.
Artifact paths must remain in the project. The analyzer checks the pair bytes
and every referenced source digest before comparing annotations.

Annotations can contain:

- `task_success` and `backend_state`. A task outcome requires backend state;
  backend state requires a separate `backend-state` source.
- `surface`: `cli-skill`, `api-tools`, `browser` or `mixed`.
- `operations`: explicit `completeness` (`complete` or `partial`) and ordered
  items containing scenario-declared `milestone`, `operation` and observed
  `arguments`. Operations require a target-request source.
- `interventions`: explicit completeness and ordered items containing milestone,
  kind (`approval` or `handoff`), observed `outcome` and observed `details`.
- `final_response` and operator-annotated `final_consistent`. Consistency needs
  both response text and independent backend state. Surface, intervention and
  response annotations require a transcript or screenshot source.

Use `null` or omit an unobserved value. Do not turn a partial trace into an empty
complete trace: a complete empty intervention list establishes observed absence,
while a missing or partial list remains unknown. Boolean annotations must be
JSON booleans; integers are rejected.

## Interpret the result

Each split reports independent scenario count and, for each dimension, compared
pairs, unknown pairs, agreements, differences and a descriptive Wilson interval.
Task outcome and final consistency also report false local passes/failures.
Individual pair results contain reasons and artifact identities, without copying
raw arguments or response text into the analysis.

Arguments align by declared scenario milestone and attempt order. Independent
milestones may reorder while their arguments still agree; operation sequence
records that difference separately. Unmatched attempts or missing arguments
produce unknown argument comparison. Request repetition distinguishes first,
repeated-argument and changed-argument requests; it does not infer retry intent.

Run with the **current** profile digest and surface revision. A mismatch or
expired observation makes the cohort stale. Every primary dimension must have
at least one comparable held-out pair for coverage. Nonstale, declared
`muse-observed` evidence with that coverage returns exit code zero and
`usable_for_comparison: true`; synthetic, stale or incomplete evidence returns
three. Invalid packets fail validation.

The [release qualification contract](external-qualification.md) adds stricter
requirements for the full `muse-calibration` gate: both scenario splits and all
nine dimensions, a hashed profile and study plan, and a saved comparison that
matches recomputation from the original observations. Standalone analyzer
eligibility does not automatically promote that gate.

Coverage is not statistical sufficiency. Repeated trials can be correlated.
Source hashes establish integrity, not authenticity or annotation correctness.
Target logs omit actions blocked before the target; visible approvals do not
expose hidden policy. Final consistency is an operator-applied rubric, not an
automatic semantic judgment.

## Scenario-cluster uncertainty

Version 2 packets may explicitly request:

```json
"analysis": {
  "method": "scenario-cluster-t-v1",
  "independent_unit": "scenario_id",
  "weighting": "equal-scenarios",
  "confidence_level": 0.95
}
```

Declare the independent unit and weighting before collecting a comparison
cohort. The analyzer records that declaration and its hash; it does not verify
when the plan was chosen or whether the scenarios really are independent.
Omitting `analysis` preserves the descriptive report without assuming scenario
independence.

- `paired-trials` estimates agreement across the observed paired trials.
  Scenarios with more observed repetitions receive more weight, but Inspect's
  clustered standard error keeps repetitions in the same scenario together.
- `equal-scenarios` first averages agreement within each observed scenario,
  then gives each scenario equal weight. One scenario with 100 successes and
  another with one disagreement has agreement 0.5 under this weighting,
  versus 100/101 under paired-trial weighting.

The implementation reuses the pinned `inspect_ai.scorer.mean`, `stderr` and
`ci(method="t", cluster="scenario_id")` metrics. It adds no statistics package
or upstream patch. Results record the engine version, declared weighting,
scenario sizes, observed/unknown counts, standard error and degrees of freedom.
The `interval_95` intersects the interval with the agreement range [0, 1];
`unclipped_interval_95` retains the upstream bounds.

Only compared pairs enter the estimate. Unknown pairs are never agreements or
disagreements; wholly unobserved and partially observed scenarios remain
explicit. The resulting estimate is conditional on the observed data, without
an assumption that missing observations are representative.

Fewer than two observed scenarios produces `insufficient-scenarios`, with no
uncertainty interval. Identical scenario agreement rates produce
`degenerate-variation`, also without an interval: many repeated identical
outcomes do not demonstrate zero population uncertainty. A `computed` result
uses a clustered t approximation; two scenarios are enough to calculate it,
not evidence that the approximation or cohort is adequate. Small cluster
counts, dependent scenarios, sampling bias and nonrepresentative missingness
still limit interpretation.

The original pair-weighted agreement and descriptive Wilson interval remain
separate fields. The optional scenario analysis does not change coverage
eligibility or promote a release gate. Its intervals are per dimension, not
simultaneous across all nine dimensions. It does not perform superiority,
noninferiority, sequential testing or a bootstrap analysis.

An installed synthetic example has two held-out scenarios, one agreement and
one disagreement:

```sh
consumer-agent-testing-tool calibrate calibration-examples/scenario-cluster-cohort.json \
  --profile-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --surface-revision synthetic-fixture-v1
```

It reports a wide [0, 1] task-outcome interval, flags constant dimensions as
degenerate, and still exits 3 because the evidence is synthetic.

Version 1 packets remain supported for outcome-only comparisons. Their output
explicitly identifies the unmeasured dimensions. Prefer version 2 for new
collections.

## Release evidence

The `calibration-analysis` gate records deterministic analyzer tests. The
`muse-calibration` gate separately requires actual authorized observations;
synthetic tests and the installed example cannot pass it. No command here
automatically promotes an external-observation gate.

Without Muse access, generate a partner collection packet:

```sh
consumer-agent-testing-tool calibrate examples/surface-preferences.yaml \
  --partner-packet --profile-sha256 PROFILE_DIGEST
```

It supplies the scenario, staging instructions, capture checklist and unanswered
host questions. The operator must obtain the observations through an authorized
interface.
