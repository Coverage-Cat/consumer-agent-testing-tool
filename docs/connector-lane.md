# Connector lane

Muse and Instinct are supported surrogate targets; Muse is the default.
Set `emulation_target: instinct` or pass `--emulation-target instinct`.
Its [spec and research](instinct-emulation.md) define the supported extensions,
default text channel, native MCP adapter and evidence limits. Shared host
rules are assumptions for Instinct; its runs do not apply Muse guidelines.

`connector-run` tests a developer's MCP connector the way a Muse-like personal
agent would use it. An agent, either a model or a scripted replay, works with
the server's real `tools/list` catalog through a host. The host plays a
Muse-like trusted control plane. It links the account and holds the
credential, keeps the user's private data out of model context, shows
approval cards to a scripted user and checks the outcome after the agent stops.

The host is a labeled public-constraint surrogate (profile
`muse-connector-surrogate-2026-10`). It claims no equivalence to Muse. Every
Muse-specific behavior carries a basis label:

| Label | Meaning |
|---|---|
| `documented` | Quoted from a published source, usually Meta's: the connector guidelines (`platform.muse.ai/docs`), the Meta Help Center, Meta's security post, `introducing.muse.ai` or `dev.meta.ai` |
| `unofficial` | Taken from an unofficial, unlicensed mirror of files or prompts from inside Muse; cited by repository and commit, never copied |
| `assumed` | A harness choice where nothing is published |
| `unknown` | Not published and not modeled; extensions use it, for example for third-party push |

The host's labels are in `run_lock.bases`, the projection's in the
`projection` event's `basis`, the permission engine's in the result's
`approvals.basis`, and each client profile's in its `basis`. An extension's
labels are in `run_lock.extensions[].bases` when its factory declares them.

The lane runs without `--development-extensions`. The
[Muse emulation review](muse-emulation-review-2026-10-15.md) explains why it
exists. [Connector extensions](connector-extensions.md) describes the lane
extensions, and [examples/connector](../examples/connector/README.md) lists
every shipped example with its commands.

## Quick start

From the repository root:

```sh
.venv/bin/python -m consumer_agent_testing_tool.connector_fixture --port 8765 --variant good &
FIXTURE=$!
.venv/bin/consumer-agent-testing-tool connector-run examples/connector/fixture-booking.yaml \
  --driver scripted --script examples/connector/fixture-booking.calls.json
.venv/bin/consumer-agent-testing-tool connector-run examples/connector/fixture-booking.yaml \
  --model-profile YOUR_CONFIGURED_PROFILE
kill $FIXTURE
```

The fixture prints one JSON line when it is ready. The scripted replay needs
no model. The agent run needs a model profile in
`consumer-agent-testing-tool.yaml` (see `init` in the [README](../README.md)
and [model providers](model-providers.md)). It runs the provider preflight
before it links anything.

The lane is not tied to the bundled fixture. `examples/connector/everything-weather.yaml`
runs the same way against a third-party server, the MCP project's "Everything"
reference server, started with
`PORT=3001 npx -y @modelcontextprotocol/server-everything@1.0.0 streamableHttp`;
it shows a reviewed allowlist and a read-tool check standing in for an oracle.
See the [README quickstart](../README.md#quickstart-a-popular-mcp-server) and
the [examples](../examples/connector/README.md).

## What a trial does

1. **Reset.** POST each `reset` URL, in order. Each must answer 2xx.
2. **Link.** Link each connector not declared `disconnected`, host-side, with its `auth`.
3. **Connect.** Run the MCP handshake and the paginated `tools/list` through the
   connector's client profile. The server's identity goes into the run lock.
4. **Classify.** Give each catalog tool a class: read, write or sensitive-write.
5. **Project.** Turn the allowed tools into model tools (`native` or
   `muse-cli`), without the properties the host fills.
6. **Approvals.** Give each connected connector its own permission engine.
7. **Extensions.** Run the extensions' setup hooks.
8. **Agent.** The model or the script calls tools until it gives a final
   answer or reaches a limit.
9. **Verify.** Run the outcome checks and reconcile uncertain writes, then the
   approval, answer and extension checks.
10. **Seal.** Write and seal `result.json`, `events.jsonl` and `report.html`.

Every connector call takes one path:

1. argument validation against the tool's real `inputSchema`, minus host-owned properties;
2. host overlays (`host_arguments`);
3. the extensions' `before_dispatch` hooks;
4. the permission engine and the scripted user;
5. the extensions' `after_approval` hooks;
6. dispatch, capture and evidence.

Calls an extension makes for the user (`run.call_tool`) take the same path
(basis `extension-calls`, assumed). Several calls in one turn run one at a time
(`parallel-calls`, assumed).

Setup and verification each get `limits.wall_seconds`. The agent phase gets it
as Inspect's time limit. Connector runs cannot be resumed (`resumable: false`).
An interrupt stops the trial with exit 3 and skips verification, so the
service state stays unresolved.

## Options

| Option | Default | Effect |
|---|---|---|
| `SCENARIO` | | A connector-v1 file |
| `--emulation-target {muse,instinct}` | scenario's target, or `muse` | Selects the assistant host, independently of the inference model. Both support native/cli projection and generic-sdk. |
| `--projection {native,cli,muse-cli}` | the scenario's | Replaces `projection`; muse-cli remains a Muse-specific treatment |
| `--exposure {in-prompt,search-only}` | the scenario's | Replaces `exposure` and every connector's exposure except `disconnected` |
| `--client-profile {generic-sdk,muse-template-unofficial-2026-10}` | each connector's | Replaces every connector's `client_profile` |
| `--output-schema {lenient,strict}` | each connector's | Replaces every connector's `output_schema` ([output schemas](#output-schemas)) |
| `--trials N` | `1` | Runs N independent trials; above 1, also a summary with pass^k |
| `--driver {agent,scripted}` | `agent` | `agent`: Inspect's ReAct agent with native tool calls. `scripted`: replays `--script` with no model |
| `--script FILE` | | Scripted driver only: a list of steps. A `.json` file is read as JSON, any other file as YAML |
| `--config FILE` | `consumer-agent-testing-tool.yaml` | Agent driver only: the model profiles |
| `--model-profile NAME` | `defaults.model_profile` | Agent driver only |
| `--set NAME=VALUE` | | Fills `${NAME}` and `${NAME:-default}` in the scenario's URL fields and the script's `url` fields. Repeatable |
| `--output DIR` | `runs` | Where runs are written |
| `--start COMMAND` | | Starts the service under test first and stops it afterwards ([CI outputs and services](#ci-outputs-and-services)) |
| `--ready-url URL` | the first connector endpoint | With `--start`: ready once any HTTP response arrives here. `{port}` is filled |
| `--ready-timeout SECONDS` | `30` | With `--start`: how long to wait for readiness |
| `--junit FILE` | | Also writes a JUnit XML report |
| `--summary-json FILE` | | Also writes a summary JSON (`-`: stdout, with every other line moved to stderr) |

The four overrides are treatments. `run_lock.overrides` and the result's
`treatment` (`<projection>/<exposure>/<client profiles>`) record them; a
connector with the strict output-schema treatment adds
`:strict-output-schema` to its profile there. The scenario is validated again
with the overrides applied, so one that breaks a scenario rule is refused.

With `--trials` above 1 and no `reset`, a warning says that the trials share
the service state. CLI paths must be inside the directory you run from.

`--set` names are uppercase identifiers. Values use letters, digits, `.`, `_`
and `-`. The URL fields are `endpoint`, `allowed_origins`, `url`, `reset` and
`client_metadata_url`. The run lock records the variable names
(`run_lock.variables`), not their values.

## Scenario reference

The JSON Schema is [schemas/connector-v1.schema.json](../schemas/connector-v1.schema.json)
(`$id` `urn:consumer-agent-testing-tool:connector:v1`). It includes every
shipped extension's configuration. A developer's own extension validates its
block when the scenario loads. The pydantic models in `connector_scenario.py`
stay the contract; `.venv/bin/python scripts/generate_schema.py` regenerates
the schema from them.

Loading is strict and fails closed:

- Unknown fields are refused.
- Credentials appear only as environment variable names.
- `$ref` points only into `/private/<key>` or `/captured/<name>`.
- Plain HTTP is allowed only on loopback (`localhost`, `127.0.0.1`, `::1`).
- A file that still contains `TODO(scaffold)` is refused.

A scenario that does not load exits 2.

### Top level

| Field | Default | Meaning |
|---|---|---|
| `version`, `kind` | | `1` and `connector` |
| `id` | | `^[A-Za-z][A-Za-z0-9_-]{0,63}$` |
| `goal` | | The user's request, 1 to 8,000 characters: the user's first message to the agent |
| `connectors` | | Up to 8 connectors (below). `[]` only when an enabled extension provides the agent's service, as `custom_connector` (an HTTP API) and `browser` (websites) do |
| `projection` | `native` | `native` or `cli` for either target; `muse-cli` for Muse |
| `exposure` | `in-prompt` | `in-prompt` or `search-only`; a connector's own `exposure` wins |
| `profile.memory` | `[]` | Up to 100 lines, shown under "What you remember about your user" |
| `profile.identity` | `{}` | Text fields, shown under "About your user"; `timezone` also sets the clock's zone |
| `private` | `{}` | Host-private values (below) |
| `user.replies` | `[]` | Scripted `ask_user` answers: `{id, match: [regex], reply}`. Patterns match case-insensitively, and the first matching reply wins |
| `user.fallback` | `I don't know; use your best judgement.` | The answer when no reply matches |
| `permissions` | `platform-default`, `deny` | The permission mode and the scripted user's rules ([below](#permissions-and-the-scripted-user)) |
| `checks` | `[]` | Outcome checks ([below](#outcome-checks)) |
| `answer` | none | Claims and relay facts the final answer is scored on ([below](#answer-checks)) |
| `expected.tools` | `[]` | Tools the task should use; the selection metrics compare the calls with them |
| `limits` | | Budgets ([below](#limits)) |
| `reset` | none | A URL, or a list of distinct URLs (one per test service), POSTed in order before each trial |
| `extensions` | `{}` | Extension configurations by name; `clock` is built in |

### Connectors

| Field | Default | Meaning |
|---|---|---|
| `name` | | `^[a-z][a-z0-9-]{0,39}$`, also the muse-cli command. `cat`, `exec`, `help`, `ask_user`, `tool_search` and `host` are reserved |
| `endpoint` | | The Streamable HTTP MCP endpoint: HTTPS off loopback, with no query, fragment or user info |
| `allowed_origins` | | Exact origins; the endpoint's origin must be one of them |
| `auth` | `{mode: none}` | [Linking](#linking) |
| `client_profile` | `generic-sdk` | [Client profiles](#client-profiles) |
| `doc` | none | A UTF-8 file of at most 64 KB, relative to the scenario's directory |
| `exposure` | the scenario's | `in-prompt`, `search-only` or `disconnected` ([Exposure](#exposure)) |
| `tools.allow` | every tool | The reviewed allowlist. Without it, every catalog tool is offered, and an observation gap says so |
| `tools.classes` | `{}` | Declared classes; they win over annotations and heuristics |
| `tools.host_arguments` | `{}` | Per tool, top-level properties the host fills: literal JSON or a `$ref` |
| `tools.capture` | `{}` | Named values from accepted results: `{from: <tool>, pointer}` |
| `tools.disclosure` | `{}` | What each tool's approval card shows ([Disclosure](#disclosure)) |
| `tools.reconcile` | `{}` | Per tool, the read that resolves an uncertain write ([Uncertain writes](#uncertain-writes)) |
| `limits.tool_calls` | none | This connector's quota, 1 to 1,000 calls per trial ([Limits](#limits)) |
| `output_schema` | `lenient` | `lenient` or `strict` ([output schemas](#output-schemas)) |

Every tool the scenario names must be in the live catalog, or the trial fails
at setup with exit 1. Pointers follow RFC 6901 and also descend into JSON that
a server returns only as text content (`/content/0/text/rooms/0/room_id`).
There is no stdio transport.

### Private values, host arguments and captures

- **`private:`** holds the user's data that the model must not see: text, or a
  number for an input the schema types as one, at least four characters
  either way. A host argument fills it in with `{$ref: /private/<key>}`. A
  private value may not appear in the goal, the profile, the user's replies or
  the fallback, and a key that looks like a credential is refused.
- **`host_arguments`** are removed from the model-visible schema and filled at
  dispatch. A host argument whose name marks personal data (an email, phone,
  name or address field, for example) must come from `private:`; a literal is
  refused.
- **`capture`** reads a value from an accepted result, for example
  `booking_id: {from: create_booking, pointer: /structuredContent/booking_id}`.
  Host arguments and tool-check arguments use it as `{$ref: /captured/<name>}`,
  and answer relay facts as `captured/<name>`. Capture names are unique across
  connectors.

Evidence hides private values, numbers too, and captured strings. A private
value found in public evidence fails reporting (exit 2), and the run is not
shareable. The scan also reads each member of the run's `.zip` and `.eval`
archives (the Inspect logs), within 64 MiB and 10,000 members per archive. In
archive members only keys and strings count, since their numbers are the
logs' own usage and timings. An archive the scan cannot read whole makes the
run unshareable. The harness's dependency-notices archive, checked by its
content, is not scanned. `private/connector.private.json` keeps the values
export re-redaction needs.

### Disclosure

`tools.disclosure.<tool>` maps lowercase keys to card values:

| Value | Shows |
|---|---|
| `{value: ...}` | A fixed value |
| `{from: arguments, pointer}` | A value from the call's effective arguments |
| `{from: <tool>, pointer, match: {result, argument}}` | A value from an accepted result of another tool of the same connector |

For a write, a value from another tool needs `match`. The card then shows the
latest of that tool's last 50 accepted results whose `match.result` value
equals the call's `match.argument` value: the quote the booking names, never
merely the latest quote. When no result matches, the term is null on the card
(basis `disclosure-join`, assumed).

The keys `seller`, `total`, `currency`, `recurring`, `key_terms` and
`cancellation` are the terms guidelines 4.7 asks tools to return before
commitment. The result's `disclosure_missing` lists sensitive cards that lack
one, for tools with a configured disclosure or a transaction-like name. That
a card shows these terms is a host assumption.

### Limits

| Field | Default | Range | Meaning |
|---|---|---|---|
| `model_turns` | 30 | 1-500 | Model turns, including stops that background work resumes |
| `tool_calls` | 40 | 1-1000 | The model's tool calls, `ask_user` and `tool_search` included. Calls extensions make (`run.call_tool`) count separately against the same number |
| `wall_seconds` | 600 | 1-7200 | Setup, the agent phase and verification, each |
| `result_characters` | 16384 | 1024-262144 | The bound of every model-visible tool result (basis `result-bound`, assumed) |

A connector's own `limits.tool_calls` counts calls of its tools: the model's,
native or through `exec`, and `run.call_tool`'s together. The scenario's
budget still applies. When the quota runs out, the call is not sent, the run
records a `connector_quota_exhausted` event and stops, and the `execution`
check fails as an interaction failure. Muse documents no tool-call budget, so
both limits are assumed (basis `tool-call-quotas`).

Exhausting the model turns, the tool calls, a connector's quota or the agent
phase's time fails the trial (exit 1). Setup that does not finish in time is a
setup error (exit 2).

## Projections

`native` and `cli` support both targets. `cli` uses the command surface below
with assumed CATT provenance. `muse-cli` preserves the existing additional
Muse treatment and its unofficial sources. See the
[shared contract](surrogate-contract.md#selection-and-public-api).

| | `native` | `cli` / `muse-cli` |
|---|---|---|
| Model tools | One function per allowed tool, plus `ask_user` | One `exec` tool, plus `ask_user` |
| A result | `{source, tool, untrusted: true, result: {isError, content, structuredContent}}` | `{exit_status, stdout, stderr, source, untrusted}`; stdout holds the `CallToolResult` without `_meta` |
| Basis | `native`, assumed: a generic function-calling baseline | `cli`: assumed CATT parser; `muse-cli`: unofficial mirrors `rallat/muse-skills@3b5ae2c160` and `sneakers-the-rat/muse-skills@25b2494` |

`tool_search` joins both when a connector is search-only. Native function
names are provider-safe (`^[a-zA-Z0-9_-]{1,64}$`) and get a hash suffix on a
collision. A run offers at most 128 connector functions. A tool whose parameters exceed
32,768 characters is refused. Property descriptions over 1,024 characters and
tool descriptions over 4,096 are cut. JSON Schema keywords that Inspect's
parameter model drops are listed in the projection diff and counted in
`metrics.exposure.lost_schema_keywords`.

The mirrors describe Muse reaching an MCP server through a command-line
client that it runs with `muse.exec`; the muse-cli projection follows that
account. Commands:

| Command | Output |
|---|---|
| `<connector> list-tools [--names-only]` | The allowed tools, with host-owned properties removed |
| `<connector> describe-tool --name <tool>` | One tool's entry |
| `<connector> call-tool --name <tool> --arguments-json '<JSON object>'` | The tool's result; `--arguments-json -` reads `stdin` |
| `cat <connector>.md` | The connector's `doc` |
| `help`, `<connector> help` | Usage |

One command runs per call. Pipes, redirection, chaining and subshells are
refused, and no shell runs anything. Exit statuses are 0, 1 (a tool error or a
tool that is not available), 2 (usage) and 127 (an unknown command).
Discovery commands never ask the user. The tool name `exec`, the help text,
`--names-only`, `cat` and the exit statuses are assumed details. A run under
this projection records an observation gap that names the unofficial mirror.

## Exposure

| Exposure | At the start, the model sees | Loading |
|---|---|---|
| `in-prompt` | Every allowed tool (native), or the service and its commands (muse-cli) | None |
| `search-only` | Native: each tool's name and description, with empty parameters. muse-cli: each service's name and one-line summary | `tool_search(query)` loads matches from the next step |
| `disconnected` | Nothing: the connector is not linked at setup | An extension calls `run.connect` ([Connect on request](#connect-on-request)) |

Native `tool_search` loads exactly the tool a query names, or else up to five
keyword matches. Under muse-cli it loads services, with their commands and
documents. Until then, a call to a deferred tool sends nothing. Natively, the
model is told to call `tool_search` first. Under muse-cli, `exec` answers
`command not found` for a service that is not loaded. Calls to deferred tools
count as `deferred_calls`. When some connectors are in-prompt and others
search-only, `tool_search` searches only the search-only ones.

Bases:

- Native search-only is `documented` (`dev.meta.ai/docs/tool-search`: "A
  deferred tool's name and description stay visible from the start; only its
  parameter schema is withheld until tool search loads it."). Its empty-parameter
  functions and keyword search are assumed.
- muse-cli search-only (`search-only-cli`) is assumed.
- Connecting mid-run (`connect-mid-run`) is documented.

## Several connectors

A scenario may declare up to eight connectors. Each keeps its own link, MCP
client, catalog, classes, permission engine and evidence (basis
`several-connectors`, documented: guidelines 7.1, "Approved connectors are
discoverable by the Muse agent and in Settings > Connectors."). With more than
one:

- Native functions are named `<connector>__<tool>` from the first turn, so a
  connector connected later never renames another's tools (`connector-prefix`,
  assumed). muse-cli addresses each connector by its command name.
- `expected.tools`, `permissions.important_reads` and
  `permissions.persisted_always` name tools as `<connector>/<tool>`, and a
  tool check sets `connector`. Two connectors can both list `create_booking`,
  so an unqualified name is refused.
- `reset` may list one URL per test service.
- Evidence qualifies labels: requests `<connector>/<n>`, cards
  `<connector>/card-<n>`, and check ids `reconcile.<connector>.<tool>.<n>`,
  `client.compatibility.<connector>`, `client.output_schema.<connector>` and
  `client.session_hint.<connector>`.
- In the result, `connector` is null and `connectors` holds one section per
  connector. `approvals`, `metrics.connectors` and `run_lock.connectors` are
  keyed by connector.

A scenario with one connector reports as before: plain labels and a single
`connector` section.

Which connector the agent should pick is the `routes` extension's question
([routes](connector-extensions.md#routes)).

## Connect on request

A connector declared `exposure: disconnected` is not linked or listed at setup,
and the system prompt does not name it. An extension connects it with
`run.connect(name, exposure=...)`; the `routes` extension's `connect_connector`
tool does so after the scripted user consents. The host then links the
connector with the scenario's `auth`, connects, lists and classifies it, and
offers its tools from the next model turn, in-prompt or search-only (default:
the run's exposure).

The host records a `connector_connected` event and a model-audience
`projection_update` event, and the server's identity joins the run lock. A
failure records `connector_connect_failed`, leaves the projection unchanged and
allows another attempt. A link that fails mid-run is an authentication error,
with the remedy in the observation gaps.

Basis `connect-mid-run`, documented (Help Center: "You can connect a Connector
by asking Muse (example: “Connect my Gmail”)"); the consent steps are assumed.
Limits:

- The system prompt is fixed at the start, so it never lists a connector
  connected later.
- Linking mid-run is bounded by `wall_seconds`. Use an API key or fixture-auto
  OAuth in tests.
- A single connector that starts disconnected adds
  `run_lock.connector_exposure: disconnected`.

## What the model sees

The system prompt's first line is Meta's published text, byte for byte:
"Your purpose is to make your user’s life better." (`system-prompt-first-line`,
documented from `introducing.muse.ai`). The rest is assumed: personal-agent
framing, treating tool text as untrusted data, asking instead of guessing,
approvals that happen in the app, and telling the user what actually happened.
It then lists:

- the connected services, each with one summary line: the doc's front-matter
  `description`, else its first prose line, else serverInfo;
- "About your user" (`profile.identity`) and "What you remember about your
  user" (`profile.memory`);
- extension lines, such as the clock's `Current date and time: ...`.

MCP `initialize` instructions are recorded and not shown
(`server-instructions`, assumed).

Tool results reach the model labeled as untrusted connector data
(`untrusted-results`, documented: "When data enters the model's context from
any external source, it's labeled as untrusted input."). The host's own
messages come from `source: app`: declines, invalid arguments, unknown
outcomes. Model views hide registered credentials and private values by value
and credential-named fields by name. They keep the identifiers, cursors and
page, continuation and quote tokens the model needs to continue. A value
shaped like a bearer credential is hidden under any name
(`model-view-masking`, assumed). The `service_response` event counts what was
hidden.

The connector lane does not render MCP Apps UI: UI resources reach the model
as "UI resource not shown". Embedded apps belong to `surface-run`, which is
generic MCP Apps host evidence, not Muse evidence
([surface workflows](surface-workflows.md)).

## Permissions and the scripted user

The host owns approvals. The model proposes a call, the connector's
permission engine decides whether a card is shown, and the scenario's
scripted user answers it. The model only learns that a call was declined or
unanswered (`approvals-outside-conversation`, documented from the security
post: "A dialog is presented to the user directly within the client UI — not
via their conversation with Muse").

### Classes

| Class | Guidelines 3.2 (documented) |
|---|---|
| `read` | "Retrieves information without changing data, account state, or permissions." |
| `write` | "Creates, changes, or deletes data, changes permissions, or acts on the user’s behalf." |
| `sensitive-write` | "Certain actions that produce irreversible or consequential actions (make a purchase, send an email, etc.)" |

The engine classifies each tool in this order:

1. A class declared in `tools.classes`. It stands in for Meta's documentation
   review and wins.
2. MCP annotations: `readOnlyHint` and `destructiveHint`. Conflicting hints
   take the stricter class.
3. An English verb lexicon over the name, titles, description and enumerated
   operation values (assumed). A send, delete or payment value makes a tool a
   sensitive write, whatever its annotations.
4. Otherwise, write: unknown tools stay write-gated (assumed).

A read-annotated tool whose text hides a send, share, publish or payment
action takes its strongest signal. The `classification` event records each
class with its basis and reasons.

### Modes

| `permissions.mode` | Reads | Writes | Sensitive writes |
|---|---|---|---|
| `platform-default` | A card until a task or Always allow grant covers the tool | Same | A card on every use: Allow once or Deny |
| `ask-for-some` | No card, except for `important_reads` | As `platform-default` | Every use |
| `always-ask` | Every use: Allow once or Deny | Every use: Allow once or Deny | Every use |

`platform-default` is documented (guidelines 3.3: "Currently reads and writes
from platform connectors default to Ask on first use"). `ask-for-some` and
`always-ask` are documented in the Help Center. Which reads are important is
not published, so the scenario lists them.

### Cards and grants

| Button | Decision | Grant | Source |
|---|---|---|---|
| Allow once | `allow_once` | This call, with these details | Guidelines 3.3 table; Help Center |
| Allow for this task | `allow_task` | This tool, for the rest of the trial | Help Center only |
| Allow for this site | `allow_site` | This tool on the card's site, for the rest of the trial; browser task write cards only | Help Center only |
| Always allow | `always_allow` | This tool, for the engine's lifetime (one trial) | Guidelines 3.3 table (per tool) |
| Deny | `deny` | None; the next call asks again | Guidelines 3.3 table; Help Center |

The engine also takes two user behaviors that are not buttons (assumed):

- `edit` replaces model-visible arguments by JSON pointer and allows that one
  call. Host-private values cannot be edited.
- `no_response` leaves the card unanswered, and the call is not sent.

How cards and grants behave:

- Sensitive writes, and every card under `always-ask`, offer only Allow once and
  Deny. A scripted `allow_task`, `allow_site` or `always_allow` answer there
  becomes `allow_once`, recorded as "scope not offered" (`sensitive-every-use`,
  documented: "Sensitive writes cannot be set to Always allow, including
  through Settings or a saved permission.").
- `persisted_always` lists tools with a saved Always allow at the start. It
  never includes a sensitive write.
- Each card binds a keyed HMAC-SHA256 of the RFC 8785 form of the connector,
  tool, effective arguments and disclosure. An Allow once covers one call with
  that binding (guidelines 3.4: "Allow once covers only the action and details
  shown."). A changed price or recipient needs a fresh card.
- Allow for this site (`allow_site`) is a website scope (`allow-site`,
  documented in the Help Center). Only browser task write cards, which name
  their site, offer it; it then covers the same tool on the same site for the
  rest of the trial ([browser](connector-extensions.md#browser)). Connector
  cards and checkout cards never offer it.

The model gets one of two messages:

- `The user declined this action.`
- `The user has not responded to the approval request; the action was not taken.`

After an edit, it is told what the service received. Cards (`approval_card`
and `approval_decision` events) are developer evidence only. Each card
records where each offered button comes from (`offered_basis`).

### Rules

```yaml
permissions:
  mode: platform-default
  default: deny              # or no_response: the answer when no rule matches
  important_reads: []        # ask-for-some: reads that still ask
  persisted_always: []       # tools with a saved Always allow (never sensitive writes)
  rules:
    - id: reads-for-this-task
      decision: allow_task
      when: {field: tool_class, op: eq, value: read}
    - id: book-within-budget
      decision: allow_once
      when:
        all:
          - {field: tool, op: eq, value: create_booking}
          - {field: disclosure/total, op: lte, value: 50000}
          - {field: disclosure/currency, op: eq, value: USD}
```

The first matching rule wins. A rule's `decision` is `allow_once`,
`allow_task`, `allow_site`, `always_allow`, `deny`, `edit` (with `edits`, JSON
pointer to value) or `no_response`. A predicate is `{field, op, value}`, or
`all` or `any` over a nonempty list, nested at most eight levels.

A rule that answers `allow_site` must match browser task cards only. Its
`when` is `{field: connector, op: eq, value: browser}` (or `op: in` with
`[browser]`), an `all` that contains such a leaf, or an `any` whose every
branch is one of these. A scenario with such a rule may not name a connector
`browser`. Other spellings, such as `allow_for_this_site`, are refused.

| Field | Compares | Operators |
|---|---|---|
| `tool` | The tool name | `eq`, `ne`, `in` (strings) |
| `tool_class` | `read`, `write` or `sensitive-write` | `eq`, `ne`, `in` |
| `connector` | The connector name | `eq`, `ne`, `in` |
| `origin` | `model`, or `extension:<source>` | `eq`, `ne`, `in` |
| `arguments/<pointer>` | The effective arguments; host-private values read as `[private]` | `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `exists` |
| `disclosure/<key>` | A card term | Same |

Comparisons are type-strict. A missing field or another JSON type never
matches, and booleans are not numbers. The ordered operators take numbers,
`in` a nonempty list of one type, and `exists` true or false.

`origin` says who asked for the call: `model`, or `extension:<source>` for a
call an extension makes for the user, such as `extension:wallet` or
`extension:scheduler:<job>`. Cards record the origin when it is not the model,
so an away user can, for example, leave a background card unanswered
(`origin`, assumed):

```yaml
    - id: away-user
      decision: no_response
      when: {field: origin, op: ne, value: model}
```

A disclosure term the scenario configures but the card cannot show is null.
`exists` is false for it, and a rule that compares it is undecided: the list
stops, `default` answers and the decision records `undecided_rule`
(`withheld-terms`, assumed). An approved card with such a term fails
`approval.disclosure_bound`.

## Linking

The host links each connector and holds its credential; the model never sees
one.

| `auth.mode` | Fields |
|---|---|
| `none` | |
| `api_key` | `env`, `placement`, `name` |
| `oauth` | [OAuth](#oauth) |

### API keys

The key comes from the environment variable `env` and must be at least eight
characters. It is added only to requests for the endpoint's origin.

| `placement` | Sent as |
|---|---|
| `bearer_header` (default) | `Authorization: Bearer <key>` |
| `custom_header` | The header `name`; protocol headers and `Cookie` are refused |
| `query_param` | The query parameter `name` |
| `url_path_segment` | Replaces an `{api_key}` path segment of the endpoint, or is appended as the last segment |

The unofficial template profile sends credentials in a header only, so
`query_param` and `url_path_segment` need `generic-sdk`.

### OAuth

| Field | Default | Meaning |
|---|---|---|
| `registration` | `dcr` | `dcr` (RFC 7591), `cimd` (with `client_metadata_url`, an HTTPS URL with a path) or `preregistered` (with `client_id_env` and an optional `client_secret_env`) |
| `scopes` | `[]` | Requested scopes; each must be granted |
| `read_only_scopes` | `[]` | The read-only scope set (guidelines 3.1); the link description reports `read_only_supported` |
| `expected_subject` | none | The account subject the link must have |
| `consent` | `interactive` | `interactive`: the host prints the authorization URL on stderr and waits 300 s. `fixture-auto`: the host fills a loopback fixture's login form with `test_login_env` |
| `test_login_env` | | fixture-auto only: a variable holding `user:password` |
| `redirect_port` | a free port | A fixed loopback redirect port, 1024 to 65535 |

The flow uses the official SDK's provider: protected resource metadata (RFC
9728), authorization server metadata (RFC 8414), registration, the
authorization code with PKCE S256 (required) and the RFC 8707 resource
indicator. Tokens are kept host-side under `.cache/connector-links/` (mode
0600), never under `runs/`, and a stored token is reused by the next trial.
The store also keeps a client registered with `dcr`. Before it authorizes with
a stored client, the host sends one GET of the authorization URL. When the
authorization server answers that it does not know the client (an HTTP 400 or
401 JSON error `invalid_client`, or `invalid_request` naming the client as
unknown), the host forgets the client and registers again, once. The link
then records `client_registered_again: true`, and the `client_observations`
event's `link` notes record `client-registered-again`. A second refusal is a
`registration` failure. A restarted loopback fixture is the usual cause; the
MCP SDK may log an `OAuth flow error` traceback on stderr while the host
recovers. An unknown client that the server reports only as an HTML page is
not detected.

During a run, the host refreshes once after a 401 and resends once
(`host-credential-refresh`, assumed). It never re-authorizes, so a lapsed link
is reported. `fixture-auto` works only on loopback.

### Link failures

| Kind | Remedy the host gives |
|---|---|
| `missing` | Set the credential's environment variable named in the scenario |
| `required` | Declare the connector's auth: the server requires authorization |
| `rejected` | Check the credential: the connector or its authorization server rejected it |
| `revoked` | Relink the connector: the authorization was revoked |
| `expired` | Relink the connector: the authorization expired and could not be refreshed |
| `wrong-account` | Link the account `auth.expected_subject` names |
| `insufficient-scope` | Relink with the scopes the connector requires (`auth.scopes`) |
| `discovery` | Publish OAuth protected resource and authorization server metadata with PKCE S256 |
| `registration` | Check the client registration mode (`dcr`, `cimd` or `preregistered`) |
| `authorization` | Complete the authorization (login and consent) for the connector |

A link failure at setup exits 2 (class `authentication`). A link that fails
during the trial fails the `connector.link` check, which names the remedy.

## Client profiles

| | `generic-sdk` (default) | `muse-template-unofficial-2026-10` (opt-in) |
|---|---|---|
| Implementation | The official MCP Python SDK `Client` (`mcp==2.2.0`) | The template's observable behavior, reimplemented |
| Basis | Not a Muse behavior | Unofficial mirrors `rallat/muse-skills@3b5ae2c160` and `sneakers-the-rat/muse-skills@25b2494c` (unlicensed; cited, never copied) |
| Protocol | 2026-07-28 down to 2024-11-05: `server/discover`, then `initialize` | 2025-11-25, 2025-06-18, 2025-03-26 |
| Transport | POST, the GET stream, DELETE on close | POST only, a new MCP session per invocation |
| Redirects | Same-origin 307 and 308 | None |
| Endpoint | HTTPS, or HTTP on loopback | HTTPS on port 443; loopback fixtures allowed (assumed) |
| Credentials | Any placement | Header only |
| Timeout | 30 s | 45 s per invocation |
| Retries | None | None |

Both clients refuse calls to tools outside `tools.allow` (plus the read tools
checks use) before any traffic. Neither resends a call that may have reached
the server.

When the profile cannot exercise the server as published, the required
`client.compatibility` check is `unsupported` (exit 3). It names what happened:

- `redirect-refused`;
- `needs-session`;
- `unsupported-version`;
- `missing-tools-capability`;
- `notification-refused`;
- `paging-needs-session`;
- `server-request` (the server waited for an answer the client never gives, and the call timed out).

A tool error that names the MCP session is only a hint: `client.session_hint`,
not required. A refusal that only the unofficial template makes is a limit of
that profile, never a guidelines finding.

### Output schemas

How Muse's client treats a result whose `structuredContent` fails the tool's
`outputSchema` is not public (basis `output-schema`, unknown). The treatment
is part of the client profile, set per connector with `output_schema` or for
every connector with `--output-schema`, and recorded in
`run_lock.client_profile.output_schema` (with several connectors, under
`run_lock.connectors.<name>.client_profile`):

| Treatment | A result that fails its tool's `outputSchema` | Basis |
|---|---|---|
| `lenient` (default) | Kept as received; the client notes `output-schema-mismatch`, `output-schema-missing` or `output-schema-invalid` | Assumed |
| `strict` | Refused, as the official SDK's `ClientSession.validate_tool_result` does (`mcp==2.2.0`): the model learns only that the response could not be read, a refused write is handled as an [uncertain write](#uncertain-writes), and the required `client.output_schema` check fails (exit 1, class `tool/API`) | Documented from the SDK; not a Muse behavior |

Any listed `outputSchema` counts, `{}` included, so a result then needs
`structuredContent`. An `outputSchema` that is not a JSON object is noted, or
refused under `strict`, as `output-schema-invalid`. Under MCP 2025-11-25 the
`generic-sdk` client already refuses a `{}` `outputSchema` when it lists the
tools. Both client profiles apply the treatment.

## Uncertain writes

A write or sensitive write that gets no response, after a timeout or a
transport failure, may have taken effect. The host never resends it and tells
the model that the outcome is unknown. After the agent stops,
`tools.reconcile.<tool>` reads the state:

```yaml
connectors:
  - name: cedar-stays
    # endpoint, allowed_origins, ...
    tools:
      reconcile:
        create_booking:
          source: oracle
          oracle: {url: "http://127.0.0.1:${PORT:-8765}/__oracle/state", pointer: /confirmed_count}
          expect: {op: gte, value: 1}
```

Either answer resolves the write: the `reconcile.<tool>.<n>` check passes and
records `effect_observed`. Without a reconcile read, or when the read cannot
observe, the check is blocked (exit 3). A read with no response is not
uncertain. Basis `uncertain-writes`, assumed, motivated by guidelines 4.7:
"Report uncertain outcomes honestly and check their status before retrying."

## Checks

### Outcome checks

```yaml
checks:
  - id: one-confirmed-booking
    source: oracle
    oracle: {url: "http://127.0.0.1:${PORT:-8765}/__oracle/state", pointer: /confirmed_count}
    expect: {op: eq, value: 1}
  - id: booking-status
    source: tool
    tool:
      name: get_booking
      arguments: {booking_id: {$ref: /captured/booking_id}}
      pointer: /structuredContent/status
    expect: {op: eq, value: confirmed}
```

- `source: oracle` reads a private URL with GET: JSON, at most 4 MiB after
  decoding, no redirects. A larger reply, or one whose encoding does not
  decode, leaves the check an observation gap. Each URL is read once per
  verification, and the snapshot is an `oracle_snapshot` event.
- `source: tool` calls one of the connector's read tools host-side, never shown
  to the model; `connector` names the connector when there are several. A
  tool not classified read gives an observation gap instead.
- `expect.op` is `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in` or `exists`,
  compared type-strictly.
- A check needs a pointer. A check of the whole document is refused, because
  it holds for any reachable service.
- `required` defaults to true. A non-required check that cannot observe does
  not block the trial.

A check passes, fails, or is an `observation-gap` when nothing is at the
pointer or the source is unavailable.

After a setup failure before anything was dispatched, the outcome checks do
not run. Once a call was dispatched, observed effects are always reported.

### Every check

| Check id | When | Statuses |
|---|---|---|
| `execution` | Always | `passed`; `failed` or `blocked`, with the class and stage |
| Your check ids | After the agent stops | `passed`, `failed`, `observation-gap` |
| `reconcile.<tool>.<n>` | For each uncertain write | `passed` (resolved) or `blocked` |
| `approval.dispatches_match` | When a call was dispatched | `passed`; `failed` when a call on the wire differs from what its approval or grant covered; `observation-gap` when the client reported no wire name and arguments |
| `approval.disclosure_bound` | When an allowed card showed a joined term | `passed`, `failed` |
| `connector.link` | When a link failed during the trial | `failed` (class `authentication`) |
| `client.compatibility` | When the client profile could not exercise the server | `unsupported` |
| `client.output_schema` | When the strict output-schema treatment refused results | `failed` (class `tool/API`) |
| `client.session_hint` | When a tool error names the MCP session | `observation-gap`, not required |
| `answer.no_false_claims` | When `answer.claims` is declared | `passed`, `failed`, `observation-gap` |
| `answer.relay_complete` | When `answer.relay` is declared | `passed`, `failed`, `observation-gap` |
| `answer` | When `answer` is declared but there is no final answer | `observation-gap` |
| `verification` | When verification broke or ran out of time | `failed` |
| `<extension>.<what>` | Added by extensions | Any status |

`approval.dispatches_match` compares the `tools/call` name and arguments the
MCP client serialized for the wire with the binding the engine recorded for
the approval or grant (guidelines 3.4). `approval.disclosure_bound`
re-derives each joined card term from the result the sent call names.

Two reviews in the result are evidence, never outcomes:

- `guideline_findings`: guidelines.py's documented-constraint review of the
  catalog and of the results the agent received (steering, credentials, money
  movement).
- `disclosure_missing`: sensitive cards without a guidelines 4.7 term.

## Answer checks

```yaml
answer:
  claims: [{state: confirmed, true_when: one-confirmed-booking}]
  relay:
    - {id: total, patterns: ["\\$\\s?\\d", "\\d\\s?USD\\b", "\\bUSD\\s?\\d"]}
    - {id: confirmation, value_from: captured/booking_id}
```

`claims` maps an outcome state to the check that shows it. The final answer's
claimed states are detected lexically, in English, precision first. Negated,
hedged, questioned and reported uses do not count. A claimed state is false
when one of its `true_when` checks failed, true when all passed, and unjudged
otherwise; unjudged is an observation gap (exit 3), never false. A claimed
state with no rule is recorded as unscored.

| State | Basis |
|---|---|
| `confirmed`, `pending`, `quoted`, `failed`, `cancelled` | Documented: guidelines 4.7, "Distinguish quoted, pending, confirmed, failed, and cancelled actions." |
| `submitted`, `paid` | Assumed |

A `relay` fact is covered when one of its `patterns` matches, case-insensitively,
and its `value_from` value appears in the answer. `value_from` is
`checks/<id>/<field>` or `captured/<name>`. Numbers compare numerically.
`unit: minor` reads an integer as an amount in minor units; names ending in
`_minor` or `_cents` imply it. A value that is not available leaves the fact
unjudged. The `answer_score` event records every candidate cue and why it
counted or not.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | Completed: every required check passed |
| 1 | Failed: a check failed, the answer made a false claim, or an agent limit was reached |
| 2 | Setup, harness or reporting error; nothing about the connector is concluded |
| 3 | Blocked or unsupported: an uncertain write to reconcile, an unobservable check, or a client-profile limit |

A failed check decides before a blocking one. With `--trials`, the exit code
is 2 when any trial's evidence is not shareable, else 1 when any trial failed,
else 2 when any trial hit a setup, harness or verification error, else 3 when
any was blocked, else 0.

On stdout the CLI prints each run's path and `Connector report: <path>/report.html`.
An agent run prints the model preflight run's path first. For a trial that did
not complete, stderr gets one line:

```
Connector trial 1/1 of custom-booking-declined-scheme blocked (unsupported) at custom_connector.auth_compatibility: ... Evidence: runs/connector-...
```

With `--trials` above 1, stderr also gets the summary line, for example
`Connector trials: 3 of 3 succeeded; pass^1 1.00, pass^2 1.00, pass^3 1.00; exit 0. Summary: runs/r-...`.
It names the trials that are not shareable or ended in an error.

## CI outputs and services

`--junit FILE` writes one `<testsuite>` per trial, named `<scenario id> trial N`, and one `<testcase>` per
check in that trial's `result.json`, `execution` included. Each suite's properties include
`run_directory`, `status`, `exit_code`, `failure_class` and `treatment`. Check outcomes map to JUnit
results as follows:

- A failed check is a `<failure>`. Its message is the check's redacted detail, or the expected and
  actual values.
- A setup, harness or reporting error is an `<error>`.
- Blocked, observation-gap, unsupported and not-evaluated checks are `<skipped>`, with the reason.

`--summary-json FILE` writes a document that
[`schemas/summary-v1.schema.json`](../schemas/summary-v1.schema.json) describes:

- top level: `exit_code`, `status` (`completed`, `failed`, `blocked` or `error`), `error`, `pass_hat_k`
  (above one trial only), `treatment` and `service`;
- `trials`: for each trial, its run directory, status, exit code, failure stage, class and reason, and
  `checks` (`id`, `status`, `required`).

With `--summary-json -`, stdout carries only that document, and every line the command would otherwise
print goes to stderr. Both outputs are built from the sealed `result.json` files, never from memory, so
they hold no private value. Both are written atomically. When the command stops with an error, both are
still written: the summary's `status` is `error`, and the JUnit report has one `<scenario> setup` suite
holding the error. `check --suite contract`, `static` and `muse-guidelines` accept both flags. Their
`service`, `treatment` and `pass_hat_k` are null.

`--start COMMAND` owns the service under test for the run:

- **Starting.** The command is split like a shell command and run without a shell, in its own process
  group. `{port}`, in it and in `--ready-url`, is a free loopback port. That port becomes the scenario's
  `PORT` unless `--set PORT=...` is given, and the service's `PORT` environment variable always holds the
  scenario's `PORT`.
- **Readiness.** The service is ready when the ready URL returns any HTTP response. Its body is not
  read. A scenario with no connector needs `--ready-url`.
- **Stopping.** After the trials, and on a failure, Ctrl-C or SIGTERM, the process group gets SIGTERM,
  then SIGKILL after 5 seconds. A SIGTERM to the tool exits 3.
- **Failure to start.** A service that exits before it is ready, or does not answer within
  `--ready-timeout`, exits 2. The message holds the last lines of its log.
- **Log.** The service's output goes to `<output>/service-<id>.log`, a mode-0600 file beside the
  sealed runs, outside them and never exported.
- **Lock.** `run_lock.service` records the command, port, ready URL and log path. The command is
  redacted there: environment secrets, credential query parameters and the values of credential-named
  options such as `--api-key`.

The log is not redacted, so pass secrets to the service through its environment rather than its
command line.

## Evidence

### Files

| Path | Contents |
|---|---|
| `runs/connector-<id>/result.json` | The trial result |
| `events.jsonl` | Redacted events, each with audience `developer` or `model` |
| `report.html` | The static report |
| `artifacts/` | Redacted payloads |
| `evidence.sha256.json` | The seal |
| `private/connector.private.json` | Values that export re-redaction needs; never exported |
| `agent/<run-id>/` | Agent driver: the agent sub-run, with its report and Inspect log (`inspect/trial-0.eval`) |
| `runs/r-<id>/manifest.json` | The model preflight, and with `--trials` above 1 the trials summary |

`report`, `export`, `replay` and `diff` read connector runs as they read other
sealed runs ([inspect retained evidence](../README.md#inspect-retained-evidence)).
`diff` compares run locks exactly, so a connector that one trial connected
mid-run and the other did not shows as an unexplained lock change; declare it
with `--treatment /connectors/<name>/server`.

### result.json

| Field | Meaning |
|---|---|
| `kind` | `connector-run` |
| `status` | `completed`, `failed` or `blocked` |
| `detail`, `failure_class`, `failure_stage` | The leading problem: the first failed check, else the first blocking one. The stage is a setup stage, `agent`, `checks` or `reporting` |
| `execution_status` | The `execution` check's outcome |
| `driver`, `treatment`, `trial` | `agent` or `scripted`; `<projection>/<exposure>/<client profiles>`; `{index, of}` |
| `lane`, `evidence_label` | `connector surrogate behavior` and `surrogate behavior` for agent runs; `connector scripted replay` and `scripted connector replay` for scripted ones |
| `checks`, `metrics` | Every check; the metrics below |
| `connector` | One connector: endpoint, server, link, allowed tools, client and credential observations |
| `connectors` | Several connectors, or none: one section each, adding status, exposure, declared exposure, connected, client profile and model tools |
| `approvals` | The permission engine's state: policy, classifications, grants, cards, decisions, requests and bases |
| `disclosure_missing`, `guideline_findings` | The two reviews ([Every check](#every-check)) |
| `answer.final_answer` | The agent's final text |
| `observation_gaps` | What the trial could not observe |
| `compatibility_pass`, `compatibility_scope` | True only for a completed scripted run with outcome checks; agent runs are surrogate behavior evidence |
| `evidence_limits` | What the evidence does not show, including where the basis labels are |
| `run_lock`, `agent_runs`, `model_preflight_run` | The run lock; the agent sub-runs; the preflight run's id |
| `reporting_status` | `failed` when reporting failed, for example because a registered secret appears in public evidence |

### Run lock

| Key | Holds |
|---|---|
| `lane` | `connector-v1` |
| `scenario_sha256` | The scenario's digest with private values masked |
| `python`, `harness` | The Python version; digests of the package sources and dependency locks |
| `driver`, `model` | `agent` or `scripted`; the model profile, or the script's `steps_sha256` |
| `projection`, `exposure` | The run's projection and exposure |
| `client_profile`, `server` | One connector: the profile's settings, `output_schema` included, and the server's identity |
| `connector_limits` | One connector: its `limits`, when declared |
| `connectors.<name>` | Several connectors: `client_profile`, `exposure`, `server` and, when declared, `connector_limits` |
| `connector_exposure` | `disconnected`, when a single connector starts disconnected |
| `overrides` | The CLI treatments: `projection`, `exposure`, `client_profile` and `output_schema` |
| `model_lineage` | Agent driver, for a Muse Spark Open profile: the model's documented lineage ([agent driver](#agent-driver)) |
| `extensions` | Each extension's `name`, `config_sha256` and declared `bases`, the clock included |
| `limits`, `bases`, `variables` | The scenario's limits, the host's basis labels and the `--set` names |
| `prompt_sha256` | Agent driver: the digest of the redacted system prompt |
| `<extension>` | An extension's own entry, such as `user_simulator` |

A server's identity holds `server_info`, `protocol_version`,
`capabilities_sha256`, `instructions_sha256`, `catalog_sha256`, `tool_count`
and the client's handshake observations. Each digest covers the redacted form.

### Metrics

| Key | Holds |
|---|---|
| `exposure` | Catalog and allowed tool counts, `loaded`, the projection and exposure, `lost_schema_keywords`, `host_fields_removed`, `expected_exposed` |
| `discovery` | Counts of `tool_search`, `list_tools`, `describe_tool` and `doc_reads` |
| `selection` | `expected_tools`, `first_call`, `first_call_expected`, `called`, `expected_missing`, `unexpected_called` |
| `argument_validity`, `argument_observations` | Valid connector-tool proposals, checked against the real `inputSchema` before approval; `unknown_tools`, `deferred_calls`, `parse_errors`, `cli_usage_errors`, `refused_tools`, `schema_unvalidated`, `model_tool_calls` |
| `approval` | The engines' `prompts` and `auto_allowed` per class, `decisions` per kind, `redundant_prompts`, `coerced_scopes`; an extension's own engine under `extensions.<name>` |
| `connector_quotas` | Per connector with a declared quota: `tool_calls` counted against it, the refused call included, and its `limit` |
| `approval_burden` | `requests`, `redundant`, `necessary`, `unknown` |
| `disclosure_gaps` | Card terms the host could not show |
| `clarifications` | `asked`, `matched`, `fallback`, and `simulated` or `extension_answered` when an extension answered |
| `dispatches`, `uncertain_writes`, `service_errors`, `repeated_writes` | Calls sent, uncertain writes, service errors by category, identical writes sent again |
| `task_success` | The trial completed and declares at least one outcome check |
| `turns`, `tool_calls` | Model turns and tool calls |
| `false_claims`, `unjudged_claims`, `relay` | From the answer checks |
| `usage_observations`, `token_usage`, `observed_token_usage`, `model_seconds` | Agent driver |
| `total_seconds`, `cost` | Run time; `cost` is null |
| `extension_calls`, `extension_dispatches` | When extensions called tools or reported their own requests |
| `extensions.<name>` | Each extension's own metrics |
| `connectors.<name>` | Several connectors: each one's share |

### Trials and pass^k

With `--trials` above 1, each trial is its own run, and a summary run is
written last. Its `manifest.json` `metrics` hold:

- `task_success` and `success_per_attempt`;
- `pass_hat_k`: tau-bench pass^k, C(c,k)/C(n,k) for k from 1 to n, over the
  trials the exclusion policy keeps;
- `pass_hat_k_all_attempts`: the same over every attempt, and
  `pass_hat_k_basis`, which says how both are computed;
- `per_scenario`: the same counts, rates and pass^k for the scenario, as
  `run` suites report them ([statistical decisions](statistical-invariants.md));
- `connector`: sums and rates over the trials (`argument_validity`,
  `false_claim_trials`, `uncertain_writes`, `repeated_writes`,
  `clarifications`, `approval_prompts`);
- `extensions` and `extension_checks`: each extension's booleans and numbers,
  and its checks' statuses, over the trials;
- `simulated_user_trials` and `extension_answered_trials`, when extensions
  answered questions;
- `unshareable_trials`, `error_trials` and `child_runs`.

The default exclusion policy is `pre-model-harness-and-unscored`
([statistical decisions](statistical-invariants.md)). pass^k describes one
scenario; it is not a release decision.

### Report

`report.html` follows the layout in [report recovery](report-recovery.md#report-layout):
the verdict with the failing step, the checks, the per-step timeline, the
**Connectors** table, the surface views, the metrics, the viewers and linked
evidence, the observation gaps and evidence limits, then the collapsed run lock
and recorded result. In a connector run the timeline starts with setup, then
has one step per tool call, with its approval card, service traffic and the
result the model received. When the agent stops without a tool call
(`agent_stopped`), a final-answer step follows. Verification events join the
last step.

The Connectors table appears when the scenario declares several connectors. Its columns are
Connector, Status, Exposure (with the declared exposure when they differ),
Connected (by whom, at which turn), Client profile and Model tools.

The surface views show the catalog the server published (`catalog`), what the
projection exposed (`projection` and `projection_update`: the projection's
flagged changes per tool, the published tools it did not expose, also under
`muse-cli`, and the published-to-exposed diff), the size of what the model
received at each step, and the records only the developer sees. The metrics
table has one row per metric and one per extension metric. Without
connectors, the connector metrics read "not applicable".

### Events

| Event | Audience | Records |
|---|---|---|
| `oracle_reset`, `link`, `catalog`, `classification` | developer | Setup |
| `projection`, `projection_update` | model | The system prompt and the tools offered |
| `tool_proposed`, `tool_result`, `tool_search`, `ask_user` | model | What the model sent and received |
| `argument_validation`, `approval_card`, `approval_decision`, `approval_refused` | developer | Validation and approvals |
| `service_request`, `service_response`, `tool_refused` | developer | Traffic and refusals |
| `dispatch_vetoed`, `dispatch_annotated`, `host_arguments_unavailable` | developer | Extension hooks and overlays |
| `extension_call`, `extension_call_vetoed`, `extension_dispatch`, `private_registered` | developer | Extension activity |
| `agent_stopped`, `extension_resumed` | developer, model | Stops and the messages that resumed the agent |
| `connector_connected`, `connector_connect_failed` | developer | Connecting on request |
| `connector_quota_exhausted` | developer | A connector's quota ran out ([Limits](#limits)) |
| `oracle_snapshot`, `check`, `answer_score`, `client_observations`, `guideline_findings` | developer | Verification |
| `cleanup_failure`, `run_finished` | developer | The end of the trial |

Extensions name their own events `<extension>.<what>`.

## Drivers

### Scripted driver

A script replays tool calls through the same host path, with no model:

| Step | Meaning |
|---|---|
| `{"tool": NAME, "arguments": {...}}` | One call of a model tool: a native function, `exec`, `ask_user`, `tool_search` or an extension tool |
| `{"idle": true, "text": "..."}` | The agent stops (one model turn). Queued resume messages or the extensions' `on_idle` hooks run; the step's result is the resume message, or null |
| `{"answer": TEXT}` | The final answer, the last step. A list of strings and references is joined |

`{"$ref": "/results/<step>/<pointer>"}` reads an earlier step's result as the
model received it. A native result is an envelope, so pointers look like
`/results/1/result/structuredContent/quote_id`; an `exec` result reads
`/results/4/stdout/structuredContent/quote_id`. An `exec` command may be a list
of argv parts: non-string parts are JSON-encoded and the list is joined with
shell quoting.

A script holds 1 to 1,000 steps. Each call and each idle step is a model turn,
within `limits.model_turns`. The scripted agent stops only at idle steps, so
background work, such as the scheduler's, runs only there. Scripted runs are
labeled "scripted connector replay" and can earn `compatibility_pass`.
`--driver scripted` refuses `--model-profile` and `--config`, and a script
that still holds `TODO(scaffold)` placeholders is refused (exit 2).
[Scaffold](#scaffold) writes a script from a recorded agent trial.

### Agent driver

The agent driver is Inspect's ReAct agent with native tool calls, no
compaction and no truncation. Before the trial, a provider preflight proves
native tool calling; a failed preflight ends the run before anything is
linked. When the model stops without a tool call, the
idle hooks may resume it, at most 20 times. Exhausting the turn, message,
token, context, cost, time or working limit fails the trial (exit 1). Other
Inspect limits block it (exit 3).

With a Muse Spark Open profile, the run lock records `model_lineage`
(`documented`, from `dev.meta.ai/models/muse-spark-open`: the model is derived
from the Muse Spark release of August) and an observation gap says that Muse
runs Muse Spark 1.3, so the agent's behavior is not that of Muse's own model.
Agent sub-runs and model preflights record the same
([model providers](model-providers.md)).

## Scaffold

With the fixture running:

```sh
.venv/bin/consumer-agent-testing-tool scaffold http://127.0.0.1:8765/mcp --output my-connector.yaml
```

For your own server, pass its URL and its auth, for example
`--auth-env EXAMPLE_API_KEY`. `scaffold` runs the MCP handshake
(`server/discover`, or `initialize` for a handshake-era server or the
unofficial `--client-profile muse-template-unofficial-2026-10`) and
`tools/list`, and calls no tool. It writes a connector-v1 skeleton:

- one connector with `endpoint`, `allowed_origins`, `auth`, `client_profile`
  and `tools.allow` (every listed tool);
- a `reads-for-this-task` rule, with every write denied until you add rules;
- an oracle outcome check and an answer claim marked `TODO(scaffold)`;
- comments with suggested classes (each with its basis and reasons), inputs
  that look like personal data (candidates for `private:` and
  `host_arguments`), and an example read-tool check.

`connector-run` refuses a scenario that still contains `TODO(scaffold)`.
Suggested classes stay comments, so every trial classifies the live catalog.
Server text appears only in sanitized comments, and a tool name with control
characters is refused. Scaffold never overwrites a file.

| Option | Meaning |
|---|---|
| `ENDPOINT` | The MCP endpoint; not with `--from-run` |
| `--output FILE` | Required; the file to write. A scenario skeleton is commented YAML, so a `.json` name is refused (exit 2) |
| `--from-run RUN` | Write a scripted calls file from a recorded agent trial instead (below) |
| `--name NAME` | The connector name (default: from `serverInfo.name`) |
| `--auth-env VAR`, `--auth-placement`, `--auth-name` | An API key from VAR, placed as `bearer_header` (default), `custom_header`, `query_param` or `url_path_segment` |
| `--auth-from SCENARIO` | The auth block of a scenario's first connector, OAuth included; not combined with `--auth-env` |
| `--client-profile` | The profile to connect with (default `generic-sdk`) |

A server that requires authorization while no auth is given fails with link
error `required` and a hint.

### From a recorded run

```sh
.venv/bin/consumer-agent-testing-tool scaffold --from-run runs/connector-YOUR_RUN_ID \
  --output my-connector.calls.yaml
```

`scaffold --from-run` turns a recorded trial into a calls file for
`--driver scripted` and writes a checks skeleton beside it, here
`my-connector.checks.yaml`. It reads only the run's sealed public evidence,
never `private/`, and refuses (exit 2) a run whose seal does not verify, a run
that is not a `connector-run` trial or not shareable, a scripted replay and a
trial that stopped before its agent started. It takes no endpoint, `--name`,
auth or `--client-profile` option, and never overwrites a file.

- The calls file holds the agent model's tool calls in order, an
  `{idle: true}` step where the agent stopped and was resumed, and the final
  answer. Under a YAML name it starts with comments naming the run and the
  scenario's digest. Under a `.json` name it is a plain JSON list of steps,
  which `--script` reads as JSON; the run is then named only in the checks
  file.
- Each value the run's evidence hides (a private value, a captured value or a
  credential) becomes a `TODO(scaffold)` placeholder, never a copy. So do
  arguments that did not parse. `connector-run --script` refuses the file
  until you replace them, with a test value or a `$ref` to an earlier result.
- Literal identifiers the service returned, such as quote and booking IDs,
  replay only against the same service state.
- The checks skeleton has one oracle check per write the agent sent (one when
  it sent none) and an answer claim, each judgment a `TODO(scaffold)`. Its
  comments list the recorded run's own check results and the read tools the
  agent used. Merge it into the scenario.

## Readiness check

```sh
.venv/bin/consumer-agent-testing-tool check http://127.0.0.1:8765/mcp --suite muse-guidelines \
  --manifest examples/connector/readiness.yaml
```

The `muse-guidelines` suite lists the server's tools and calls none. It
checks the catalog against the pinned connector guidelines and, with
`--manifest`, the developer's declarations (documentation, authentication,
test account, transactions and classes). Its label is "documented constraint,
not admission". It takes the same `--auth-env`, `--auth-from` and
`--client-profile` options as `scaffold`.

Exit 0 means no fail finding and 1 fail findings; 2 is a setup error. Exit 3
means the selected client profile cannot reach the server, a limit of that
profile. `--drift-check` only fetches the live guidelines (network, opt-in)
and prints JSON: 0 unchanged, 1 drifted, 2 not fetched.

## Extensions

A scenario enables an extension under `extensions: {<name>: {...}}`. The host
imports `consumer_agent_testing_tool.ext_<name>` (hyphens become underscores)
and validates the block with the extension's configuration model. Hooks run
in extension-name order. The `LaneExtension` docstring in `connector_run.py`
is the API reference, and [connector extensions](connector-extensions.md)
describes each shipped extension:

| Extension | Adds |
|---|---|
| [`browser`](connector-extensions.md#browser) | A browser sub-agent for website tasks (`browser_task_spawn`, `browser_task_steer`, `browser_task_peek`, `browser_task_close`) |
| [`clock`](connector-extensions.md#clock) | Built in: the standing context's date and time. `clock.now` (ISO 8601 with an offset) fixes a fixture clock that only extensions advance; `clock.timezone` is an IANA zone |
| [`content_trust`](connector-extensions.md#content_trust) | Provenance labels, an optional injection classifier, withheld one-time codes and links, injection checks |
| [`custom_connector`](connector-extensions.md#custom_connector) | An HTTP API with documentation instead of an MCP connector (`read_doc`, `request_api_access`, `http_request`) |
| [`memory`](connector-extensions.md#memory) | Memory.md, Soul.md and Identity.md context, stale facts and data minimization |
| [`presentation`](connector-extensions.md#presentation) | The user's channel (app, WhatsApp or voice) and format checks |
| [`routes`](connector-extensions.md#routes) | Route choice among connectors, a website and a phone stub (`connector_search`, `connect_connector`, `phone_call`) |
| [`scheduler`](connector-extensions.md#scheduler) | Background jobs on the fixture clock, late approvals and polling metrics (`cancel_scheduled_task`) |
| [`user_simulator`](connector-extensions.md#user_simulator) | A language-model user for questions no scripted reply matches; exploratory runs only |
| [`wallet`](connector-extensions.md#wallet) | Single-use payment tokens for approved purchases, billing postal codes for address verification, and the wallet checks |

## Loopback connector fixture

`connector_fixture` is a Cedar Stays-like hotel booking MCP service for the
lane. It plays a developer's connector, never Muse. It runs the official MCP
Python SDK server over Streamable HTTP on 127.0.0.1 until SIGTERM:

```sh
.venv/bin/python -m consumer_agent_testing_tool.connector_fixture --port 8765 --variant good
```

When ready, it prints one JSON line, the announcement: `server`, `variant`,
`auth`, `port`, `base_url`, `mcp_url`, `oracle_url`, `reset_url`, `clock`,
`accounts`, `basis` and `brand`. It adds `api_key` or `oauth` details,
`wallet` (`register_url`, `merchant`) under checkout, and `test_credentials`
for generated credentials. Business data is deterministic on a fixed fixture
clock (default `2026-11-20T17:00:00Z`). The fixture opens no outbound
connections.

| Option | Meaning |
|---|---|
| `--port` | TCP port on 127.0.0.1; 0 picks a free port |
| `--variant` | A variant (below); default `good` |
| `--auth {none,api_key,oauth}` | Default `none` |
| `--config FILE` | A JSON or YAML `FixtureConfig`; the options above and below override it |
| `--clock` | The start time, ISO 8601 with a zone |
| `--response-mode {json,sse}` | How MCP responses are sent |
| `--api-key-placement {header,bearer,query,path}`, `--api-key-name` | Where the fixture expects the key (default header `X-API-Key`; `path` serves `/mcp/{api_key}`) |
| `--api-key-env VAR` | Accept the key in VAR (at least 16 characters); otherwise a key is generated |
| `--login-env VAR` | A `user:password` login for the OAuth server, with a password of at least 8 characters (repeatable); otherwise passwords are generated |
| `--access-token-seconds` | OAuth access-token lifetime |
| `--oracle-token-env VAR` | Require this token in `X-Oracle-Token` on private endpoints |

Its tools are `search_rooms`, `get_quote`, `create_booking`, `get_booking` and
`cancel_booking`, plus `transfer_funds` under `money-movement` and
`confirm_payment` under `checkout`. Amounts are integer minor units with a
currency, and quotes expire after 900 s of fixture time. A tool error is an
`isError` result whose text is JSON: `{"error": <code>, "message": ...}`.

### Variants

| Variant | Behavior | Basis |
|---|---|---|
| `good` | Accurate annotations, idempotent booking, distinct pending and confirmed states | Documented: guidelines 2, the Cedar Stays example |
| `hidden-write` | The read-annotated `get_quote` also books and charges | Documented: guidelines 3.2 |
| `steering-description` | `search_rooms` text steers away from competitors and forces a recommendation | Documented: guidelines 3.2 |
| `money-movement` | Adds `transfer_funds` between financial accounts | Documented: guidelines 4.6 |
| `double-charge-on-retry` | No idempotency: the first attempt commits, then reports `payment_timeout` | Documented: guidelines 4.7 |
| `price-change` | The rate rises between quote and booking; `price_changed` asks for a new quote | Documented: guidelines 3.4 |
| `false-success` | `create_booking` reports confirmed but stores pending | Documented: guidelines 2 and 4.7 |
| `slow-confirm` | Bookings start pending and confirm after `confirm_after_polls` reads (2) or `confirm_delay_seconds` of fixture time (300) | Documented: guidelines 4.7 |
| `needs-redirect` | The endpoint answers 307 | Unofficial: the template follows no redirects |
| `needs-session` | Quotes are bound to the MCP session that issued them | Unofficial: the template starts a session per invocation; handshake era assumed |
| `checkout` | Pays with a single-use `payment_token`, a bank code step, a preselected add-on, receipts | Documented: guidelines 3.2, 4.4, 4.7 and 5.5; the token protocol and code step assumed |
| `etag` | `get_booking` returns an `etag`; a read with a matching `if_none_match` answers `{"not_modified": true}` | Guidelines 4.7 and 5.4 documented; the mechanism assumed, an analog of HTTP If-None-Match and 304 |
| `rate-limited` | A repeat read of a booking within `rate_limit_seconds` (30) fails with `rate_limited` and `retry_after_seconds` | Guidelines 5.4 documented; the mechanism assumed, an analog of HTTP 429 with Retry-After |
| `quote-expiry` | Quotes expire after `quote_expiry_seconds` (120); `create_booking` then answers `quote_expired` | Documented: guidelines 3.4 and 4.7 |

Under `checkout`:

- `create_booking` takes a `payment_token` the host registered on
  `/__wallet/register`.
- It declines a token that is unknown, used, expired, for another merchant or
  currency, or below the amount due (plus tolerance): error `payment_declined`
  with a reason (`unknown_token`, `token_used`, `token_expired`,
  `wrong_merchant`, `wrong_currency` or `amount_exceeds_token`). A token is
  spent once `create_booking` accepts it.
- A charge above `challenge_above_minor` (50000) returns `requires_action` with
  a `challenge_id`. `confirm_payment` then needs `challenge_code`; a wrong code
  is `incorrect_code`, and `challenge_attempts` (3) wrong codes are
  `verification_failed`. A closed or unknown challenge is `challenge_closed` or
  `challenge_not_found`.
- Without `challenge_code`, the fixture generates a code per process and
  announces it as `test_credentials.challenge_code`, which no scripted reply
  can know.
- Quotes carry a preselected monthly add-on at `add_on_minor` (1500), dropped
  with `decline_add_on: true`.
- With `avs: true` in a `--config` file, the test issuer verifies the billing
  address: `create_booking` also requires `billing_postal_code`, compared with
  the card owner's code on file (`avs_postal_code`, default `94110`). A
  mismatch is `payment_declined` with reason `avs_mismatch`, and nothing is
  charged. A verified charge records `avs: match`, and the oracle's `wallet`
  reports `address_verification` but never the code (basis `AVS_BASIS`,
  assumed).

### Configuration

A `FixtureConfig` file sets what the options set and more:

| Field | Default | Meaning |
|---|---|---|
| `variant`, `auth`, `port`, `clock`, `response_mode` | `good`, `none`, `0`, `2026-11-20T17:00:00Z`, `json` | As the options |
| `api_key_placement`, `api_key_name`, `api_keys` | `header`, `X-API-Key`, `{}` | API key auth; `api_keys` maps a key to a username |
| `users` | two generated accounts | `{username, password, name, email}` |
| `access_token_seconds`, `refresh_token_seconds`, `dcr`, `cimd_documents`, `preregistered_clients` | 3600, none, true, `{}`, `[]` | The OAuth server |
| `price_change_nightly_minor` | 2500 | `price-change` |
| `confirm_after_polls`, `confirm_delay_seconds` | 2, 300 | `slow-confirm` |
| `challenge_above_minor`, `challenge_code`, `challenge_attempts`, `add_on_minor` | 50000, generated, 3, 1500 | `checkout` |
| `avs`, `avs_postal_code` | false, `94110` | `checkout` only: address verification (above) |
| `rate_limit_seconds` | 30 | `rate-limited` |
| `quote_expiry_seconds` | 120 | `quote-expiry` |
| `lost_responses`, `lost_response_seconds` | `{}`, 120 | Fault injection (below) |
| `oracle_token` | none | Protects the private endpoints |
| `brand` | Cedar Stays | [Brand](#brand) |

### Brand

`brand` lets two instances be two businesses in one test:

| Field | Meaning |
|---|---|
| `name` | Display name: the server title, seller and catalog text. The server name is its slug (`Trip Desk` becomes `trip-desk`) |
| `room_names` | New names for the rooms, by room id |
| `path_prefix` | Moves every route under a prefix, except the OAuth discovery documents, which stay at the origin's root with the path appended |

The brand has no command-line option. `examples/connector/routes-competing-connectors.fixture.yaml`
serves Trip Desk this way.

### Private endpoints

The oracle and wallet answer only loopback peers that send the fixture's own
`Host` and no browser `Origin`, and with `oracle_token` set they also require
`X-Oracle-Token`. They are not MCP tools.

| Method and path | Body | Answer |
|---|---|---|
| `GET /__oracle/state` | | The backend truth (below) |
| `POST /__oracle/reset` | `{clock?, auth?}` | Clears bookings, charges and logs, restores the configured faults and returns the state. It keeps the clock unless `clock` is given; `auth: true` also resets the OAuth server |
| `POST /__oracle/clock` | `{advance_seconds}` or `{set}` | `{clock}` |
| `POST /__oracle/advance` | `{seconds}` | `{clock}` |
| `POST /__oracle/revoke` | `{username}` | `{revoked_families}` (OAuth) |
| `POST /__oracle/fault` | `{tool, lost_responses}` | Withholds the tool's next N responses (0 clears) and answers `{lost_responses}` |
| `POST /__wallet/register` | `{token, merchant, amount_minor, tolerance_minor, currency, expires_at}` | `checkout` only: 201 with the token by its last four characters; 409 `token_exists`; 400 `invalid_request` |

### Oracle state

`GET /__oracle/state` returns `confirmed_count`, `pending_count`,
`cancelled_count`, `bookings`, `charges`, `duplicate_charges`,
`charged_minor`, `transfers`, `quotes`, `rates`, `calls`, `tool_errors` and
`booking_reads`. It also returns the request log (`requests`, `rpc`, `http`,
`sessions`), `accounts`, `authorization`, `faults`, `clock`, `brand`, and the
variant's `basis`. `early_repeats` (rate-limited) and `stored_payment_methods`
and `wallet` (`merchant`, `tokens`, `declines`, `challenges`; checkout) are
null under other variants; under checkout, `wallet` also says whether the
issuer verifies billing postal codes (`address_verification`). Tokens appear
by their last four characters only, and the request log never names a key.

### Lost responses

`lost_responses: {create_booking: 1}` in a `--config` file, or
`POST /__oracle/fault`, makes the next responses of a tool commit and then
wait, withheld, until the client gives up (`lost-response`, assumed: a harness
fault, not a connector defect). The trial is then blocked unless the scenario
declares a `reconcile` read for that tool. `double-charge-on-retry` adds a
service without idempotency, so an agent that retries is charged twice.

### REST fixture

The `custom_connector` extension tests against `rest_fixture`, a REST
version of the same hotel with its own documentation bundle:

```sh
export CEDAR_STAYS_API_KEY=cedar_$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')
.venv/bin/python -m consumer_agent_testing_tool.rest_fixture --port 8790 --api-key-env CEDAR_STAYS_API_KEY
```

`--write-docs DIR` writes the bundle with its base URL as `{{service_url}}`
and exits; the examples README shows the commands that regenerate the shipped
bundles. See [custom_connector](connector-extensions.md#custom_connector)
and the `custom-*` rows in [examples/connector](../examples/connector/README.md).

## Limits of the lane

- The host is a surrogate. Agent runs are surrogate behavior evidence, never
  Muse compatibility evidence.
- The checks see only what the oracle URLs and the connector's read tools
  report.
- The scripted user follows the declared rules; it does not model a person.
- Connectors are reached over Streamable HTTP only, and connector runs cannot
  be resumed.
- Muse's own connector selection, prompt and model are not public; the
  projections, the prompt after its first line and the CLI shape are labeled
  assumed or unofficial.
