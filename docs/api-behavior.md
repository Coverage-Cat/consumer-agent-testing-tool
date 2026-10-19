# Developer API behavioral trials

The experimental `api-tools-surrogate-v1` lane can run an explicitly mapped local
developer API through the existing Inspect ReAct runner. The agent receives the
curated tools, user goal, stored synthetic user profile and requested scripted
replies. Lifecycle controls, approval rules, grants and oracle state stay private.
This is surrogate testing, not a measurement of Muse.

The [FastMCP-generated baseline](generated-api-baseline.md) uses this same
runner and private protocol with its actual native catalog and serializer.
Its mapping identity and result envelope differ from curated HTTP mappings;
both are recorded explicitly.

Start with a [developer HTTP mapping](api-mappings.md). Add an explicit private
control socket and principal to the target:

```yaml
targets:
  inventory:
    kind: api
    transport: http
    base_url: http://127.0.0.1:8765
    permitted_destinations: [http://127.0.0.1:8765]
    source_revision: inventory-test-v1
    api_mapping: api-mappings/inventory.yaml
    auth:
      mode: api_key
      env: INVENTORY_TEST_TOKEN
    api_control:
      protocol: consumer-agent-testing-tool-api-control-v1
      socket: .cache/inventory-control.sock
      principal: synthetic-customer
```

The developer supplies the test service and control adapter. The host does not
launch commands from this descriptor. The HTTP destination must be loopback.
The Unix socket must be inside this workspace, owned by the current user and
inaccessible to group/other users; symlink components are rejected. The target
credential is inserted privately, as `Authorization: Bearer` or in the mapping's
[`credential_header`](api-mappings.md#mapping-contract). Its environment
variable must differ from every inference credential's, including
`MODEL_API_KEY` and `META_API_KEY`. These checks are not an OS sandbox against a
malicious same-user process.

The scenario uses `surface: api-tools`, `interface_profile:
api-tools-surrogate-v1`, this target name and a named `initial_state.fixture`.
The control adapter must advertise that seed and the selected persona, which
must be a name: a profile object is refused (Unsupported, exit 3), because the
adapter seeds the user's profile by name. Existing scenario assertions,
private approval predicates and registered event hooks apply.

```sh
consumer-agent-testing-tool run scenarios/inventory.yaml --config consumer-agent-testing-tool.yaml \
  --surface api-tools --interface-profile api-tools-surrogate-v1 \
  --model-profile YOUR_CONFIGURED_PROFILE --trials 1
```

Static mapping validation precedes the command's model preflight. The private
adapter handshake and reset precede the first behavioral model generation.
Tests use injected scripted models and owned local HTTP/Unix services.

## Private adapter protocol

One persistent Unix connection owns one trial. Requests and replies are
newline-terminated strict JSON, bounded to 4 MiB. Commands have no automatic
retry or reconnection. Request IDs increase on that connection; replies must
match the complete ID and identity.

```json
{
  "id": 1,
  "identity": {
    "protocol": "consumer-agent-testing-tool-api-control-v1",
    "run_id": "host-generated-run-id",
    "task_id": "inventory_trial",
    "target": "inventory",
    "base_url": "http://127.0.0.1:8765",
    "source_revision": "inventory-test-v1",
    "mapping_sha256": "digest-of-normalized-mapping",
    "principal": "synthetic-customer"
  },
  "operation": "describe",
  "args": {}
}
```

A reply contains exactly `id`, `identity` and `result`. An adapter must validate
the identity against its actual configuration and active lease, not simply trust
an identity echoed by its caller. Mapping hashes use
`consumer_agent_testing_tool.serialization.digest(mapping.model_dump(mode="json", by_alias=True))`.
The control protocol is host infrastructure, not MCP or a model tool.

| Command | Arguments | Required result and behavior |
| --- | --- | --- |
| `describe` | Empty object | Exactly `descriptor`, `environment`, `capabilities`, `events`, `fixtures`, `personas`. Descriptor contains the identity's protocol, target, base URL, source revision, mapping digest and principal, excluding run/task IDs. Environment must be `test`. Other fields are explicit string lists. |
| `acquire` | `initial_state` from the scenario | Atomically acquire an exclusive test-state lease and reset/seed that state. Return `reset_completed: true`, the requested `clock`, and a `user_profile` object containing only stored synthetic user context. Reject a second owner without changing state. |
| `prepare` | `proposal_id`, logical `operation`, exact `arguments` | Read current authoritative state and return exactly `{"envelope": ...}`. Do not grant or execute a business action. |
| `grant` | `proposal_id`, `principal`, `target_operation`, `arguments`, `decision`, `expires_at`, frozen `envelope`, `schema` | Issue/associate a one-use grant for the exact frozen binding. Return nonempty `id` and `binding_sha256`. IDs never enter the public tool result or model context. |
| `event` | Stable `event_id`, registered `action`, validated `args` | Apply the advertised event idempotently to this lease's test state. `advance_time` returns the resulting `clock`; never move time backwards. |
| `snapshot` | Empty object | Exactly `metrics`, `pending`, `provenance`, read from independent backend state. See below. |
| `release` | Empty object | Revoke unused grants and relinquish ownership; return exactly `{"released": true}`. Preserve unresolved work for diagnosis. |

All adapters need `acquire`, `snapshot` and `release` capabilities. A map with
`commit`/`cancel` effects additionally requires `prepare` and `grant`. Scheduled
service events require `event` and the exact advertised action. `interrupt` is a
host event. Unsupported capabilities, seeds or personas stop before reset.
Registered event arguments remain closed; this does not add arbitrary event
names or executable scenario code.

The adapter must also release ownership/revoke unused authority on disconnect.
A lost acquire response may follow a completed reset: the host stops and closes
the connection instead of resetting again. A lost control response poisons that
connection; it cannot certify a subsequent oracle read. Cleanup has a separate
bounded five-second allowance after the original trial deadline.

## Consent and public execution

The consent envelope has exactly the fields in
`consumer_agent_testing_tool.consent.ENVELOPE_FIELDS`. Its run, task, target, principal,
destination, operation, effect and credential reference must match the host's
configuration and proposal. Resources, state version, price and currency come
from the private adapter's authoritative state.

The host freezes this envelope and arguments before running the
`approval_requested` event and first-match scripted policy. Default is deny.
A matching approval issues a grant whose lifetime is the scenario's
`approvals.grant_seconds` (an integer from 1 to 86,400; default 300) in the
controlled scenario clock. Grant acknowledgment must match
`consent.digest(consent.binding(envelope, schema, arguments))`.

Canonicalization is full RFC 8785 for schemas and arguments: UTF-16 key order
and ECMAScript number serialization. Unsafe integers, NaN, Infinity and unpaired
surrogates are rejected. The binding's `numeric_domain` is
`safe-integer-json-v1` when the schema and arguments contain only integers (the
digests are unchanged from earlier releases) and `rfc8785-number-v1` when either
contains a non-integer number. Adapters must reproduce the label and the
RFC 8785 number form. The envelope's `amount_minor` stays in integer minor
units. Schemas of `commit` and `cancel` tools are checked for canonicalization
when the mapping loads, not during a trial. Omitted material schema defaults
must be made explicit before a transaction can be authorized.

The target must enforce the grant atomically with the state version check,
idempotency record and business effect. Associate it privately by the exact
principal/operation/argument binding. No grant header or credential is supplied
by the model. A changed price or revision must fail at execution and require a
fresh proposal. A host-side approval or acknowledgment alone cannot prove the
service enforced this contract.

Read/draft tools use their declared effects; commits/cancellations always pass
through the private policy, even when mapped to GET. Requests use the same bounded
transport as contract checks: fixed base URL and routes, explicit input/output
validation, source-before-projection evidence, whole-response timeout, byte
budget, no environment proxy, redirects or automatic HTTP retries.

The shared Inspect runner retains call/turn/wall-time limits and one proposal per
turn. Unknown tools, invalid arguments and multi-call turns are returned to the
model as tool errors, and exhausted limits fail the trial; see
[agent mistakes and limits](api-scenarios.md#agent-mistakes-and-limits). Each
public dispatch has a private pending intent before the request. An
invalid/missing write response or HTTP server error stops the trial without
another model generation or mutation retry. Declared error responses, such as a
409 conflict or a 422 validation error with an `application/problem+json` body,
reach the model, which can read state and propose again. Public pending
responses remain unfinished.

## Oracle and evidence

`snapshot.metrics` accepts only the registered `backend.*` scorers with their
exact types. Integer counts must be nonnegative; a boolean is not an integer.
Missing metrics remain unknown. `pending` is a list of unresolved operation
identifiers, and `provenance` identifies the independent data source. Do not
derive these metrics from public success JSON, model text or the approval
decision.

The host combines the oracle with recorded clarification and capability
exposure. Native exposure counts mapped tool definitions in the recorded Inspect
provider input before the first matching proposal. Installed metadata and text
mentions do not count; exposure after selection cannot retroactively pass this
check. This does not certify downstream provider serialization or comprehension.
A pending backend operation or unmatched write intent makes backend
outcome assertions provisional. A failed/malformed oracle is an observation gap,
never zero effects. Semantic answer and OS-isolation claims cannot be supplied
as arbitrary metrics by this adapter.

A developer API scenario may therefore assert only `backend.*` scorers,
`exposure.required_capability` (which needs `evaluation.capability`) and
`user.required_clarification` (which needs `evaluation.clarification`). Other
scorers, such as `answer.no_false_booking_claim` or `isolation.*`, are rejected
when the scenario is validated. A forbidden `production_network` is recorded
under `unobserved_invariants` and as an observation gap, because developer
targets have no traffic oracle.

With a retained lease, finishing the model trajectory does not discard pending
verification. The same resume command can recheck independent state without
more inference or public dispatch. Proven zero-effect violations survive later
oracle gaps or inconsistent counts. The result distinguishes terminal verification
from passing assertions; see [outcome verification](developer-api-continuation.md#verify-a-deferred-outcome).

Reports lock the mapping, adapter descriptor, inference configuration and
transport policy. The run lock's `api_control.identity` omits the per-trial
`run_id`, so trials of one configuration share a lock and a decision contract can
pass. The trial's `run_id` is in the manifest's `api_control_trial_identity` and
the `api_control_described` event; adapters still receive it in the protocol
identity. Public/source responses, projected observations, private decisions and
independent assertions remain separate evidence. Exports redact credentials and
exclude private checkpoint state.

## Limits

Native Inspect tools cannot preserve every JSON Schema 2020-12 feature.
Curated behavioral inputs require descriptions for each parameter and an exact
native schema round trip. The generated baseline retains upstream descriptions
and records its explicit open-object normalization. Unsupported fields, including local `$defs`/`$ref` in the
pinned native interface, are rejected before inference rather than flattened.
Static/contract mapping support remains broader. `ask_user` is reserved for
the private scripted user, and a mapped operation may not use the name. The
mapped and generated lanes always offer it as a free-text tool, even when
`user.replies` is empty: reply ids stay private, an unmatched question gets
`user.fallback`, and the `user_reply` event records `question`, `matched` and
`reply` ([questions and saved profiles](api-scenarios.md#questions-and-saved-profiles)).
The prompt is the shared host prompt plus "Propose exactly one tool call per
turn." ([what the model sees](api-scenarios.md#what-the-model-sees)).

Curated developer APIs support [retained-trial continuation](developer-api-continuation.md)
when the private adapter advertises the required capabilities. Public write
reconciliation requires a declared read-only receipt mapping; saved mutations
are never replayed. [Generated FastMCP continuation](generated-api-continuation.md)
also preserves serialized requests and reconstructs results through the native adapter.
Arbitrary portal restoration, generic pending-operation
reconciliation, calibrated generated OpenAPI comparisons, OS isolation, live provider/cloud
qualification and actual Muse calibration remain separate unfinished work.
