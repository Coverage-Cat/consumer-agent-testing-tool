# Isolated embedded consumer apps

`surface-run` can explicitly select the same Linux ARM64 worker used by the
restricted browser. It supports both standard MCP Apps and the legacy bridge,
with the existing scripted and Inspect drivers. The worker is opt-in; native
rendering does not inherit its isolation. Full browser-isolation qualification
and real-Muse equivalence remain open.

Prepare the workspace-owned VM and immutable image using the
[browser worker setup](browser-worker.md). Add `browser_worker` to the surface
scenario, rather than `execution.browser_worker` in the model configuration:

```python
import json
from pathlib import Path
import yaml

scenario = yaml.safe_load(Path("examples/surface-preferences.yaml").read_text())
connection = json.loads(Path(".cache/browser-vm/connection.json").read_text())
image = json.loads(Path(".cache/browser-vm/worker-image.json").read_text())["image"]
scenario["browser_worker"] = {
    "image": image,
    "docker_socket": connection["docker_socket"],
    "docker_config": connection["docker_config"],
    "destinations": {},
    "rules": [],
}
Path(".cache/isolated-preferences.json").write_text(json.dumps(scenario, indent=2))
```

With the synthetic preferences fixture running on the endpoint in that
scenario:

```sh
.venv/bin/consumer-agent-testing-tool surface-run .cache/isolated-preferences.json
```

This self-contained app needs no external browser origins. The host separately
uses the scenario's permitted MCP endpoint. Worker admission checks the explicit
workspace Docker socket, configuration and immutable image before opening MCP
or starting inference. An unavailable worker fails without a native fallback.

## Embedded network authority

For apps that fetch external resources, declare exact `browser_origins`,
literal IP pins in `browser_worker.destinations`, and nonoverlapping
`browser_worker.rules`. Each rule identifies an origin, path, exact query,
method list and read/write effect. App resource metadata cannot add authority.
The existing broker budgets apply across renders in one host connection.

Chromium has no direct network route. The host brokers permitted HTTP requests
with TLS verification, bounded transfers and no automatic redirects. Private
Chromium guards cover the page and embedded frames; the app frame guard is
ready before scripted interaction. Allowed read redirects are serviced through
the broker. Redirects to write destinations are denied even when an initial
write was authorized. WebSockets and service workers remain disabled.

Every initial direct HTTP write additionally needs a host-owned entry in
`ui.network_grants`, with these fields:

- `url`: exact HTTP(S) URL, without credentials or a fragment.
- `method`: explicit uppercase method.
- `body_sha256`: SHA-256 of the exact outgoing body bytes, as lowercase hex.
- `expires_at`: future ISO timestamp with a timezone, checked against host time.

The grant can be inline or resolved as a whole object from `/fixture/`,
`/secrets/` or `/replies/`. For example,
`network_grants: [{"$ref": "/secrets/request_grant"}]` keeps it in the private
input file. Tool results and model inputs cannot supply grants. The host
persists consumption before permitting dispatch; failed transfers do not
refund authority. Repeated requests need separate grants.

These are transport-level test permissions. They do not replace the service's
domain consent contract or establish that a business transaction committed.
An app's server-tool callbacks still use their separate tool allowlists,
schemas, host-injected arguments and domain-consent controls. Card details
belong only in the secure form, never planner observations or diagnostic logs.

## Recovery, evidence and limits

An interrupted UI that consumed a direct-write grant cannot replay its form.
Configure `ui.network_reconcile` with a declared read tool, arguments, response
pointer and typed expected value. Bind that lookup to the intended operation,
not a generic success message. A missing or unconfirmed lookup pauses the run.
Existing callback reconciliation must also succeed; no callback is invented
when interruption preceded it.

The private host records the measured image, Docker/kernel identity, sandbox
settings and Chromium version before interaction. Continuation and locked
behavioral reruns reject a changed runtime. Detailed network/runtime events
stay in developer evidence; planner capabilities contain only stage outcomes
and the selected bridge.

Local checks use a synthetic preference app, exact one-use writes, a
destination-side counter and both allowed and denied redirects. This is
consumer-surface workflow evidence, not a Stripe, cloud-provider or actual
Muse qualification.
