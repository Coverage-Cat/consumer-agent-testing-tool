# Dependency security review

Reviewed 21 October 2026. GitHub's Dependency Graph reports the optional Node
lockfiles twice: once under `tooling/`, and once in the packaged copies under `src/`.
Each pair must stay in sync.

## Applied fixes

- Pin `certifi` to 2026.10.19 in the runtime requirements and lockfile. This removes
  the `emSign ECC Root CA - G3` root affected by compromised intermediate CA keys
  (GHSA-6q48-6c7w-9gw2). A regression test checks the actual CA bundle for the
  affected root's fingerprint.
- Pin workflow `uv` to 0.13.4, whose own CA bundle removes the same root
  (GHSA-hq85-5cpj-mr62). This covers CI, platform diagnostics and release setup.
- Update optional MCP Inspector to 2.11.0 and its compatible MCP SDK and Vite
  dependencies to patched versions. Keep the packaged initialization manifests
  synchronized. `npm audit` reports no findings for `tooling`,
  `tooling/payment-ui` or `tooling/ci`.
- Pin `aiohttp` to 3.14.4. This fixes GHSA-5wr8-gcqf-gqf3, GHSA-2g87-pp6c-29x5
  and GHSA-494j-54xm-rwp3 in the supported Python runtime.
- Remove Postman's pinned Lodash 4.17.21 (GHSA-r5fr-rjxr-66jc, GHSA-f23m-r3pf-42rh and
  GHSA-xxjr-mmjv-4gpg). An earlier override forced 4.18.1; the Postman stub below now
  removes that copy, and the rest of the Node tree already uses 4.18.1.
- Replace `postman-collection` with a local stub in `tooling/api/stubs/`. Prism loads the
  package but CATT uses only OpenAPI; the stub's `Collection` constructor rejects Postman input.
  This removes `@faker-js/faker` 5.5.3 (GHSA-qxc2-j82w-r537) and `uuid` 8.3.2
  (GHSA-w5hq-g745-h8pq). Upstream Postman 5.3.1 still pins both, so no compatible release
  exists. Prism's own Faker copy is 10.6.0, which is patched.
- Override `json-schema-faker`'s `json-schema-ref-parser` 6.1.0 with
  `@apidevtools/json-schema-ref-parser` 9.1.2. Its `js-yaml` 4 uses `argparse` 2, which drops
  `sprintf-js` 1.0.3 (GHSA-hp3w-g68c-fv3c, no patched release). `json-schema-faker` calls only
  the parser's `bundle()`, whose signature is unchanged.

`npm audit` reports no vulnerabilities in the optional API lockfile after these changes.
`tests/test_release_scope.py` checks that both lockfile copies match and exclude the
affected versions.

## Remaining limits

The stub is first-party code under the project license, installed as a copy through
`install-links=true` in `tooling/api/.npmrc`, so the inventory's no-link rule still holds.
Keep `.npmrc` and `stubs/` with the lockfile wherever it is copied. If a future Prism or
http-spec release needs real Postman support, remove the stub and review the new chain.

The worker's network restrictions are not a general code-execution sandbox. Do not use the
optional Node API tools with untrusted specifications, customer data or production
credentials. Review these dependencies before promoting any development feature, and
whenever they or their callers change.

The upstream advisories and fixes are recorded in GitHub's Security tab. Private reports
go to **engineering-safety@coveragecat.com**.
