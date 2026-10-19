# Generated OpenAPI baseline

Set `openapi.adapter: fastmcp-generated` to inspect and exercise a workbench-
generated MCP interface. The optional API dependency group pins FastMCP and
FastMCP Slim 4.1.0. The normal `validation` adapter continues to run direct
Spectral, Prism and Schemathesis checks separately.

This baseline supports static inspection, explicit contract cases and Inspect
behavioral trials with private lifecycle, consent and backend assertion controls.
[Generated continuation](generated-api-continuation.md) retains test-state leases
and native response checkpoints. Real-Muse comparison remains unfinished.

After installing `consumer-agent-testing-tool[api]`, add a target to your configuration:

```yaml
targets:
  inventory-generated:
    kind: api
    transport: http
    source_revision: inventory-test-v1
    base_url: http://127.0.0.1:8765/v1
    permitted_destinations: [http://127.0.0.1:8765/v1]
    openapi:
      path: openapi/inventory.yaml
      adapter: fastmcp-generated
      operations:
        listItems: read
      wall_seconds: 90
    contract_cases:
      - tool: listItems
        arguments: {count: 1}
        effect: read
```

```sh
consumer-agent-testing-tool inspect inventory-generated
consumer-agent-testing-tool check inventory-generated --suite contract
```

Inspect before naming contract cases: FastMCP can rename operations, flatten
arguments, generate descriptions and wrap output schemas. The report retains
the original OpenAPI document, native generated catalog, source-operation
associations and a projection diff. It does not claim the model received these
tools during a static or contract run, or that this is Muse's API wrapper.

Only explicitly selected, uniquely identified operations enter the catalog.
Static inspection sends no target requests. Contract cases must match generated
tool names and developer-declared effects; reads and nonbinding drafts are
allowed. Contract cases cannot commit or cancel; those effects require the
behavioral lane and its private consent adapter. Nondefault
Schemathesis `examples`, `seed` and `modes` are rejected because cases here are
explicit. Contract checks do not generate cases or invoke a model.

## Behavioral trials

Add the `api_control` socket and principal described in
[Developer API behavioral trials](api-behavior.md). Use the same
`api-tools-surrogate-v1` scenario profile and CLI. The service adapter must pin
the generated mapping record, supply an exclusive test-state lease, and enforce
transaction grants atomically with its business effects.

For generated targets, the private `mapping_sha256` is the digest of
`GeneratedMapping.model_dump()`: the source document, actual native catalog,
effective tool schemas, source-operation associations, pinned generator versions
and runtime settings.
The adapter must obtain that expected record independently; it must not accept
whatever hash arrives in the host's handshake. Logical operation names in
private consent envelopes are the source `operationId` values. Public tool names
come from the generated catalog and can differ.

When a native input schema omits top-level `additionalProperties`, the host makes
it explicitly `true`: JSON Schema allows extra fields by default, but Inspect
otherwise defaults to `false`. The untouched native catalog and this effective
schema are both retained. Other schema constraints must survive the pinned
Inspect serialization unchanged. Unsupported names or lossy conversions stop
before reset or behavioral model generation. The model receives generated tool
descriptions and those effective schemas, plus the common synthetic workflow
instructions. Tool results use
a JSON envelope containing the HTTP status and native MCP result. This is an
explicit host transformation, not raw MCP transport parity or a Muse claim.

Before consent, the actual generator serializes the proposed request inside its
network-denied worker. The host validates and freezes that request. A denial
cancels it without target HTTP; approval can dispatch only the identical
proposal. Omitted material defaults cannot silently change a transaction's
binding. HTTP server errors, lost responses or native output validation failures
after a write stop the trial without another mutation or model generation.

The common private oracle supplies backend metrics. Public success text cannot
substitute for it. Unresolved work leaves success counts provisional; a validated
unauthorized or duplicate effect still fails a zero-effects assertion.
Actual native schemas in model input support exposure measurement independently
of selection and backend success. Private controls, approval rules, target
credentials and grant IDs do not enter model context.

## Request and result boundaries

FastMCP runs in the existing macOS worker boundary, with kernel-denied outbound
networking. Its caller-supplied HTTP transport hands serialized requests back
to the trusted host. The host checks the method, exact target origin and route,
path parameters, headers and body budget before dispatch. Source `servers`
URLs cannot change the destination. Neither a generated read-only annotation
nor an HTTP verb grants permission.

The host inserts the separately referenced target credential after validation.
Generated workers receive no target credentials, inherited provider environment,
ambient `.env` configuration, telemetry or update checks. Private response
headers are withheld, and both client cookie jars are cleared after each
response; this is not a session/authentication-flow profile.

The returned response passes through the native FastMCP/MCP client. Source
HTTP responses and generated MCP results remain separate evidence. Invalid
structured output can cause native client validation to reject the result;
the report retains that rejection and the prior source response, without
inventing an unavailable parsed result.

Requests have the target's existing timeout and byte limits plus a whole-run
deadline. There are no HTTP redirects, proxies, retries, repeated requests
within one tool call, or network fallback. A failed/uncertain draft stops
later cases. Pending request identities cannot be replaced or mismatched.
These boundaries do not establish the target's own OS isolation or an
independent business outcome.

## Observed conversion differences

The local tests establish two limitations for the pinned baseline:

- An explicit null query parameter is omitted by the upstream serializer,
  making it indistinguishable from omission on the wire.
- A tool can attempt HTTP for an argument below its advertised numeric
  minimum. Consumer Agent Testing Tool records the wrapper-enforcement failure and blocks
  that request before target dispatch, even when the case expected an error.

These observations are not repaired by silently rewriting the upstream
generator. A passing direct API check does not establish that the generated
wrapper preserves its source contract. Curated mappings retain their separate
source/effective-schema checks.

The distribution verifier checks actual generated catalog inspection, native
calls to an owned loopback API and rejection of broken responses from the
installed wheel. Its behavioral stages use scripted Inspect proposals with
independent SQLite state, checking an approved commit, a denied request and
false public success. It checks worker and host kernel egress policies.
This is synthetic harness evidence, not live model or Muse calibration.
