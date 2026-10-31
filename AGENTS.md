# AGENTS.md

Guidance for coding agents that use this tool to test a service, or that change the tool itself. The
[README](README.md) is the user guide; this page is the short path through it.

## What the tool does

It runs a consumer agent (a model, or a scripted replay with no model) against your real MCP server
through a labeled Muse or Instinct surrogate host, then checks the
outcome your server recorded rather than what the agent said. It is not either hosted assistant and
proves nothing about admission: every report says "surrogate".

## Setup

```sh
uv sync --extra openrouter --extra dev
npm --prefix tooling ci --ignore-scripts
npm --prefix tooling/payment-ui ci --ignore-scripts
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" node tooling/payment-ui/node_modules/playwright/cli.js install chromium
uv run consumer-agent-testing-tool doctor
```

The connector lane (the usual entry point) needs only `uv sync`; Node and Chromium are for the browser
and MCP Apps lanes. On Linux, install Chromium with `install --with-deps chromium` (see the README's
Linux notes). `doctor` prints one line per failed check with the command that fixes it.

## Testing an MCP server: the workflow

Run these in order. Each step needs only what the step before it produced.

1. **Scaffold** a scenario from the live catalog. It calls `initialize` and `tools/list` only:

   ```sh
   uv run consumer-agent-testing-tool scaffold http://127.0.0.1:8080/mcp --emulation-target muse --output my-connector.yaml
   ```

   Then replace every `TODO(scaffold)` in the file. Validate against
   [`schemas/connector-v1.schema.json`](schemas/connector-v1.schema.json). The fields are:
   - `goal`: the user's request in their words.
   - `connectors[].tools.allow`: the reviewed allowlist.
   - `permissions`: approval rules for the scripted user.
   - `private`: host-private values such as the email. The model never sees them.
   - `checks`: read the real outcome from an `oracle` state URL or a `tool` the host calls after the run.
   - `answer.relay`: regexes the final answer must contain.

2. **Readiness**, which reads the catalog and runs no agent:

   ```sh
   uv run consumer-agent-testing-tool check http://127.0.0.1:8080/mcp --suite muse-guidelines --manifest readiness.yaml
   ```

3. **Scripted run**: no model, no key, deterministic. This is the one to put in CI:

   ```sh
   uv run consumer-agent-testing-tool connector-run my-connector.yaml --driver scripted --script my-connector.calls.json
   ```

   A calls file is a JSON (or YAML) list of steps:
   - `{"tool": NAME, "arguments": {...}}` for a tool call;
   - `{"idle": true}` where the agent stops so background work can resume it;
   - a final `{"answer": [...]}`. Its parts may be `{"$ref": "/results/N/result/structuredContent/..."}` to
     quote earlier results. Under `--projection muse-cli`, use `/results/N/stdout/...` instead.

   [`examples/connector/`](examples/connector/README.md) has a working pair for every feature.

4. **Agent trials**: paid model inference. They need a model profile (`init`) and an API key in the
   environment:

   ```sh
   uv run consumer-agent-testing-tool connector-run my-connector.yaml --config agent.yaml --trials 5
   ```

   Repeat under each treatment a Muse-like host may use:
   - `--projection muse-cli`
   - `--exposure search-only`
   - `--client-profile muse-template-unofficial-2026-10`
   - `--output-schema strict`

   Turn a trusted trial into a calls file with
   `scaffold --from-run runs/connector-<id> --output FILE`.

5. **Compare builds**: `diff RUN_A RUN_B` exits 1 on an outcome regression.

Never start agent trials, or any command that calls a model provider, unless the user asked for paid
inference. Scripted runs, `scaffold`, `check` without `--drift-check`, and `doctor` without
`--model-preflight` make no model request. A saved model profile, `--config`, or `--model-profile` does
not opt `doctor` into inference; `doctor --model-preflight` does and requires paid-inference authorization.

## Exit codes

`connector-run`, `check` (contract suite) and `surface-run` share these:

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | every required check passed | done |
| 1 | a check failed, the answer made a false claim, or an agent limit was reached | read the report; it is a finding about the service or scenario |
| 2 | setup, harness or reporting error; nothing about the service is concluded | fix the command, server or config |
| 3 | blocked or unsupported, for example an uncertain write to reconcile, an unobservable check, or a client-profile limit | not a pass; read the reason |

The guidelines readiness suite uses 0 (no fail finding), 1 (fail findings), 2 (setup error) and 3 (the
client profile cannot reach the server).

## CI and machine-readable output

For several scenarios or treatments, use a declared scripted suite:

```sh
uv run consumer-agent-testing-tool ci validate examples/ci/booking.suite.yaml --jobs 4 --json
uv run consumer-agent-testing-tool ci run examples/ci/booking.suite.yaml \
  --jobs 4 --build-id local --report-dir reports/booking --summary-json -
```

`ci` makes no model request. Suites require explicit targets and service-state declarations.
`ci plan` is offline; `--case`, `--tag` and `--failed-from` select work. Sharded runs require a common
`--batch-id` and `--build-id`; `ci merge` keeps missing shards unrun and nonpassing. `ci diff` rejects
missing or changed coverage as a passing comparison. Read [the suite guide](docs/ci-suites.md).
Upload only the report directory. `.cache/catt-ci/` contains private snapshots, runs and raw logs.

For one scenario:

```sh
uv run consumer-agent-testing-tool connector-run my-connector.yaml \
  --driver scripted --script my-connector.calls.json \
  --start "python -m my_service --port {port}" --ready-url "http://127.0.0.1:{port}/health" \
  --junit reports/connector.xml --summary-json reports/connector.json
```

- `--start CMD` launches the service under test (shlex-split, no shell) and waits until `--ready-url`
  (default: the first connector endpoint) answers HTTP. It then runs the scenario and stops the
  service's process group: SIGTERM, then SIGKILL after 5 s.
  - `{port}` is a free loopback port. The service also gets it as `PORT` and the scenario as `${PORT}`.
  - The service's output goes to `runs/service-<id>.log`, outside the sealed run.
  - A service that exits early or isn't ready within `--ready-timeout` seconds (default 30) exits 2.
- `--junit FILE` writes one test suite per trial and one test case per check. Blocked and unsupported
  checks are `<skipped>`, and setup errors are `<error>`.
- `--summary-json FILE` writes a summary that
  [`schemas/summary-v1.schema.json`](schemas/summary-v1.schema.json) describes.
  - To read results from a command, use `--summary-json -` and parse stdout as one JSON document. The
    human-readable lines go to stderr in this mode; don't parse them.
  - Read `exit_code` and `status`: `completed`, `failed`, `blocked` or `error`. `error` means nothing
    was concluded; `error.detail` says why.
  - Then read each `trials[]` entry's `run`, `failure_stage`, `failure_class`, `reason` and
    `checks[]` (`id`, `status`, `required`).
- `check --suite muse-guidelines` (and the `contract` and `static` suites) accepts both flags.
- The service log isn't redacted. Pass secrets to `--start` services through the environment, not
  their command line.

[`examples/ci/`](examples/ci/README.md) has a GitHub Actions workflow to copy: scripted runs and
readiness on every pull request, and nightly agent trials gated on a secret.

## Evidence

Each run is sealed under `runs/<lane>-<id>/`, with `report.html` at the top. `report RUN` re-renders the
report, and `replay RUN` verifies the seals. `private/` holds host-private values: never paste it, upload
it or commit it. To share a run, use the output of `consumer-agent-testing-tool export RUN`.

For exploratory behavioral research, organize public tests by generic behavior in
[`docs/behavior-tests/`](docs/behavior-tests/README.md). Keep named-participant reports, exact prompts,
raw transcripts and operational details in ignored `reports/` storage. Public descriptions should
summarize the sandbox method and its limits without identifying an assistant or presenting incomplete
checks as passes. Preserve actual target identity and provenance in private evidence. This does not
rename the surrogate-selection API or the product's documented target names.

## Changing the tool itself

- Human-facing writing follows Strunk and White: active voice, concrete words, short sentences,
  no needless words or promotional filler. Apply this to the README, CLI help, errors and reports.
  Keep safety caveats and evidence labels; do not change machine-readable fields or model-facing
  prompts during a copy edit.
- Muse and Instinct are equal supported surrogate targets; neither is the default. Set
  `emulation_target` to `muse` or `instinct` in each scenario, or pass the choice with `--emulation-target`.
  Catalog scaffolding and shared surrogate readiness also require the flag. Instinct's supported
  extensions, research and unimplemented features are in [the spec](docs/instinct-emulation.md).
  Keep the assistant target distinct from the model that drives a trial. Do not present shared
  CATT algorithms or Muse sources as documented Instinct behavior.
  Both targets support `native` and `cli` projection and `check --suite surrogate`.
  `muse-cli`, the unofficial Muse client and app widgets remain Muse-specific.
  See the [shared contract](docs/surrogate-contract.md) for capability and qualification requirements.
- Code is in `src/consumer_agent_testing_tool/` (CLI entry: `cli.py`). The connector lane is in
  `connector*.py`.
- Tests and lint: run them locally before every push. See
  [Verify locally, push rarely](#verify-locally-push-rarely).
- Tests must make no network request beyond loopback and no model request. Use the stand-in servers in
  `tests/` and the `connector_fixture` module.
- After changing a pydantic model behind a schema, run `uv run python scripts/generate_schema.py` to
  regenerate `schemas/` and `src/consumer_agent_testing_tool/data/schemas/`.
- Don't record release evidence as part of a change. It is generated per release by
  `.github/workflows/release.yml` on a `v*` tag and attached to the GitHub release, not committed.
  - It is bound to a digest of `src/`, `scripts/`, `tests/`, the lock files and the third-party
    notices, so any change there makes recorded gates stale. Docs, the README and examples are outside
    the digest.
  - Without recorded evidence, `release-audit` exits 3. That is expected between releases, not a
    failure to fix.
  - `docs/release-gates.json` is the committed policy. Edit it only to change the gates themselves.
  - `docs/release-evidence/` holds historical records that the docs cite; don't add generated output
    there.
  - To record locally, follow [Release audit](README.md#release-audit) in order. Everything goes to
    `release/`, which git ignores.
- Reference: [docs/README.md](docs/README.md) indexes the docs. The most useful pages are:
  - [connector-lane.md](docs/connector-lane.md): scenario format, flags and evidence;
  - [connector-extensions.md](docs/connector-extensions.md);
  - [readiness-checks.md](docs/readiness-checks.md).

## Verify locally, push rarely

Remote CI is slow:
- A test job takes 25–30 minutes.
- macOS jobs queue behind release runs.
- `main` requires branches to be up to date, so merging one PR can force another full run on the
  next.

Each failed or repeated run costs about half an hour, so catch failures on your machine first.

Before every push, run what CI runs:

```sh
uv run ruff check src tests scripts
uv run python scripts/generate_schema.py
git diff --exit-code -- schemas src/consumer_agent_testing_tool/data/schemas
python3 scripts/check_repository.py
uv run pytest -q -p no:cacheprovider -o faulthandler_timeout=120 tests
```

- **The full collection:** it takes about 20 minutes and makes no network request beyond
  loopback. Run the files you touched first, for example
  `uv run pytest -q tests/test_connector_*.py`. Then run the full collection in the background and
  keep working.
- **Connector lane:** if you change it, the CLI or `examples/`, also run the matching
  `connector-run` commands from the `connector-lane` job in `.github/workflows/ci.yml`.
- **Browser or timing changes:** repeat the affected tests several times, then once more with
  every CPU core busy. One pass doesn't show a test is stable.
- **New regression tests:** make sure the test fails on the old code before you rely on it.
- **Failures you didn't cause:** if a test also fails on unmodified `main`, you didn't cause it.
  Check with `git stash` before you investigate, and say so in the PR.
- **Linux-only failures:** CI also runs on Ubuntu. If a failure appears only there, reproduce it in
  a local Linux container if you can. If you can't:
  1. Push a temporary `debug/` branch whose `ci.yml` runs only the failing tests in a loop.
  2. Dispatch it with `gh workflow run ci.yml --ref debug/<name>`.
  3. Delete the branch when you're done.

  Don't re-run full CI over and over to diagnose a flake.
- **Known flakes:** re-run a failed job only when the failure is a known flake. If it's new,
  investigate it first.

Batch work into fewer, larger pull requests:
- **Group the session's changes:** put every change from one session on one branch, related or
  not: a fix, the test flakes it uncovered, a docs correction. Make one commit per change, and
  explain each in its message. Open a separate PR only for work that must merge on its own
  schedule, such as a security fix or a release.
- **Write the PR body as the commit message:** `main` allows only squash merges, and the squash
  commit's message is the PR body. Give each change in the PR its own heading or bullet, with its
  cause, its fix and how you verified it.
- **Finish locally before you push:** open the PR once the branch passes locally, not to see what
  CI says. If more fixes turn up while CI runs, add them to the same branch in one push.
- **Know what goes stale:** changes under `src/`, `scripts/`, `tests/`, the lock files or the
  notices make recorded release evidence stale. Group them, so a release needs one rehearsal, not
  several.
