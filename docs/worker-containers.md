# Pinned worker container deployment

`worker container-pack` exports an explicitly selected immutable Docker image.
`worker container-install` imports its verified archive onto the selected local
or SSH host. Setup uses a preinstalled Docker engine and Python; it does not
provision a VM, install an engine, contact a registry or download dependencies.

```sh
consumer-agent-testing-tool worker container-pack sha256:IMAGE_DIGEST \
  --docker-socket /absolute/path/to/docker.sock \
  --output deployment/sandbox.zip

consumer-agent-testing-tool worker container-install deployment/sandbox.zip \
  --sha256 BUNDLE_SHA256 \
  --docker-socket /absolute/path/to/docker.sock \
  --directory deployment/host \
  --output-receipt deployment/sandbox-receipt.json
```

Use the complete image ID and bundle hash returned by the preceding steps.
Bundle and receipt files must stay inside the workspace. Existing outputs are
not overwritten. The archive contains one untagged Linux image and is bounded
to 2 GiB. Setup checks paths, member types, duplicate names, blob hashes, the
selected image graph, platform and conflicting manifests before import.
Repeated exports of the same retained image use deterministic ZIP metadata.

The supported archive shape is the OCI layout emitted by the tested Docker
engine's platform-specific export. This is not a general-purpose OCI registry,
multi-platform image-index or legacy Docker archive importer. Local qualification
uses Linux ARM64 images and Docker 29.5.2. The schema also accepts Linux amd64;
that does not establish tested amd64 support.

The installer streams a standard-library bootstrap and archive over the
existing strict SSH transport when `--worker-config ssh-worker.json` is supplied.
It uses that configuration's `ssh_python` and stores shared Docker configuration
under `ssh_worker_root/containers`; the supplied socket is an absolute path on
that host. Local installation uses `--directory`. The SSH path is implemented
but has no live SSH qualification.

Select the returned receipt and its file hash in the ordinary run configuration:

```yaml
execution:
  backend: worker
  worker:
    gateway_config: worker-config.json
    container_images:
      sandbox:
        receipt: deployment/sandbox-receipt.json
        sha256: RECEIPT_FILE_SHA256
```

The optional `browser` role selects a separately packaged browser worker image.
Both roles must use the same Docker socket and configuration directory.
Use the same installation directory when installing both images. Runtime
Node/browser assets remain a separate [runtime archive](worker-runtime.md).
Existing explicit image or Docker environment settings must agree with the
receipt; conflicting selections fail before execution.

Receipts bind the bundle, immutable image, platform, daemon identity/version,
connection and selected worker host. The fixed coordinator rechecks the actual
daemon, image, empty Docker configuration and runtime environment before
inference. Removing configuration is an error, not a request to recreate it
during execution. Changed daemon versions require a new reviewed deployment.

The socket is an explicitly selected privileged host-control endpoint.
The setup process uses a private, empty Docker configuration and does not inherit
ambient registry helpers, AWS credentials, OpenRouter keys or SSH-agent sockets.
The browser host receives exact connection authority only from the fixed
coordinator; ordinary browser paths retain their workspace restrictions.
The agent does not receive the Docker socket as a tool or container mount.

Installed images and software configuration persist after individual jobs are
removed. Job execution, verified evidence collection and job cleanup have
separate states. Remove software explicitly after dependent jobs finish.
If setup loses its reply after import, retry the same pinned archive with a
new receipt path. Reimport is tested; actual SSH acknowledgement loss is not.

Image code and the worker coordinator remain trusted operator inputs. Hashes
establish integrity and selection, not hostile-code safety or remote attestation.
These commands do not close cloud provisioning, independent cloud TTL cleanup,
browser-isolation qualification or native-component inventory gaps.

The focused proof restores an absent disposable image, uses it for native SDK
CLI execution or browser continuation, verifies collected evidence and cleanup,
and preserves cached ancestor images. The fresh-install distribution proof
also exercises the public setup CLI and an independently scored synthetic draft.
