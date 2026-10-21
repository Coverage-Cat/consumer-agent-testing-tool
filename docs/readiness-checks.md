# Readiness checks

`check` has four readiness suites. The shared surrogate suite checks CATT's
testing contract; the others compare published information with pinned sources.
None runs a model.

| Suite | TARGET | What it reads | Network | Label |
| --- | --- | --- | --- | --- |
| `surrogate` | An MCP endpoint URL, or a configured target with `base_url` | `initialize`, `tools/list`, optional `surrogate-readiness` manifest; actual native/CLI projection | The endpoint and its authorization server | `surrogate contract, not hosted-assistant compatibility` |
| `muse-guidelines` | An MCP endpoint URL, or a configured target with `base_url` | The MCP handshake (`server/discover` or `initialize`) and `tools/list`; an optional readiness manifest | The endpoint and its authorization server | `documented constraint, not admission` |
| `poppy` | A company's origin | `poppy.json` and the metadata it names; with `--manifest`, active probes | The company's hosts only | `draft 0.1; no production Muse evidence` |
| `gadget` | A local JSON or YAML file | A device's `link.register` message, with optional captured results and timings | None | `documented SDK limits, not admission (the Gadget SDK is not a developer platform)` |

A clean result is not an admission, an approval or proof of Muse compatibility. Every report
sets `compatibility_pass: false`. This page also covers the contract lane's
[MCP 2026-07-28 profile](#mcp-2026-07-28-contract-profile), which is generic MCP evidence, and
their behavior [under `--offline`](#offline-behavior).

Use `--suite surrogate --emulation-target muse|instinct` for either supported
surrogate. `--projection native|cli` selects its shared projection. The suite
checks catalog names, credential exposure, reviewed allowlists, schema syntax
and projection limits. It never calls a service tool. It accepts `--junit` and
`--summary-json`; offline mode is refused because the catalog must be read.
See the [shared contract](surrogate-contract.md#shared-readiness) for the
manifest, limitations and exit codes. `muse-guidelines` remains Muse-specific.

## Shared behavior

Each run writes a sealed evidence directory under `--output` (default `runs/`):
`guidelines-<id>`, `poppy-<id>` or `gadget-<id>`. It holds `result.json`, `events.jsonl`,
`report.html` and `evidence.sha256.json`. The command prints the directory on stdout. On
stderr it prints one outcome line with the finding counts, each failing rule and its subject,
and the evidence path. `report`, `export` and `replay` read these directories like other
sealed runs.

`result.json` records `lane: readiness`, the suite's `label`, `findings`, a `summary` of
counts by severity, `checks`, `run_lock` and `evidence_limits`. Each finding has a `rule`, a
`severity` (`fail`, `warn` or `info`; Poppy adds `blocked`), its `evidence`, a `basis`, and the
`quote` with its `source`. `checks` has one row per finding, plus one `no-finding` row for each
rule without a finding. A rule is never reported as `passed`. Only `fail` findings, and Poppy's
`blocked` ones, are `required`.

The suites run locally, with their own fixed interface. They refuse `--interface-profile`,
`--profile`, `--model-profile` and an `--execution` other than `local` (exit 2) rather than
ignore them. `poppy` and `gadget` also refuse `--endpoint`, `--auth-from`, `--auth-env`,
`--auth-placement`, `--auth-name` and `--client-profile`, and `gadget` refuses `--manifest`.
TARGET files, `--manifest` and `--output` must be inside the directory you run from.

| Exit | `muse-guidelines` | `poppy` | `gadget` |
| --- | --- | --- | --- |
| 0 | No `fail` finding | No `fail` or `blocked` finding | No `fail` finding |
| 1 | A `fail` finding | A `fail` finding | A `fail` finding |
| 2 | Setup or reporting error | Setup or reporting error | The document cannot be read, or reporting failed |
| 3 | The client profile cannot reach the server, or `--offline` | Blocked, with no `fail` finding, or `--offline` | `--offline` without the macOS kernel boundary |

`--drift-check` has its own exit codes; see [Policy drift](#policy-drift).

## Muse connector guidelines

`check --suite muse-guidelines` reviews one MCP server's catalog against the Muse connector
guidelines that Meta publishes at [platform.muse.ai/docs](https://platform.muse.ai/docs),
pinned on 15 October 2026 (basis: documented). It lists tools and never calls one. Meta reviews
each connector in three stages, and meeting the guidelines does not guarantee approval
(guidelines section 6). A finding is a reason to look.

This example uses the loopback fixture's `hidden-write` variant. Its `get_quote` is annotated
read-only, but its description says rates are "secured instantly with the card on file":

```sh
.venv/bin/python -m consumer_agent_testing_tool.connector_fixture --port 8765 --variant hidden-write --auth none &
.venv/bin/consumer-agent-testing-tool check http://127.0.0.1:8765/mcp --suite muse-guidelines \
  --manifest examples/connector/readiness.yaml
```

The run exits 1 and names `3.2-hidden-write (get_quote)`. For your own server:

```sh
.venv/bin/consumer-agent-testing-tool check https://example.com/mcp --suite muse-guidelines --auth-env EXAMPLE_API_KEY
```

The endpoint is TARGET as a URL, `--endpoint URL`, or the `base_url` of a configured target. An
`api_key` target links with its `auth.env` as a bearer header. `--auth-env` supplies an API
key, placed with `--auth-placement` and `--auth-name`. `--auth-from SCENARIO` uses the auth
block of a connector-v1 scenario's first connector, OAuth included. OAuth links are kept in
`.cache/connector-links`, which `connector-run` shares.

The client profile is `generic-sdk` unless `--client-profile muse-template-unofficial-2026-10`
selects the unofficial template (basis: unofficial). What only that template refuses is a limit
of the profile (exit 3), never a guidelines finding. The connector lane describes
[linking](connector-lane.md#linking) and [client profiles](connector-lane.md#client-profiles).

### Rules

| Rule | Section | Finding |
| --- | --- | --- |
| `3.2-hidden-write` | 3.2 | `fail`: a tool presented as a read (declared `read`, annotated `readOnlyHint: true`, or named with a read verb) whose name, description or operation enum states a consequential action, such as a send, share, payment, booking, deletion or cancellation. `warn`: the lane's classifier sees a hidden-write signal, or a tool declared `write` looks consequential |
| `3.2-combined-read-write` | 3.2 | `fail`: a tool annotated `readOnlyHint: true` or declared `read` whose operation enum offers both reads and writes |
| `3.2-steering` | 3.2 | `fail` or `warn`: tool text, schema descriptions and titles, the `initialize` instructions or sampled responses tell the agent to ignore its rules, steer away from competitors, force recommendations, conceal commercial interests, take unrequested actions or access unrelated data |
| `4.4-credential-exposure` | 4.4 | `fail`: a credential shape, a payment card number, the linked credential or a value under a secret-named field, in the listing, the instructions or sampled responses |
| `4.6-money-movement` | 4.6 | `fail`: a tool not presented as a read that looks like money movement or a trade order |
| `5.4-doc-topics` | 5.4 | Manifest. `fail`: no documentation URL, or topics missing. `warn`: a URL that is not `https://` |
| `5.5-test-account` | 5.5 | Manifest. `fail`: no sign-in instructions, required permissions or representative test data, or no commitment to keep the account accessible throughout review |
| `5.5-no-charge-checkout` | 5.5 | Manifest. `fail`: declared transaction tools without a no-charge test path. `warn`: tools that look like transactions are not declared |
| `5.6-tool-classes` | 5.6 | Manifest. `fail`: a tool declared `read` that is annotated `readOnlyHint: false` or `destructiveHint: true`. `warn`: unclassified tools. `info`: declared tools that are not listed |
| `3.1-read-only-oauth` | 3.1 | `info`: OAuth with no read-only scope set and no reason given, in the manifest or the link |

Detection is lexical and English only. A scan stops after 5,000 values or 12 levels and says so
in an `info` finding. Without `--manifest`, the four manifest rules are `not-evaluated` rows,
never a pass. Findings give a credential's kind and length, never its value.

### Readiness manifest

`--manifest` takes the developer's declarations for the submission (guidelines section 5), as
[JSON or YAML](data-formats.md). The check verifies that each declaration is present; its truth
stays with Meta's reviewers. [`examples/connector/readiness.yaml`](../examples/connector/readiness.yaml)
is a complete example.

| Field | Content |
| --- | --- |
| `version`, `kind` | `1` and `muse-readiness` |
| `connector` | An optional name |
| `documentation` | `url`, and `covers`: the section 5.4 topics `setup-instructions`, `inputs-and-outputs`, `read-write-classifications`, `sensitive-designations`, `permissions`, `side-effects`, `status-handling`, `errors` and `rate-limits` |
| `authentication` | `method` (`oauth`, `api-key`, `none` or `other`), `scopes`, `read_only_scopes`, `read_only_not_possible_reason` and `credentials_environment` (`test` or `production`) |
| `test_account` | `sign_in_instructions`, `required_permissions`, `representative_test_data`, `limits` and `accessible_throughout_review`. Credentials do not belong here: any other field, such as `password`, fails validation (exit 2) |
| `transactions` | `has_transaction_tools`, `tools` and `no_charge_test_path` |
| `classes` | Each tool's documented class: `read`, `write` or `sensitive-write` |
| `sample_responses` | `tool` and `result` pairs: `tools/call` results captured on the test account, reviewed for steering and credentials |

### Classification sheet

`classification_sheet` has one row per listed tool: `tool`, `suggested`, `basis`, `reasons`
and, when the manifest declares a class, `declared`. `suggested` comes from the connector lane's
own classifier (`permissions.classify_tool`) without the declaration, so a disagreement stays
visible. `basis` is `annotation`, `heuristic`, `default` or `combined-operations`. A tool whose
schema enumerates both reads and writes is a write (section 3.2). The rows follow the shape of
the third-party MCPJam Muse readiness sheet
([`classification.ts` at `da3b8ac`](https://github.com/MCPJam/inspector/blob/da3b8ac30d2cc914be838637b5422bd66f6dd514/sdk/src/muse-readiness/classification.ts)).
`classification_sheet_markdown` renders them as a Tool, Classification and Basis table for the
tool documentation that section 5.6 asks for.

### Policy pin

The quotes come from
`src/consumer_agent_testing_tool/data/policy/muse-connector-guidelines-2026-10-15.json`:

| Field | Value |
| --- | --- |
| `source` | `https://platform.muse.ai/docs` |
| `terms_source` | `https://platform.muse.ai/terms`, with `terms_effective` 2026-09-18 |
| `fetched` | 2026-10-15 |
| `extract_sha256_32` | `60628e5a1c6199b8635a161b7f5a8aaa`, over 13,929 characters |
| `terms_extract_sha256_32` | `c627bd546cfde9bdbcad01de593ca721` |
| `method` | `MCPJam extractText + sha256[0:32]` |
| `sections` | Verbatim quotes by guideline section |

The hash covers a page's visible text, extracted as MCPJam's `policy-manifest-sync.mjs` does
(ported under Apache-2.0; see `method_source`), so a change to markup alone does not move it.
Each report records the pin as `policy`; `run_lock.policy` and `run_lock.policy_sha256`
identify the pin file.

### Policy drift

```sh
.venv/bin/consumer-agent-testing-tool check --suite muse-guidelines --drift-check
```

`--drift-check` fetches the two pinned pages, hashes them with the pin's method and prints JSON.
It is opt-in, and it is the only part of the suite that fetches the guidelines. It needs no
TARGET and writes no evidence directory. It follows redirects, uses a 20-second timeout and
reads at most 5,000,000 bytes per page. Nothing schedules it.

| Exit | `status` | Meaning |
| --- | --- | --- |
| 0 | `ok` | Both pages match the pin |
| 1 | `drift` | A page's hash differs, or the pin has none for it; `drifted` names the pages |
| 2 | `error` | A page was not fetched; `failures` says why. A failed page is never hashed, and `error` outranks `drift` |

`pages` gives each page's `url`, `final_url`, `revision`, `chars`, `pinned`, `etag`,
`last_modified` and `drifted`. On drift, `action` says to re-audit the rules that cite the
drifted page before trusting a check made against the pin. `--drift-check` with any other suite
exits 2.

### Limits

The suite is static. Behavior on a test account, such as approvals, duplicates and honest
outcomes, needs `connector-run`; see [Connector lane](connector-lane.md). `connector-run` also
applies these rules, without a manifest, to each catalog and to the tool results the agent
received. It records them as `guideline_findings`: evidence, not an outcome check (see
[Every check](connector-lane.md#every-check)).

## Personal Agent Protocol draft 0.1

`check --suite poppy` plays a personal agent against a company that publishes
`/.well-known/poppy.json`. Its rules quote Personal Agent Protocol draft 0.1, pinned at commit
`e910c866` of
[personal-agent-protocol/personal-agent-protocol](https://github.com/personal-agent-protocol/personal-agent-protocol)
with SHA-256 digests of the draft's two sources, retrieved 2026-10-16. Findings have the basis
`documented` against that draft, not against Muse documentation. Muse's only evidence is a
Sierra demo post ([sierra.ai/blog/poppy](https://sierra.ai/blog/poppy), 9 October 2026), which
the report's `muse` field records with the basis `unofficial`. Hence the label
`draft 0.1; no production Muse evidence`. A later draft needs the rules re-audited.

```sh
.venv/bin/consumer-agent-testing-tool check https://shop.example --suite poppy
.venv/bin/consumer-agent-testing-tool check https://shop.example --suite poppy --manifest poppy-probes.yaml
```

Replace `https://shop.example` with the company's origin: `https://host[:port]`, with no path,
query or user info. A bare `shop.example` means `https://shop.example`. Plain HTTP is accepted
on loopback only, for a test deployment.

### Discovery

Without `--manifest`, the suite only reads documents:

1. `/.well-known/poppy.json` on the origin, following up to five redirects, each to HTTPS
   (draft sections 3, 3.1 and 3.3, with 4.4 for scopes and 6 for APIs).
2. The issuer's OAuth Authorization Server Metadata (RFC 8414; section 3.2). Its `issuer` must
   equal `auth.issuer`, and `poppy_domains` must include `organization.domain`. The token and
   revocation endpoints, and those the sign-in types need, must be HTTPS URLs.
   `dpop_signing_alg_values_supported` must include `ES256`.
3. For each `apis` entry of type `mcp`, its protected resource metadata (RFC 9728), whose
   `authorization_servers` must include `auth.issuer` (section 6).

A `protocol_version` with another major version is `blocked`, because a personal agent must not
use the document. A newer minor version is checked against draft 0.1, with an `info` finding.

### Probes

With `--manifest`, the suite then acts on the company as one personal agent. The probes start
Sessions, sign the declared test account in, confirm the declared operation (which performs it)
and revoke every Account Token they obtained. Use a company and a test account that may be used
this way. The groups run in this order:

| Group | Sections | The company must |
| --- | --- | --- |
| Token endpoint | 4.1 to 4.3 | Start a signed-out, DPoP-bound Session from a jwt-bearer assertion, and renew it. Refuse a replayed assertion (400 `invalid_grant`), a replayed proof (400 `invalid_dpop_proof`) and a client assertion addressed to the token endpoint (401 `invalid_client`). With the loopback identity, also fetch the client metadata, and refuse a mismatched `client_id`, an off-site key set and an unknown key (401 `invalid_client`) |
| Token use | 4.3, 6, 7.13, Operations 9 | At an unknown operation or conversation, answer a valid token with 404, and no token with 401 `invalid_token` and a DPoP challenge. Refuse the Bearer scheme, a missing or mismatched proof (401 `invalid_dpop_proof`) and a token in the URL |
| Browser join | 5 | When `web.browser_session_endpoint` is listed, answer 303 to `return_to` with a cookie that is `Secure`, `HttpOnly` and `SameSite=Lax` or `None`. Refuse a replayed, mistyped, long-lived or misaddressed assertion and a foreign `return_to` with 400 and no cookie |
| Account | 4.7, 4.8, 6 | Answer `requests.sign_in_required` with 403 `sign_in_required` when signed out, and serve it when signed in. Complete Mediated Sign-In without returning credentials or granting extra scopes. Answer `requests.insufficient_scope` with 403 `insufficient_scope`, naming the scope. Sign the Session in again with the Account Token |
| Operations | Operations 3 to 7 | Propose without performing. Answer a stale revision with 409 `terms_changed`, and `standing_permission` with 403 `user_approval_required` when the operation needs the user. Return the same operation for three confirms, two of them concurrent, and perform it at most once, as `effects` shows. Return a final operation unchanged on cancel |
| Sign-out | 4.8 | Revoke the Account Token (RFC 7009, 200), then refuse it (400 `invalid_grant`) |

The probes do not run when a personal agent must not use the document: an `organization.domain`
that is not the serving host, issuer metadata that is missing or does not match (section 3.2),
or another major version. An `info` finding says so. When discovery yields no `poppy.json`, or
no issuer metadata for another reason, the probes are `blocked`. `probes_run` lists every probe
that ran, `http_exchanges` counts the requests, and an `http_exchange` event records each
request with its response.

### Probe manifest

```yaml
version: 1
kind: poppy-probes
agent:
  identity: hosted
  client_id: https://agent.example/poppy/client.json
  private_key_env: POPPY_AGENT_KEY
  key_id: probe-1
sign_in:
  type: mediated
  scope: poppy:read
  credentials_env: {email: POPPY_TEST_EMAIL, password: POPPY_TEST_PASSWORD}
requests:
  sign_in_required: {method: GET, url: "https://shop.example/api/orders"}
  insufficient_scope:
    method: POST
    url: https://shop.example/api/orders/ord_7Hk2/exchanges
    json: {item_id: itm_4Qa, replacement_sku: m-insulated}
operations:
  propose:
    method: POST
    url: https://shop.example/api/orders/ord_7Hk2/exchanges
    json: {item_id: itm_4Qa, replacement_sku: m-insulated}
  scope: poppy:read poppy:write
  effects: {url: "https://shop.example/test/effects", pointer: /performed/exchanges}
browser:
  return_to: https://shop.example/orders
```

| Field | Content |
| --- | --- |
| `version`, `kind` | `1` and `poppy-probes` |
| `agent.identity` | `loopback` (default) or `hosted` |
| `agent.client_id`, `agent.private_key_env`, `agent.key_id` | Hosted only: the HTTPS URL of a client metadata document you publish, the environment variable that holds its P-256 private key (PEM or JWK), and the key's ID in the published key set |
| `user_id` | The User ID in the assertions: 8 to 128 letters, digits, `_` or `-`. Default: a new random value per run |
| `sign_in` | A Mediated Sign-In test account: `type: mediated`, `credentials_env` (the company's sign-in field names, each mapped to an environment variable) and `scope` |
| `requests.sign_in_required` | A request that needs a signed-in token |
| `requests.insufficient_scope` | A request that needs a scope beyond `sign_in.scope` |
| `operations.propose` | A request that proposes exactly one operation |
| `operations.scope` | The scopes to sign in with for the operation; default `sign_in.scope` |
| `operations.approved_by` | `user` (default) or `standing_permission` |
| `operations.effects` | A test oracle: `url` answers GET with JSON that holds an integer count of performed actions at the JSON pointer `pointer` |
| `browser.return_to` | Where the browser join must redirect; default the root of the browser session endpoint's origin |

A request has a `method` (`GET` by default, or `POST`, `PUT`, `PATCH` or `DELETE`), a `url` and
an optional `json` body. Without `sign_in`, the operation probes use a signed-out Session.

The `loopback` identity serves the checker's own client metadata and key set on 127.0.0.1 for
the run, with an `http://127.0.0.1` `client_id`. Only a loopback test deployment can fetch them,
and it must accept an http `client_id` there, which production companies must refuse. Against
any other company, a loopback identity is refused (exit 3). A real company needs `hosted`. The
checker then fetches your client metadata document, checks it against section 4.1, and requires
its key set to hold only public keys, including the public half of your key.

### Hosts and credentials

The suite contacts only these hosts:

- the TARGET host and its subdomains (for a `www.` host, the domain without `www.` as well);
- `organization.domain` and its subdomains, only when that domain served `poppy.json`; a domain
  the document merely claims adds nothing;
- the issuer's host, without its subdomains;
- hosts that the probe manifest names: its request and `effects` URLs and a hosted `client_id`.

A request to any other host, or a request that is not HTTPS, is refused before it is sent, and
the run is blocked (exit 3) when nothing failed. Plain HTTP is accepted only when TARGET is on
loopback, which keeps every request on loopback. A host you did not name must not resolve to
an internal address unless TARGET is itself internal. Requests ignore proxy environment
variables and follow no redirects outside `poppy.json` discovery. Each request has a 20-second
timeout, and each response may be at most 1 MiB, measured after a gzip, deflate or zstd body is
decoded. A larger response, or a body that does not decode in its declared encoding, is refused.

Credentials come only from the environment variables that the manifest names. An unset variable
stops the run with exit 2 when a probe first needs it, after the earlier probes have run.
Credential values, tokens, assertions and proofs are registered as secrets and masked in
evidence. Each `http_exchange` keeps request header names and cookie attributes, not their
values.

### Blocked findings and limits

A `blocked` finding makes the run exit 3 when nothing failed. Causes include another major
version, a host outside the list above, a sign-in that needs a person (the company asked for a
one-time code), and a manifest that declares what `poppy.json` lacks, such as a mediated account
without `auth.mediated.endpoint` or an operation without the operations extension.

`evidence_limits` names two departures from a literal reading of the draft. The checker's HTTP
client posts the browser join form; no browser navigates. Confirms send `approved_by` for a test
account, but no person approved the operation. The token-in-URL probe also sends a signed-out
test Session Token in a query string on purpose.

Not covered: Direct and Device Sign-In, which need a person; conversations beyond
`conversation_not_found` (section 7); MCP API sessions with Bearer Session Tokens, of which
discovery checks only the protected resource metadata; and payments and push, which draft 0.1
leaves open.

## Gadget SDK limits

`check --suite gadget` checks a device's `link.register` message, and any captured `link.result`
messages and invoke timings, against the limits that the official Muse Gadget SDK documents:
[facebookincubator/muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk) at
commit `5f26edab`, retrieved 2026-10-16. It reads one local file and opens no connection.
Nothing is paired, connected or invoked, and the Noise transport and the VM are not exercised.
The SDK's token terms ([gadgets.muse.ai/sdk-terms](https://gadgets.muse.ai/sdk-terms)) say it is
not a developer platform. The suite is therefore optional, and its label says so.

TARGET is one file inside the directory you run from, up to 16 MiB, in one of three shapes:

- A `link.register` message: `type: req`, an `id`, `method: link.register` and `params`.
- Its `params` alone: an object with `commands_v2`. The suite adds an envelope whose fixed `id`
  has the length of the UUID the Linux client sends, so the measured size is the size sent. The
  report's `device.envelope_assembled` is then `true`.
- A `gadget-readiness` document: `version: 1`, `kind: gadget-readiness`, `register` (the
  message), and optional `results` and `invokes`. Each result is the `link.result` `message` as
  the device sent it, with an optional `command`. Each invoke is a `command`, an optional
  `received_ms`, `started_ms` and `finished_ms`, in milliseconds on one clock.

For example, save this as `device.json`:

```json
{"node_id": "homelink-a1b2c3", "display_name": "garage-pi", "platform": "linux", "version": "0.4.1",
 "device_family": "homehub", "model_id": "linux", "is_wakeup_supported": false,
 "commands_v2": {
   "device.health": {"description": "Report uptime, load, memory and disk usage.",
                     "required": {}, "optional": {}}}}
```

```sh
.venv/bin/consumer-agent-testing-tool check device.json --suite gadget
```

Results and timings are the developer's own captures. The suite does not verify them against
the device.

### Limits table

`limits` lists each documented limit, what the document `measured`, and whether the limit
`applies` to the device's platform:

| `id` | Limit | Applies | SDK source |
| --- | --- | --- | --- |
| `register-bytes-esp32` | 8,191 bytes: the firmware prints `link.register` into an 8 KB buffer that includes the terminating NUL | ESP32 only; elsewhere `applies: false`, with a note | `esp32/AGENTS.md` line 541 |
| `message-bytes` | 262,144 bytes (256 KB) per message from the device | All | `linux/AGENTS.md` line 164 |
| `stream-output-bytes` | 98,304 bytes (96 KB) of JSON text per output stream | All | `linux/src/musegadget/executor.py` line 49 |
| `default-timeout` | 30,000 ms, unless the command sets `timeout_ms` | All | `linux/AGENTS.md` line 171 |
| `concurrent-invokes` | 4 at once: the official Linux client's cap | All; the VM's own limit is undocumented | `linux/src/musegadget/link_client.py` line 60 |

Sizes are compact JSON as each platform prints it: the ESP32 firmware keeps UTF-8, and the Linux
client escapes non-ASCII characters. Platforms other than ESP32 are bound only by the 256 KB
message limit.

### Gadget rules

| Rule | Basis | Finding |
| --- | --- | --- |
| `register.envelope` | documented | `fail`: not `type: req` with a nonempty `id`, `method: link.register` and a `params` object. `info`: envelope keys no official client sends |
| `register.fields` | assumed | `fail`: `node_id`, `platform`, `device_family` or `commands_v2` is missing, or a field has the wrong type. `warn`: another field the official clients send is missing, or `metadata` is not a map of strings. `info`: unknown `params` keys |
| `register.size` | documented | `fail`: above 262,144 bytes |
| `register.size.esp32` | documented | ESP32 only. `fail`: above 8,191 bytes. `warn`: within 10% of that |
| `register.node-id` | documented | `warn`: not `homelink-` and six lowercase hex digits |
| `platform.official` | assumed | `warn`: a platform other than `linux` or `esp32` |
| `family.link` | documented | `fail`: family `link` on a device that is not ESP32 |
| `family.platform` | documented | `warn`: a `linux` device whose family is not `homehub` |
| `ota.advertised` | documented | `fail`: a device that is not ESP32 advertises `device.ota` |
| `commands.present` | assumed | `warn`: an empty `commands_v2` |
| `command.name` | assumed | `warn`: not a dotted lowercase name |
| `command.management` | documented | `fail`: `device.list_vms`, `device.set_vm`, `device.reset_vm` or `device.unpair` in `commands_v2` |
| `command.description` | documented | `fail`: no description. `warn`: under 15 characters, or a parameter without a description |
| `command.spec` | documented | `fail`: a spec, `required` or `optional` value of the wrong shape, a parameter that is both required and optional, or one without a JSON type. `warn`: unknown keys, a missing `required` or `optional` map, or a `number`, `object` or `array` parameter |
| `command.timeout` | documented | `fail`: `timeout_ms` is not a positive integer |
| `command.head-of-line` | assumed | `info`: some commands may run longer than 30 seconds, so four of them can hold every invoke slot while a default-timeout command waits |
| `result.envelope` | documented | `fail`: a malformed `link.result`, an `ok` result with neither `payload` nor `payload_json`, a `payload_json` that is not JSON, or a failure without an error message. `info`: keys that are dropped |
| `result.size` | documented | `fail`: above 262,144 bytes |
| `result.output` | documented | `fail`: `stdout` or `stderr` above 98,304 bytes as JSON text |
| `invoke.command` | documented | `fail`: an invoked command that is not in `commands_v2` |
| `invoke.timeout` | documented | `fail`: an answer after the command's timeout, counted from `received_ms` when it is given |
| `invoke.concurrency` | documented | `warn`: more than four invokes ran at once |

`documented` rules quote the pinned SDK. `assumed` rules infer from what both official clients
do where the SDK states no requirement. Two limits warn rather than fail. More than four
concurrent invokes is a warning because the VM's own limit is undocumented. An ESP32
registration within 10% of its buffer is a warning because one more command or a longer
description stops registration.

The report also has a `device` summary (`platform`, `device_family`, `model_id`, `node_id`,
`version`, `commands`, `register_bytes`, `encoder`, `envelope_assembled` and `timeouts_ms`), and
`run_lock` records `sdk_commit`, `rules_sha256` and `document_sha256`. Exit 2 covers a file that
is missing, outside the workspace, larger than 16 MiB, of another shape or invalid, and invoke
times out of order.

## MCP 2026-07-28 contract profile

`mcp-contract-2026-07-28` lets the contract lane test a server at MCP revision 2026-07-28 with
`check --suite contract`, `check --suite static` and `inspect`. The profile is labeled
`protocol-tested`. Its evidence is generic MCP evidence, not Muse evidence. The connector lane's
unofficial Muse template offers only earlier revisions (basis: unofficial; see
[Client profiles](connector-lane.md#client-profiles)). The other contract profiles,
`mcp-contract-2025-11-25`, `mcp-contract-2025-06-18` and `mcp-contract-2025-03-26`, negotiate
with `initialize`. `mcp-contract-2025-11-25` is the default unless `defaults.interface_profile`
names another contract profile. The MCP Apps host in `surface-run` negotiates on its own; see
[MCP Apps host](surface-workflows.md#mcp-apps-host).

With the scripted configuration from [Setup](../README.md#setup)
(`init --provider none --config scripted.yaml`), `booking-service` is the reference fixture
over stdio:

```sh
.venv/bin/consumer-agent-testing-tool check booking-service --config scripted.yaml --suite contract \
  --interface-profile mcp-contract-2026-07-28
.venv/bin/consumer-agent-testing-tool inspect booking-service --config scripted.yaml \
  --interface-profile mcp-contract-2026-07-28
```

Each command prints its run directory, `runs/r-<id>`, whose `manifest.json` holds the result. A
server of your own needs a target such as this one, with read or draft `contract_cases`:

```yaml
targets:
  my-server:
    kind: mcp
    transport: streamable-http
    base_url: https://mcp.example.com/mcp
    permitted_destinations: [https://mcp.example.com/mcp]
    contract_cases:
      - tool: search_rooms
        arguments: {check_in: "2026-12-04", check_out: "2026-12-06", adults: 2}
        effect: read
```

Without `contract_cases`, the required `operation-contracts` check is `unsupported` and the run
is blocked (exit 3).

### Negotiation and era

The lane sends `server/discover` at 2026-07-28, then stamps every request with its version.
There is no `initialize` handshake and no session. `manifest.json` records:

| Field | Content |
| --- | --- |
| `run_lock.negotiated_protocol_revision`, `run_lock.negotiated_protocol_era` | `2026-07-28` and `modern`; the other profiles record `handshake` |
| `run_lock.server_info` | The server's identity, or null: a 2026-07-28 server may stay anonymous |
| `protocol_negotiation` | `offered`, `profile_revision`, `method` (`server/discover`), `negotiated`, `era` and `supported_versions` |
| `protocol_coverage` | `era`, `negotiation`, `tested`, `not_applicable` and `unverified` |
| `http_transport` | For each HTTP exchange: `method`, `status`, the request's `Mcp-Protocol-Version` and `Mcp-Method` headers as `protocol_version` and `mcp_method`, and `session_issued` |

When the server negotiates another revision, the run is blocked (exit 3) and the message names
the profile to select. When `server/discover` fails, the lane tries `initialize` only to name the
era the server speaks. It records `discover_error` and `fallback: initialize` in
`protocol_negotiation`, and the run is blocked. A handshake profile that meets a server refusing
`initialize` with error -32022 records the `refusal` and points to `mcp-contract-2026-07-28`.

### Checks at 2026-07-28

| Check | At 2026-07-28 |
| --- | --- |
| `ping` | Not run: 2026-07-28 removed ping, and `protocol_coverage.not_applicable` says so |
| `unsupported-version-rejection` | `server/discover` at the unassigned revision 2099-01-01 must be refused with error -32022, whose `supported` list includes the negotiated revision. Any other answer, such as a bare HTTP 400, fails (exit 1) |
| `stateless-http` | Streamable HTTP only. `passed` when no `Mcp-Session-Id` was issued; `warning` when the server issued one, which 2026-era clients ignore. `http-session-termination` and `expired-http-session` do not apply |
| `request-cancellation` | Over Streamable HTTP, the client cancels by closing the response. A target that keeps processing gets a `warning`, not a failure, because the revision says processing SHOULD cease. The reference fixture over HTTP gets this warning by design: the pinned SDK server stops a handler on disconnect only in SSE mode |
| `requests-after-cancellation` | A `tools/list` after cancellation, in place of the handshake profiles' `session-after-cancellation` ping |
| `upstream-conformance` | `unsupported`, with `required: false` and `gap: upstream`. It does not block the run; see [Conformance](#conformance) |

Warnings never change the exit code. The cancellation, deadline and disconnect probes run only
against the reference fixture; for other targets, `protocol_coverage.unverified` lists them.

### Conformance

The pinned upstream suite, `@modelcontextprotocol/conformance` 0.1.16, defines server scenarios
for 2025-06-18 and 2025-11-25 only. The lane records 2026-07-28 as an upstream gap and still
runs its direct SDK checks, and `protocol_coverage.unverified` names the missing scenarios. Only
the unpinned 0.2.0-alpha line has a 2026-07-28 requirement set; pinning an alpha is a release
decision.

## Offline behavior

| Command | With `--offline` |
| --- | --- |
| `check --suite muse-guidelines`, with or without `--drift-check` | Refused (exit 3) before any configuration is read. No evidence is written |
| `check --suite poppy` | Refused (exit 3) before any configuration is read. No evidence is written |
| `check --suite gadget` | Runs in a macOS kernel sandbox that denies every outbound connection, loopback included, and reads no configuration. The report records the policy in `run_lock.offline_policy`, and `locations.egress` reads `kernel-enforced default-deny coordinator` |
| `check --suite contract` and `inspect`, with any contract profile | Loopback targets only; a remote target is refused (exit 3). The sandbox allows the target's port and, for `check --suite contract`, one reserved loopback port for the reference fixture |

Without `--offline`, the gadget suite opens no connection either; `--offline` makes the kernel
enforce that and records the boundary. The sandbox needs macOS `sandbox-exec`; elsewhere
`--offline` is refused (exit 3). `inspect --offline` of the reference fixture over Streamable
HTTP fails (exit 2), because only `check --suite contract` reserves a loopback port for it.
