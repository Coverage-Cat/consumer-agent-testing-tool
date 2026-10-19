> **Behavior update, 16 October 2026:** The run recorded below cannot be
> resumed, because the host has changed since and a resume requires the same
> harness. Retest with a new run. If its submission response is rejected again,
> it fails (exit 1) but stays resumable, and resuming it reconciles the
> submission as described below. See
> [resume and uncertain outcomes](surface-workflows.md#resume-and-uncertain-outcomes).

# Coverage Cat sandbox checkout example

The extended scenario is implemented, but the normal host's live run is blocked
by Coverage Cat's advertised output schema. This does not invalidate the passing
draft-only quickstart or the tool's scoped release audit. It does prevent claiming
that the full checkout example currently passes against Coverage Cat.

Use the commands and reply fixtures in the
[README](../README.md#extend-through-submission-and-sandbox-checkout).
The original draft example remains unchanged.

## What the example exercises

| Stage | Expected evidence |
| --- | --- |
| Prefill and review | Sandbox marker, matching intake identity, ready-for-submission status, synthetic review confirmation |
| Submit | Submitted application, synthetic Monoline $2M offer at $389 |
| Select | Explicit test selection, soft-credit consent fixture, declined UM/UIM, carrier answers, ready-to-finalize status |
| Bind follow-up | Both home and auto declarations requested |
| Attach | Two generated PDFs acknowledged by filename and type |
| Document review | Pending-review state, 60-second polling interval, explicit pause/resume |
| Payment preparation | Payment-needed state, test environment, collection-ready response, name/email/address defaults in private setup metadata |
| Mock callback | Sandbox SetupIntent plus mock payment-method ID; server reports payment method on file and waiting on carrier |

All seven tools use the default consumer umbrella transport. The host fixes
`sandbox: true` on the first prefill and injects the same intake identity and
latest captured access token on follow-ups. Service checks require the sandbox
marker before subsequent steps. The payment callback input schema rejects a
SetupIntent without the `seti_sandbox_` prefix and rejects any payment-method ID
other than the named synthetic fixture.

The reply files contain simulated shopper approvals. Missing review input pauses
before submission, mismatched selection fixtures block selection, and a declined
payment fixture blocks the callback. These are scenario checks, not a replacement
for collecting and binding a real customer's transactional consent.

## Live failure, 15 October 2026

The normal scripted run completed prefill, follow-up, and the synthetic review
step. `consumer_intake_patch` then returned a quote that the MCP SDK rejected:

- `/offers/0` includes `choice_guidance`, `display_notes`, `minimum_requirements`,
  and `rank`, but its declared output schema disallows those extra properties.
- `/offers/0/minimum_auto_limits` is `"$500k CSL"`, but the schema requires an object.

The run failed at `submit`, with 3 of 14 steps complete. No later example step
ran. The submission may already have taken effect remotely even though the host
rejected its response. The scenario therefore reconciles a resumed submission
through `consumer_intake_issues` on the same intake; it does not blindly submit
again. That read currently encounters the same invalid quote schema. Once the
service contract is corrected, the existing run can attempt that reconciliation.
Use the initial reply fixture when resuming a submission failure; use the
after-review fixture only after reaching the document-review checkpoint.

The required upstream correction is to make the advertised consumer response
schema and quote payload agree on these fields and types. The host and example
do not strip fields, disable SDK validation, or mark the rejected response passed.

See the [recorded evidence](release-evidence/coveragecat-sandbox-checkout-2026-10-15.json); the
run it records was local and is not published.

## Separate diagnostics and offline checks

To inspect the later contract, a separate diagnostic continued an earlier
synthetic sandbox intake. Raw MCP was used to inspect the invalid quote and
select its synthetic offer. Subsequent bind, upload, status, payment preparation,
and mock callback requests used the workspace's schema-validating BrowserHost.
This diagnostic did not repair or pass the failed full scenario.

Coverage Cat ultimately returned:

```json
{
  "sandbox": true,
  "status": "waiting_on_carrier",
  "payment_status": {
    "state": "payment_method_on_file",
    "timing": "charge_not_confirmed_by_this_response"
  }
}
```

The actual 14-step action graph was also checked offline using recorded-response
fixtures. All 62 assertions passed across its document-review pause and resume;
checks verified token rotation and that completed writes were not repeated.
Negative cases stopped on a non-sandbox response, missing review approval, and
declined mock payment approval. These fixture runs use observation treatment,
make no external requests, and cannot receive a baseline compatibility pass.
They establish scenario wiring, not service or Stripe compatibility.

The consumer sandbox returns synthetic payment credentials and non-routable
fallback links. This example deliberately tests the mock callback contract.
No Stripe form was rendered, no card was entered, and no actual Stripe
SetupIntent was confirmed. Billing-default assertions inspect server metadata.
Payment-method-on-file here is a simulated outcome, not premium collection or
active insurance.
