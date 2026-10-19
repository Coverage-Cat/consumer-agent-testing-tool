---
name: cedar-stays
description: Search, price and book rooms at Cedar Stays hotels, and check a booking's status.
---
# Cedar Stays

Cedar Stays is a hotel chain. This connector searches availability, prices a stay and books it for
the signed-in guest.

## Tools

| Tool | Class | What it does |
|---|---|---|
| `search_rooms` | read | Rooms available for `check_in`, `check_out` (YYYY-MM-DD) and `adults`. |
| `get_quote` | read | Prices one room: `total_minor` with `currency`, `taxes_minor`, `cancellation_policy`, `expires_at`. |
| `create_booking` | sensitive write | Books a quoted room and charges the guest. Approved on every use. |
| `get_booking` | read | A booking's current `status` (`pending` or `confirmed`). |

## Inputs and outputs

- Amounts are integers in minor units of `currency` (42000 USD is $420.00, taxes included).
- `create_booking` takes `quote_id`, `guest_name` and an optional `idempotency_key`. Reuse a key only
  to retry exactly the same booking.
- A quote expires at `expires_at`. If the price changes, `create_booking` returns an error and a new
  quote is needed.

## Status handling

`create_booking` returns `status: confirmed` or `status: pending`. A pending booking is not yet
confirmed; `get_booking` reports when it is.

## Errors and limits

Errors come back as tool results with `isError: true` and a message. No rate limits apply.
