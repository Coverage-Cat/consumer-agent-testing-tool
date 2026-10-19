# Experimental isolated browser worker

The restricted `browse` lane and [browser scenarios](browser-scenarios.md) can
use a Linux ARM64 container in a dedicated, workspace-owned VM.
This is an opt-in implementation, with initial local
qualification. It does not yet pass the complete browser-isolation release
gate or qualify other platforms. [Embedded apps](embedded-browser-worker.md)
can select the worker separately through their surface scenario.
The native browser path remains available with its existing isolation limits.

The worker has no external network interface, host mounts, Docker socket or
inference credentials. Its root filesystem is read-only; memory, CPU, process
count, shared memory and temporary storage are bounded. Chromium runs as UID
65532 with its own sandbox and no-new-privileges. The launcher drops all
capability sets before execution. Chromium subsequently creates a sandbox
subprocess with a capability in a user namespace mapping only UID 65532;
the recorded process/namespace evidence distinguishes that subprocess from
the parent namespace. Separate
IPv4/IPv6 firewall rules deny that identity's traffic, including loopback.
The root Playwright driver loses firewall administration after startup.

The trusted host uses Docker exec and a private relay to reach the root driver's
loopback transport. That transport is not an agent tool. Its readiness record is
root-only. The existing in-memory Playwright MCP restriction still controls
actions and emitted references.

Approved HTTP requests are fetched by the host and fulfilled into Chromium.
Every approved origin needs an explicit literal IP pin. The host preserves the
origin's Host header and TLS name, validates TLS, bounds request/response bytes
and time, and does not follow redirects automatically. A separate Chromium
interceptor services allowed read redirects; redirects cannot consume a second
write grant. One-use transactional authorization precedes each initial write.
Missing browser interception therefore leaves Chromium offline.

The HTTP broker also limits cumulative work over its lifetime: by default,
8 concurrent requests, 256 admitted requests, 32 MiB of request bodies and
64 MiB of response bodies. Rejected admissions never contact the destination.
Failed transfers do not refund admitted requests or reserved request bytes.
Exceeding the response budget cancels active transfers and closes admission;
closing the broker is also terminal. Observations record the reason and usage.
These limits bound HTTP work, not total host memory or CPU under arbitrary
control-message flooding.

Uploads use bounded byte payloads; downloads are streamed to the existing
scoped directory. The worker has an independent lifetime watchdog. Docker
automatically removes it after exit, including if the host has disappeared.
Normal shutdown also checks that its uniquely owned worker was removed.

## Explicit setup

The setup below is scoped to macOS ARM64 with an already installed Colima and
Docker CLI. It starts a dedicated local VM with two CPUs, 4 GiB RAM, zero host
mounts, no SSH-agent forwarding and no global Docker-context change. It uses
only workspace-owned configuration and cache directories.

```sh
.venv/bin/python scripts/prepare_browser_vm.py plan
.venv/bin/python scripts/prepare_browser_vm.py start
.venv/bin/python scripts/browser_worker_image.py pull
.venv/bin/python scripts/browser_worker_image.py fetch-deps
.venv/bin/python scripts/browser_worker_image.py build
```

Setup downloads a digest-pinned Playwright image and hash-locked Ubuntu
firewall packages. Building then uses `--network=none` and the existing pinned
workspace Node SDK. Runtime uses `--pull=never`. It never downloads model
weights or contacts a test target during setup. The separate `resolve-deps`
action is for maintainers intentionally updating the committed package lock.

`worker-image.json` records the immutable local image ID and build-input hashes.
`connection.json` records the explicit Docker socket and configuration path.
Both are under `.cache/browser-vm/`. Add the following trusted `worker` object
to a browser policy, replacing the image ID and origin with those for the test:

```json
{
  "image": "sha256:REPLACE_WITH_LOCAL_IMAGE_ID",
  "docker_socket": ".cache/browser-vm/colima/b/docker.sock",
  "docker_config": ".cache/browser-vm/docker",
  "destinations": {"http://portal.test:8765": "127.0.0.1"},
  "max_request_bytes": 10485760,
  "max_response_bytes": 10485760,
  "max_concurrent_requests": 8,
  "max_requests": 256,
  "max_total_request_bytes": 33554432,
  "max_total_response_bytes": 67108864,
  "lifetime_seconds": 240
}
```

IP pins must cover exactly the policy's approved origins. The image ID must
contain the full 64-character digest; the placeholder above is rejected.
The lifetime is independent of the Inspect task budget, includes browser
startup, and is limited to 30–3900 seconds.

## Verification and remaining gaps

```sh
node scripts/browser_worker_probe.mjs
node tests/browser_worker_workflow.mjs
node tests/browser_worker_network.mjs
node tests/browser_worker_expiry.mjs
SURFACE_BROWSER_WORKER_TEST=1 .venv/bin/pytest \
  --basetemp=.cache/pytest-browser-worker tests/test_browser_worker.py
```

The workflow uses real Chromium, the restricted MCP facade and a synthetic
local HTTP fixture. It checks one-use writes, allowed and forbidden redirects,
cookies, file transfer and direct access with request interception removed.
Deterministic processes run under the browser UID and attempt TCP/UDP over both
address families, DNS, host aliases, gateway/direct access, private control
access and child-process connections. Positive-control canaries first prove the
detectors can receive traffic. Probe records distinguish syscall errors,
timeouts and destination observations; a successful UDP send alone never
establishes delivery.

The network matrix also exercises real page fetches, WebSocket upgrades,
installed service-worker fetches, WebRTC STUN packets and a hostname whose
zero-TTL DNS answer changes between two loopback addresses. Its positive
control temporarily allows the browser UID to reach local canaries, then
removes that allowance and requires zero destination hits. It clears Chromium's
DNS cache and socket pools between DNS phases. The test context explicitly
grants local-network access and enables loopback ICE; neither treatment is an
agent-facing setting or a claim about Muse configuration. Production policies
reject the test-only ICE option, and readiness records bind that distinction.

Runtime evidence records the exact image, Docker version, kernel, architecture,
Chromium and probe treatment. Resumed runs cannot silently substitute a recorded
worker identity. [Embedded-app integration](embedded-browser-worker.md) and the
[agent-directed canary](browser-canary.md) now have local regression evidence,
including scripted Inspect execution. This is not the complete §22 acceptance
matrix: configured live-model attempts and qualification on other claimed
supported platforms remain open.
The worker's base-image/native-component redistribution inventory also remains
part of the open distribution gate. None of these gaps is treated as a pass.

The upstream seccomp source, original license and attribution are packaged
beside the worker. The derived profile additionally permits `chroot`, which
Chromium needs inside its user namespace. Kernel capability checks still deny
that operation to the unprivileged process in the parent namespace.

Stop the dedicated VM when finished:

```sh
.venv/bin/python scripts/prepare_browser_vm.py stop
```
