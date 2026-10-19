# Historical Checkout Diagnostics

These one-off hosts exercise the Coverage Cat consumer payment app against an
explicitly authorized **test** application. They are not the general MCP Apps
lane; use `consumer-agent-testing-tool surface-run` for that.

Neither script has a default server, application, checkpoint, or shopper.
Install the dependencies with `npm --prefix tooling/payment-ui ci --ignore-scripts`
and install Chromium as described in the root README. From the workspace root:

```sh
node tooling/payment-ui/blackbox-checkout.mjs \
  --origin "$TEST_ORIGIN" --uid "$APPLICATION_UID" \
  --checkpoint tooling/payment-ui/.private/consumer-checkpoint.json \
  --billing-file tooling/payment-ui/.private/billing.json
```

Set `TEST_ORIGIN` and `APPLICATION_UID` to the authorized test target and
application yourself. An origin is HTTPS, or HTTP on loopback, with no path,
credentials, query, or fragment. The MCP path is `/api/consumer/mcp`. `--help`
and invalid arguments exit without connecting to the service or opening a browser.

Both input files must resolve inside the workspace, including symlink targets.
The checkpoint must contain `origin`, `uid`, and `intake_access_token`; the first
two must match the explicit arguments. New checkpoints preserve this binding.
Legacy checkpoints without an origin are rejected: recreate one with the Python
consumer helper, or add the origin privately only after verifying which server
issued its token. Never place a token on the command line.

The billing file contains synthetic test facts, for example:

```json
{
  "name": "Test Shopper",
  "email": "shopper@example.com",
  "address": {
    "line1": "123 Example St",
    "city": "Seattle",
    "state": "WA",
    "postal_code": "98101"
  }
}
```

Do not put card numbers or real customer data in this file.
`blackbox-checkout.mjs` only checks these facts against the service and rendered
Stripe defaults; it never injects them. `checkout.mjs` uses the same arguments
but deliberately patches one historical widget's `confirmSetup` call to provide
the supplied billing facts. It fails if that call site has changed, and its
result is not evidence of an unmodified widget working.

Both hosts require a fresh payment status, use only the status/payment tool
subset, pin MCP requests to the selected origin, disable redirects, and refuse
anything but a Stripe test SetupIntent and test publishable key. They still make
real requests to the selected service and Stripe: run them only with explicit
authorization. No model inference is involved.

Standard input accepts JSON commands such as `{"action":"inspect"}`,
`{"action":"status"}`, and `{"action":"close"}`. The black-box host also requires
`{"action":"billing"}` to verify defaults before `{"action":"save"}`.
Keep card entry inside Stripe-hosted test fields, never in chat or source files.

Treat `.private/` captures and all `runs/payment-ui-*/` output as private, including
screenshots and registered HTML. Both directories are ignored by Git. This is a
guard against accidental commits, not a sanitizer for manually shared files.
The metadata-only helper also requires an explicit target:

```sh
.venv/bin/python scripts/payment_ui_metadata.py --origin "$TEST_ORIGIN"
```
