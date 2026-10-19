# Main CLI worker execution

`inspect`, `check`, `doctor`, and `run` accept `--execution worker`. The same
CLI and Inspect coordinator execute on the selected worker, with independent
model and interface selections. `execution.backend` supplies the default;
an explicit `--execution local` overrides it.

Add the placement to an existing model/target configuration:

```yaml
execution:
  backend: worker
  worker:
    gateway_config: worker-config.json
    files: []
    environment: {}
    credential_env: []
    api_control_sockets: []
```

```sh
consumer-agent-testing-tool inspect booking-service --config consumer-agent-testing-tool.yaml --execution worker
consumer-agent-testing-tool run scenarios/ --config consumer-agent-testing-tool.yaml --execution worker \
  --model-profile openrouter-glimmer \
  --interface-profile muse-cli-skills-public-2026-09
consumer-agent-testing-tool worker reap worker-config.json --output runs
```

`gateway_config` selects the existing [worker lifecycle](worker-evidence.md).
For a local reference gateway, use explicit interpreter, installed gateway and
workspace job-directory paths. The gateway configuration's `credential_env`
contains only provisioning credentials. Existing-host SSH requires a matching
Consumer Agent Testing Tool installation and selected adapter dependencies on that host.
The explicit [Python deployment commands](worker-python.md) can prepare that
environment from pinned wheels and generate a gateway configuration.
The [Maritime adapter](worker-maritime.md) instead provisions one agent per
trial from an explicitly prepared template. It uses the same pinned coordinator
and requires a separately operated reaper for cloud resource cleanup.

The controller validates the selected scenarios, model, profile and targets
before provisioning. It packages normalized configuration and scenarios,
the validated fixed-sample decision contract if supplied, and explicit files.
Target skill, API mapping and OpenAPI files are included automatically. Files
must stay inside the workspace, total at most 32 MiB, and cannot replace
generated coordinator paths. Directories and automatic dependency or weight
downloads are not deployment inputs.

Each invocation pins its bundle hash and checks the installed implementation
and required adapter package versions at readiness and again before execution.
An explicitly configured `expected_harness_sha256` is never overridden. That
field pins the complete archive, including invocation identity; omit it for
generated invocations, which supply their own exact archive pin. The normal
run lock additionally records the executing runtime and dependency inventory.

Only selected model and target credentials travel through the private
coordinator input. The worker starts the fixed installed coordinator, not an
arbitrary bundled program, with those values in its environment. Its existing
tool and container launchers retain their separate environment controls.
Credentials never enter the bundle. Loader and runtime-control environment
names are rejected as credential references. Optional IAM references are
limited to explicitly selected AWS access-key/session variables and an IAM
model; no ambient profile directory is copied.

`environment` accepts only `DOCKER_HOST`, `DOCKER_CONFIG`,
`CONSUMER_AGENT_TESTING_TOOL_TOOLING`, and `AWS_EC2_METADATA_DISABLED`. These describe
preconfigured worker resources, not laptop resources presumed reachable
remotely. [Container deployment](worker-containers.md) can import selected images
and pin their connection through `container_images` receipts. The Docker engine,
socket access and local inference artifacts must already be set up on the worker. Browser
and Node tooling assets can be supplied through an explicit pinned
[`runtime` archive](worker-runtime.md). Worker HOME/cache/temp directories
belong to its job.

Developer API behavioral trials also need their private control service on the
worker. List its exact absolute socket path in `api_control_sockets`, matching
the selected target's `api_control.socket`. The fixed coordinator enables only
selected, pinned paths; ownership, permissions and symlink checks still apply.
The execution lock records path hashes; raw paths stay in the private invocation
so public redaction does not change continuation identity.
The default local lane remains restricted to workspace sockets. This selection
does not deploy or tunnel the service, and the socket is never exposed as an
agent tool.

The ordinary manifest, events, Inspect logs, report and notices are collected
through the verified public archive before cleanup. A `worker_execution`
receipt records the particular trial, deadline, input digest and invocation.
The run lock's `execution` field records stable configuration separately, so
distinct trial IDs cannot manufacture statistical configuration drift.
Scenario outcomes and worker cleanup outcomes remain separate.

Recovery uses the original selected credential values to redact uncollected
worker output. If they are unavailable or rotated, collection fails before
requesting that output; cleanup may still succeed. A previously verified public
export can be reused without credentials. Public exports exclude raw
continuation and browser media. Explicitly resumable runs collect a separate
[private checkpoint](worker-continuation.md) before cleanup.

## Limits

The local reference proves process lifecycle and integration, not cloud
provisioning or hostile coordinator isolation. A named cloud provider still
needs its own provision/run/export/destroy and independent-expiry proof.

`--offline` rejects worker placement before requests. API and browser scenarios
can use `--resumable`; `--resume` selects one collected private checkpoint with
the same scenario and execution configuration. Public evidence cannot restore
private continuation state. Python environment deployment requires a compatible
preinstalled CPython; container deployment requires an existing Docker engine.
[Negotiated polling](worker-polling.md) now allows a job to outlast one gateway
request. The total TTL must still cover the chosen invocation; a large batch
can exceed it and will fail rather than silently complete a smaller sample.

Local tests exercise real HTTP SDK calls to a synthetic OpenRouter endpoint,
the existing Inspect API and Docker CLI lanes, independent false-success
detection, changed inputs/harness rejection and scoped credentials. They do
not make paid inference requests or qualify hosted model behavior. Fresh
installation separately exercises the main CLI's static worker selection.

- [Focused checks](release-evidence/worker-selector-focused-2026-10-13.xml)
- [Installed worker artifact verification](release-evidence/worker-selector-installed-2026-10-13.json)
- [Fresh-install log](release-evidence/worker-selector-distribution-2026-10-13.log)
