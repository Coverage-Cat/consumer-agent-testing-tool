# Changelog

## Unreleased

Initial private testing of version 0.1.0 before an open-source launch.

- Consumer-agent surrogate testing for MCP connectors, MCP Apps, browsers and mapped HTTP APIs.
- Scripted, model-free trials; service-side checks; sealed evidence, exports, replay and diff.
- Task-owned streamed HTTP cleanup, with cleanup exceptions treated as test failures.
- GitHub CI, secret scanning, dependency update proposals and audited release automation.
- The MIT license excludes the Coverage Cat and CATT names and the logo; see `TRADEMARKS.md`.
- Optional Node API tools: a local stub replaces `postman-collection` and rejects Postman
  collections, and a maintained ref-parser replaces `json-schema-faker`'s old one. This removes
  Faker 5.5.3, uuid 8.3.2 and sprintf-js 1.0.3 and their advisories.

This is alpha software. Supported scope and exclusions are in the README. No public release is
claimed by this entry; release artifacts must pass the release audit for their own commit.
