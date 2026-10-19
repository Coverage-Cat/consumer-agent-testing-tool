# Developer-maintained HTTP mappings

`inspect` and `check --suite contract` accept a `kind: api` target with an explicit
request/response map. They run without inference. The HTTP target can be a local
service or an explicitly selected HTTPS staging endpoint. This is the curated
mapping lane; it does not import OpenAPI, generate a client, or claim to reproduce
Muse's API wrapping.

See `examples/api-target.yaml` and
`src/consumer_agent_testing_tool/data/api-mappings/catalog.yaml`. `init` copies the example map
into the new project's `api-mappings/` directory. Change the endpoint, source
revision, map and cases to match your test service.

```sh
consumer-agent-testing-tool inspect catalog --config examples/api-target.yaml
consumer-agent-testing-tool check catalog --config examples/api-target.yaml --suite contract
```

Inspection reads the local mapping only and does not require a target credential.
Contract execution inserts the separately referenced credential in the trusted
HTTP client, as `Authorization: Bearer` or in the mapping's `credential_header`.
It never treats an inference key as target authorization.
Mapping files must stay inside the workspace, including resolved symlink paths.
The default API interface is `api-tools-surrogate-v1`; the evidence records the
actual adapter, `curated-http-v1`, and developer-supplied mapping provenance.

## Mapping contract

The machine-readable shape is `schemas/api-mapping-v1.schema.json`, also shipped
in the package. Runtime validation additionally checks cross-field relationships,
supported references, route placeholders, argument coverage and projections.

Each named tool declares a stable operation ID, description, business effect,
closed input object schema, request map and exact response statuses.

- Every input property maps exactly once to a path placeholder, query parameter,
  JSON body property or request header. Nothing is silently dropped or copied to
  another place.
- Path inputs are required strings or integers. Dot segments, separators and
  encoded escape attempts are rejected.
- Query inputs serialize scalar strings, booleans and finite numbers. Missing
  optional inputs are omitted. Array/object/null query styles require another
  mapping version; their schemas are rejected before exposure rather than
  failing only after a request is proposed. Path/query schemas must declare
  their scalar types directly or through a local `$defs` reference. Supported
  scalar type unions remain unchanged.
- JSON bodies preserve explicit nulls, nested values and union semantics.
  Schema defaults are annotations and are not automatically inserted.
- `request.header_parameters` maps header names to input properties, for headers
  such as `Idempotency-Key` or `If-Match`. Their schemas may allow only strings
  or integers. A value must be 1 to 1,024 visible ASCII characters without
  leading or trailing spaces. Any other value is rejected before the request is
  sent; in behavioral trials the model receives it as an invalid-argument error.
  Names must be valid field names, unique ignoring case. The `network_request`
  event records the header names, not their values. The values are tool
  arguments and appear in the proposal evidence like any other argument.
- The schema dialect is JSON Schema 2020-12. Document-local `$defs` references
  are supported. External references, alternate schema IDs and dynamic anchors
  are rejected before execution. Unknown formats cannot silently pass.
- Responses declare exact HTTP status codes and JSON schemas. `schema: null`
  means an empty response body. JSON responses may use `application/json` or
  any `application/*+json` type, such as `application/problem+json`; malformed
  or duplicate-key JSON fails the check. A declared error status, such as a 409
  conflict or a 422 validation error, is an ordinary result: in behavioral
  trials the model receives its body.
- An optional `project` list selects explicit top-level response properties.
  Source validation runs before projection, and projected validation follows.
  Root relational constraints that cannot be preserved cause mapping rejection.
  Both source and effective schemas remain in the run lock and inspection view.

The host owns credential, framing and routing headers, so `header_parameters`
cannot name `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`,
`Host`, `Accept` or `accept-*`, any `content-*` header, `Range`, `If-Range`,
`Max-Forwards`, `Forwarded`, `Via`, `Origin`, `Referer`, the connection headers
(`Connection`, `Keep-Alive`, `Upgrade`, `TE`, `Trailer`, `Expect`,
`Transfer-Encoding`), `X-Forwarded` or `x-forwarded-*`, method, URL and host
override headers, client-IP headers, `proxy-*`, `sec-*` or `x-envoy-*`. Names
that look like credentials are refused too: any containing `auth`, `token`,
`secret`, `passw`, `credential`, `session`, `cookie`, `signature`, `csrf`,
`xsrf`, `jwt`, `api-key` or `access-key`, or `key` as a name part, except
`Idempotency-Key` and `X-Idempotency-Key`.

An optional top-level `credential_header` names an API-key header, such as
`X-API-Key`, for the target's host-held credential. Without it the credential
is sent as `Authorization: Bearer`. The value always comes from the target's
`auth.env`, never from a model argument, and no header parameter may name the
credential header. Contract checks and behavioral trials place the credential
the same way.

The `effect` field is explicit: `read`, `draft`, `commit` or `cancel`. HTTP method
does not establish business effect. Static inspection can show all four.
Contract cases currently permit only reads and nonbinding drafts, and each case
must repeat the mapping's effect. [Behavioral API trials](api-behavior.md) add
committing workflows through a private lifecycle/consent/oracle adapter.
Labeling a GET as read does not prove safety.

## Contract execution and evidence

All cases are validated before the first request. Each case names a mapped tool,
arguments, effect and optional `expect_error`. Requests use the declared base URL
and route, with no arbitrary model-supplied URLs, headers or executable code.
Redirect following, environment proxies and automatic HTTP retries are disabled.
The target's timeout is a whole-response deadline; decoded response bytes also
have a configured limit.

Source JSON responses and projected tool results are separate redacted evidence
events. The report records status, content type, retry hints and elapsed time.
No model request is generated. Published, exposed and observed views therefore
do not imply the tool was selected by an assistant.

A transport failure is infrastructure failure. No mutation retry or subsequent
case is sent after an uncertain draft request. A failed draft response likewise
stops the remaining case plan. Source/schema/content-type/expectation failures
are contract failures. An expected declared error can pass its response contract.

A passing contract is not a verified backend task outcome. These mappings do
not reset a service, obtain domain grants, read a private oracle or isolate the
target's outbound traffic. Those gaps are recorded rather than replaced with a
success string. Separately, `run --surface api-tools` supports local developer
APIs configured with the [private behavioral adapter](api-behavior.md).

`--offline` rejects remote targets before creating an HTTP client and uses the
existing coordinator egress policy for local endpoints. Spectral, Prism,
Schemathesis and the FastMCP-generated baseline use separately selected
adapters; none is reported as having run by these curated checks.
