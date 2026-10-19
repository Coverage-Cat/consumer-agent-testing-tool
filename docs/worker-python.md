# Worker Python environment deployment

Workers can install the selected Consumer Agent Testing Tool wheel and its dependencies from an
explicit bundle. The destination needs a compatible CPython with `venv` and
`ensurepip`; Consumer Agent Testing Tool, Inspect and the selected provider SDK need not already
be installed. This deploys a Python environment, not an operating system or
base interpreter.

Create a JSON manifest with:

- `wheels`: the complete list of workspace-relative wheel paths, including the
  current Consumer Agent Testing Tool wheel and every selected dependency.
- `model_provider`: the provider whose SDK must be checked, such as `openrouter`.
- `platform`: optionally the target `system`, `machine` and Python major/minor
  pair. It defaults to the packaging host.

Use pinned wheels for the target platform. The distribution verifier's explicit
`--prepare-cache` stage also prepares the workspace wheel cache; offline
verification checks those wheels against `uv.lock`. A custom deployment
manifest is an operator-selected artifact set, not a claim of upstream provenance.

```sh
consumer-agent-testing-tool worker python-pack wheels.json --output .cache/worker-python.zip
consumer-agent-testing-tool worker python-install .cache/worker-python.zip \
  --sha256 <digest-returned-by-python-pack> \
  --directory .cache/worker-python \
  --output-config worker-python.json
```

The installer verifies the archive, wheel inventory, byte hashes, paths,
expansion bounds and platform before installing. Pip installs only supplied
wheels with hash checking, dependency resolution disabled and no index. No
source builds or dependency downloads occur during installation. `pip check`
then checks the installed dependency set, and the installed Consumer Agent Testing Tool harness
must match the selected implementation and adapter package versions.

The resulting `worker-python.json` is a gateway configuration for the ordinary
`execution.worker.gateway_config` selector. Worker jobs retain their existing
deadlines, credential boundaries, evidence collection and cleanup behavior.

## Existing SSH hosts

Supply an existing SSH worker configuration instead of `--directory`:

```sh
consumer-agent-testing-tool worker python-install .cache/worker-python.zip \
  --sha256 <digest-returned-by-python-pack> \
  --worker-config ssh-worker.json \
  --output-config ssh-worker-python.json
```

The existing configuration selects the host, workspace identity and known-hosts
files, and remote `/tmp` root through `ssh_worker_root`. Setup uses that host's
`python3` and standard library. It preserves strict host-key checking and does
not forward an SSH agent or inference credentials. The generated configuration
records the installed interpreter as `ssh_python`; subsequent worker calls use
that interpreter. No cloud host, SSH key or public tunnel is created.

## Reuse and limits

Installation has a content-addressed directory and exclusive local lock.
Successful publication requires a completed receipt and installed-file
inventory. Reuse rechecks that inventory and the installed harness. Changed
files or an incomplete deployment fail closed; an interrupted installation
requires explicit inspection and cleanup before retrying. The installer does
not replace an existing output configuration.

The environment stays at its final path so installed script shebangs remain
valid. It persists after individual worker jobs are deleted. It contains
software, not reusable fixture/browser state; each job still gets its own
workspace. Remove an installation explicitly only after its jobs have stopped.

Wheels are executable, trusted operator inputs. Hashes establish integrity,
not the safety of arbitrary Python code. This is not hostile-worker isolation.
Base interpreter, native OS libraries, Docker resources and target-control
services remain separate prerequisites. Local deployment tests do not establish
SSH connectivity or named-cloud lifecycle qualification.
