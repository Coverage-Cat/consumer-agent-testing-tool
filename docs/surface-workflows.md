# General consumer-surface workflows

`consumer-agent-testing-tool surface-run` exercises a configurable consumer workflow against MCP
and an embedded app. It is a supported lane of `local-consumer-v1`, separate
from the development CLI/skill lane (`run --surface cli-skill`). It does not
certify Muse or any other consumer-agent product.
Its embedded-app results are generic MCP Apps host evidence, not Muse evidence:
Muse documents no third-party UI. See [MCP Apps host](#mcp-apps-host).

## Local example

Use the workspace Python environment and optional Node tooling. The historical
`tooling/payment-ui` directory now also supplies the general host dependencies;
no insurance-specific behavior is used by this command.

```sh
npm ci --prefix tooling/payment-ui --cache .cache/npm
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" \
  node tooling/payment-ui/node_modules/playwright/cli.js install chromium
node scripts/surface_fixture.mjs 8765
```

In another terminal in this workspace:

```sh
.venv/bin/consumer-agent-testing-tool surface-run examples/surface-preferences.yaml
```

The synthetic service stores a preference in memory. It performs no external
requests. Restart it for a fresh test. The scenario verifies the prefilled name,
clicks the embedded form, accepts the app's scoped tool callback, and reads the
service to verify a single write. Both standard MCP Apps and the explicitly
selected `legacy-openai` bridge are tested. The legacy bridge is a host
compatibility adapter, not evidence that another product supports that interface.

The fixture takes an optional mode after the port, for example
`node scripts/surface_fixture.mjs 8765 ext-apps`. The last column is the outcome
of `examples/surface-preferences.yaml`, which uses the `mcp-app` bridge:

| Mode | Server behavior | Example outcome |
| --- | --- | --- |
| `mcp-app` (default) | Declares the UI extension as `{}`; links the UI from the `prepare` tool definition and from tool results | Exit 0 |
| `ext-apps` | Behaves like the official example servers: no extension declaration; the link is only on the tool definition | Exit 0 |
| `ext-apps-blob` | As `ext-apps`, with the HTML returned as a base64 `blob` | Exit 0 |
| `legacy-openai` | Links the UI through `openai/outputTemplate` and serves an app written for `window.openai` | Exit 1: the app never initializes the MCP Apps bridge, and the host's readiness wait times out; a `legacy-openai` step renders it |
| `openai-template` | Links an MCP App only through `openai/outputTemplate` | Exit 1: failed link check |
| `result-link` | Links the UI only in tool results, which MCP Apps hosts ignore | Exit 1: failed link check |
| `invalid-link` | Puts a non-`ui://` URI in `_meta.ui.resourceUri` | Exit 1: failed link check |
| `no-ui` | Links no UI anywhere | Exit 3: unsupported |
| `modern` | Speaks only MCP 2026-07-28: `server/discover` works and `initialize` fails with -32022. Links the UI only when the request declares MCP Apps | Exit 3: unsupported at connect. Exit 0 when a copy sets `version_negotiation: auto` |
| `host-contract` | As `ext-apps`, plus a declared CSP, the clipboard-write permission, `prefersBorder`, `_meta.ui.visibility` (`status` for the model only, a `poll` tool for views only), and a view that sends model-context, message, open-link and display-mode requests and answers teardown. The view shows its message's outcome (`delivered` or `isError`) in `#message` | Exit 0 |
| `listing-csp` | As `ext-apps`, with the CSP declared only on the `resources/list` entry | Exit 0 |
| `hostile-csp` | As `ext-apps`, declaring CSP sources and a permission that the host must refuse, and a `domain` | Exit 0 |
| `early-call` | A view without the SDK that calls `save`, with a value the service rejects, before it finishes initializing | Exit 1: the service rejects the write (`tool/API`); the view gets `tool-cancelled`, then teardown |
| `silent-teardown` | As `ext-apps`, with a view that never answers teardown | Exit 0, after the teardown wait |
| `proxy-escape` | As `ext-apps`. After rendering, the view navigates its sandbox proxy away and calls `save` from there | Exit 0: the scripted click saves first. Without UI actions, the escaped call is dropped and the callback times out (exit 1) |
| `resources` | As `ext-apps`, with an official-SDK view that reads and lists `fixture://` resources through the host, including one it may not read and one just over 5 MiB | Exit 0: the example names no `callback_resources`, so the view's requests get -32601 |
| `resource-relay` | As `resources`, with a listing over three pages, reads answered with -32002, with -32602 carrying `data.uri` and with -32001, and a note clients may cache for 60 s | Exit 0, as for `resources` |
| `native-network` | As `ext-apps`, with a view that fetches, writes and follows redirects (a 302, and a 307 of a write) to the fixture's origin and to a second "escape" origin that its CSP admits | Exit 0: the example sets no `browser_origins` |
| `api-key` | As `ext-apps`, but `/mcp` accepts only the key in `SURFACE_FIXTURE_API_KEY`, as a bearer token, an `X-API-Key` header or an `api_key` query parameter, and answers 401 otherwise | Exit 2 at connect (`authentication`). Exit 0 with the variable exported before the fixture starts and a matching [`auth`](#scenario-contract) block |
| `network-app`, `network-autoload`, `network-autoload-nested` | As `mcp-app`, plus a declared CSP and app controls or automatic requests that read, write, redirect and reach a forbidden canary | Used by the isolated-renderer tests |

An optional scenario-level `browser_worker` selects the
[isolated embedded renderer](embedded-browser-worker.md). It uses explicit
destination pins, per-request write authority and runtime locks, with no native
fallback. Without that setting, the existing native renderer is used. The
isolated renderer is a development extension: `surface-run` refuses a scenario
that sets `browser_worker` (exit 3) unless `--development-extensions` comes
before `surface-run`.

## Agent driver: a scripted action menu

```sh
.venv/bin/consumer-agent-testing-tool surface-run examples/surface-preferences.yaml \
  --driver agent --config consumer-agent-testing-tool.yaml --model-profile openrouter-dev
```

`--driver agent` is a scripted action menu, not an agent using the service's
tools. Inspect ReAct offers one native tool, `perform_action`, whose `id` enum
lists the scenario steps that are ready. The model picks one step per turn, and
the host runs it with the scenario's host-resolved arguments. The model never
sees the server's tools, schemas or raw results. Each observation contains only
the goal, the ready actions (ID, description and kind), the IDs of completed
steps, and the result fields that steps declare under `expose`, with credentials
masked. After a view sends `ui/update-model-context`, it also contains the
latest update as `app_context`, and after a view sends `ui/message`, the
messages no model has read yet as `app_messages` (see
[view requests](#view-requests)). It contains no check outcomes, host
capabilities, fixture or secret values, or UI fields.

Inspect owns generation, proposal approvals, tool execution, task limits and
evaluation logs. The tool schema updates as dependencies complete. A turn with
more than one call, an unavailable ID or extra arguments ends the run as an
agent error (exit 1). Inspect refuses a call to any other tool name, or one with
unparsable arguments, before the host sees it. The model gets it back as a tool
error, and the turn still counts toward `max_model_turns`. This approval permits
scenario dispatch, not transactional consent. Inference runs wherever the
selected model profile is hosted. Scripted runs use no model. Model credentials
are not inherited by the browser sidecar.

Before connecting to the service, the driver runs the provider preflight: one
native tool call and its continuation. The probe is listed under
`model_preflight_runs`, and the run's `failure_stage` is `model-preflight` if it
fails. A provider error is a setup failure (exit 2), and a provider without
native tool calls is unsupported (exit 3). Returned checks are classified as in
`run`: a failed check exits 1, required coverage left unverified exits 3, and
optional observation gaps are tolerated. Each attempt writes a sub-run under
`agent/`, linked from `agent_runs`.

Agent runs are labeled as what they are. `result.json` records
`lane: action-menu`, `evidence_label: scripted action menu`,
`layers.agent.exercised: false` and the observation gap "Service tool surface
not exposed to the model: the agent chose among scenario-authored actions and
the host resolved every tool argument". Agent runs never receive
`compatibility_pass`. Sub-run metrics are computed, but labeled
`agent_interface: scripted action menu` and `tool_use_quality: not measured`;
token usage and timing are measured. Use agent runs to see how a model
sequences a known workflow, not to test whether it can use the service's tools.

## Scenario contract

Scenarios are YAML or JSON with strict field validation:

- `endpoint` and exact `allowed_origins` configure the MCP transport. Redirects
  and alternative transport URLs are denied. `browser_origins` separately
  permits required embedded resources; an empty list is valid for self-contained
  apps. `test_only: true` records the operator's declaration, not independent
  attestation that an arbitrary remote service is a sandbox.
- `fixture` contains synthetic public test data. Put private values in a
  workspace-local JSON object supplied with `--secrets-file`.
- `tools` names the permitted operations, their read/write effect, input JSON
  schema, host-owned arguments, captured credentials, and optional reconciliation.
  Host arguments override caller values. The browser's callback allowlist is
  further restricted per UI step.
- `steps` contain tool calls, embedded UI interactions, supplied user responses,
  or assertions. Dependencies are explicit through `after`; an agent can choose
  among ready actions. UI steps must depend on their source tool step.
- `{"$ref": "/fixture/name"}`, `/secrets/access`, `/steps/prepare/result/...`,
  and `/replies/preference` resolve only in the host. JSON pointers use standard
  `~0` and `~1` escaping.
- `checks` compare typed expected values against JSON pointers rooted at
  completed/observed steps. Each check declares an expectation, agent, host, or
  service layer. Missing evidence is an observation gap, never a pass.
- `expose` maps safe names to result JSON pointers for the planner. It defaults
  to empty. Private metadata paths are rejected. Credentials in exposed values
  are masked; the user's own data stays visible.
- A `user` step declares `reply_key`; missing input pauses with its description.
  Supply recorded answers through `--replies replies.json` and resume.
- `max_model_turns` (default 100) caps model generations in an agent attempt.
  `wall_seconds` (default 600) bounds the Inspect task, including its host calls;
  host startup and evidence export are outside this deadline. Reaching either
  limit blocks the workflow (exit 3) with its durable state intact, so an
  interrupted write can still be reconciled. The API and browser lanes count
  exhausted limits as failed trials instead. `timeout_seconds` separately bounds
  each host step: a step that overruns it is an integration failure (exit 2)
  that can resume, and a readiness or callback timeout inside the app fails the
  step (exit 1).
- `version_negotiation` selects how the host connects: `legacy` (default) uses
  the 2025 `initialize` handshake, and `auto` also reaches MCP 2026-07-28
  servers. See [protocol versions](#protocol-versions).
- `auth` gives the MCP connection an API key:
  `{mode: api_key, env, placement, name}`, with `placement` `bearer_header`
  (default), `custom_header` or `query_param`, and `name` the header or query
  parameter for the last two. `url_path_segment` and OAuth are refused. The key comes
  from the environment variable `env`, needs at least 8 characters, must not
  reuse an inference-provider key from the environment (the selected model
  profile's included), and is sent only to the endpoint, over HTTPS or to a
  loopback endpoint. It is registered for
  redaction before anything is sent and reaches the browser host only over its
  private channel, so it never appears in evidence. The `capabilities` event
  records `endpoint_auth` (`mode`, `placement`, `name`, `env`). A missing key,
  or one shared with a provider, fails at connect before the provider
  preflight; a key the endpoint refuses (HTTP 401 or 403) fails at connect
  after it, because the preflight runs before any contact with the service.
  Both exit 2 with class `authentication`. The field stays out of the scenario
  digest while unset.

UI steps name their `source` tool step, a `bridge` (`mcp-app` or
`legacy-openai`), a readiness selector, and `expect_value`, `expect_text`,
`fill`, or `click` actions. `callback_resources` lists, as distinct absolute
URIs (at most 100, `mcp-app` bridge only), the server resources the view may
read through the host (see [view requests](#view-requests)); it stays out of
the scenario digest while empty. Actions execute in the embedded app unless an exact
allowed `frame_url` selects a hosted child frame. Expected values and private
fill values are resolved by the host, not the planner. Callbacks are validated
against the declared tool schema and receive host-injected credentials.

The host finds the UI the way MCP Apps hosts do: from the source tool's
definition in `tools/list`, through `_meta.ui.resourceUri` or the deprecated
`_meta["ui/resourceUri"]`. For the `legacy-openai` bridge,
`openai/outputTemplate` also counts. The server need not declare the UI
extension; the host declares it as a client. `ui.resource_uri` is optional; a
scenario URI must equal the definition's link. Result metadata alone is not a
link. The resource must contain `text/html;profile=mcp-app` content, as `text`
or as a base64 `blob`; otherwise the step fails (exit 1).

Each UI step gets the automatic host check `mcp-apps-resource-link`; scenarios
cannot use that ID. It passes when the rendered URI is the one the tool
definition links. It fails, with a `detail` and exit 1, when:

- the URI came from the scenario or from result metadata, and the tool
  definition links a different URI or none;
- an `mcp-app` step is linked only through `openai/outputTemplate`;
- the definition's link is not a `ui://` string.

A tool that links no UI anywhere blocks the step as unsupported (exit 3).
Nothing rendered, so a resume can try again. The check is marked `automatic`
and cannot make a compatibility pass on its own.

No fallback page is opened automatically. Negotiation, resource discovery,
rendering, interactions, and callbacks have separate observed outcomes.
Negotiation passes when at least one tool definition links a UI. A readiness or
callback timeout fails at that stage; merely discovering HTML does not count as
successful rendering.

## MCP Apps host

The host follows MCP Apps 2026-01-26. The `mcp-app` bridge uses the official
`AppBridge` from `@modelcontextprotocol/ext-apps`. Results are generic host
evidence: `result.json` records `mcp_apps_host` with `evidence`
(`generic MCP Apps host; not Muse evidence`), the `specification`, a
`scope_basis` labeled `assumed`, the `renderer`, the scenario's
`version_negotiation` and, in the native renderer, `network_writes`
(see [sandbox and content security policy](#sandbox-and-content-security-policy)). The basis is an absence of evidence: Muse's guidelines
never mention UI, and no Meta client is in the MCP extension matrix (see the
[review](muse-emulation-review-2026-10-15.md#22-not-a-muse-requirement-embedded-mcp-apps)).

After the app initializes, the host sends:

- `tool-input` with the arguments of the source tool call, as recorded when the
  call was sent. They are never resolved again, so a resume needs no earlier
  reply. Host arguments are never part of them. Positions filled from
  `/secrets`, including the whole `/secrets` map, and any string that contains
  a registered secret are sent as `[REDACTED]`.
- `tool-result` with the source call's result.

The `hostContext` describes this host: theme `light`, display mode `inline` (the
only one in `availableDisplayModes`), the browser's locale and time zone,
platform `web`, `containerDimensions` with the frame's width and a `maxHeight`
of 6,000 px, and `toolInfo` with the source tool's definition. The step result
records it as `host_context`, with `tool_info: true` in place of the
definition, and the input's keys as `tool_input_keys`.

### Sandbox and content security policy

In the native renderer, the host page runs on `https://surface-host.invalid`
and loads each `mcp-app` view through a sandbox proxy on
`https://surface-app.invalid`, as the ext-apps reference host does. The proxy
writes the view's HTML into an inner frame, so the view has the proxy's origin
and CSP. The bridge sends only to the app origin and accepts messages only from
it. A message from any other origin, for example from a document the view
loaded in place of the proxy, is dropped and recorded as `ui_message_dropped`.

The host reads the UI resource's `_meta.ui` from its `resources/read` content,
or else from its `resources/list` entry. The `legacy-openai` bridge also reads
`openai/widgetCSP`. From the `connectDomains`, `resourceDomains`,
`frameDomains` and `baseUriDomains` in `_meta.ui.csp`, the host builds the
policy as the specification describes. A resource that declares no CSP gets the
specification's restrictive default, plus `frame-src 'none'`,
`object-src 'none'` and `base-uri 'self'`:

```text
default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'self'
```

A declared source is accepted only as an exact `http`, `https`, `ws` or `wss`
origin, with an optional `*.` label and port. Keywords, bare schemes, paths and
anything else are left out and listed under `csp.rejected`. Metadata that is not
an object falls back to the restrictive default.

The restrictive default blocks every request (`connect-src 'none'`) and every
external script, style, image, font and media file. An app that fetches from or
loads resources from `browser_origins` must also declare those origins:
`connectDomains` for requests, `resourceDomains` for scripts, styles, images,
fonts and media, and `frameDomains` for nested frames. A request must pass both
the CSP and the host's `browser_origins` check; resource metadata never adds a
destination.

In the native renderer with a nonempty `browser_origins`:

- The host page is granted Chromium's local-network-access permission, as a
  user who accepts the prompt would, so a view's requests reach allowlisted
  loopback and private origins. The CSP and the origin check still decide
  every request.
- Every redirect hop is checked again. A hop may go only to a
  `browser_origins` origin, and only as a GET or HEAD, so a 307 or 308
  redirect of a write is refused.
- A view's direct writes are sent without host consent, but the coordinator
  first records each one as a `host_action` event with `channel: ui-network`,
  `consent: none`, the `url`, `method`, `body_sha256` and `body_bytes`. A write
  that cannot be recorded is refused. `result.json` counts them in
  `mcp_apps_host.network_writes.unconsented`, with the `policy` that applied.
  The isolated renderer instead needs a one-use grant per write.

A view that relies on a redirect to an origin outside `browser_origins`, or on
a 307 or 308 redirect of a write, therefore fails.

The `camera`, `microphone`, `geolocation` and `clipboardWrite` entries of
`_meta.ui.permissions` become the frame's `allow` attribute; other features are
ignored. `_meta.ui.domain` is recorded with `applied: false`, and
`prefersBorder` as `prefers_border`.

The `ui_sandbox` event and the UI step result's `sandbox` field record the
`renderer`, the `proxy` origins, the `view_origin`, where the `metadata` came
from (`resource-content`, `resource-listing` or `none`), the CSP `source`
(`declared` or `restrictive-default`), its `policy`, accepted `domains` and
`rejected` entries, and the granted permissions with their `allow` attribute.
In the native renderer they also record `network`: `destinations`
(`browser_origins`, or `none` when it is empty) and, with destinations,
`local_network_access`, `redirects` and `writes`.
The `legacy-openai` bridge and the
[isolated embedded renderer](embedded-browser-worker.md) keep one app iframe on
the app origin, with the same CSP and permissions but no proxy (`proxy: null`).

### View requests

The host offers views the `serverTools`, `openLinks`, `logging`, `message` and
`updateModelContext` capabilities, plus its sandbox's permissions and CSP, and
`serverResources` when the UI step lists `callback_resources`. View requests
are evidence, never authority:

| Request | Host behavior | Event |
| --- | --- | --- |
| `tools/call` | See [view tool calls](#view-tool-calls) | `ui_callback`, `ui_callback_denied`, `ui_callback_refused` |
| `resources/read` | Forwarded to the server, as one request, only for a URI in the step's `callback_resources`; see [resources](#resources) | `ui_resource_read` |
| `resources/list` | Forwarded as one request; the view gets the server's page filtered to `callback_resources`, with the server's `nextCursor` | `ui_resource_list` |
| `ui/message` | Recorded with its `role` and `content`. Delivered to agent runs as an app message; see [app messages](#app-messages) | `ui_message` |
| `ui/update-model-context` | Recorded. Each update replaces the previous one, and an empty update clears it | `ui_model_context` |
| `ui/open-link` | Recorded; the link is not opened | `ui_open_link` |
| `ui/request-display-mode` | Answered with `inline`, the only mode | `ui_display_mode` |
| `notifications/message` (logging) | Recorded | `ui_log` |
| `ui/notifications/request-teardown` | Recorded; the UI step still decides when the view closes | `ui_request_teardown` |
| `ui/notifications/size-changed` | Sets the frame's height, up to 6,000 px; the width is fixed | None; see `view_size` |

Message content or a model-context update over 16,384 characters is recorded
as its size and SHA-256 only. Log data and link URLs are cut at 2,000
characters. Apart from tool calls, a UI step records at most 200 view requests
and at most 50 log entries, and counts the rest as `dropped`. The step result's
`view_requests` counts the recorded requests by event. Its `view_size` holds
the last reported `width` and `height`, the `count` of reports and the
`applied_height`.

The latest model-context update is the run's app context. In agent runs, each
later observation includes it as `app_context` (`step`, `source`, `content`,
`structuredContent`), with credentials masked. A context over 8,000 characters
is replaced by an `omitted` note. The app context persists across UI steps and
resumes until a view clears it.

#### Resources

Without `callback_resources`, the host declares no `serverResources`, and a
view's `resources/read` and `resources/list` get -32601. With it:

- A read of a listed URI, and each listing, is one server request, forwarded
  as the ext-apps reference host does, with no client response cache.
- A read of another URI, or a listing cursor over 2,000 characters, is denied
  with -32000. A result over 5 MiB of JSON is withheld with -32000.
- Server JSON-RPC errors reach the view with their code and their `data` (up
  to 16 KiB). A legacy resource-not-found -32002 reaches the view as -32602,
  which is how the bridge SDK encodes it anyway.
- `resources/templates/list` still gets -32601.

The `ui_resource_read` and `ui_resource_list` events record the `disposition`
(`proxied`, `denied`, `withheld` or `failed`), the `characters` of the result,
and per content item (at most 20, with `content_count`) its `uri`,
`mimeType`, `encoding`, `characters` and `sha256`, never the content. A
listing records the number of entries the server listed (`listed`) and the
URIs the view was shown (`shown`). A failure records the server's `code`,
`view_code` when the view got another, and `data_characters` when data was
relayed.

#### App messages

In agent runs a view's `ui/message` is queued and delivered once, in the next
observation a model reads, as `app_messages` (each with `step`, `source`,
`role` and `content`, credentials masked; a message over 8,000 characters has
`content: null` and an `omitted` note). At most 20 wait at a time. The host
adds no model turn for a message, although the pinned ext-apps 2.0.3 types say
that `sendMessage`, unlike `updateModelContext`, triggers an immediate model
response. The `ui_message` event records the disposition:

| Disposition | When | The view's result |
| --- | --- | --- |
| `queued for the agent's next observation as an app message` | Agent runs | `{}` |
| `recorded; not delivered to a conversation` | Scripted runs | `isError: true` |
| `recorded; not delivered: no model turn follows the workflow's last step` | The message came during the last step | `isError: true` |
| `recorded; not delivered: 20 app messages were already waiting` | The queue is full | `isError: true` |

A message past the step's 200-request evidence bound, or sent outside a UI
step, is neither recorded nor delivered and also gets `isError: true`. The
legacy bridge's `sendFollowUpMessage` resolves either way, because its API has
no delivery result.

When a turn limit, time limit or failed model call ends the agent loop before
a model read queued messages, the run records `app_messages_undelivered`
(`count`, `steps`, `reason`, `requeued: true`) and puts them first in a
resumed run's first observation. A run that ends with messages still queued
records the event with `requeued: false`.

### Teardown and cancellation

Before a view closes, for any reason, the host sends `ui/resource-teardown`. It
waits up to 3 s for the answer after a completed step, and 1 s after a stopped
step or when the host closes, for example after Ctrl-C. If the view never
received the source call's result, for example because the view's own call
stopped the step first, `tool-cancelled` comes before the teardown. The
`ui_teardown` event records the `reason`. Both the event and the step result's
`teardown` record the `status` (`acknowledged`, `no-response`, `rejected` with
the view's error `code`, `failed` or `not-initialized`) and `tool_cancelled`. A
view built with the official SDK but without a teardown handler answers -32601,
recorded as `rejected`. The `legacy-openai` bridge gets no teardown.

### Tool visibility

`_meta.ui.visibility` on a tool's `tools/list` definition says who may use the
tool: `model`, `app` or both, the default. The `capabilities` event records
each declared value under `tool_visibility`, for example
`{"status": {"model": true, "app": false, "declared": ["model"]}}`.

- A view's call to a tool without `app` is refused and recorded as
  `ui_callback_denied` with reason `visibility excludes app`. The view gets an
  error and the step continues. The coordinator applies the same rule to any
  call that reaches it.
- A tool step that calls a tool without `model` stops the run at connect,
  before any call (exit 2, `setup/infrastructure`), because MCP Apps hosts
  never offer such a tool to the model. Use it as a UI callback tool instead.
  Host reconciliation reads may still use it.
- A value that is not an array is the server's defect. The host offers the tool
  to neither the model nor a view, and a tool step that calls it fails at
  connect (exit 1, `tool/API`).

### Protocol versions

By default the host connects with the 2025 `initialize` handshake. With
`version_negotiation: auto`, the MCP client SDK tries MCP 2026-07-28
(`server/discover`) first and falls back to `initialize` for older servers. A
server that refuses the 2025 handshake as an unsupported protocol version
(-32022) blocks the run at connect (exit 3, `unsupported`) with the hint
`set version_negotiation: auto to reach MCP 2026-07-28 servers`. The field is
part of the scenario digest, so start a new run after setting it instead of
resuming. A scenario that leaves it at `legacy` keeps its earlier digest.

The `capabilities` event records `protocol_version`, `protocol_era` (`modern`
for 2026-07-28, `legacy` for `initialize`) and `version_negotiation`.
`run_lock.server` pins the version and era.

### The legacy `window.openai` bridge

The `legacy-openai` bridge installs `window.openai` before the app's scripts
run. Its member list is labeled `assumed`: no OpenAI Apps SDK reference is
pinned.

| Member | Behavior |
| --- | --- |
| `toolInput`, `toolOutput`, `toolResponseMetadata` | The call's arguments, the result's `structuredContent` and its `_meta` |
| `theme`, `locale`, `displayMode`, `maxHeight` | `light`, the browser's locale, `inline` and 6000 |
| `widgetState`, `setWidgetState` | The widget state. Setting it dispatches `openai:set_globals` and becomes the app context (`ui_model_context`) |
| `callTool` | A view tool call |
| `sendFollowUpMessage`, `openExternal`, `requestDisplayMode` | Recorded as `ui_message`, `ui_open_link` and `ui_display_mode`; the display mode stays `inline` |
| `notifyIntrinsicHeight` | A size change |
| `requestModal`, `requestClose`, `uploadFile`, `getFileDownloadUrl`, `setOpenInAppUrl`, `callCompletion`, `streamCompletion` | Rejects with an error, recorded as `ui_bridge_unsupported` |

Reading any other member returns `undefined`, and the first read of each name
is recorded as `ui_bridge_unsupported`.

### View tool calls

The app may call only the step's `callback_tools` that are visible to apps
(see [tool visibility](#tool-visibility)), with arguments that satisfy the
tool's `arguments_schema`. The browser host refuses a call to a tool outside
`callback_tools`, or from another frame, and records it as a
`ui_callback_denied` host observation. The coordinator refuses a call whose
arguments break the tool's `arguments_schema` and records it as
`ui_callback_refused`. In both cases the view receives an error and the step
continues. Accepted calls take the same host path as scenario calls, including
host arguments and consent:

- Reads reach the service every time, so a polling view never sees a frozen
  result.
- Identical writes within one UI step are one logical write: a repeat returns
  the recorded result. A write with changed arguments pauses the run (exit 3)
  and is not sent.
- A result with `isError`, or a value at one of the tool's `error_paths`,
  reaches the view as the service's `CallToolResult` and is recorded. A
  rejected write ends the UI step at once (exit 1, class `tool/API`). Negative
  UI paths that show a service error after a rejected write therefore cannot be
  tested in this release.
- A pause (approval, changed arguments, an ambiguous write) or a host failure
  inside a call stops the step visibly. The view learns only that its call
  failed.
- A consent-gated write pauses for approval (exit 3). If the step sets
  `callback_required: true`, resuming with `--replies` sends exactly that call
  without rendering the app again.
- A read completes the step only when all of the step's callback tools are
  reads. Polling never stands in for a submission.
- A call still in flight when the step ends fails the step (exit 2).

### Not implemented

- `resources/templates/list`, `notifications/resources/list_changed`, URI
  templates or prefixes in `callback_resources`, `ui/download-file`, sampling,
  partial tool input and host-context change notifications.
- A model turn for a view's `ui/message`: the message waits for the next
  observation a model reads.
- Applying `_meta.ui.domain`, and display modes other than `inline`.
- A sandbox proxy for the `legacy-openai` bridge and the isolated embedded
  renderer. Both keep one app iframe with
  `allow-scripts allow-same-origin allow-forms`.
- OAuth, or an API key in the URL path, on the MCP connection; see `auth` in
  the [scenario contract](#scenario-contract).
- Consent for a view's direct writes in the native renderer: they are recorded,
  not gated. The isolated embedded renderer adds per-request grants.

## Resume and uncertain outcomes

A run directory must stay within the workspace; paths and symlinks escaping it
are rejected. While a run can resume, its atomic, mode-0600
`checkpoint.private.json` holds tool results, credentials, and host inputs.
Keep it private. A run can resume after it blocks (exit 3), after a setup or
infrastructure failure (exit 2), or after a failed outcome (exit 1) that left a
write unconfirmed.

```sh
.venv/bin/consumer-agent-testing-tool surface-run examples/surface-preferences.yaml \
  --resume runs/surface-RUN_ID --replies replies.json
```

A completed run, or one with a failed outcome (exit 1), is final unless a write
is unconfirmed. A write whose call failed after it was sent, for example because
its response broke the tool's output schema, may have taken effect. The run then
records `uncertain_writes` and keeps its private state, and a resume reconciles
the write as described below instead of sending it again. A final run's
checkpoint is rewritten as a scrubbed record of step statuses, checks,
capabilities, the run lock and linked agent runs, without secrets, arguments or
raw results. `consent.private.sqlite` is deleted. `result.json` records
`resumable` and `private_state` (`scrubbed` or `retained for resume`). Resuming
a final run repeats nothing: it records `resume_declined`, seals the report
again and returns the original exit code.

Resume requires the same scenario digest, driver, harness and model lock, and
the same server build (see [server identity](#server-identity)). Recorded
completion prevents replay; rotated credentials are retained. Pending writes are
reconciled using the scenario's read operation and expected response. Reconcile
expectations should bind to the intended operation/record, not merely a generic
“success” string. The runner does not guess service idempotency semantics.

If a write cannot be confirmed, it remains blocked and is not repeated. If an
embedded interaction was interrupted before its callback was recorded, the
runner conservatively blocks rather than repeating a possibly consequential
click. If a callback was recorded, it can be reconciled without replaying the
form. A read-only step with `callback_required: true` that was interrupted after
its completing read also reconciles on resume. An `.active` lock prevents
concurrent CLI resumes; after a process crash, verify the old process stopped
before removing a stale lock.

Each agent resume starts a fresh Inspect task from the host's durable workflow
state and safe observation, with the same scenario and model lock. Completed
actions are unavailable; pending writes still require reconciliation. A missing
user answer or uncertain write stops generation immediately. An already complete
workflow makes no model request on resume. Task limits apply per attempt.

## Observation versus intervention

`treatment` is explicit:

- `baseline`: the original resource and normal host adapter. Assertions inspect
  UI behavior and service results without changing the app.
- `observation`: may read named properties through `ui.observations`, in addition
  to ordinary UI assertions. Arbitrary scenario JavaScript is not executed.
- `workaround`: may load a workspace-local replacement through
  `ui.resource_file`. Original and rendered hashes and bridge identity are
  recorded. This tests a proposed repair, not original compatibility.

Observed and workaround runs retain their actual outcomes but cannot receive a
baseline compatibility pass. No missing fixture values are injected by assertion
code. The host does not patch application source strings to repair tests.

## Evidence and limits

Every run produces redacted `events.jsonl`, `result.json`, and a standalone
`report.html`, alongside its private checkpoint. Reports distinguish scripted
and agent-driven execution, treatment, capability stages, and layered assertions.
Agent attempts additionally write redacted native Inspect logs under
`agent/r-.../inspect/`; the result's `agent_runs` links every attempt. Raw logs
remain in memory until redacted export and are then removed. The public replay
seal covers the linked logs and artifacts as well as the parent report and events;
private checkpoints remain excluded.
Reports never claim actual-product calibration or independent backend verification
from service responses. Secure field values and screenshots are not recorded
automatically.

`compatibility_pass` is true only for a completed, scripted `baseline` run with
at least one scenario-authored check (`compatibility_scope: scripted baseline
runs only`). Completion without assertions, the automatic link check alone,
observation and workaround treatments, and agent runs do not qualify.

### Exit codes

The command follows the exit codes in spec §18:

| Exit | Meaning |
| --- | --- |
| 0 | Completed |
| 1 | Failed outcome: a failed assertion, a service rejection, a UI interaction failure or an agent error |
| 2 | Setup, infrastructure, integration or reporting failure |
| 3 | Blocked or unsupported: a missing reply or approval, an unconfirmed write, an interrupted UI step or run, a turn or wall limit, missing UI coverage or an observation gap |

`result.json` records `failure_class`, `failure_stage` and `resumable`. Before
`Surface report: ...`, the command prints one outcome line on stdout:

```text
Surface run failed (exit 1) at verify [assertion]: service assertion saved failed: expected "saved", actual "form_needed"
```

A completed run prints `Surface run completed (exit 0)`.

### Server identity

`run_lock.server` pins the server build: `server_info`, the negotiated
`protocol_version` and `protocol_era` (see
[protocol versions](#protocol-versions)), digests of the server's
instructions, capabilities and tool catalog (`tools_sha256`), `tool_count`,
and each UI step's resource URI and SHA-256 under `ui_resources`. `tools_digest_basis` states what the catalog digest
covers: `tools/list` as the pinned MCP client SDK parses it, with the tool
fields MCP defines, full input and output schemas and `_meta`. Unknown top-level
and annotation fields are dropped. The `server_catalog` event holds the full
catalog.

A resume against a changed server identity or catalog stops (exit 2); start a
new run to test the changed build. A behavioral rerun (`replay --mode rerun`)
may meet another build. Compare such runs with `diff --treatment /server`.

### Captures and redaction

Before a service response is recorded, the host registers its captured values
and every string it holds under a credential-like name, such as
`session_token` or `password_reset_url`, but not counts or types such as
`max_tokens` or `token_type`. Captured values must be strings: a capture that
holds a number stops the run (exit 2) and the response is withheld from
evidence, because evidence redaction matches strings only. Numbers from
`--secrets-file` used in `host_arguments` are masked in `host_action` events.

A value that appeared in evidence before it became a secret, such as a session
handle captured by a later step, stays in the local run files.
`private/late-secrets.private.json` keeps exports redacting it, and
`result.json` lists an observation gap. Public files in the run directory
(events, artifacts, Inspect logs, the result and the report) use evidence
redaction; the model's observation masks only credentials. Private files, such
as the checkpoint, are not redacted and are never exported. See
[redaction](sealed-event-provenance.md#redaction).

### Tests and isolation

Browser tests explicitly skip when the optional workspace browser tooling is
absent; skips are not passes.

```sh
.venv/bin/pytest tests/test_surface.py tests/test_surface_agent.py tests/test_surface_browser.py \
  tests/test_surface_p0_agent.py tests/test_surface_p0_mcp_apps.py tests/test_surface_p0_outcomes.py \
  tests/test_surface_p3_mcp_apps.py tests/test_surface_p3_endpoint_auth.py
```

The synthetic browser test requires permission to listen on loopback and launch
Chromium. The general host is not a network-isolated OS sandbox: HTTP requests
are constrained by transport/browser routing and the view's CSP, service
workers and WebSockets are disabled, and there are no agent shell tools. Use
the existing container lane when testing hostile shell execution or OS-level
isolation.
