# Open-source launch checklist

The initial repository is **private** under `Coverage-Cat`. A passing CI run is not permission
to change visibility. Only the repository owner should authorize the public launch.

## Private testing

- Keep `main` protected: pull requests, maintainer review, the required `CI` status check,
  no force pushes or deletion, and resolved review conversations.
- Use squash merges and automatic branch deletion. Keep Actions tokens read-only by default;
  pull-request jobs receive no provider keys or service credentials.
- Require all macOS and Linux jobs. Keep skips limited to named OS or Python-version constraints:
  macOS kernel enforcement, `compression.zstd`, and macOS `os.waitid`. A skipped feature is not
  a tested pass, and it does not excuse other failures.
- Run the staged-path/size check and Gitleaks before the first push. CI scans Git history, not
  just the latest working tree. Review exceptions individually and keep them value/path scoped.
- Enable Dependabot alerts and reviewed update pull requests. Dependency changes require lockfile
  and notice review. Do not enable paid security products without the owner's approval.
- Rehearse the release workflow manually. It uploads verified artifacts but does not create a
  release. Publishing is a separate, protected-environment job triggered only by a `v*` tag.
  Publishing is explicitly refused while the repository is private.

## Before changing visibility

- Review the complete Git history, issues, pull requests, Actions logs and retained artifacts for
  secrets, customer data, private operational details and unreviewed attachments. Making a
  repository private again does not recall copies made while it was public.
- Confirm rights to all code, documentation, branding and artwork. The project is MIT,
  copyright Coverage Cat, Inc.; dependency licenses and notices remain separate. The
  [trademark notice](../TRADEMARKS.md) excludes the names and logo from the license.
- Confirm the security and conduct reporting channels are monitored. Prepare private
  vulnerability reporting in the Security tab and enable it as part of the public transition.
- Review repository access, Actions policies and fork-workflow approvals. Require approval for
  all outside contributors' workflows; never use `pull_request_target` to execute PR code.
- Recheck branch protection, CODEOWNERS and the release environment. Release tags must point to
  reviewed `main` commits and match `project.version`. Configure required reviewers on the
  `release` environment after the public transition: the initial private Team-plan repository
  does not support that environment protection rule.
- Enable GitHub's public-repository secret scanning and push protection, and configure code
  scanning where available. Verify the settings after the visibility change; a private plan
  may not offer the same features. Gitleaks remains required regardless.
- Review all dependency alerts and the latest full CI run. Resolve security findings or document
  a reasoned, reviewed disposition rather than hiding them.
  The [dependency security review](dependency-security.md) records the fixes and remaining
  limits in the optional Node API tools; review them before launch or any expansion of the
  supported scope.
- Update the changelog and support scope; record new release evidence for the exact tag.
  Do not reuse historical local evidence to qualify a changed source tree.

## First release

Create the reviewed version tag only after the public transition is approved. The release
workflow verifies the tag/version match, performs an offline distribution proof and the full
macOS regression, and requires the release audit to pass. The publish job checks the artifact
hashes, signs provenance, and attaches the wheel, source archive, checksum file and evidence
archive to the GitHub release. It never publishes to PyPI.

Manual rehearsal can verify the private build without publication or attestations. Never
bypass the private-publication guard, provenance, or release audit to make a rehearsal green.
See the README's [release audit](../README.md#release-audit).

## Review record

Recorded 19 October 2026 for the private repository. This is not launch approval.

- Access: five admin collaborators, no teams, pending invitations, deploy keys or webhooks.
  No repository Actions secrets. One `release` environment.
- Issues and pull requests: eight pull requests, all opened by Dependabot. Their comments
  are Dependabot status notes. No issues.
- Actions logs and artifacts: 62 runs and 43 retained artifacts, including the release
  rehearsal's wheel, source archive and evidence archive, were downloaded and scanned for
  credential formats, authorization values, email addresses, home paths, hosts and public IP
  addresses. Matches were synthetic fixture values, runner paths, public package hosts and
  documentation addresses. No `private/` run directories were retained.
- Git history: every commit uses the owner's GitHub noreply identity. First-party email
  addresses and hosts are synthetic, public documentation, the public Coverage Cat sandbox
  or the monitored reporting address.
- Dependencies: the six open Dependabot alerts in the optional API lockfiles are fixed;
  see the [dependency security review](dependency-security.md). `npm audit` reports none.

Still open: the public-transition settings above, release evidence for the exact tag, and
the owner's launch approval.
