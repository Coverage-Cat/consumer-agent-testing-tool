# Workspace-contained distribution verification

The verifier builds and installs the current wheel into a fresh environment,
then exercises the installed package from a separate project. Everything it
creates—homes, configuration, temporary files, caches, environments, logs and
browser state—stays under this workspace. It does not inspect other repositories
or use the user's package-manager configuration. System-provided Python, Node,
npm and macOS sandbox tools remain runtime prerequisites.

Use the workspace's `.venv/bin/python` and `.venv/bin/uv`. The build backend and
its transitive dependencies have a separate hash-pinned lock at
`tooling/build-python/uv.lock`.

## Setup and verification are separate

```sh
# Explicit dependency setup, when workspace caches are not populated:
.venv/bin/python scripts/verify_distribution.py --prepare-cache

# Fresh installation and checks with external egress denied:
.venv/bin/python scripts/verify_distribution.py
```

Setup may download locked Python and Node packages. It prints the maximum
compressed size of compatible Python wheels before installing them. Node
tarball sizes are absent from the npm lockfile; uncached transfer size is
explicitly unknown before setup. Setup downloads neither model weights nor
browser binaries. It does not call an inference provider, customer service,
production target, or cloud worker.

Verification requires an existing browser runtime under `.cache/ms-playwright`.
It uses that local runtime with a fresh Node tooling installation; it does not
claim to have downloaded a new browser. Browser build information, runtime-file
hashes and the Playwright registry are recorded separately.

The required container deployment stage also needs the workspace-owned Docker
VM already running at `.cache/browser-vm/colima/b/docker.sock`, with the cached
`consumer-agent-testing-tool-sandbox:0.1.0` image. It builds a disposable image without network,
exports and removes it, restores it through the installed CLI, then executes a
synthetic draft through the native SDK and worker. The independent oracle,
receipt selection, evidence export and job deletion must all pass. Cleanup
preserves cached ancestor images. Verification does not start a VM or download
images automatically; this requirement applies to the release proof, not to
account-free static use of the installed tool.

`--lock-build-tools` is an explicit maintainer setup action for resolving the
build-tool lock. Normal verification uses frozen lock exports, hash-required
binary-wheel installation, offline npm installation and disabled install
scripts. It does not fall back to a global cache or silently enable downloads.

The current enforcement implementation requires macOS. Build/install/inventory
steps have kernel-denied outbound connections. Contract checks use the CLI's
own exact-port offline policy. The fixed packaged API/browser smoke permits
loopback and workspace Unix sockets while denying external egress, and checks
that the kernel denial is active. That smoke policy is not the restricted
browser worker's eventual OS-isolation qualification.

## What is verified

- Fresh, separate core and build environments; observed Python, Node, npm and uv
  versions retained in the verification record.
- Separate fresh installs of the protocol and browser/MCP-app Node tool groups.
- Exact wheel contents compared against current packaged source, lock and
  notice files; unexpected files are rejected.
- Required source-archive files, including the build lock and optional tooling;
  escaping and linked archive members are rejected.
- No-model initialization, static inspection/checks, native MCP contract checks,
  report generation and redacted export from the installed wheel.
- Installed version 2 calibration analysis over the bundled synthetic packet,
  with all nine observable dimensions and explicit ineligibility as Muse evidence.
- Installed API/browser authentication expiry and refresh, verified against the
  target attempt ledger and independent subject/scope/session assertions.
- An installed Inspect API scenario that creates and confirms a synthetic
  reservation under private consent, checked against the independent oracle.
- API/browser continuation from retained private state, plus recovery of a lost
  stop-pending response through its exact public receipt without repeating the
  cancellation request.
- An installed developer-maintained HTTP map, inspected without inference and
  exercised through a separately authenticated local contract request.
- A mapped developer API behavioral trial through native Inspect tools and a
  private reset/oracle adapter, with native exposure and selection assertions.
- Retention and continuation of that developer API trial across private control
  connections, with one reset, no duplicate public request and preserved history.
- A separate fresh optional API installation, running actual Spectral, Prism
  and Schemathesis positive/negative checks and rejecting a broken response.
- An installed FastMCP-generated catalog and native tool call through the
  host-mediated transport, with rejection of a broken response.
- Generated Inspect trials with an independent SQLite oracle: an approved
  commit, a denied proposal that sends no transaction request, and false public
  success rejected by the backend assertion.
- Generated continuation after native conversion loss, with a retained HTTP
  response, regenerated catalog and native result reconstruction on a fresh
  worker; the independent order count and public requests remain unchanged.
- API and browser detection of a proven unauthorized effect while cancellation
  remains pending, with independent ledger checks, native exposure/selection,
  failed exported results and failed browser playback.
- Stateless Prism mocks with positive/negative inputs and no HTTP, plus the
  offline CLI path with verified worker/coordinator policies and direct TCP/UDP
  denial probes.
- Installed Chromium snapshot/ref interaction with the packaged synthetic
  portal, followed by sealed playback verification.
- Private state remains in the generated project; packaging includes no
  workspace cache, installed Node modules, or private checkpoints.

The workflow smoke uses scripted native model responses. It cannot establish
live inference support or actual Muse behavior. Browser interaction without an
independent business assertion remains an observation, not proof of a purchase.

Curated and generated continuation proofs also leave an independent SQLite
callback pending after the model finishes. They verify that the lease is retained,
complete the synthetic callback, then resume verification without another model
response or public dispatch. Missing either installed proof fails qualification.

The broken API/browser proofs additionally resume against an oracle that
underreports an earlier forbidden effect. Qualification requires that the
original failure survive in the parent report, browser playback, summary counts
and redacted export.

## Inventories and notices

`scripts/inventory.py` generates inventories of actual installations.
Other-platform optional npm packages are listed separately rather than counted
as installed components. The core-only Python SBOM is separate from the
development environment, optional API Python installation and optional Node tooling.

Original notice files are copied without changing their text. Some platform
tarballs omit their project's notice; matching upstream-release copies are
recorded in `third_party/supplemental-notices.json` with version, source URL,
retrieval date and content hash. Offline regeneration verifies those hashes.
A missing or changed supplemental notice fails verification.

`scripts/inventory.py` replaces `notices.zip` instead of writing into the
existing file. Run evidence never links that archive directly: each evidence
directory holds a read-only hard link to a verified copy in the workspace's
`.cache/notices/`, or a read-only copy on another filesystem. See
[dependency notices](sealed-event-provenance.md#dependency-notices).

`native-runtime.json` separately inventories recognized binary/font payloads
from these actual installations and retains upstream SBOM references. Fresh
verification binds that document alongside the package SBOMs. It does not
declare embedded dependency, font, host-runtime or container coverage complete.
Those require the separate [native runtime review gate](native-runtime-inventory.md).

An author-hosted notice referenced by a published README is tied to that
installed README's exact hash and URL. Both documents are retained. The
retrieval date describes the saved page; it does not establish historical page
contents or legal clearance.

The [optional API notice review](dependency-notice-review.md) records the three
notices recovered on 12 October 2026 and the six unresolved packages. Declared
license labels or a sibling package's notice do not close those remaining gaps.

The Node inventory records the SHA-256, size and permission mode of each declared
component's installed files. Nested dependencies have their own entries.
Noncanonical package locations, symbolic links or special files, mismatched package names,
partial optional installations and manifest changes fail verification.
The distribution proof snapshots these contents before and after the installed
workflows and rejects changes. The recorder verifies both snapshot hashes and
their dependency-lock scope. Publisher authentication and runtime confinement
remain separate evidence requirements.

The wheel contains the current inventories and original notices. Existing
container records retain their historical scope. Browser binaries, system
fonts, model weights, tokenizer/template and quantization artifacts are not
silently counted as Python/Node dependencies. Project notices do not establish
a complete SBOM for subcomponents statically embedded in native binaries.
Service terms and legal clearance are not inferred from package license labels.

## Results and release evidence

`dist/verification.json` records the latest attempt, with stage logs and hashes,
packaging input hashes, inventory hashes, artifact hashes, installed-workflow
results and limitations. Each attempt also retains its record under its unique
workspace cache directory. Failed attempts do not publish a new wheel.
Previously published artifacts and verification records are preserved under
`dist/checkpoints/`.

After successful verification:

```sh
.venv/bin/python scripts/record_distribution.py
.venv/bin/consumer-agent-testing-tool release-audit
```

The recorder writes `release/evidence/distribution.json` and its gate entry in
`release/release-gates.json`, which it starts from the committed policy in
`docs/release-gates.json`. `release/` is generated and git-ignored; the
[release workflow](../.github/workflows/release.yml) attaches it to each release.

The recorder rejects incomplete stages, missing network-policy records, changed
logs or packaging inputs, inventory/notice gaps, changed artifacts and missing
backend assertions. Its gate establishes the scoped fresh-install result.
It does not close native-binary subcomponent inventory gaps, browser isolation,
live model/cloud gates, or real-Muse calibration.
