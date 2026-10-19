# Explicit worker runtime deployment

The main CLI can deploy a pinned archive before starting its installed worker
coordinator. This supplies Node tooling and native browser assets to an empty
job workspace. It does not download dependencies, run installation scripts,
provision machines, load container images, or install Python.

Create a JSON manifest selecting existing workspace assets:

```json
{
  "platform": {"system": "Darwin", "machine": "arm64"},
  "paths": {
    "tooling/payment-ui": "tooling/payment-ui",
    "browsers/chromium_headless_shell-1243": ".cache/ms-playwright/chromium_headless_shell-1243",
    "browsers/ffmpeg-1011": ".cache/ms-playwright/ffmpeg-1011"
  },
  "entrypoints": {
    "chromium": "browsers/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell"
  }
}
```

This example selects macOS ARM64 assets. For another worker platform, supply
compatible assets and its exact reported `system` and `machine`. Platform
labels are the pack author's assertion, not independent binary inspection.

```sh
consumer-agent-testing-tool worker runtime-pack runtime-input.json --output .cache/runtime.zip
```

Use the returned SHA-256 in the placement:

```yaml
execution:
  backend: worker
  worker:
    gateway_config: worker-config.json
    runtime:
      archive: .cache/runtime.zip
      sha256: REPLACE_WITH_THE_REPORTED_SHA256
```

Archive paths resolve relative to the configuration file. All source files
must remain inside the workspace. Manifest destination paths belong to
`tooling`, `browsers`, or the optional `node` executable. Nested destinations
select individual tooling groups or browser builds.

The browser host uses deployed `tooling/payment-ui` and `browsers` directories.
`entrypoints.chromium` optionally selects a pinned executable for native
browser and embedded-app execution. Without it, existing Playwright executable
selection remains in effect. Optional deployed `node` starts the browser
sidecar; otherwise Node must already be on the worker's executable path.
Other tooling consumers use the deployed `tooling` directory through the
existing selector. Do not also set `CONSUMER_AGENT_TESTING_TOOL_TOOLING` in the placement.

## Transfer and integrity

Readiness negotiates `runtime-chunks-v1` and checks the declared platform.
The invocation pins archive size/hash, platform and manifest hash. Transfer
uses 1 MiB chunks with limits of 512 MiB compressed, 1 GiB expanded and 32,768
entries. This is separate from the 32 MiB scenario-input limit. Deployment
and execution share the job TTL.

Lost replies can retry identical bytes at the same offset. Conflicting or
out-of-order chunks fail. Commit verifies the entire archive, declared files,
hashes and modes in private staging, then atomically renames it into `.runtime`.
Partial extraction never becomes executable. Repeated commit verifies the
installed tree. Run admission and cancellation forbid further transfers.

Declared internal symlinks retain Node `.bin` package resolution and avoid
duplicating framework trees. Targets must be declared regular files or
directories; escaping, dangling and ancestor-cycle links fail. Runtime assets
cannot replace job control files or scenario inputs.

Before execution, the coordinator independently checks the pinned manifest,
all files, execute bits and link targets. Selected inference/target credential
values in archive file content fail admission before provisioning. Assets
remain trusted executable setup inputs; integrity is not code-safety proof.

The stable execution lock and worker journal record runtime identity. Public
exports exclude assets. Confirmed job destruction removes uploads, partial
extraction and installed assets; the controller's source archive remains.

## Limits

The reference gateway supports file and source-only SSH layouts with the
matching Python harness already installed. Tests exercise actual transfer,
lost replies, failed admission, tampering, Node package execution and a native
Chromium/Inspect scenario against a synthetic loopback model endpoint.

[Python wheel bundles](worker-python.md) now install the matching harness onto
a compatible preinstalled CPython. [Private continuation](worker-continuation.md)
separately retains and restores API/browser checkpoints. Runtime archives contain
neither of these: they supply only the explicitly selected executable assets.
Docker image deployment/socket access, named cloud provisioning and large
local-model weight deployment remain separate work. Local proofs do not qualify
live SSH, hosted models or cloud lifecycle behavior, and do not give native
Chromium the isolated browser worker's security properties.

The [release policy](release-gates.json) names the current release gates; their results are recorded per
release (see [release audit](../README.md#release-audit)).
