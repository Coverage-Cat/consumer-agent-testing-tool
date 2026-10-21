# Changelog

## Unreleased

- Experimental Instinct emulation target for connector trials and selected
  extensions, with source research, plain-text checks, separate provenance,
  a scripted example and CI coverage.
- Connector run locks, CI summaries and JUnit now record the emulation
  target. Comparing older Muse evidence with a new run can report lock
  changes; published Muse scenario digests and older summary validity are
  preserved. Text presentation is also available as an assumed Muse treatment.
- Connector-only interface profiles are rejected by other execution commands
  instead of labeling a run with host behavior those commands do not implement.
- Update optional MCP Inspector to 2.11.0 and its locked MCP SDK and Vite
  dependencies to patched versions. Fresh `init` projects receive the same
  manifests as the repository.
- Pin certifi to 2026.10.19 and workflow uv to 0.13.4 to remove the emSign
  trust root affected by compromised intermediate CA keys.

## 0.1.0 - 2026-10-20

First public release.

- Consumer-agent surrogate testing for MCP connectors, MCP Apps, browsers and mapped HTTP APIs.
- Scripted, model-free trials; service-side checks; sealed evidence, exports, replay and diff.
- Task-owned streamed HTTP cleanup, with cleanup exceptions treated as test failures.
- GitHub CI, secret scanning, dependency update proposals and audited release automation.
- The MIT license excludes the Coverage Cat and CATT names and the logo; see `TRADEMARKS.md`.
- Optional Node API tools: a local stub replaces `postman-collection` and rejects Postman
  collections, and a maintained ref-parser replaces `json-schema-faker`'s old one. This removes
  Faker 5.5.3, uuid 8.3.2 and sprintf-js 1.0.3 and their advisories.

This is alpha software. Supported scope and exclusions are in the README. Release artifacts must
pass the release audit for their own commit.
