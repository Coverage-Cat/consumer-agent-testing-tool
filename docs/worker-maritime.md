# Maritime worker adapter

The optional `maritime` provider places the existing Inspect coordinator on a
new agent for each trial. It implements provision, readiness, seed, start/poll,
export, cancellation and resource deletion through Maritime's documented REST
API. Runtime and private-checkpoint transfers reuse the existing worker
protocols. Inference, target access and interface profiles remain independently
selected. No provider is contacted merely by loading this configuration.

This is an implemented adapter with local contract and subprocess tests.
**Live cloud execution and independently scheduled resource cleanup have not
been qualified.** The release's cloud gate remains unverified.

## Prerequisites and selection

Explicitly select a template that contains a compatible POSIX runtime and the
matching Consumer Agent Testing Tool installation at the configured Python path. It must
support detached execution. Use a dedicated test template without a separately
running assistant, automatic chat/inbox processing or ambient inference keys.
No model weights, Python environment or Docker engine are installed by this
adapter. The normal CLI checks installed harness and dependency identities
before seeding and inference.

Place this configuration in the workspace as `worker-maritime.json`:

```json
{
  "provider": "maritime",
  "credential_env": ["MARITIME_API_KEY"],
  "ttl_seconds": 300,
  "call_timeout_seconds": 60,
  "run_mode": "polling",
  "poll_interval_seconds": 1,
  "maritime": {
    "api_url": "https://api.maritime.sh",
    "api_key_env": "MARITIME_API_KEY",
    "namespace": "my-consumer-agent-testing-tool-tests",
    "template_id": "YOUR_PREPARED_TEST_TEMPLATE",
    "python": "/opt/consumer-agent-testing-tool/bin/python",
    "cleanup": "external-reaper-required"
  }
}
```

Use a dedicated namespace and explicitly supplied provisioning key authorized
for agent management, files and detached exec. That key is separate from the
selected inference/target keys. Set `execution.worker.gateway_config` to this
file in the [ordinary worker placement](worker-run.md), then select
`--execution worker`. `--offline` refuses worker execution before provisioning.
The low-level `worker run` command additionally requires the existing exact
bundle digest pin.

Browser/CLI container prerequisites still belong to the prepared template.
Local or SSH container deployment receipts cannot authorize a Maritime
connection. This adapter does not copy laptop sockets, deploy a Docker engine,
or qualify the browser/container isolation boundary. Neither a template name
nor a remote runtime report authenticates the provider's host implementation.

## Lifecycle and recovery

Before its first HTTP request, the adapter durably records trial identity and
the original deadline under `.cache/maritime/<configuration-digest>/`. Keep
that private state alongside the ordinary worker journals. Each unique trial
uses its own namespaced end-user identity and the idempotent end-user agent
creation route. It does not adopt an unexpected preexisting agent, reuse a
pool, or renew a deadline on retry.

Requests go to the selected HTTPS origin with redirects and ambient proxies
disabled. Volume paths come from the provider's file listing, rather than an
assumed `/data` mount. Private input files are uploaded to an explicitly named,
mode-0700 directory. They are never sent as chat attachments. Credentials do
not enter command arguments; the bridge reads and removes each input and
clears its inherited environment before invoking the existing trusted
gateway/guard. The ordinary coordinator receives only its separately selected
inference/target keys. Provider error bodies and exec stdout/stderr are not
copied into public diagnostics.

Every exec is submitted once, then observed by its execution ID. The only
submission retries are the documented `vm_not_ready`/`guest_command_unavailable`
responses with `execution_started: false`. Ambiguous errors, lost replies,
older attached-only agents and malformed execution identities never trigger
automatic exec resubmission. The worker's separate admission and process
leases continue to prevent task restarts. Result JSON is collected through
bounded file downloads because provider exec output may be truncated.

Public exports are hash-verified and journaled before deletion is requested.
Private checkpoints remain opt-in. Deletion uses the provider's agent-delete
route, which covers its data volume, then verifies that the agent and the
trial's end-user record are unavailable. A lost delete reply can be reconciled
using the retained resource ID. Resource cleanup does not imply successful
execution or complete evidence collection. If a create reply is lost and no
resource is yet visible, cleanup stays unconfirmed; a reaper must revisit the
original identity instead of declaring success or creating another trial.

Retired local identity records are retained to prevent reuse. Do not erase
them to retry a failed trial.

## Independent resource cleanup

The remote process guard enforces the task deadline. **That does not delete
the provider's agent or volume.** Maritime's documented sleep/restart operations
are not resource TTL cleanup, so the adapter does not claim a provider TTL.
Readiness evidence explicitly reports `resource_ttl_verified: false`.

Before a live run, arrange an independently scheduled control-plane invocation:

```sh
consumer-agent-testing-tool worker reap worker-maritime.json --output runs
```

It must retain the exact gateway configuration, both journal/state directories,
and access to the selected provisioning key. Use the original `--output`
directory for a differently placed run. A per-trial file lock prevents
coordinator/reaper admission races; a busy trial must be revisited. The command
does not run periodically merely because it is shown here: deployment and
monitoring of that independent schedule remain the operator's responsibility.
A process on the same machine does not protect against loss of that machine
or its disk. Missing redaction credentials can prevent complete export;
cleanup and the resulting evidence failure remain separately recorded.

Live qualification must demonstrate provision/run/export/delete, actual volume
cleanup, and cleanup after losing the coordinator, using the independently
deployed reaper. Local HTTP mocks, successful task expiry or a manually invoked
reaper do not establish that cloud guarantee.

## Evidence scope

The tests use a local HTTP contract transport with actual Python subprocesses.
They exercise ordinary CLI inspection, pinned harness checks, complete chunked
exports, and native Inspect execution against a synthetic loopback OpenRouter
SDK endpoint. The latter checks Phala/ZDR request settings, the scoped
credential path and an independent draft/effect oracle. It makes no hosted
model call and has no real shopper/payment data.

Additional cases cover lost create/start/delete replies, late resource
appearance, foreign resources, unsafe configuration, changed execution
identities, oversized responses, redirects, explicit non-admission retries,
workspace locks, and forbidden provisioning-key forwarding.

API contract reference: Maritime's public REST API documentation at
`https://maritime.sh/docs/api`, read on 14 October 2026. No dashboard-internal
endpoints, agent chat API, delegated insurance tools or production insurance
services are used.
