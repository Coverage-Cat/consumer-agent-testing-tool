# CI templates

[`connector-lane.yml`](connector-lane.yml) is a GitHub Actions workflow for your MCP server's own
repository. Copy it to `.github/workflows/` and replace each `TODO`.

| Job | When | What it runs | Needs |
| --- | --- | --- | --- |
| `scripted` | every pull request and manual run | your calls file with lenient and strict output schemas, then the guidelines readiness review | no model or key |
| `agent-trials` | only when opted in | trials under the default mode, `muse-cli`, `search-only` and the unofficial Muse template client | explicit approval, an `OPENROUTER_API_KEY` secret and a committed model profile; paid inference |

Pin `TOOL_REF` to a reviewed, full commit SHA. The template pins its Actions and installs CATT
with its lockfile. It keeps checkout credentials out of Git configuration and uploads only reports.
While CATT is private, a checkout from another repository also needs a read-only token with access
to it. Do not expose that token to untrusted pull requests.

Paid trials are off by default. To run them once, select **Run paid model trials** in the manual
workflow form. To enable nightly trials, set the repository variable
`ENABLE_SCHEDULED_AGENT_TRIALS` to `true`. Both choices require the model key and incur charges.
Adding a key alone does not enable paid trials.

Each step uses the CI flags of `connector-run` and `check`:

- `--start "CMD {port}"` starts your server on a free loopback port and passes the port to it as `PORT`
  and to the scenario as `${PORT}`. It waits until the server answers HTTP, then stops the server's
  process group afterwards. The server's output goes to `runs/service-<id>.log`. A server that fails
  to start exits 2.
- `--junit FILE` writes one test suite per trial and one test case per check, so the CI's test view
  shows which check failed.
- `--summary-json FILE` writes the verdict, exit code, checks and pass^k for later steps. With `-`,
  stdout carries only the JSON.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every required check passed |
| 1 | a finding about the service: a failed check, a false claim, or an agent limit reached |
| 2 | setup, harness or reporting error |
| 3 | blocked or unsupported, which is not a pass |

A step fails on any non-zero code. Do not treat exit 3 as a pass or ignore it in a required check.
Keep an unfinished scenario in a separate diagnostic job until it can meet the required checks.

## Writing the scenario and calls file

Run `scaffold` against a local build to get a scenario skeleton. Fill in its `TODO(scaffold)` fields
and commit the result with a calls file. To create a calls file from an agent trial you trust, run
`scaffold --from-run runs/connector-<id> --output FILE`. The
[connector lane](../../docs/connector-lane.md) reference describes both formats. This repository's own
[CI workflow](../../.github/workflows/ci.yml) runs the shipped examples the same way.

Do not upload `runs/`: its `private/` folders hold host-private values. Upload the reports, or the
output of `consumer-agent-testing-tool export RUN`.
