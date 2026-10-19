# Proposal validity and target validation

Argument-validity reports now count native tool proposals at the Inspect provider
boundary, before tool budgets, approval decisions or target dispatch. A proposal
that fails input validation remains in the denominator even when no HTTP request
or browser action occurs.

Each selected model response has a `proposal_validation` record linked to its
response, request and observation. It identifies each call by response position,
call ID and tool name. Reusing a call ID in a later response does not merge two
proposals, and replaying retained history without another model response does not
create another proposal.

Validation uses the locally serialized Inspect tool schema. The record includes
that schema and the exact serialization policy:
`ToolParams.model_dump(mode="json", exclude_none=True)`. Inspect's general
diagnostic dump contains unset schema fields as null; those diagnostic fields are
not treated as schema constraints. Provider-specific filtering or transformation
can differ and remains a separate observation boundary.

Known schema dialects use the pinned JSON Schema validator with registered format
assertions. Defaults are not inserted by the measurement. Local references and
unions retain their semantics; external references are never retrieved.
Unavailable schemas, unknown formats and unsupported dialects remain unknown.
The measurement does not modify a proposal, grant permission or retry execution.

Validation happens before redaction. Records contain validity and a reason or
failed schema keyword, without copying argument values into error messages.
Export preserves the recorded decision rather than validating redacted placeholder
values against the original schema.

| Metric | Scope |
|---|---|
| `argument_validity` | Valid native proposals divided by all observed native proposals, only when validation coverage is complete. |
| `argument_observations` | Versioned counts of valid, invalid and unknown proposals, response coverage and evidence gaps. |
| `target_argument_validity` | The independent target's argument-validation observations for requests that reached it. |
| `observed_argument_validity` | Trial-summary rate over proposals with known validity, explicitly excluding unknown observations. |
| `argument_trial_coverage` | Trial counts with observations, complete coverage or no compatible measurement. |

A malformed native call or a call to an unexposed tool is invalid. A
schema-valid proposal rejected by consent or a dispatch budget is still
schema-valid. For CLI tools, validating the outer `command` string does not
establish shell syntax or domain argument correctness. Browser schema validation
does not establish reference freshness or permission for resulting network
requests. Independent policy, interaction and backend results retain those
responsibilities.

Trial summaries include failed, blocked and infrastructure attempts. Incomplete
trials retain their known valid/invalid counts and explicit unknown counts.
Old reports containing only target-side validity cannot fill native-proposal
coverage. Missing, duplicated or incorrectly linked validation records prevent
an apparently complete rate. No proposals means a zero denominator and a null
rate, never a successful task.

The deterministic suite exercises real Inspect API and Chromium rejection paths,
dynamic tool schemas, reused call IDs, budget rejection, redacted exports and
offline reference handling. The installed FastMCP proof includes an invalid
purchase proposal: the report counts it even though the target receives only the
preceding read and no grant or purchase.
