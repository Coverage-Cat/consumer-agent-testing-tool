# Instinct emulation specification

Research and design: 21 October 2026. Status: supported connector surrogate.

CATT supports `instinct` as an explicit surrogate emulation target beside
`muse`. It approximates public behavior; it does not run Instinct, reproduce
its private implementation, or certify that Instinct can use a service.
The inference model remains a separate choice. The
[shared contract](surrogate-contract.md) specifies parity, the Python/CLI API,
memory lifecycle and qualification requirements.

## Research method and findings

The research covered Instinct's public site, terms, privacy policy, acceptable
use policy, founder announcements, a founder interview, public browser
assets, and payment-provider documentation. It used public, unauthenticated
GETs only. No account was created, no application API was called, and no
message or transaction was sent.

Searches for developer documentation, MCP, integrations, permissions,
credentials, payments, agent communication and security found no published
Instinct MCP integration contract. The site's sitemap lists the homepage;
`/llms.txt` returns the application HTML, not a documentation index. This is
an observation about the sources reviewed, not proof that Instinct has no
MCP support.

Primary sources take precedence over press reports and open-source projects
that imitate Instinct. Those projects helped locate announcements, but their
code, claimed architecture and inferred system prompts are not evidence of
Instinct's implementation.

### Source register

All sources were checked on 21 October 2026. The packaged policy records
source IDs, dates, retrieval hashes for the reviewed first-party pages, and
the findings used by the target. Hashes identify the retrieved bytes; dynamic
pages need not return the same bytes later.

| ID | Primary source | What it establishes; limits |
| --- | --- | --- |
| homepage | [Instinct](https://instinct.com/) | Text and phone interaction, computer and phone use, and examples such as groceries and travel. No tool schemas, execution architecture or channel limits. |
| launch | [Founder announcement, 26 August](https://x.com/noahrshinn/status/2092691344456351744) | A personal agent used through existing communication channels, with examples of delegated tasks. Product description, not an integration contract. |
| terms | [Terms](https://instinct.com/terms) | Connected applications, authorized actions, purchases, communications and Vault. Does not define per-tool approval classes or a precise consent state machine. |
| privacy | [Privacy policy](https://instinct.com/privacy-policy) | OAuth/API-style account connections, indexed context, third-party model providers and revocation. Disconnecting future access and deleting indexed data are distinct operations. It does not reveal the runtime model or token-handling architecture. |
| acceptable-use | [Acceptable use policy](https://instinct.com/acceptable-use-policy) | Restrictions on unauthorized access, transactions, impersonation and harmful activity. These are use restrictions, not claims about automatic enforcement. |
| email | [Agent email, 8 September](https://x.com/noahrshinn/status/2097443132816396649) | An agent can have its own email address for account creation, bookings and correspondence. Sending protocols and deliverability are unpublished. |
| network-launch | [Trusted Person network, 9 September](https://x.com/noahrshinn/status/2097794967574028448) | Agent coordination is restricted to the user's trusted-person network. The announcement does not publish the communication protocol. |
| vault-totp | [Authenticator support, 11 September](https://x.com/noahrshinn/status/2098466184912089143) | Vault can retain authenticator setup and generate TOTP codes. Initial enrollment or some security checks may still need the user. Secret storage and code-generation implementations are unpublished. |
| files | [Shareable Files, 18 September](https://x.com/noahrshinn/status/2101080443667767385) | Agents can produce private or public interactive pages and share them by link. This is distinct from an MCP App or a Muse widget. |
| network-update | [Network update, 23 September](https://x.com/noahrshinn/status/2102896414514688212) | Explicit approval of connection requests, invalidatable invitation links, file delivery and a claim of end-to-end encryption between agents. No wire format or cryptographic specification. |
| security | [Founder security statement, 23 September](https://x.com/noahrshinn/status/2102896837522804954) | Claims about isolated sandboxes, short-lived credentials, signed tool execution and a model-based hallucination monitor that can intercept execution. These are vendor statements, not independently verified guarantees. |
| interview | [Colossus episode 493, 28 September](https://colossus.com/episode/instinct-the-personal-agent/) | The public episode description and chapter list cover trusted networks, safety, payments, proactive work and understandability. The full transcript requires login and was not used as accessible evidence. |
| web-client | [Public browser entry bundle](https://instinct.com/assets/app-D0eDWfwq.js) | The delivered client contains routes for Vault, trusted-network requests, account connections and Workspace, and references to Link, 1Password and local-browser features. Client code can include gated or unused features; this does not prove availability or backend behavior. |
| stripe-wallet | [Stripe Link Agent Wallet](https://docs.stripe.com/agentic-commerce/agents/link-agent-wallet/use-link-wallet-pay-online.md) | Stripe documents customer-approved spend requests and single-use payment credentials. These mechanics are Stripe's contract. The client reference to Link does not establish that every Instinct purchase uses this flow. |

The browser entry bundle was 1,366,489 bytes with SHA-256
`885609421eb9969d4f903f99c356ef73102ff0abf2be9c91ce0e091ac0f4d7d3`.
Inspection was limited to delivered route, feature and channel references;
no code was copied into CATT and no protected routes were visited.

### Consequences for emulation

1. **Communication is the primary surface.** A text answer must work as a
   message. The initial target supplies a plain-text context and checks for
   tables, headings, HTML and Markdown link/image syntax. This formatting
   rule is a CATT assumption. Instinct publishes no universal text limit.
2. **Service access is broader than MCP.** CATT can exercise its existing
   browser and HTTP extensions as local approximations. Native MCP is a
   convenient service-testing adapter, not a claimed Instinct integration.
3. **Authorization belongs to the user.** Retain CATT's host-owned approvals,
   disclosure binding and private-value boundary. Label the entire approval
   mapping as assumed for Instinct. Never import Muse's published buttons or
   tool classes as documented Instinct behavior.
4. **Outcome evidence remains independent.** A confident answer cannot prove
   a booking, email or payment succeeded. Preserve oracle checks, uncertain
   write reconciliation and false-claim checks.
5. **Account and agent identity matter.** Trusted-network membership is not
   consent to every disclosure or transaction. A future network extension
   needs both an authenticated peer boundary and scoped authorization.
6. **Security statements are not implementations.** CATT's deterministic
   content checks are not Instinct's trained monitor. A local browser is not
   proof of equivalent sandboxing. Private overlays are not a Vault clone.

## User interface

The connector scenario gains one optional field:

```yaml
version: 1
kind: connector
emulation_target: instinct
# The rest of connector-v1 is unchanged.
```

Omitting it selects Muse. The CLI override is
`connector-run SCENARIO --emulation-target instinct`. The same flag on
`scaffold URL --output FILE` records the target in the generated scenario.
`scaffold --from-run RUN` preserves an Instinct trial's target in the checks
skeleton so merging it into a scenario keeps the selected treatment.
Scaffolding still calls only initialize and tools/list.

Target, projection, MCP client and model are separate choices:

| Choice | Meaning | Instinct |
| --- | --- | --- |
| `emulation_target` | Which assistant host is approximated | `instinct` |
| `projection` | How CATT offers service tools to its model | `native` or the neutral `cli` treatment |
| `client_profile` | The MCP transport CATT runs | `generic-sdk` only |
| `exposure` | Immediate or deferred tool discovery | `in-prompt` or `search-only`, both assumed |
| model profile | Which model drives a paid trial | Existing supported providers; unchanged |
| `driver: scripted` | Deterministic calls without inference | Supported |

`muse-cli` and the unofficial Muse template client are rejected for Instinct.
The rejection happens before linking, model preflight, service startup or a
run directory is created. Overrides must be validated together, so switching
a Muse CLI scenario to Instinct works only when native or neutral CLI projection and the
generic client are also selected.

The `check --suite muse-guidelines`, `poppy` and `gadget` suites remain Muse
specific. There is no `instinct-guidelines` suite: no equivalent guidelines
were found. `check --suite surrogate --emulation-target instinct` checks CATT's
shared catalog/projection contract; generic MCP contract checks cover protocol evidence.
Instinct runs do not attach Muse guideline findings after execution.
Its connector profile remains resolvable for replay and diff, but selecting it
through `--interface-profile`, `--profile` or `defaults.interface_profile` for
`run`, `inspect`, `check` or `doctor` is rejected. Those commands do not
implement the connector host.

## Supported implementation

### Target definition and provenance

Add a small target module and a packaged
`data/policy/instinct-public-2026-10-21.json` source register. The Instinct
profile is `instinct-connector-surrogate-2026-10-21`.

Each run records the target, profile, source-policy digest and behavior
bases. Documented product observations and local implementation assumptions
remain separate. Source retrieval is a research step, never a test/run
dependency. Runs load the packaged policy offline.

Keep the existing Muse profile, prompt, permissions and projection behavior.
Instinct receives a neutral personal-assistant prompt written for CATT;
no purported recovered prompt and no Meta-specific first line. The prompt
must not reveal testing, grading, oracle or fixture context.

Shared host algorithms may retain their existing behavior. Their evidence
must identify them as CATT assumptions for Instinct, including permission
classes, grant scopes, tool search, schema handling, result limits, browser
task management and HTTP access. Selecting Instinct must never mutate
module-level Muse provenance or affect a later Muse trial.

### Channels

Instinct runs automatically enable `presentation` with `channel: text` and
no widget-field checks. Text has no default character cap. A scenario can
set `max_characters` or disable enforcement using the existing presentation
configuration. The host records findings; it never rewrites the answer.

Optional `voice` reuses the existing speakability checks and assumed length
limit. `whatsapp` reuses the existing format treatment and assumed limit.
Neither claims to send a real message or call. `app` and Muse widget-field
checks are rejected for Instinct; Instinct Files need a separate future
contract. The default text channel does not claim SMS encoding, segmentation,
delivery, iMessage rendering or WhatsApp rate-limit fidelity.

### Extensions

Both targets support these extensions, with separate provenance:

| Extension | CATT behavior available to Instinct tests | Unproven part |
| --- | --- | --- |
| `clock` | Deterministic time in standing context | Instinct's prompt wording |
| `presentation` | Text format and optional voice/WhatsApp checks | Exact rendering, length limits and delivery |
| `browser` | Bounded browser tasks, consent, takeover and outcome checks | Instinct's tools, sub-agent prompt and isolation |
| `custom_connector` | Allowlisted HTTP requests and host-owned credentials | Instinct's API discovery and credential broker |
| `scheduler` | Deterministic background work at scripted idle points | Instinct's scheduler, timing and notification delivery |
| `content_trust` | Planted untrusted data, deterministic filters and checks | Instinct's classifier, coverage and monitor |
| `routes` | Connector discovery/consent, competing routes, phone fixtures and duplicates | Instinct's route selection and real phone execution |
| `memory` | Neutral standing context, stale facts, data minimization, scoped index and unlink/delete fixtures | Instinct's indexing, retrieval, storage and deletion implementation |
| `wallet` | Approval-bound test cards, local issuer, bank verification and merchant settlement checks | Stripe/Instinct payment rails and live financial behavior |

Existing browser/HTTP request restrictions and approval checks remain in
force. Browser tests can declare no MCP connectors and use oracle checks.
The browser extension still needs Node, Chromium and its existing setup.
No additional provider or service dependency is introduced.

Memory uses neutral headings for Instinct. Routes, wallet and memory retain
their local algorithms with assumed provenance in locks, events and checks.
`user_simulator` is available as the same exploratory, agent-only extension;
neither target qualifies it through deterministic tests. Registered developer
extensions use the same API for both targets; their implementations are not
part of CATT's qualified capability list.

### Evidence and comparison

- `result.json`, run locks, event envelopes and model sub-run manifests use
  the selected interface profile.
- The run lock adds `emulation_target` and `surrogate_contract`; Instinct also seals its source-policy
  digest. CLI overrides include target selection.
- CI summaries and JUnit properties name the target. Unknown/setup-error
  values remain nullable under the summary contract. The target field is
  optional in the version 1 schema so earlier summaries remain valid.
- Multi-trial summary evidence uses the effective target, including a CLI
  override. A model preflight also uses that target.
- Existing diff compares the new run-lock fields, so a target change is an
  explicit treatment rather than an invisible change.
- Reports identify an Instinct surrogate and its limits. Generic report
  disclaimers cover both targets.
- Export and replay preserve the target through the existing sealed-evidence
  path. No source text, private values or raw research download is added to
  release evidence.
- Muse scenarios keep their prior published scenario digest when the target
  is omitted or explicitly `muse`. Instinct scenarios have a different digest.

### Failure behavior

Keep connector exit codes: 0 pass, 1 finding, 2 setup/reporting error,
3 blocked/unsupported execution. Invalid target combinations are setup
errors; an unknown or unimplemented runtime capability never becomes a pass.
An assumed adapter may complete successfully while Instinct compatibility
remains unestablished. Reports must say so.

An unanswered approval is not authorization. Sensitive actions remain bound
to one approval and its effective arguments/disclosure. The model never
receives host-private inputs or credentials. A changed price or recipient
requires a fresh approval under this conservative CATT policy. This is an
assumed Instinct treatment, not a statement about every action Instinct takes.

## Acceptance and validation

Before the initial implementation, new regression tests must fail on main.
The completed tests must cover:

1. Default Muse selection; explicit Instinct in YAML and the CLI; target
   override with projection/client overrides; invalid target combinations
   rejected before any side effect or inference.
2. Successful scripted booking using the real loopback fixture, independent
   outcome checks, target-specific provenance and no private-value leakage.
3. An injected mock model exercising the target prompt, tool calls and
   sub-run evidence without a model-provider request.
4. Text formatting success/failure, optional channel settings, and no
   implicit text-length rule or Muse widget checks.
5. Correct provenance on approvals, projection, supported extensions,
   result/report, CI summary, JUnit and multi-trial evidence.
6. Browser and HTTP extension regressions, including permission denial and
   uncertain outcomes. Existing origin, credential and checkout boundaries
   must remain intact.
7. Default Muse behavior and source dictionaries unchanged after an
   Instinct run.
8. Scaffolded target selection, generated JSON schemas, sealed replay and
   exported evidence.

The parity matrix covers both targets under `native` and `cli`, including
positive and adverse routing, memory and payment cases. Installed-wheel smoke
tests verify separate runs, readiness and exports for each combination. The
`surrogate-parity` release gate requires exact named cases for every target.
Before pushing, run lint, schema regeneration and drift checks,
repository checks, the full test collection, and all connector-lane CI
commands locally. Browser changes also require repeated affected tests and
a run under CPU load. No paid trial is part of validation.

## Follow-on specifications

These need work beyond the shared surrogate contract:

- **Trusted network:** immutable peer identities, explicit connection
  approval, invitation revocation, per-peer/per-operation disclosure grants,
  separation of peer messages from user authority, denial notifications,
  and private-state isolation. Use an in-process scripted network first.
  Test unknown peers, forged identity, revoked grants, disallowed fields,
  cross-user leakage and prompt injection before adding any live transport.
  Do not call it Instinct's communication protocol.
- **Vault/TOTP:** host-private secrets and enrollment approvals, a controlled
  clock, code freshness/reuse checks, origin-bound credential use and
  explicit user handoff. No bypass of phone approval or service security
  checks. Specify how this differs from Muse credential surrogates.
- **Live payment rails:** the local wallet now covers approval, denial,
  expiry, single-use credentials and independent settlement. Real Stripe,
  Shop Pay and Instinct integrations would need their own contracts and
  authorized observations.
- **Email and calls:** scripted delivery adapters, sender identity,
  recipient-bound approval, incoming untrusted content and independent
  delivery receipts. Real outbound communication is outside local tests.
- **Files:** private/public access transitions, sharing consent, revocation,
  file limits, hostile HTML and downloads. Do not treat Files as MCP Apps.
- **Live memory integration:** the local index now separates unlinking,
  retained context, deletion and account isolation. Live ingestion, storage,
  OAuth revocation and deletion receipts remain unimplemented.
- **Calibration:** authorized real-Instinct observations paired with local
  runs, recording versions, dates and differences. Until then, keep
  compatibility and admission claims out of the results.
