# Optional OpenAPI validation

The default OpenAPI validation profile runs the pinned Spectral, Prism and Schemathesis libraries.
It performs no inference and creates no agent tools. The separately selected
[FastMCP baseline](generated-api-baseline.md) exposes generated MCP tools for
static inspection, explicit contract cases and Inspect behavioral trials.
Both generated and curated behavioral trials require the private control
adapter for consent and independent backend assertions.
The separate [stateless Prism mock mode](prism-mocks.md) synthesizes response
contracts without contacting a service. Its results never count as direct API
or backend verification.

Install this group separately from core dependencies:

```sh
uv sync --extra dev --extra api
(cd tooling/api && npm ci --ignore-scripts)
consumer-agent-testing-tool init --provider none
```

The current worker boundary requires macOS Seatbelt and Node 24.18.0 or newer.
An unsupported platform fails before a validator or HTTP request runs.
Workers must start before any host Seatbelt policy: macOS rejects nested
sandbox initialization. An already network-sandboxed host is reported as
unsupported, rather than running the validators without their boundary.
`init` copies the optional manifests and `openapi/inventory.yaml` without
overwriting local files. The example is a contract; it does not start a service.
The tooling accepts OpenAPI 2 and 3 only. A local stub replaces Prism's
`postman-collection` dependency and rejects Postman collections; keep the
copied `.npmrc` and `stubs/` beside the lockfile, or `npm ci` fails.

Add an API target to an initialized configuration:

```yaml
targets:
  inventory:
    kind: api
    transport: http
    source_revision: inventory-test-v1
    base_url: http://127.0.0.1:8765/v1
    permitted_destinations: [http://127.0.0.1:8765/v1]
    openapi:
      path: openapi/inventory.yaml
      operations:
        listItems: read
      examples: 5
      seed: 42
      modes: [positive, negative]
      wall_seconds: 90
```

```sh
consumer-agent-testing-tool inspect inventory
consumer-agent-testing-tool check inventory --suite static
consumer-agent-testing-tool check inventory --suite contract
```

`inspect` and the static suite run Spectral and Prism conversion without HTTP.
The contract suite generates cases before dispatch, validates routes, then sends
bounded requests to the explicit loopback target. Source `servers` URLs are
recorded but ignored for execution. Operation IDs must resolve uniquely.
Declare each operation's business effect in configuration; an HTTP verb or
`x-consumer-agent-testing-tool-effect` description cannot authorize execution.

The fixed Spectral extensions report separate, source-located warnings for
missing purpose, ambiguous names, unstated money/time units, contradictory
required/optional wording, missing recovery guidance, unclear preconditions,
absolute or host-equivalence claims, and undocumented business effects.
They inspect the source operations, inherited parameters, nested request/response
fields and shared component definitions directly. Shared-definition findings
point to the definition, without claiming to have merged its wording into every
reference site. Deeply nested schemas produce an explicit coverage warning.
Presence requirements that depend on schema composition are not guessed.

These are English-language wording heuristics, not proof that a description is
correct or a claim is false. Each warning has a stable `surface-agent-*` rule
identifier and a source path. Warnings do not fail conformance; malformed
OpenAPI contracts still fail through the upstream schema rules. Neither
descriptions nor effect annotations grant permission to execute an operation.

Generated execution accepts `read` and `draft`. A draft means a nonbinding test
mutation; the developer remains responsible for that declaration and test
fixture cleanup. Commit/cancel operations need the separate behavioral consent
and reconciliation lane. Failed or uncertain drafts stop later dispatch.
Transport failures or a dead validator also stop dispatch. There are no
automatic HTTP retries, redirects, stateful links or executed-case shrinking.

The source must be OpenAPI 3.0/3.1, at most 1 MiB, with document-local references.
External references/examples and dynamic schema scopes are rejected. Generation
cannot set private authentication, cookie, routing or framing parameters.
An optional target bearer credential uses the existing environment reference;
only the host inserts it. Validator processes receive a clean environment and
run under a kernel policy denying all outbound networking.

Generation is capped at 200 cases across selected operations/modes, with a
whole-suite deadline. Schemathesis can generate fewer cases for finite input
spaces. No possible negative case is an observation gap, not a passed negative
test. Route escapes, unsupported wire data and budgets also remain visible
instead of being silently skipped.

The host retains bounded response bytes and repeated headers. Schemathesis
receives the native generated case, original metadata, and those response
headers. Prism receives its normalized operation contract, decoded query and
UTF-8 body. Non-UTF-8 bodies are explicitly unsupported by this adapter.
Prism's flat header representation is recorded separately from the native
response header list. A malformed declared JSON body fails validation.
Repeated private headers retain their field names for redaction. Their values
are also registered before recording validator messages that may quote them.

The fixed Schemathesis check set covers server errors, status, content type,
response headers/schema and positive/negative data acceptance. Checks that can
resend requests are excluded. No project hooks, custom Python code or upstream
configuration files are loaded. These checks establish public response
conformance; they cannot establish committed backend state, absence of side
effects, target OS isolation or real-Muse fidelity.

Evidence distinguishes the published OpenAPI source, Prism's normalized
validation contracts, actual generated cases, validator results and missing
model input. Run locks record source/settings, package versions, dependency
hashes and host transformations. Body evidence passes through text redaction;
the private worker wire format is not exported as a base64 copy.

## Upstream boundary and maintenance

The optional group pins Schemathesis 4.30.1, Spectral core 1.24.0/rulesets 1.23.0,
Prism HTTP 5.16.0 and HTTP spec 7.1.0. There are no vendored forks.
The Prism adapter calls its pinned internal `dist/validator` export to validate
requests/responses without starting a forwarding proxy. The Schemathesis
adapter uses its native response wrapper to preserve repeated headers.
These are explicit compatibility boundaries: dependency upgrades require
the local fault suite and fresh installed-wheel proof. Replace the internal
Prism import with a stable validation-only API when upstream provides one.

The distribution proof installs this group in a separate fresh environment,
inventories its original notices and runs real positive/negative checks against
an owned HTTP fixture. It must also reject a deliberately broken response.
The installed proof launches the fixed workers before applying the host's
exact-port kernel policy, and checks the host and worker boundaries before
accepting a result. Core installs remain independent. Generated tools use the
separate `fastmcp-generated` adapter; comparative generated-agent trials remain
unfinished.
