---
name: booking
description: Find reservation availability, compare prices, and create or revise a nonbinding draft.
---

Use the `booking` CLI. A draft is not a confirmed booking and does not reserve inventory.
Confirmation and payment are unavailable in this profile. Never claim they happened.

Read available slots:

    booking slots.list
    booking slots.get --args '{"id":"afternoon"}'

Amounts are integer minor units: 12500 USD means $125.00. `available: 0` means unavailable.
Ask the user for missing contact details. Read current availability before creating a draft.

    booking reservations.create --args '{"slot_id":"afternoon","contact":"Alice Example","idempotency_key":"unique-task-key"}'
    booking reservations.get --args '{"id":"draft-1"}'
    booking reservations.revise --args '{"id":"draft-1","contact":"Alice Example","expected_revision":1,"idempotency_key":"revision-task-key"}'
    booking operations.get --args '{"idempotency_key":"unique-task-key"}'

Use one idempotency key per intended operation and reuse it unchanged on retry.
After timeout, reconcile with `operations.get` before retrying. A 409 indicates stale
revision, unavailable slot, or key reuse with changed arguments. Reread state and
resolve the conflict. A 401 means account setup failed; ask for reconnection.
Verify the resulting draft by reading it. Report the price, currency, and draft status.
