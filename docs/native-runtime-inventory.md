> **Scope update, 15 October 2026:** The supported release is now
> [`local-consumer-v1`](release-scope.md). The full-spec gates and development
> capabilities discussed below are historical context, not current release prerequisites.

# Native runtime inventory

The package SBOM and copied project notices do not establish the dependencies
embedded in native payloads. `scripts/inventory.py` now writes a separate
`third_party/native-runtime.json`, includes it in `notices.zip`, and binds its
hash in subsequent fresh-install verification records.

The collector reads only explicitly selected installed-file inventories inside
the workspace: core Python, optional API Python, the three Node tooling groups
and the cached browser distribution. It recognizes ELF, Mach-O (including
bounded universal-image tables), PE, WebAssembly, TrueType, OpenType, font
collections and WOFF headers. Filenames alone do not classify a payload.
This is header identification, not a full executable-format validator.

Each artifact path records its format, kind, size and SHA-256. The parent
component retains the observed package name/version when available, and copied
upstream SBOM document references. Browser aliases and separate installations
remain visible; summary counts distinguish artifact paths from unique hashes.
The collector checks known installed-file hashes and rejects missing Python
payloads, changes during observation and paths escaping the selected workspace.
Disposable Python bytecode caches are excluded.

The collector does not execute the observed payloads, infer embedded libraries
from package names, or treat a package license string as an original notice.
Unrecognized formats and archive contents are not claimed as analyzed.
System interpreters, libraries and fonts outside the workspace are not scanned.
Container images require their own current payload and notice reconciliation;
the historic OS inventory does not silently qualify newer images.

Observed files start with **unverified dependency and notice coverage**.
Available upstream SBOMs are evidence to review, not automatic completeness
certificates. The report retains explicit gaps for each component and for
unobserved host/container scope.

`consumer-agent-testing-tool release-audit` has a separate `native-runtime-inventory` gate with
basis `inventory-review`. It requires a current-source review artifact linked
to the inventory by path and hash. The reviewed inventory must have no
outstanding gaps, cover all declared installation/host/worker scopes, and retain
nonempty artifact hashes plus linked dependency and original-notice review
evidence for every component. Missing scopes, unreviewed fields, changed
inventories or changed review documents cannot pass.

The review artifact also supplies an `observation` reference whose path is
exactly `third_party/native-runtime.json` and whose SHA-256 matches that file.
The observed inventory must match the single `native-runtime.json` member in
`third_party/notices.zip`, which is included in the source fingerprint.
The reviewed inventory must preserve every known component and all its
observed metadata and artifacts; only the dependency/notice coverage fields
can be replaced with review decisions. Additional discovered components are
allowed and must meet the same review requirements. Known null metadata must
remain explicit. A review cannot clear a gap by deleting its known payload.

The [reproduction](release-evidence/native-binding-reproduction-2026-10-14.log)
demonstrated that the former checker accepted an omitted known component or
payload. The binding fix passed
[109 focused checks](release-evidence/native-binding-final-2026-10-14.xml).
The actual generated inventory also
[matches its sealed copy](release-evidence/native-binding-observation-2026-10-14.json).
This establishes binding to known observations, not their authenticity or the
completeness of unknown embedded dependencies.

Review quality and provenance remain operator-attested. Hash validation does
not establish the truth or completeness of a review. Do not relabel generated
observations as reviewed without completing that work. The collector does not
publish a passing review artifact or modify live qualification gates.

The [supplemental-notice fix](native-notices.md) now retains an embedded
payload's original notice even when its containing package has a project
license. Exact installed-payload hashes now bind three recovered original
notices to all ten observed font paths. Python bindings use the contained
distribution metadata root. That notice recovery does not qualify the
containing packages' complete native dependency coverage or other native
scopes.

The 14 October 2026 scan observed **138 binary/font artifact paths** across
**57 components**, with **82 distinct payload hashes**, **10 font paths** and
**12 upstream SBOM references**. Its **116 coverage gaps** remain unresolved.
The current complete checkpoint reconciles these observations against a fresh
installation and includes the review-binding fix in **1,614 passing tests**
with both real Docker lanes enabled. This does not resolve the inventory's
coverage gaps; host and worker-image scopes still require separate review.

The [rpds-py follow-up](rpds-native-notices.md) recovered 34 original notices
from 17 exact Rust dependency archives. Their all-target SBOM association and
observed binary bindings are retained; linkage and compiler/platform review
remain open. This does not reduce the 116 unresolved coverage gaps.

The [broader retained-SBOM pass](native-sbom-notices.md) recovers original
notice material for all 344 declared registry archives and the separately
hashed OpenSSL archive. It also corroborates local Rust component names and
versions against seven locked parent source distributions. This adds review
material and exact binary bindings without declaring complete native coverage.
