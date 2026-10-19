# Supplemental notices for bundled payloads

The [native review](native-runtime-inventory.md) found that supplemental
notices were only collected when a package had no installed project notice.
That omitted an explicitly supplied font notice whenever the containing
package already had a license. The
[reproduction](release-evidence/native-notice-reproduction-2026-10-14.json)
records the omission with a synthetic package and the previous source hash.

The Python and Node collectors now retain both installed and supplemental
notices. A supplemental entry can also declare an `artifacts` map of relative
installed paths to SHA-256 hashes. All listed files must exist and match before
the supplemental notice is copied. Empty, invalid, noncanonical and escaping
bindings are rejected. For Node, paths are relative to the individual installed
package. For Python, they are relative to the distribution root reported by
`importlib.metadata` (normally `site-packages`), which must remain inside the
observed interpreter prefix. Bindings do not include a Python-version-specific
`lib/pythonX.Y` prefix.
The generated notice reference retains that map as `installed_artifacts`.

This extends the existing inventory collector; it does not infer a license from
a package name, rename an upstream license, or automatically qualify an
embedded dependency. The manifest and original notice are preserved in
`notices.zip`, along with the generated references.

## Recovered Codicon notice

The six Codicon font paths in the two installed Playwright Core versions
(`1.63.0` and `1.64.0-alpha-1790635538000`) have identical SHA-256:

```text
0f1d5219934e96e83b8db162d60b4d8c09b5de1e7d38031cbafe4a3c0f2889c9
```

The font also matches the bytes in Playwright source commit
`1b025d7e20a026371cd5f98ba0cdce48892737c8`, the `gitHead` in the npm
`playwright-core@1.63.0` release metadata. That source subtree contains the
original notice. The source font, CSS and license were checked against their
Git blob identities; the font was additionally checked against the installed
payload hashes.

The [unmodified notice](../third_party/supplemental/playwright-codicon/LICENSE.txt)
and [provenance record](../third_party/supplemental/playwright-codicon/provenance.json)
are retained separately from Playwright's top-level project license.
[Supplemental entries](../third_party/supplemental-notices.json) bind it to the
three font paths in each exact package version. The alpha package's coverage
comes from the identical font bytes, not an assertion that its entire source
tree equals the release revision.

## Review limits

The Codicon recovery covers six font paths; the Inspect recovery covers four
more. These additions supply original notices for all ten observed font paths.
They do not complete the containing packages' dependency review or the other
native scopes. The generated inventory therefore retains its 116
component/scope coverage gaps and its unverified status.

The remaining Inspect font notices have now also been recovered:

- Both installed Inspect fonts match bytes in the integrity-verified
  `@tanstack/devtools-ui@0.7.0` archive. That archive includes the original
  font-specific OFL notices, now retained under
  [the supplemental font directory](../third_party/supplemental/inspect-fonts/provenance.json).
  The package archive integrity matches the pinned Inspect frontend lock at
  the commit introducing those font assets. This closes the earlier
  byte-provenance uncertainty for these fonts; it does not qualify Inspect's
  entire dependency tree.
- The installed NumPy build configuration identifies Accelerate for BLAS and
  LAPACK. Its general license document also names OpenBLAS-related binary paths
  that are absent from this installation. Neither the general list nor the
  build configuration alone establishes all statically embedded dependencies.

The six outstanding original notices in optional Node API packages are a
separate list and are not cleared by this change. No cloud, model or
real-Muse qualification follows from a notice recovery.

The focused [distribution/native checks](release-evidence/native-notice-focused-2026-10-14.xml)
passed 76 cases for the Codicon change. The later Python-root and Inspect-font
checks [passed 79 cases](release-evidence/inspect-font-notices-final-2026-10-14.xml),
including interpreter-root containment and version-independent artifact paths.
The [reproduction](release-evidence/python-notice-base-reproduction-2026-10-14.json)
records why prefix-relative Python bindings were unsuitable.
Current full-regression and fresh-install status is recorded per release (see
[release audit](../README.md#release-audit)); historical receipts are not renewed
merely because this document changes.
