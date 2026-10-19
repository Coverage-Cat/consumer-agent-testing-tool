# Standalone event provenance

The consumer-flow and browser runners previously wrote ordered flat events
without the common event identity, clocks, audience, profile or artifact
references required by specification §15. Their standalone results also lacked
explicit lane/location/notice metadata. The
[synthetic reproduction](release-evidence/sealed-event-reproduction-2026-10-14.json)
records the missing fields in both runners.

New events use `event_schema_version: 2`. They reuse the core evidence
envelope and content-addressed artifact writer. Each event records run,
scenario, event, agent and task identities; timezone-qualified wall time;
monotonic time; interface profile; audience; provenance; redaction status; and
the hash/path of its redacted payload. Parent IDs remain null when no causal
parent was explicitly established.

A clock epoch identifies each recorder lifetime. Resuming a consumer workflow
retains its run identity and increasing event sequence but starts a new epoch.
Do not subtract monotonic timestamps across epochs or infer elapsed user wait
from them. Wall-clock discontinuities remain visible. Browser attempts with
the same directory basename receive distinct run identities.

`result.json` is the versioned manifest for these sealed trajectories; core
Inspect runs retain their existing `manifest.json` layout. The result includes
lane, evidence label, interface profile, execution-location declarations and a
reference to the bundled public dependency notices. Those notices do not
establish distribution completeness or clear the release's missing-notice
gate. Interrupted recovery marks unavailable location metadata as unknown
rather than reconstructing an execution environment it did not observe.

The original flat payload fields remain available to existing readers, while
the host's envelope fields cannot be replaced by payload data. The original
payload is also retained in its artifact, including any fields whose names
overlap the envelope. Diff compares those payloads so diagnostic timestamps and
fresh recorder IDs do not create spurious behavioral differences.

Playback and sealing validate the event/result identities, profile, clocks,
payload agreement, artifact hashes and notice reference. Export includes only
referenced public artifacts, applies the [export redaction](#export) and
recomputes payload hashes. Private checkpoints, screenshots and traces retain
their existing exclusion rules. Each event row and its artifact are written
from the same single redaction pass.

Historical flat records remain historical. Playback does not invent timestamps,
profiles or artifact references for them. A current-format result cannot claim
coverage over a mixture of current and historical events.

Verification passed **1,559 tests without skips** and all **28 fresh-install
stages**. The installed interpreter verified 17 browser trajectories and 313
events. The first fresh build exposed a diagnostic-pairing incompatibility:
the verifier hashed newly added recorder metadata as part of the snapshot.
It now compares the verified observation payload. Private screenshots and
traces remain excluded; the public notice ZIP is retained.
This improves retained evidence; it does not authenticate
external observations or turn service responses into independent backend
assertions.

## Redaction

Evidence uses three redaction scopes:

| Scope | Hides | Used for |
| --- | --- | --- |
| `mask_credentials` | Registered credentials; values under credential keys such as `token`, `password`, `api_key` or `session_id`; bearer tokens; credential-named URL parameters; the same inside JSON embedded in text | What the model sees in `surface-run --driver agent` and the consumer helper |
| `clean` | All of the above, plus every registered secret, `contact`, `email` and `phone` fields, email addresses (shown as `[EMAIL]`), URL parameters such as `code`, `key` and `sig`, and the home directory (shown as `~`) | Terminal output and error messages |
| `clean_evidence` | All of `clean`, plus personal-data fields | Every event, artifact, manifest, result and Inspect log the evidence layer writes, and every export |

Model views hide credentials only, because the model must see the user's own
data. The browser's model view masks registered credentials and password fields;
see [restricted browser behavior](release-extensions.md#restricted-browser-behavior).
In the API lanes the model receives service responses as returned, and the
provider boundary refuses any model request that contains a registered
credential (`credential_in_model_context`, exit 1).

Personal-data fields are matched after camelCase and kebab-case normalization:
`full_name`, `first_name`, `last_name`, `given_name`, `family_name`; `birthday`,
`date_of_birth`, `dob`; `license_number`, `drivers_license`, `ssn`,
`social_security_number`, `passport_number`, `tax_id`; `address`, `street`,
`street_address`, `address_line1`, `address_line2`, `billing_address`,
`shipping_address`, `home_address`, `mailing_address`, `postal_code`, `zip`,
`zip_code`; and the contact fields `e-mail`, `email_address`, `phone_number`,
`telephone` and `mobile`.

A value under a credential or personal-data key is replaced whole: strings,
numbers and entire nested objects or lists become `[REDACTED]`; booleans and
null stay. In JSON Schema and OpenAPI maps (`properties`, `patternProperties`,
`$defs`, `definitions`, `dependentSchemas`, `schemas`), an entry that is a schema
(a boolean, an empty object, or an object that uses schema keywords) is cleaned
as a schema, so a property named `password` keeps its definition. Other entries
follow the key rules. Content nested deeper than 64 levels is replaced with
`[REDACTED: nested deeper than 64 levels]`.

### What counts as a secret

Registered secrets are at least 4 characters after trimming. Booleans, null
words (`true`, `false`, `null`, `none` and similar), token types (`Bearer`,
`Basic`, `Digest`, `DPoP`, `Negotiate`), redaction markers, harness status and
envelope words (`passed`, `completed`, `developer` and similar) and the run's
registered identity are never secrets. Numbers are registered by their decimal
text. These rules apply to every registration, including direct appends to
`Redactor.secrets`.

Environment variables count only when their name mentions authorization, a
cookie, a password, a secret, a token, an API or access key, storage state,
contact, email or phone. A value counts only with at least 8 characters and 3
distinct characters, and never as a boolean or a number. A local path counts
only with an opaque, token-like segment; a URL only with user info, credential
query parameters or opaque path segments. For example,
`TOKENIZERS_PARALLELISM=false` registers nothing. Contact-named variables
without a credential word, such as `EMAIL`, are hidden in evidence but are not
credentials: model views show them, and the provider-boundary guard ignores them.

The run's identity (run, scenario, profile and clock names) is never rewritten.
Machine values (digests, generated IDs, evidence paths and ISO timestamps) lose
only an exact or 16-plus-character secret match, so a short secret cannot
corrupt a seal or a link.

### Export

`export` rebuilds an allowlisted copy with `clean_evidence`. It applies the
environment's credentials, the run's own private values and any parent run's
values. Private values come from `checkpoint.private.json`,
`consumer-checkpoint.json` and `private/*.private.json`: everything under a
`secrets` mapping, and string values under credential keys. These files are
never exported. Unstructured private state is itself treated as a secret.
Linked, oversized (over 64 MiB) or unreadable private state refuses the export.
Export rules are context-free, so each exported row still agrees with its
exported artifact.

Before it publishes anything, export scans every staged file for the run's
private values, as whole words outside digests and timestamps, JSON- and
HTML-escaped forms included. Values of at least 4 characters count; numbers
need at least 6 digits. The scan reads the members of `.zip` and `.eval`
archives (Inspect logs hold system prompts and provider payloads no other file
repeats) and of any file that starts like a zip, nested archives two levels
deep, with at most 10,000 members, 64 MiB per member and 256 MiB per archive.
Only a dependency-notice archive that a `result.json` or `manifest.json`
declares by path and digest, and that still matches, is skipped. A value
found, or an archive that is unreadable, encrypted, nested too deeply or
beyond these bounds, refuses the export (exit 2, category `reporting`), and
nothing is published. The message never names the value, and for an archive
names only the staged file, not its members. This applies to sealed exports
and to standalone core-run exports (`export <core-run>`).

Limits: a value that appeared in evidence before the run learned it was a
secret stays in the local run files. Surface runs keep such values in
`private/late-secrets.private.json`, so exports still redact them. A learned
secret that equals a scenario check ID, such as `saved`, also hides that check
ID. The seal stays valid, but the displayed ID and diff alignment by ID are
affected.

## Dependency notices

Each sealed result and core manifest references the bundled public dependency
notices as `artifacts/<sha256>.zip`. In each evidence directory, that file is a
read-only hard link to a verified copy in the workspace's
`.cache/notices/<sha256>.zip`, or a read-only copy when the run is on another
filesystem. It is never a link to `third_party/notices.zip` or the installed
package data, so regenerating the distribution archive cannot change sealed
evidence; `scripts/inventory.py` also replaces `notices.zip` rather than writing
into it. Do not edit evidence files in place: hard-linked files share their
contents.
