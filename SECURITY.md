# Security policy

## Report a vulnerability privately

Email **engineering-safety@coveragecat.com**. This is the private reporting channel during
initial testing and after public launch. Do not disclose vulnerabilities in public issues,
pull requests, discussions, or workflow logs.

Include the affected version or commit, a description of the impact, and a minimal reproduction
using synthetic data and a service you control. Identify any affected trust boundary, such as
credential isolation, approval binding, network restrictions, private evidence, or export
redaction. Do not send live API keys, payment details, customer records, or raw run directories.
Ask for a suitable transfer method if a report needs sensitive attachments.

Maintainers will investigate and coordinate remediation and disclosure with the reporter.
If you have not received a response after five business days, follow up at the same address.
Once public, GitHub's private vulnerability reporting may also be used when enabled in the
repository's Security tab.

## Supported versions

The project is currently in pre-release testing. Security fixes target `main` and the latest
release once releases begin. Older releases and opt-in development extensions have no promised
maintenance window. Experimental status does not prevent reporting a vulnerability.

The optional Node API tools have [open dependency findings](docs/dependency-security.md).
Do not use that experimental stack with untrusted inputs while those findings remain open.

## Scope and safe testing

CATT is a surrogate testing tool, not a guarantee that a model or target service is safe.
Only test systems and accounts you are authorized to use. Do not use real customer data or
execute real purchases to demonstrate a problem. Model inference and live-service operations
require separate, explicit authorization.

`runs/`, `.private/`, local model profiles, checkout checkpoints, service logs and environment
files can contain secrets or personal data. Keep them out of Git and issue attachments.
Use `consumer-agent-testing-tool export RUN` and review the result before sharing; an export
is not permission to disclose the underlying customer's data.

The repository uses locked dependencies, least-privilege CI, secret scanning and reviewed
updates. These are safeguards, not a claim of complete security. If a real credential is ever
committed, revoke or rotate it first, then coordinate removal from history and retained
artifacts. Deleting the latest copy does not revoke the credential.
