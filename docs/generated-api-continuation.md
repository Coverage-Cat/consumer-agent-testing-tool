# Generated API continuation

[Pending public receipts](public-receipt-polling.md) can be polled through the
native lookup tool with bounded reads and retained response reconstruction.
The source's native output schema must represent pending and completed values.

FastMCP-generated API trials support `--resumable` and `--resume` through the
same Inspect journal and [retained developer-state lease](developer-api-continuation.md)
as curated trials. The developer's private adapter must preserve state, retire
old authority and enforce the original expiry independently of the host.

Use the same scenario, target descriptor, source OpenAPI, inference profile and
implementation. Resume checks the private original mapping before model preflight,
then regenerates the actual native catalog before reacquiring the lease. A changed
catalog fails the continuation contract. Existing owner locks, deadlines and
cumulative budgets apply.

After the agent's final response, pending or unavailable backend observations
keep the retained trial open for [outcome verification](developer-api-continuation.md#verify-a-deferred-outcome).
The same resume command checks the independent oracle without another model
response or public tool dispatch. Native catalog identity is still checked.

## Native result restoration

The host retains generated request bytes before asking for approval. These
records are private and contain no injected target bearer credential. If HTTP
returns, the host saves its response bytes and permitted headers before asking
FastMCP to convert the response into its native MCP result.

- A saved native result is delivered without another dispatch.
- If native conversion was interrupted, a fresh worker reproduces the original
  serialization using its network-denied mock transport. The host requires the
  request to match, then supplies the retained response through native FastMCP
  conversion. No original target request is sent.
- If a write response is missing or uncertain, a declared public receipt lookup
  is required. A missing lookup/receipt or mismatched identity stops continuation.
- An interrupted proposal that never reached dispatch is retired. The agent
  must reread state and propose under fresh approval.

The host neither retains the old worker's in-memory task nor fabricates a native
result from a backend assertion. Invalid native output still fails conversion.
The independent private oracle continues to determine business outcomes.

## Public receipt configuration

Add `recovery` under the target's `openapi` settings. Names here are native
generated tool names, as shown by inspection:

```yaml
openapi:
  path: openapi/inventory.json
  adapter: fastmcp-generated
  operations:
    buyItem: commit
    lookupReceipt: read
  recovery:
    buyItem:
      lookup_tool: lookupReceipt
      arguments:
        key: key
```

Every lookup input maps to a required original argument. The lookup must be
declared read-only and return a native `structuredContent` receipt with exactly
the outer identity fields documented for curated receipts:

```json
{
  "run_id": "original-run",
  "task_id": "original-task",
  "principal": "original-principal",
  "operation": "buyItem",
  "arguments_sha256": "consumer_agent_testing_tool.serialization.digest(original_arguments)",
  "response": {
    "status": 200,
    "body_base64": "eyJzdGF0dXMiOiJjb21wbGV0ZWQifQ==",
    "headers": {"content-type": "application/json"}
  }
}
```

The response differs from the curated receipt's parsed JSON body: native
conversion needs exact body bytes and permitted headers. It must represent
the original successful response, recorded atomically with its outcome.
Private headers, invalid base64, oversized bodies and wrong run/task/principal/
operation/argument identities are rejected. Source, serialized request, native
result and independent backend observations remain separate.

Encoded bodies are excluded from normal text exports because ordinary redaction
cannot inspect encoded personal data reliably. Original response bytes remain
only in explicitly retained private checkpoints. Public evidence records the
lookup and native reconstruction; exported results use normal field/text redaction.

## Verification and limits

Tests use actual pinned workers, local HTTP/Unix services and independent SQLite
state. Process-exit cases stop immediately before and after the HTTP checkpoint:
the former requires one public receipt lookup, while the latter reconstructs
the native result without target traffic. Other tests cover interrupted approval,
catalog/source changes, missing or wrong receipts and export redaction.
Native schema diagnostics preserve the failed validator and schema path without
quoting the private offending instance. Typed private response values are also
registered before conversion, including on the ordinary dispatch path.

The installed smoke prelaunches fixed workers before sealing its macOS
coordinator, because Seatbelt cannot be initialized inside an existing sandbox.
It is an installation proof, not a general offline generated-CLI qualification.
Public pending receipts use the explicit [polling contract](public-receipt-polling.md).
Broader target/browser OS isolation, other asynchronous job contracts, arbitrary
portal/native checkpoint restoration and actual Muse calibration remain separate
work.
