> **Scope update, 15 October 2026:** The supported release is now
> [`local-consumer-v1`](release-scope.md). The full-spec gates and development
> capabilities discussed below are historical context, not current release prerequisites.

# Optional API dependency notice review

The 14 October 2026 follow-up recovered the original `tr46@0.0.3` project
notice, reducing original-notice gaps from six to **five**. The earlier
12 October review reduced the count from nine to six.
It does not establish legal clearance or a complete inventory of native binary
subcomponents. The remaining gaps continue to reject distribution qualification.

## tr46 follow-up — 14 October 2026

The npm release records source commit
`a8009f9ce80ff5dbe71dd71e203afe4e4c878d28`. Its immediate child,
`3a6f29721e7063b9ffd421e461a54beae6170001`, is the author's “Add MIT license”
commit. Recursive Git trees establish that `LICENSE.md` is the only added
file and every existing tracked file has the same mode, type and blob hash.
The recovered notice's bytes match its Git blob.

The registry archive's SHA-512 integrity matches the committed dependency
lock. All five installed files match that archive. `index.js` and
`package.json` also match the release's Git blobs. The generated
`lib/mappingTable.json` is absent from those Git trees; it is verified against
the published archive, not claimed as a tracked-source match. Supplemental
collection binds the notice to the installed index, manifest and mapping-table
SHA-256 values. Package versions and executable code are unchanged.

The unmodified notice and [provenance](../third_party/supplemental/tr46-0.0.3/provenance.json)
are packaged under `third_party/supplemental/tr46-0.0.3/`.
The [follow-up record](release-evidence/notice-followup-2026-10-14.json)
retains hashes of the upstream responses and the remaining findings.
This resolves the project-notice gap; it does not establish a complete review
of embedded data or native dependencies.

Independent wheel reconciliation verifies the original notice and provenance,
all installed tr46 files and the unchanged API dependency lock.
Distribution qualification still refuses the five remaining project notices.

## Notices added

| Package | Version | Retained source |
|---|---|---|
| dnspython | 2.9.0 | Original project LICENSE at commit `e790c8f20a67ad439c7aafef2321e161c6028eee`, resolved through release tag `v2.9.0` |
| fastmcp-slim | 4.1.0 | Original project LICENSE at commit `51b618f7d5970d2870dc351081e16600f371291f`, resolved through release tag `v4.1.0` |
| abstract-logging | 2.0.1 | Dated plain-text snapshot from the author license host explicitly referenced by the installed `Readme.md` |

Both Python source archives were downloaded and checked against their SHA-256
values in `uv.lock`. Neither archive contains an original license file. A source
file from each archive matches the corresponding tagged upstream source exactly:
`dns/version.py` and `fastmcp/__init__.py`. These matches corroborate the release
association; they do not establish complete source equivalence or publisher
authentication. Notices fetched by tag also match the copies fetched by resolved
commit.

The abstract-logging notice is tied to the installed README's hash and referenced
URL. Its copyright year reflects the retrieved page. The record does not claim
that the page had the same contents when version 2.0.1 was published.

All three notices are copied byte-for-byte into
`third_party/supplemental/`. Their source URLs, retrieval dates, content hashes and
limitations are in `third_party/supplemental-notices.json`. Offline inventory
generation verifies their hashes, and verifies the author notice's installed
README reference before accepting it.

## Unresolved packages

| Package | Version | Evidence and remaining requirement |
|---|---|---|
| @stoplight/json-schema-merge-allof | 0.7.8 | Exact npm `gitHead` root tree has no notice file. The README's license declaration does not supply a complete permission notice |
| fnv-plus | 1.3.1 | Exact npm `gitHead` root tree has no notice file. The README's MIT label does not close the gap |
| parse-multipart-data | 1.5.0 | Exact npm `gitHead` root tree has no notice file; no complete original notice located |
| pretty-data | 0.40.0 | Published source and README declare dual MIT/GPL terms and link to generic license pages. No complete original permission notice located; registry metadata has no `gitHead` |
| proxy-agent-negotiate | 1.1.0 | Matching release-tag package manifest confirms name/version, but package-local and root LICENSE requests return 404. A sibling package's notice was not substituted |

Registry metadata for all seven originally missing Node notices was checked
against the corresponding tarball integrity in `tooling/api/package-lock.json`.
Root-tree inspection is scoped evidence, not proof that a notice cannot exist
elsewhere. Closing these five entries requires a traceable original notice or an
explicit dependency change followed by qualification of that changed stack.
No upstream messages or issues were sent during this review.

The 14 October follow-up inspected recursive current-branch trees for all five
remaining packages. None provided a new package-specific notice. In particular,
`fnv-plus` issue #14 contains an unanswered, downstream-authored license proposal;
`parse-multipart-data` PR #20 was closed **without merging**. Neither was
substituted for a publisher's original notice. The proxy-agents tree contains
sibling package notices, which were not applied to `proxy-agent-negotiate`.
These are bounded findings, not proof that every possible source has been searched.

The [retrieval record](release-evidence/notice-provenance-2026-10-12.json)
contains exact release identities, source/archive hashes, retrieved-evidence paths
and unresolved findings.
