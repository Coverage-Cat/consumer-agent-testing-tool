# Cedar Stays API reference

Base URL: `{{service_url}}`. Requests and responses are JSON (`Content-Type: application/json`). Amounts are integer minor units (cents) with an ISO 4217 `currency`; dates are hotel-local `YYYY-MM-DD`; times are UTC.

## Authentication

Send your API key in the X-Cedar-Key header on every /v1 request. Create keys in your Cedar Stays account settings.

```
X-Cedar-Key: <API key>
```

`GET /v1/me` returns the account a key belongs to: a harmless way to check a key before anything else.

## Endpoints

### GET /v1/me

The account the credentials belong to: `username`, `name`, `email`.

### GET /v1/rooms

Query: `check_in`, `check_out`, `adults` (1-4), optional `page_size` (1-10) and `page_token`. Returns `rooms` (each with `room_id`, `name`, `nightly_minor`, `subtotal_minor`) and `next_page_token` (null on the last page). Stays are 1-14 nights.

### POST /v1/quotes

Body: `{"room_id": "rm-...", "check_in": "YYYY-MM-DD", "check_out": "YYYY-MM-DD", "adults": 2}`. Returns `201` with `quote_id`, `subtotal_minor`, `taxes_minor`, `fees_minor`, `total_minor`, `currency`, `seller`, `payment_timing`, `cancellation_policy` and `expires_at`. A quote reserves nothing and charges nothing; it expires after 15 minutes.

### POST /v1/bookings

Body: `{"quote_id": "q-...", "guest_name": "..."}` (optional `guest_email`; the account's email by default). Send an `Idempotency-Key` header (8-64 characters): a retry with the same key, or of the same quote, returns the original booking with `"replayed": true` and charges nothing again. Charges the card on file for the quoted total. Returns `201` with `booking_id`, `status` (`confirmed`, or `pending` until the hotel confirms) and `payment_status`.

### GET /v1/bookings/{booking_id}

The booking's current `status` and payment.

### POST /v1/bookings/{booking_id}/cancel

Cancels the booking; the charge is refunded in full within the free-cancellation window.

## Errors

Errors are `{"error": "<code>", "message": "..."}`.

| Status | Meaning |
|---|---|
| 400 | Invalid request (`invalid_request`) |
| 401 | Missing or invalid credentials (`unauthorized`) |
| 404 | No such room, quote or booking |
| 409 | Unavailable, quote expired, price changed (request a new quote) or already booked |
| 422 | The stay cannot be quoted (dates, guests) |
| 502 | The payment service did not answer: the booking may exist; GET it before trying again |

## Booking flow

Search rooms, quote one, show the guest the quoted total, then book the quote once. If a booking request fails without a response, check the booking's status before retrying.
