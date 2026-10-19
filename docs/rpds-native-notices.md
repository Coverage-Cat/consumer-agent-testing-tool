# Native notice recovery for rpds-py

The retained `rpds-py@2026.6.3` Rust SBOM declares 17 dependency archives with
SHA-256 values. Both observed Python installations contain the same SBOM and
the same `rpds/rpds.cpython-314-darwin.so` payload. Previously, their notice
inventory contained the parent project's notice and the SBOM, without the
original notices from those dependency archives.

The 14 October 2026 review retrieved all 17 exact archives, totaling 2,942,237
bytes, and verified every archive against its declared hash before reading it.
No archive code was executed. All 34 original notice files are retained
byte-for-byte under `third_party/supplemental/rpds-native-2026.6.3/`.
This includes the nested `pyo3-runtime` notices, the LLVM exception in
`target-lexicon` and the separate Unicode notice in `unicode-ident`.
No alternative license was selected or rewritten.

The [provenance record](../third_party/supplemental/rpds-native-2026.6.3/provenance.json)
retains archive identities, Cargo manifest hashes, declared license expressions,
notice member paths and hashes, the upstream SBOM reference and both observed
parent payload records. Supplemental collection binds each notice to the exact
observed binary hash before including it in the package inventory.
These bindings are for the observed CPython 3.14 macOS ARM64 payload; another
wheel or architecture requires a corresponding review and bindings.

This is partial native review. The upstream SBOM says `all_targets=true`:
it can include build-time and other-target dependencies. Keeping every listed
notice avoids silently dropping that material, but does not establish which
code is linked into the binary. It does not establish a reproducible build,
upstream authenticity, compiler/runtime coverage, platform-library coverage
or completeness of unlisted vendored code.

The native inventory's 116 coverage gaps remain open, as do the five unrelated
original project-notice gaps. Verification passed **1,559 tests without
skips** and all **28 fresh-install stages**, with 99 focused inventory,
distribution and recorder checks. The installed package proof verified all
34 notices and 68 payload bindings across the two Python installations.
The [qualification input guide](qualification-inputs.md) still describes the
work required before the release can qualify.

Read-only inspection of the exact installed binary identified two
`LC_LOAD_DYLIB` entries: `/usr/lib/libiconv.2.dylib` and
`/usr/lib/libSystem.B.dylib`. Its `@rpath/rpds.cpython-314-darwin.so` entry is
its own `LC_ID_DYLIB`, not a third dependency. No referenced system file was
opened. The binary's load names and version fields do not identify the actual
deployed library bytes. Those payloads, their original notices and build/linkage
evidence remain specific qualification inputs.

- [Archive retrieval hashes](release-evidence/rpds-notices-retrieval-2026-10-14.json)
- [Packaged notice and binary reconciliation](release-evidence/rpds-notices-package-2026-10-14.json)
- [Binary dependency review and remaining inputs](release-evidence/rpds-notices-native-review-2026-10-14.json)
