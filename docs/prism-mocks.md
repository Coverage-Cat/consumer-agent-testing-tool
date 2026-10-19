# Stateless Prism mocks

Select `openapi.adapter: prism-mock` to exercise an OpenAPI document without a
running API or inference account. Actual pinned Prism synthesizes responses
from examples or static schema sampling; Schemathesis supplies bounded positive
and negative requests and independently checks those synthesized contracts.
Spectral checks the source first.

A passing mock run establishes agreement between the source and this named
mock configuration. It does not test the developer's running service, create
agent tools, verify an account, or establish a backend outcome.

## Configure and run

Install the existing optional API group and its pinned Node dependencies. The
packaged `openapi/inventory.yaml` example is created by `consumer-agent-testing-tool init`.

```yaml
targets:
  inventory-mock:
    kind: api
    transport: http
    source_revision: inventory-test-v1
    base_url: http://127.0.0.1:8765/v1
    permitted_destinations: [http://127.0.0.1:8765/v1]
    openapi:
      path: openapi/inventory.yaml
      adapter: prism-mock
      operations:
        listItems: read
      examples: 2
      seed: 42
      modes: [positive, negative]
      wall_seconds: 90
```

The loopback address is a virtual base for request serialization. Nothing needs
to listen there, and the mock mode creates no HTTP client. Source `servers`
URLs do not select a destination.

```sh
consumer-agent-testing-tool inspect inventory-mock --offline
consumer-agent-testing-tool check inventory-mock --suite contract --offline
```

Static inspection emits source/normalized-contract views without generating
responses. Contract checks record each mock response and its actual upstream
diagnostics, linked to the serialized request and Prism's normalized URL/query
input. Request bodies have a redacted UTF-8 view, original byte count/digest,
and an explicit flag if non-UTF-8 bytes could not be represented faithfully.
Failure to generate a required case remains an observation gap.
Invalid examples can fail Spectral before synthesis. A Prism error with no
synthesized response is recorded as `prism_mock_error`, never invented as an
HTTP exchange.

## Boundaries

The worker uses `isProxy: false`, `dynamic: false`, input/output validation,
security-schema validation and a disabled forwarding extension. Selected
operations with callbacks are unsupported: Prism's regular mocker can execute
callbacks, which this stateless profile does not admit. External references and
external example fetches remain unsupported.

Target credentials, private lifecycle/consent adapters and explicit mapped
contract cases are rejected in the descriptor. Schemathesis does not generate
authentication credentials. Security-schema validation is not authentication
against a real issuer, principal, session or permission scope.

Declared commit/cancel operations may be synthesized, but no grant is issued and
no business effect occurs. There is no persistent cart, reservation, payment
state, callback queue or backend oracle. Use the actual test service with its
private control adapter for those behavioral checks.

The existing macOS worker boundary denies outbound networking. With `--offline`,
the CLI launches its fixed workers under their own policies, then seals the
coordinator before upstream schema processing or case generation. The resulting
coordinator policy allows no TCP/UDP destination or Unix connection. Its
enforcement state is recorded only after the kernel confirms the policy.
Unsupported platforms or failed startup cannot produce a passing run.

The mock result uses zero synthetic response elapsed time; it is not API latency.
Source, settings, dependency locks and normalized contracts remain in the run
record. Sensitive cookie/token examples are registered before upstream
diagnostics can quote them, including examples reached through local references.
Standard exports use the usual text redaction.

## Verification

Source tests exercise real Prism synthesis, positive/negative requests, examples,
invalid source examples, callback rejection, missing validation responses,
response budgets, redaction and a live destination canary. Offline CLI tests
also probe direct TCP and UDP calls after checking both worker and coordinator
kernel policies. The distribution verifier includes the actual installed mock
and offline CLI paths.

These checks do not qualify browser isolation, target isolation, live models,
cloud workers or real-Muse behavior.
