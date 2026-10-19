# Original notices from retained native SBOMs

The 14 October 2026 follow-up examines all eight distinct upstream SBOMs
already retained with the observed Python native packages. Seven describe
Rust dependencies; one separately describes OpenSSL. Copies across installed
scopes remain distinct in the inventory.

The review verified 344 exact registry archives against their declared SHA-256
values. It reused the 17 previous rpds-py downloads and retrieved 42,132,130
additional compressed bytes. The 55,235,667-byte OpenSSL archive also matched
its separate SBOM digest. No archive code was executed.

The retained material comprises **630 original notice and attribution files**,
with **752 new package bindings** and reuse of the 34 earlier rpds-py bindings.
One repeated source association is deduplicated. Every new binding checks the
exact observed Python binary before copying its notice into the inventory.
This is evidence for those installed payloads, not other wheels or architectures.

## Source exceptions and associations

The registry archives supply 622 notice/attribution files. The initial filename
scan missed the two r-efi notices stored in `AUTHORS`; their READMEs explicitly
refer to those files. The original combined texts are retained unchanged.

Three crates omit standalone notices but record exact source commits:

- `uuid-simd@0.8.0` and `vsimd@0.8.0` reference the repository's root `LICENSE`.
  All 10 and 24 comparable published files respectively match their commit-tree
  blobs, including their original Cargo manifests and source files.
- `wit-bindgen-rt@0.39.0` inherits the workspace license declaration. The root
  Cargo manifest and three original license files match their Git blobs.
  Ten comparable published files match the recorded commit; the archive's
  `Cargo.lock` is absent from that tree and remains an explicit exception.

The five recovered commit-level notice copies bring the registry-associated
file count to 627. The OpenSSL archive adds three original notice/attribution
files, including its bundled Text-Template notice. Its `copyright.pm` candidate
is a Perl maintenance helper, not a standalone notice, and was excluded without
execution.

Seven Python source distributions also match their `uv.lock` hashes. Their
Cargo manifests corroborate all **14 distinct local Rust components** listed
without separate registry hashes, across **21 installed-scope references**.
This establishes source name/version associations, not compiled-source
equivalence.

The [provenance record](../third_party/supplemental/native-sbom-2026-10-14/provenance.json)
retains archive identities, original member paths, notice hashes, source-commit
comparisons, parent source references and observed binary bindings. The initial
OpenSSL request correctly refused its size under a 32 MiB limit; after checking
the declared size, a bounded 60 MiB request succeeded and matched the digest.
The registry and OpenSSL transfers remained within the 256 MiB budget.

## Qualification limits

All-target SBOM declarations can include build-time and other-target code.
Retaining their notices does not prove the exact linkage set, the completeness
of vendored code, upstream authenticity, or compiler/platform coverage. No
license alternatives were selected and no legal clearance is claimed.

The native inventory's **116 coverage gaps** remain unverified. The **five
unrelated original project-notice gaps** also remain open. Host and worker-image
scopes still require separate evidence; no files outside the authorized
workspace were scanned.

Verification passed **1,559 tests without skips**, all **28 fresh-install
stages**, and 99 focused inventory/distribution/recorder checks. Independent
reconciliation verified all 630 notice files, **1,070 installed bindings**
across ten package scopes, 352 source archives and 44 source-commit file
comparisons. The one-off verifier initially conflated the Cargo `openssl`
crate with the C OpenSSL archive by name; its lookup was corrected and the
successful check did not change packaged inputs.

Read-only metadata inspection covered seven binary payloads and eight
architecture slices, including both slices of one universal Mach-O payload.
It retained load entries naming libSystem, libiconv, CoreFoundation,
CoreServices and Security. Those names do not identify the deployed dependency
bytes or notices, and inspecting an additional slice does not qualify its
execution platform. No referenced host library was opened.

The [qualification guide](qualification-inputs.md) describes the remaining
inputs and work. See [package reconciliation](release-evidence/native-sbom-notices-package-2026-10-14.json)
and [binary metadata review](release-evidence/native-sbom-notices-native-review-2026-10-14.json).
