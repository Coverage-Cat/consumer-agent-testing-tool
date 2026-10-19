# Contributing

This project is maintained by Coverage Cat, Inc. It is a consumer-agent surrogate, not a
certification of compatibility with any hosted agent. Preserve those scope and evidence labels.
The [README](README.md) is the user guide; [AGENTS.md](AGENTS.md) is the coding-agent guide.

## Start here

Use issues for reproducible bugs and proposed changes. Discuss new lanes, permissions, model
providers, or large dependency changes before implementing them. For vulnerabilities, follow
[SECURITY.md](SECURITY.md), not the public issue tracker. Participation is governed by the
[code of conduct](CODE_OF_CONDUCT.md).

Install Python 3.12 or newer, uv, and Node.js 24, then follow the README's setup instructions.
The connector lane only needs Python dependencies; the full test collection also needs the
Node tooling and Playwright Chromium. Linux browser tests need Chromium's system libraries.

```sh
uv sync --locked --extra openrouter --extra dev
npm --prefix tooling ci --ignore-scripts
npm --prefix tooling/payment-ui ci --ignore-scripts
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" \
  node tooling/payment-ui/node_modules/playwright/cli.js install chromium
uv run consumer-agent-testing-tool doctor
```

No model key is needed. Do not use paid inference, live checkout, or external customer data in
tests. `doctor` without `--model-preflight` makes no model request.

## Make and verify a change

Create a branch, keep the change focused, and include a regression test. Run affected tests
first, then lint and the full supported collection:

```sh
uv run ruff check src tests scripts
uv run pytest -q
```

The full collection takes about twenty minutes. Unraisable async cleanup exceptions and
unhandled thread exceptions fail tests. Do not suppress these warnings or skip failing tests
to make CI green. Mac-only kernel enforcement checks are skipped explicitly on other platforms;
this does not qualify those features on Linux. Development extensions are outside the default
supported collection.

After changing schema-backed contracts, run `uv run python scripts/generate_schema.py` and
commit both schema copies. Dependency changes must include the corresponding lockfile and
review of third-party notices; use `npm ci --ignore-scripts` for Node installations. Actions are
pinned to full commit SHAs. Automated dependency pull requests are reviewed, not auto-merged.

Include verification results and any limitations in your pull request. Maintainer review and
the required `CI` check gate merges. Contributions are accepted under the repository's
[MIT license](LICENSE); only submit material you have permission to contribute. Changes to
the logo or other files in `docs/assets/` need maintainer approval; see [TRADEMARKS.md](TRADEMARKS.md).

## Protect private data

Never commit `.env` files, local model profiles, `runs/`, `.private/`, checkout checkpoints,
customer records, or generated `release/` evidence. Do not paste raw run directories or service
logs into issues. Use `consumer-agent-testing-tool export RUN`, then inspect the export before
sharing it. Keep fixtures synthetic and documentation examples credential-free.

## Write for people

Follow Strunk and White's principles in the README, CLI help, errors and reports: use the
active voice, choose concrete words, and omit needless words. Lead with what happened, what
the user can do, or what a result proves. Avoid sales language and stock AI phrasing.

Keep safety limits and evidence labels. Do not change machine-readable fields or model
prompts as part of a copy edit. Muse is the initial emulation target; Instinct and other
assistants are planned, not implemented. A model provider is not an emulation target.

## Releases

Normal changes do not require recording release evidence. The committed
`docs/release-gates.json` is policy; generated evidence belongs under ignored `release/`.
Maintainers use the [release workflow](.github/workflows/release.yml) to rehearse a build, and
only a reviewed `v*` tag can publish artifacts after its audit passes. See the README's
[release audit](README.md#release-audit) and [launch checklist](docs/open-source-launch.md).
