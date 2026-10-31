# CI development reviews

Reviews use `claude -p` against committed checkouts with read-only tools. Raw reviews and authentication
settings remain in ignored local storage. Review findings are reproduced before fixes.

## Iteration 1: planning and portable scripts

Reviewed commit: `4ff15bc`.

| Finding | Disposition and evidence |
| --- | --- |
| Public test values and symbolic token references collided after general redaction | Reproduced. Identity now masks declared private literals without email, card, key-name or home-directory heuristics. Known environment credentials in public input are rejected. |
| Scripts could use the wrong projection, a null portable response or an unavailable tool | Reproduced. Validation checks statically known tool surfaces, allowlists and response envelopes before startup. Runtime values and live schemas remain runtime checks. |
| Negative expectations accepted typos and contradictory statuses | Reproduced. Expected IDs resolve to required scenario or supported host/readiness checks; exit 1 requires a failure and exit 3 excludes failures. |
| Native and CLI handled unavailable portable tools differently | Reproduced. Both fail at the portable-call boundary before dispatch. |
| Uppercase HTTP schemes bypassed loopback validation | Reproduced. URL detection parses schemes case-insensitively. |
| Fixed ports and invented ports for existing services undermined isolation | Reproduced. Owned readiness uses its allocated port; process-local URLs use `${PORT}`; existing services use their declared values/defaults. |
| Credential-like check IDs triggered a false privacy error | Reproduced. Public identifiers are checked as text without applying data-field redaction heuristics. |
| Custom API documentation was outside validation and identity | Reproduced against the reviewed commit. Bundles are validated, hashed and frozen with their URL placeholder intact. |

Matrix defaults remain explicit documented suite defaults. The surface check now rejects a script made
unavailable by a default override. Requiring the author to spell every default again adds no validation.

Additional implementation tests caught order-dependent private masking, inconsistent serialized plans,
a malformed private container, integer/float report-digest disagreement, misclassification of ordinary
failed outcome checks as infrastructure errors, and missing JSON diagnostics for invalid output paths.
Each reproduction failed before its fix.

A malformed-input test also exposed credential values through a Python container's diagnostic representation.
`SecretList` now hides values in `repr` and `str`; iteration and explicit private serialization retain their
existing behavior. Its regression failed on the old representation.

## Iteration 2: execution and reporting

Reviewed commit: `0895747`, including the preceding diagnostic fix `a929ca2`.

| Finding | Disposition and evidence |
| --- | --- |
| External cancellation could start queued cases | Reproduced with three cases: the old runner started two more services after cancellation. Cancellation now stops the queue, checkpoints unrun cases and propagates to an outside caller. Only the CLI installs process signal handlers. |
| A report could omit required observations or contradict its exit code | Reproduced independently before the review returned. Report validation requires declared checks and consistent underlying statuses. Digests are documented as consistency checks, not authentication. |
| Optional failed checks and emitted extension IDs could not be expected | Reproduced, including a real `wallet.receipt` check. Optional failures and the missing presentation/browser IDs are supported. Informational optional skips do not count as significant findings; cleanup errors remain unexpectable infrastructure errors. |
| `execution` and `answer` were broad expected failures | Reproduced. These IDs require a pinned failure class and stage. The verdict checks the cause as well as the exact check set. The false-policy purchase case also requires zero policies and charges, so duplicate effects cannot masquerade as the intended failure. |
| Deferred portable calls could omit discovery | Reproduced for both projections. An earlier explicit `tool_search` is required. A separate shipped discovery trace exercises both targets/projections. Live ranking and dynamic search queries still require execution; discovery is never implicit. |
| Malformed URLs or report I/O errors could erase JSON output | Reproduced. URL errors produce static diagnostics; URL-valued scripted arguments use the same substitutions in validation and snapshots. A report-write failure stops queued work and preserves observed rows in stdout with a fatal error. Merge preserves source reporting errors. |
| Cleanup queued behind the default executor | Reproduced with the executor occupied. Process-group cleanup now polls asynchronously and retains its cancellation shield. |
| Public-file modes and abandoned atomic files | Reproduced. Public reports use mode 0644. Reserved atomic temporary files do not block a later run; CI uploads name only the two final reports. XML explicitly uses UTF-8. Output-error diagnostics distinguish older files from the current result. |
| Short/common private values could enter fingerprinting | Not reproduced: `ConnectorScenario` already rejects those values before a plan exists. Added characterization tests for numeric and common-word cases. The environment-credential guard remains intentional; validation can reject a coincidental match rather than hash a known credential. |

The reviewer found no model-call path and confirmed the diagnostic-secret fix. Both targets' readiness
paths have also run through the real shipped suite. The service runs as the same OS user as the harness;
this is not a security sandbox or a signed attestation against a malicious local process.

Additional regressions separate the installed harness from the service's dependency lock, bind installed
runtime dependencies into harness identity, validate legacy projection references, reject a fixed port
that happens to equal the planning placeholder, and preserve a fatal shard error through aggregation.
