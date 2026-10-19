# Test a developer CLI

The local CLI adapter keeps the developer's CLI and backend in separate images.
The trusted sandbox image supplies the firewall namespaces and independent
forbidden canary; the developer images receive no networking capabilities, host
mounts, provider credentials, or Docker socket.

The CLI image needs `/bin/sh`, `/bin/sleep`, `/usr/bin/timeout`, and Python 3 for
Inspect's skill-file operations. Install the actual CLI in that image. Images must
already be built locally; runs resolve their immutable image IDs and never pull.

The service listens on `0.0.0.0:8765` (or the configured `service_port`). The CLI
receives `CONSUMER_AGENT_TESTING_TOOL_TARGET_URL` and `FIXTURE_IP`. The only permitted outbound
connection from the evaluated CLI is to that service port. Port 8766 belongs to
the forbidden canary.

```yaml
targets:
  booking-service:
    kind: cli-skill
    source_revision: "your-reviewed-commit"
    container_image: your-booking-cli:tested
    service_image: your-booking-service:tested
    command: ["python", "/app/server.py"]
    cli_command: ["booking"]
    service_port: 8765
    readiness: ["test", "-S", "/control/oracle.sock"]
    oracle_command: ["python", "/app/control.py"]
    skill: ./skills/booking/SKILL.md
    operation_schemas:
      reservations.create:
        type: object
        properties:
          slot_id: {type: string}
          contact: {type: string}
          idempotency_key: {type: string}
        required: [slot_id, contact, idempotency_key]
        additionalProperties: false
```

`readiness` and `oracle_command` execute only in the service container through
trusted Docker control. The evaluated CLI cannot invoke them. The service gets
`CONSUMER_AGENT_TESTING_TOOL_RUN_ID`, `SERVICE_PORT`, and disposable writable `/data`, `/control`,
and `/tmp` directories; its root filesystem is read-only.

The oracle accepts one JSON line on stdin:

```json
{"operation":"reset","run_id":"r-...","clock":"2026-10-01T17:00:00Z"}
```

Reset must finish before returning a successful JSON object. Snapshot requests
use `operation: snapshot` and return the read/draft reference schema: `runs`,
`slots`, `reservations`, `operations`, `effects`, `requests`, and optionally
`attempts`. See `Fixture.snapshot()` for column names. Snapshot assertions operate
on that independent state, never on the CLI's success text. This is an explicit
reservation-workflow mapping, not a universal backend oracle.

Before its final snapshot, the runner stops the evaluated CLI container and
sends a private finalization request:

```json
{"operation":"quiesce","run_id":"r-..."}
```

The service must close public ingress, finish accepted requests, and ensure no
background work can change the asserted outcome before returning:

```json
{"quiesced":true,"run_id":"r-..."}
```

This operation must be idempotent and leave the private snapshot command
available. A closed client connection is insufficient: an accepted request may
still commit. A connector with unresolved remote work must return an error or
an unconfirmed result. Missing, malformed or wrong-trial acknowledgements
produce a required observation gap. Available positive-effect observations
are still retained; no zero-effect success is inferred. See
[final CLI snapshots](shell-finalization.md).

`consumer-agent-testing-tool inspect` executes the configured CLI's actual `--help` in this
boundary and preserves its output, exit code, skill digest, image IDs, and supplied
operation schemas. Relative skill and model-artifact paths resolve against the
configuration file's directory.

The real-container test `test_inspect_loads_skill_and_runs_draft_in_actual_container`
exercises both the built-in fixture and this custom adapter. Its scripted model
tests composition; live inference qualification is separate.

## A connector service that reaches owned staging

The service image can instead contain the developer's connector worker for an
existing staging API. Declare `base_url`, list its HTTPS origin in
`permitted_destinations`, and reference a separate target credential:

```yaml
base_url: https://staging.example.test/api
permitted_destinations: ["https://staging.example.test"]
auth:
  mode: api_key
  env: STAGING_API_CREDENTIAL
```

The trusted connector worker receives `CONSUMER_AGENT_TESTING_TOOL_REMOTE_URL` and
`CONSUMER_AGENT_TESTING_TOOL_TARGET_TOKEN`. It owns the actual API mapping, TLS verification,
credential insertion, and independent oracle implementation. The evaluated CLI
receives neither credential nor direct staging connectivity. This is an explicit
developer-supplied connector mapping; the workbench does not generate a REST
behavioral driver in M1.

The namespace firewall permits only the declared IPv4 address/port pairs resolved
at setup. Hostname mappings are pinned for the trial, and all other outbound
traffic remains denied. The policy is an **origin policy**, not a URL-path policy;
the connector worker must enforce operation/path and account scope. Loopback,
metadata/link-local, multicast and host aliases are rejected. IPv6-only origins
are unsupported. `--offline` rejects this remote configuration before execution.

The run lock records resolved destinations and worker/CLI image IDs. The owned
canary test verifies an allowed connector connection, a blocked neighboring port,
blocked direct CLI access, and target credentials absent from the CLI environment.
It tests network enforcement, not the TLS or API behavior of an arbitrary worker.
