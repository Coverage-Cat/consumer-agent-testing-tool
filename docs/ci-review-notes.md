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
