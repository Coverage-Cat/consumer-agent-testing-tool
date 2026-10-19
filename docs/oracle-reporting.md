# Backend verification across reports

An agent finishing its interaction does not establish a successful service
outcome. The CLI/skill and browser lanes preserve independent observations
through scoring, failure handling, playback and redacted export.

## CLI/skill trials

The Inspect scorer uses the same typed assertion evaluator as API/browser
scenarios. Boolean observations must be booleans; counts must be nonnegative
integers. Missing observations, `false` in place of zero, floating-point zero,
negative counts and string counts cannot establish a zero-effect pass.

Unauthorized and duplicate effects have mandatory zero expectations even if a
scenario omits those assertions. An independently observed positive count
records a failed invariant with its original evidence event. A conflicting
scenario expectation cannot authorize the effect; it is retained as
`declared_expected` when scored.

If execution fails before scoring, the runner still queries the independent
oracle and records effect checks. The command's error handler retains these
checks alongside the execution failure. A proven effect fails the run even
when the execution interruption would otherwise have produced a blocked result.
An unavailable oracle is recorded as an observation gap, without claiming zero
effects. Infrastructure exit codes remain unchanged when no independent check
has failed.

These changes use the existing Inspect agent and sandbox boundary. They do not
add CLI continuation or qualify shell isolation.

## Browser playback

Browser scenario reports include both count and boolean backend assertions.
An unavailable oracle also becomes an explicit backend observation gap in the
sealed child report; it is not confined to the parent scenario manifest.

| Independent verification | Completed browser interaction | Sealed overall status |
|---|---|---|
| Expected backend observation established | `execution_status: completed` | `completed` |
| Backend assertion failed | `execution_status: completed` | `failed` |
| Oracle or required observation unavailable | `execution_status: completed` | `blocked` |

Reporting completion remains separate. `verified_service_outcome` includes
only specifically passed independent checks; it excludes missing and failed
observations. Playback and redacted export preserve the checks and statuses.
Exports still remove private browser diagnostics and record their own provenance.

The runner-only `assert_backend_value` accepts boolean or nonnegative integer
observations. `assert_backend_gap` records missing verification without inventing
a count. The existing count-specific method remains available. None of these
methods is exposed as an agent action.

## Verification scope

`tests/test_oracle_reporting.py` exercises actual Inspect composition with a
scripted model, independent SQLite effect records, execution limits, CLI error
handling, and the embedded MCP/Chromium browser. The CLI sandbox in those tests
is explicitly a double. The installed-package smoke repeats unavailable and
failed browser verification through playback and export.

These checks establish reporting behavior under controlled faults. Live model
inference, operating-system isolation, cloud lifecycle and real-Muse calibration
remain separate release gates.
