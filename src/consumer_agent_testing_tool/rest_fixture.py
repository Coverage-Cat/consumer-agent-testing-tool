"""Loopback Cedar Stays REST API for the custom-connector lane (P2.2): a developer's service, never Muse.

A REST facade of the connector fixture's booking backend (connector_fixture.Hotel: the same rooms,
quotes, bookings, charges and business variants) with JSON endpoints under /v1. Authentication is an
API key in a configurable header (or Authorization: Bearer, or a query parameter), none, or a scheme
Muse custom connectors decline (signed requests with a key ID and secret, or a password login with a
session cookie), so compatibility trials have something to find. The service publishes its own
documentation for its bound URL: an OpenAPI 3.1 document, llms.txt and a markdown API reference;
docs_bundle() renders the same files with SERVICE_URL in place of the base URL, for a scenario's docs
bundle (examples/connector/docs/cedar-stays). A private loopback oracle (not part of the API) reports
the backend truth for independent outcome checks. Every variant carries its basis (VARIANT_BASIS,
AUTH_BASIS, DOCS_BASIS) in the oracle state. uvicorn binds 127.0.0.1 only; the service never opens
outbound connections. Credentials are disposable and generated per process unless supplied.
"""
import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .connector_fixture import (
    DEFAULT_CLOCK,
    MAX_NIGHTS,
    QUOTE_SECONDS,
    Adults,
    BookingId,
    Day,
    Email,
    FixtureClock,
    FixtureConfig,
    GuestName,
    Hotel,
    IdempotencyKey,
    LoopbackHost,
    PageSize,
    PageToken,
    QuoteId,
    RoomId,
    ToolFailure,
    bind,
    describe,
    http_server,
    iso,
    read_body,
    serve_until_stopped,
    subject_for,
)
from .connector_fixture import VARIANT_BASIS as HOTEL_BASIS
from .errors import LabError
from .serialization import load, loads

SERVICE, TITLE, VERSION = "cedar-stays-rest", "Cedar Stays", "1.0.0"
# Where a docs bundle writes the API base URL; the custom_connector extension fills it from its url.
SERVICE_URL = "{{service_url}}"
MAX_BODY, MAX_LOG = 64 * 1024, 500
SESSION_COOKIE, SIGNATURE_SKEW = "cedar_session", 300
GUESTS = (("taylor", "Taylor Example", "taylor@example.com"),)

RestVariant = Literal["good", "price-change", "false-success", "slow-confirm", "double-charge-on-retry", "moved-api"]
RestAuth = Literal["api_key", "none", "signed", "cookie"]
DocsVariant = Literal["complete", "vague-auth"]
VARIANTS: tuple[str, ...] = RestVariant.__args__
MUSE_TEMPLATE = ("unofficial mirror rallat/muse-skills@3b5ae2c160 custom-connector-setup/references/mcp-cli.md:46 "
                 "(retrieved 2026-10-15)")
REQUEST_ACCESS = ("unofficial: credentials.request_api_access in mirrored Muse skill files (review 2026-10-15, "
                  "section 2.1 row 6): API keys and the OAuth code flow are supported; cookies, passwords and "
                  "signed requests are declined")
# Business variants are the booking backend's own (connector_fixture.VARIANT_BASIS); moved-api is REST only.
VARIANT_BASIS = {**{name: HOTEL_BASIS[name] for name in VARIANTS if name in HOTEL_BASIS}, "moved-api": {
    "basis": "unofficial", "source": MUSE_TEMPLATE, "quote": "The template follows no redirects and retries nothing.",
    "behavior": "the API moved to /v2 and answers 308 to every /v1 request; the published documentation still "
                "says /v1"}}
AUTH_BASIS = {
    "api_key": {"basis": "documented", "source": "https://platform.muse.ai/docs section 5.4",
                "quote": "such as OAuth client credentials or an API key", "detail": REQUEST_ACCESS},
    "none": {"basis": "assumed", "detail": "a public API without authentication"},
    "signed": {"basis": "unofficial", "detail": REQUEST_ACCESS + "; this variant needs a key ID and a secret "
               "(HMAC-SHA256 request signatures), so it is also a multi-secret scheme"},
    "cookie": {"basis": "unofficial", "detail": REQUEST_ACCESS + "; this variant needs a password login and a "
               "session cookie"},
}
DOCS_BASIS = {
    "complete": {"basis": "assumed", "detail": "the documentation names where the API key goes"},
    "vague-auth": {"basis": "assumed", "detail": "the documentation says to use an API key but never where it "
                   "goes, and the API's errors do not say either (documentation sufficiency)"},
}
# Business errors (connector_fixture.ToolFailure codes) as HTTP statuses.
STATUS = {"invalid_dates": 422, "too_many_guests": 422, "unknown_room": 404, "quote_not_found": 404,
          "booking_not_found": 404, "unavailable": 409, "quote_already_booked": 409, "idempotency_conflict": 409,
          "price_changed": 409, "quote_expired": 409, "payment_timeout": 502, "invalid_page_token": 400}
HEADER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
QUERY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# Names an API key header cannot take: framing, cookies and the signed scheme's own headers.
RESERVED_HEADERS = frozenset({"host", "content-length", "content-type", "transfer-encoding", "connection", "cookie",
                              "authorization", "accept", "user-agent", "idempotency-key", "x-cedar-key-id",
                              "x-cedar-timestamp", "x-cedar-signature"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestConfig(Strict):
    """Declarative service settings; credentials left empty are generated per process."""
    variant: RestVariant = "good"
    auth: RestAuth = "api_key"
    api_key_placement: Literal["header", "bearer", "query"] = "header"
    api_key_name: str = "X-Cedar-Key"            # the header (or query parameter) that carries the key
    api_keys: dict[str, str] = {}                # key -> username
    docs: DocsVariant = "complete"
    port: int = Field(0, ge=0, le=65535)
    clock: str = DEFAULT_CLOCK
    oracle_token: str | None = Field(None, min_length=16)

    @model_validator(mode="after")
    def coherent(self):
        if self.api_key_placement == "header" and (not HEADER_NAME.fullmatch(self.api_key_name)
                                                   or self.api_key_name.lower() in RESERVED_HEADERS):
            raise ValueError("api_key_name must be a plain header name other than Authorization, Cookie and the "
                             "framing headers (use api_key_placement bearer for Authorization)")
        if self.api_key_placement == "query" and not QUERY_NAME.fullmatch(self.api_key_name):
            raise ValueError("api_key_name must be a plain query parameter name")
        if any(len(key) < 16 for key in self.api_keys) or not set(self.api_keys.values()) <= {
                name for name, _, _ in GUESTS}:
            raise ValueError("api_keys map keys of at least 16 characters to the account taylor")
        if self.api_keys and self.auth != "api_key":
            raise ValueError("api_keys apply to auth api_key only")
        try:
            FixtureClock(self.clock)
        except LabError as error:
            raise ValueError(str(error)) from None
        return self


# Request inputs: the same constraints the MCP fixture's tool schemas publish.
class RoomQuery(Strict):
    check_in: Day
    check_out: Day
    adults: Adults
    page_size: PageSize = None
    page_token: PageToken = None


class QuoteBody(Strict):
    model_config = ConfigDict(extra="forbid", strict=True)
    room_id: RoomId
    check_in: Day
    check_out: Day
    adults: Adults


class BookingBody(Strict):
    model_config = ConfigDict(extra="forbid", strict=True)
    quote_id: QuoteId
    guest_name: GuestName
    guest_email: Email | None = None


class IdempotencyHeader(Strict):
    key: IdempotencyKey


class BookingPath(Strict):
    booking_id: BookingId


class OracleReset(Strict):
    clock: str | None = None


class Failure(Exception):
    def __init__(self, status: int, error: str, message: str, headers: dict | None = None, **details):
        super().__init__(message)
        self.status, self.headers = status, headers
        self.payload = {"error": error, "message": message, **details}


def error_response(status, error, message, headers=None, **details):
    return JSONResponse({"error": error, "message": message, **details}, status_code=status, headers=headers)


# Published documentation ------------------------------------------------------------------------
def key_location(config: RestConfig) -> str:
    """Where an API key goes, as the documentation words it."""
    return {"header": f"in the {config.api_key_name} header",
            "bearer": "as a bearer token: Authorization: Bearer <API key>",
            "query": f"as the {config.api_key_name} query parameter"}[config.api_key_placement]


def _auth_summary(config: RestConfig) -> str:
    """One sentence: how the API authenticates, as its documentation says it."""
    if config.auth == "none":
        return "No authentication: every endpoint is public."
    if config.auth == "signed":
        return ("Sign every /v1 request with your key ID and secret: send X-Cedar-Key-Id, X-Cedar-Timestamp (Unix "
                "seconds) and X-Cedar-Signature, the hex HMAC-SHA256 of METHOD, path with query, timestamp and the "
                "hex SHA-256 of the body, joined with newlines, under your secret.")
    if config.auth == "cookie":
        return ("Sign in with POST /v1/login and your Cedar Stays username and password; send the cedar_session "
                "cookie it sets on every later /v1 request.")
    if config.docs == "vague-auth":
        return "Requests are authenticated with your Cedar Stays API key."
    return (f"Send your API key {key_location(config)} on every /v1 request. Create keys in your Cedar Stays "
            "account settings.")


def _security_schemes(config: RestConfig) -> dict:
    if config.auth == "none" or config.auth == "api_key" and config.docs == "vague-auth":
        return {}
    if config.auth == "signed":
        return {"keyId": {"type": "apiKey", "in": "header", "name": "X-Cedar-Key-Id"},
                "signature": {"type": "apiKey", "in": "header", "name": "X-Cedar-Signature",
                              "description": _auth_summary(config)}}
    if config.auth == "cookie":
        return {"session": {"type": "apiKey", "in": "cookie", "name": SESSION_COOKIE,
                            "description": _auth_summary(config)}}
    if config.api_key_placement == "bearer":
        return {"apiKey": {"type": "http", "scheme": "bearer", "description": _auth_summary(config)}}
    return {"apiKey": {"type": "apiKey", "in": "header" if config.api_key_placement == "header" else "query",
                       "name": config.api_key_name, "description": _auth_summary(config)}}


def _schemas() -> dict:
    day = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "Hotel-local date."}
    minor = {"type": "integer", "minimum": 0, "description": "Amount in minor units (cents)."}
    policy = {"type": "object", "required": ["refundable", "free_cancellation_until", "summary"], "properties": {
        "refundable": {"type": "boolean"}, "free_cancellation_until": {"type": "string", "format": "date-time"},
        "summary": {"type": "string"}}}
    room = {"type": "object", "required": ["room_id", "name", "beds", "max_adults", "floor", "summary",
                                           "nightly_minor", "subtotal_minor"], "properties": {
        "room_id": {"type": "string"}, "name": {"type": "string"}, "beds": {"type": "string"},
        "max_adults": {"type": "integer"}, "floor": {"type": "string"}, "summary": {"type": "string"},
        "nightly_minor": minor, "subtotal_minor": {**minor, "description": "Whole stay before taxes and fees."}}}
    return {
        "Error": {"type": "object", "required": ["error", "message"], "properties": {
            "error": {"type": "string", "description": "A stable error code."}, "message": {"type": "string"}}},
        "Account": {"type": "object", "required": ["username", "name"], "properties": {
            "username": {"type": "string"}, "name": {"type": "string"}, "email": {"type": ["string", "null"]}}},
        "Room": room,
        "RoomSearch": {"type": "object", "required": ["check_in", "check_out", "nights", "adults", "currency",
                                                      "rooms", "search_session_id", "next_page_token"],
                       "properties": {"check_in": day, "check_out": day, "nights": {"type": "integer"},
                                      "adults": {"type": "integer"}, "currency": {"type": "string"},
                                      "rooms": {"type": "array", "items": {"$ref": "#/components/schemas/Room"}},
                                      "search_session_id": {"type": "string"},
                                      "next_page_token": {"type": ["string", "null"],
                                                          "description": "Pass as page_token for more rooms."}}},
        "QuoteRequest": {"type": "object", "additionalProperties": False,
                         "required": ["room_id", "check_in", "check_out", "adults"], "properties": {
                             "room_id": {"type": "string", "pattern": r"^rm-[a-z0-9-]{1,40}$"}, "check_in": day,
                             "check_out": day, "adults": {"type": "integer", "minimum": 1, "maximum": 4}}},
        "CancellationPolicy": policy,
        "Quote": {"type": "object", "properties": {
            "quote_id": {"type": "string"}, "room_id": {"type": "string"}, "room_name": {"type": "string"},
            "check_in": day, "check_out": day, "nights": {"type": "integer"}, "adults": {"type": "integer"},
            "nightly_minor": minor, "subtotal_minor": minor, "taxes_minor": minor, "fees_minor": minor,
            "total_minor": {**minor, "description": "Full price: subtotal, taxes and fees."},
            "currency": {"type": "string", "description": "ISO 4217 code."}, "seller": {"type": "string"},
            "payment_timing": {"type": "string"}, "recurring": {"type": "boolean"},
            "cancellation_policy": {"$ref": "#/components/schemas/CancellationPolicy"},
            "expires_at": {"type": "string", "format": "date-time",
                           "description": "After this time the quote cannot be booked."}}},
        "BookingRequest": {"type": "object", "additionalProperties": False, "required": ["quote_id", "guest_name"],
                           "properties": {"quote_id": {"type": "string", "pattern": r"^q-\d{4,}$"},
                                          "guest_name": {"type": "string", "minLength": 1, "maxLength": 100},
                                          "guest_email": {"type": "string", "format": "email",
                                                          "description": "Defaults to the account's email."}}},
        "Booking": {"type": "object", "properties": {
            "booking_id": {"type": "string"}, "status": {"enum": ["pending", "confirmed", "cancelled"]},
            "quote_id": {"type": "string"}, "room_id": {"type": "string"}, "room_name": {"type": "string"},
            "check_in": day, "check_out": day, "nights": {"type": "integer"}, "adults": {"type": "integer"},
            "guest_name": {"type": "string"}, "guest_email": {"type": ["string", "null"]}, "total_minor": minor,
            "currency": {"type": "string"},
            "payment_status": {"enum": ["pending", "captured", "refunded", "not_charged"]},
            "charged_minor": minor, "refunded_minor": minor,
            "cancellation_policy": {"$ref": "#/components/schemas/CancellationPolicy"},
            "created_at": {"type": "string", "format": "date-time"},
            "replayed": {"type": "boolean", "description": "True when this repeats an earlier request."}}},
    }


def openapi_document(config: RestConfig, base_url: str) -> dict:
    """The service's OpenAPI 3.1 description (JSON Schema 2020-12 dialect)."""
    def ref(name):
        return {"$ref": f"#/components/schemas/{name}"}

    def json_body(name):
        return {"content": {"application/json": {"schema": ref(name)}}}

    def errors(*codes):
        reasons = {"400": "Invalid request", "401": "Missing or invalid credentials", "404": "Not found",
                   "409": "Conflict with the current state", "422": "The stay cannot be quoted",
                   "502": "The payment service did not answer; check the booking before retrying"}
        return {code: {"description": reasons[code], **json_body("Error")} for code in codes}
    secured = config.auth != "none"
    booking_id = {"name": "booking_id", "in": "path", "required": True,
                  "schema": {"type": "string", "pattern": r"^b-\d{4,}$"}}
    paths = {
        "/v1/me": {"get": {"operationId": "getAccount", "summary": "The account the credentials belong to",
                           "responses": {"200": {"description": "The account", **json_body("Account")},
                                         **errors("401")}}},
        "/v1/rooms": {"get": {
            "operationId": "searchRooms", "summary": "Rooms available for a stay",
            "description": "Nightly rate and stay subtotal before taxes and fees. Stays are 1 to "
                           f"{MAX_NIGHTS} nights; check_out must be after check_in.",
            "parameters": [{"name": "check_in", "in": "query", "required": True, "schema": {"type": "string",
                                                                                          "format": "date"}},
                           {"name": "check_out", "in": "query", "required": True, "schema": {"type": "string",
                                                                                           "format": "date"}},
                           {"name": "adults", "in": "query", "required": True,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 4}},
                           {"name": "page_size", "in": "query", "schema": {"type": "integer", "minimum": 1,
                                                                            "maximum": 10}},
                           {"name": "page_token", "in": "query", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Matching rooms", **json_body("RoomSearch")},
                          **errors("400", "401", "422")}}},
        "/v1/quotes": {"post": {
            "operationId": "createQuote", "summary": "Quote one room for a stay",
            "description": "Subtotal, taxes, fees, total, payment timing and cancellation policy. A quote reserves "
                           f"nothing and charges nothing; it expires after {QUOTE_SECONDS // 60} minutes.",
            "requestBody": {"required": True, **json_body("QuoteRequest")},
            "responses": {"201": {"description": "The quote", **json_body("Quote")},
                          **errors("400", "401", "404", "409", "422")}}},
        "/v1/bookings": {"post": {
            "operationId": "createBooking", "summary": "Book the room in a quote",
            "description": "Charges the card on file for the quoted total. A retry with the same Idempotency-Key "
                           "(or the same quote) returns the original booking without charging again. The status "
                           "is confirmed, or pending until the hotel confirms; GET the booking for its current "
                           "status.",
            "parameters": [{"name": "Idempotency-Key", "in": "header", "required": False,
                            "schema": {"type": "string", "pattern": r"^[A-Za-z0-9._:-]{8,64}$"}}],
            "requestBody": {"required": True, **json_body("BookingRequest")},
            "responses": {"201": {"description": "The new booking", **json_body("Booking")},
                          "200": {"description": "The original booking (a repeated request)",
                                  **json_body("Booking")}, **errors("400", "401", "404", "409", "502")}}},
        "/v1/bookings/{booking_id}": {"get": {
            "operationId": "getBooking", "summary": "A booking's current status", "parameters": [booking_id],
            "responses": {"200": {"description": "The booking", **json_body("Booking")}, **errors("401", "404")}}},
        "/v1/bookings/{booking_id}/cancel": {"post": {
            "operationId": "cancelBooking", "summary": "Cancel a booking",
            "description": "Refunds the charge in full within the free-cancellation window. Cancelling a cancelled "
                           "booking changes nothing.", "parameters": [booking_id],
            "responses": {"200": {"description": "The cancelled booking", **json_body("Booking")},
                          **errors("401", "404")}}},
    }
    if config.auth == "cookie":
        paths["/v1/login"] = {"post": {
            "operationId": "login", "summary": "Sign in and receive the session cookie", "security": [],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["username", "password"],
                "properties": {"username": {"type": "string"}, "password": {"type": "string"}}}}}},
            "responses": {"200": {"description": "Signed in; the response sets the cedar_session cookie"},
                          **errors("401")}}}
    schemes = _security_schemes(config)
    document = {"openapi": "3.1.0",
                "info": {"title": f"{TITLE} API", "version": VERSION,
                         "description": f"Rooms, price quotes and bookings at {TITLE}. Amounts are integer minor "
                                        "units with an ISO 4217 currency code. " + _auth_summary(config)},
                "servers": [{"url": base_url}], "paths": paths,
                "components": {"schemas": _schemas(), **({"securitySchemes": schemes} if schemes else {})}}
    if secured and schemes:
        document["security"] = [{name: [] for name in schemes}]
    return document


def llms_txt(config: RestConfig, base_url: str) -> str:
    """llms.txt (llmstxt.org): a title, a summary, and links to the documentation."""
    return "\n".join([
        f"# {TITLE} API", "",
        (f"> Search {TITLE} rooms, quote a stay and book it over HTTP with JSON bodies. Amounts are integer minor "
         "units (cents) with an ISO 4217 currency code; dates are hotel-local YYYY-MM-DD."), "",
        f"Base URL: {base_url}", "", f"Authentication: {_auth_summary(config)}", "",
        "## Docs", "",
        f"- [API reference]({base_url}/docs/api.md): endpoints, fields, errors and the booking flow",
        f"- [OpenAPI 3.1 document]({base_url}/openapi.json): every endpoint, machine-readable", "",
        "## Booking flow", "",
        "1. GET /v1/rooms?check_in=YYYY-MM-DD&check_out=YYYY-MM-DD&adults=N",
        "2. POST /v1/quotes with room_id, check_in, check_out and adults",
        "3. POST /v1/bookings with quote_id and guest_name, and an Idempotency-Key header",
        "4. GET /v1/bookings/{booking_id} for the current status", ""])


def api_reference(config: RestConfig, base_url: str) -> str:
    """The markdown API reference."""
    lines = [
        f"# {TITLE} API reference", "",
        (f"Base URL: `{base_url}`. Requests and responses are JSON (`Content-Type: application/json`). Amounts "
         "are integer minor units (cents) with an ISO 4217 `currency`; dates are hotel-local `YYYY-MM-DD`; times "
         "are UTC."), "",
        "## Authentication", "", _auth_summary(config), ""]
    if config.auth == "api_key" and config.docs == "complete":
        example = {"header": f"{config.api_key_name}: <API key>", "bearer": "Authorization: Bearer <API key>",
                   "query": f"?{config.api_key_name}=<API key>"}[config.api_key_placement]
        lines += ["```", example, "```", "", ("`GET /v1/me` returns the account a key belongs to: a harmless way "
                                              "to check a key before anything else."), ""]
    if config.auth == "signed":
        lines += ["```", "X-Cedar-Key-Id: <key ID>", "X-Cedar-Timestamp: <Unix seconds>",
                  ("X-Cedar-Signature: hex(HMAC-SHA256(<secret>, METHOD + \"\\n\" + path?query + \"\\n\" + "
                   "timestamp + \"\\n\" + hex(SHA-256(body))))"), "```", ""]
    lines += [
        "## Endpoints", "",
        "### GET /v1/me", "", "The account the credentials belong to: `username`, `name`, `email`.", "",
        "### GET /v1/rooms", "",
        ("Query: `check_in`, `check_out`, `adults` (1-4), optional `page_size` (1-10) and `page_token`. Returns "
         "`rooms` (each with `room_id`, `name`, `nightly_minor`, `subtotal_minor`) and `next_page_token` (null on "
         f"the last page). Stays are 1-{MAX_NIGHTS} nights."), "",
        "### POST /v1/quotes", "",
        ("Body: `{\"room_id\": \"rm-...\", \"check_in\": \"YYYY-MM-DD\", \"check_out\": \"YYYY-MM-DD\", "
         "\"adults\": 2}`. Returns `201` with `quote_id`, `subtotal_minor`, `taxes_minor`, `fees_minor`, "
         "`total_minor`, `currency`, `seller`, `payment_timing`, `cancellation_policy` and `expires_at`. A quote "
         f"reserves nothing and charges nothing; it expires after {QUOTE_SECONDS // 60} minutes."), "",
        "### POST /v1/bookings", "",
        ("Body: `{\"quote_id\": \"q-...\", \"guest_name\": \"...\"}` (optional `guest_email`; the account's "
         "email by default). Send an `Idempotency-Key` header (8-64 characters): a retry with the same key, or of "
         "the same quote, returns the original booking with `\"replayed\": true` and charges nothing again. "
         "Charges the card on file for the quoted total. Returns `201` with `booking_id`, `status` (`confirmed`, "
         "or `pending` until the hotel confirms) and `payment_status`."), "",
        "### GET /v1/bookings/{booking_id}", "", "The booking's current `status` and payment.", "",
        "### POST /v1/bookings/{booking_id}/cancel", "",
        "Cancels the booking; the charge is refunded in full within the free-cancellation window.", ""]
    if config.auth == "cookie":
        lines += ["### POST /v1/login", "", (f"Body: `{{\"username\": \"...\", \"password\": \"...\"}}`. Sets "
                                             f"the `{SESSION_COOKIE}` cookie."), ""]
    lines += [
        "## Errors", "",
        "Errors are `{\"error\": \"<code>\", \"message\": \"...\"}`.", "",
        "| Status | Meaning |", "|---|---|",
        "| 400 | Invalid request (`invalid_request`) |",
        "| 401 | Missing or invalid credentials (`unauthorized`) |",
        "| 404 | No such room, quote or booking |",
        "| 409 | Unavailable, quote expired, price changed (request a new quote) or already booked |",
        "| 422 | The stay cannot be quoted (dates, guests) |",
        "| 502 | The payment service did not answer: the booking may exist; GET it before trying again |", "",
        "## Booking flow", "",
        ("Search rooms, quote one, show the guest the quoted total, then book the quote once. If a booking "
         "request fails without a response, check the booking's status before retrying."), ""]
    return "\n".join(lines)


def docs_bundle(config: RestConfig | None = None, base_url: str = SERVICE_URL) -> dict[str, str]:
    """The published documentation files by bundle path (the same paths the service serves)."""
    config = config or RestConfig()
    return {"llms.txt": llms_txt(config, base_url),
            "openapi.json": json.dumps(openapi_document(config, base_url), indent=2, ensure_ascii=False) + "\n",
            "docs/api.md": api_reference(config, base_url)}


def write_docs(directory: Path, config: RestConfig | None = None) -> list[Path]:
    """Write docs_bundle() (base URL as SERVICE_URL) under `directory`; returns the files written."""
    written = []
    for name, text in docs_bundle(config).items():
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


DOC_TYPES = {"llms.txt": "text/plain; charset=utf-8", "openapi.json": "application/json",
             "docs/api.md": "text/markdown; charset=utf-8"}


# The service ------------------------------------------------------------------------------------
class RestFixture:
    """One service instance: booking backend, REST API, published docs and private oracle."""

    def __init__(self, config: RestConfig | None = None, *, clock: Callable[[], datetime] | None = None):
        self.config = config or RestConfig()
        self.lock = threading.RLock()
        self.clock = FixtureClock(self.config.clock, clock)
        self.generated: dict = {}
        self.accounts = {"guest": {"name": "Guest", "email": None, "subject": None}} | {
            name: {"name": full, "email": email, "subject": subject_for(name)} for name, full, email in GUESTS}
        self.api_keys, self.signing, self.passwords, self.sessions = dict(self.config.api_keys), {}, {}, {}
        holder = GUESTS[0][0]
        if self.config.auth == "api_key" and not self.api_keys:
            key = "cedar_" + secrets.token_urlsafe(24)
            self.api_keys[key] = holder
            self.generated["api_key"] = key
        elif self.config.auth == "signed":
            key_id, secret = "kid_" + secrets.token_hex(8), secrets.token_urlsafe(32)
            self.signing[key_id] = (secret, holder)
            self.generated["signing"] = {"key_id": key_id, "secret": secret}
        elif self.config.auth == "cookie":
            password = secrets.token_urlsafe(12)
            self.passwords[holder] = password
            self.generated["login"] = {"username": holder, "password": password}
        variant = self.config.variant if self.config.variant in HOTEL_BASIS else "good"
        self.hotel = Hotel(FixtureConfig(variant=variant, clock=self.config.clock), self.clock, self.accounts)
        self.requests: deque[dict] = deque(maxlen=MAX_LOG)
        self.http, self.routes, self.auth_failures, self.request_count = Counter(), Counter(), Counter(), 0
        self.port: int | None = None
        self.server = self.thread = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise LabError("REST fixture is not bound yet")
        return f"http://127.0.0.1:{self.port}"

    @property
    def oracle_url(self) -> str:
        return self.base_url + "/__oracle/state"

    @property
    def reset_url(self) -> str:
        return self.base_url + "/__oracle/reset"

    # Controls (in-process twins of the HTTP oracle) ----------------------------------------
    def state(self) -> dict:
        with self.lock:
            return copy.deepcopy({
                "service": SERVICE, "version": VERSION, "variant": self.config.variant, "auth": self.config.auth,
                "docs": self.config.docs,
                "basis": {"variant": VARIANT_BASIS[self.config.variant], "auth": AUTH_BASIS[self.config.auth],
                          "docs": DOCS_BASIS[self.config.docs]},
                "clock": iso(self.clock.now()), **self.hotel.snapshot(), "http": dict(self.http),
                "routes": dict(self.routes), "auth_failures": dict(self.auth_failures),
                "requests": list(self.requests),
                "accounts": [{"username": name, "subject": subject_for(name), "name": full}
                             for name, full, _ in GUESTS]})

    def reset(self, *, clock: str | None = None) -> dict:
        with self.lock:
            if clock is not None:
                self.clock.set(clock)
            self.hotel.reset()
            self.requests.clear()
            self.http.clear()
            self.routes.clear()
            self.auth_failures.clear()
            self.sessions.clear()
            self.request_count = 0
        return self.state()

    def announcement(self) -> dict:
        """The single JSON line a subprocess prints for its trusted launcher."""
        line = {"service": SERVICE, "variant": self.config.variant, "auth": self.config.auth, "docs": self.config.docs,
                "port": self.port, "base_url": self.base_url,
                "docs_urls": {name: f"{self.base_url}/{name}" for name in DOC_TYPES},
                "oracle_url": self.oracle_url, "reset_url": self.reset_url, "clock": iso(self.clock.now()),
                "basis": VARIANT_BASIS[self.config.variant]}
        if self.config.auth == "api_key":
            line["api_key"] = {"placement": self.config.api_key_placement, "name": self.config.api_key_name}
        if self.generated:
            line["generated_credentials"] = copy.deepcopy(self.generated)  # Disposable, generated per process.
        return line

    # HTTP -----------------------------------------------------------------------------------
    def build(self, port: int) -> LoopbackHost:
        self.port = port
        methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
        routes = [Route("/__oracle/{action}", self.oracle, methods=["GET", "POST"]),
                  *(Route(f"/{name}", self.doc, methods=["GET", "HEAD"]) for name in DOC_TYPES),
                  Route("/{path:path}", self.api, methods=methods)]
        return LoopbackHost(Starlette(routes=routes), port)

    async def doc(self, request: Request) -> Response:
        name = request.url.path.lstrip("/")
        text = docs_bundle(self.config, self.base_url)[name]
        self.count(request.method, f"/{name}", 200)
        return Response(text, media_type=DOC_TYPES[name])

    def count(self, method, template, status, entry=None):
        with self.lock:
            self.http[str(status)] += 1
            self.routes[f"{method} {template}"] += 1
            if entry is not None:
                entry["status"] = status

    async def api(self, request: Request) -> Response:
        path, method = request.url.path, request.method
        with self.lock:
            self.request_count += 1
            # Route templates only: query strings and paths may carry credentials or identifiers.
            entry = {"n": self.request_count, "method": method, "route": None, "status": None, "auth": None}
            self.requests.append(entry)
        moved = self.config.variant == "moved-api"
        if moved and (path == "/v1" or path.startswith("/v1/")):
            location = f"{self.base_url}/v2{path[3:]}" + (f"?{request.url.query}" if request.url.query else "")
            entry["route"] = "/v1 (moved)"
            self.count(method, "/v1/*", 308, entry)
            return error_response(308, "moved_permanently", "This API moved to /v2.", {"Location": location})
        prefix = "/v2" if moved else "/v1"
        if not (path == prefix or path.startswith(prefix + "/")):
            self.count(method, "(unknown)", 404, entry)
            return error_response(404, "not_found", "No such endpoint.")
        route, handler, parameters = self.route(method, path[len(prefix):] or "/")
        entry["route"] = f"{prefix}{route}" if route else "(unknown)"
        try:
            if handler is None:
                raise Failure(405 if route else 404, "method_not_allowed" if route else "not_found",
                              "Method not allowed." if route else "No such endpoint.",
                              {"Allow": parameters} if route else None)
            body = await read_body(request.receive, MAX_BODY)
            if body is None:
                raise Failure(413, "request_too_large", f"Request bodies are at most {MAX_BODY} bytes.")
            if route == "/login":
                return self.login(body, entry)
            account = self.authenticate(request, body, entry)
            status, payload = handler(account, request, body, **parameters)
            response = JSONResponse(payload, status_code=status)
        except Failure as failure:
            status, response = failure.status, JSONResponse(failure.payload, status_code=failure.status,
                                                            headers=failure.headers)
        self.count(method, entry["route"], status, entry)
        return response

    def route(self, method, rest):
        """(template, handler, path parameters); handler None for a wrong method (parameters: Allow)."""
        table = [("/me", {"GET": self.me}), ("/rooms", {"GET": self.rooms}), ("/quotes", {"POST": self.quote}),
                 ("/bookings", {"POST": self.book}), ("/bookings/{booking_id}", {"GET": self.booking}),
                 ("/bookings/{booking_id}/cancel", {"POST": self.cancel})]
        if self.config.auth == "cookie":
            table.append(("/login", {"POST": True}))
        parts = rest.rstrip("/").split("/") if rest != "/" else [""]
        for template, handlers in table:
            pieces = template.split("/")
            if len(pieces) != len(parts):
                continue
            parameters = {}
            for piece, part in zip(pieces, parts, strict=True):
                if piece.startswith("{"):
                    if not part:
                        break
                    parameters[piece[1:-1]] = part
                elif piece != part:
                    break
            else:
                if method in handlers or method == "HEAD" and "GET" in handlers:
                    handler = handlers.get(method) or handlers["GET"]
                    return template, handler, parameters
                return template, None, ", ".join(handlers)
        return None, None, None

    def authenticate(self, request: Request, body: bytes, entry: dict) -> str:
        mode = self.config.auth
        if mode == "none":
            entry["auth"] = "none"
            return "guest"
        if mode == "api_key":
            presented = self.presented_key(request)
            account = next((name for key, name in self.api_keys.items()
                            if presented and secrets.compare_digest(key.encode(), presented.encode())), None)
            if account:
                entry["auth"] = "ok"
                return account
            # A key sent where this API does not read it: say where, unless the docs are vague about it.
            misplaced = presented is None and self.elsewhere(request)
            reason = "invalid" if presented else "misplaced" if misplaced else "missing"
            hint = "" if self.config.docs == "vague-auth" else f" Send it {key_location(self.config)}."
            raise self.unauthorized(entry, reason, "Invalid API key." if presented else
                                    f"An API key is required.{hint}")
        if mode == "signed":
            return self.verify_signature(request, body, entry)
        token = request.cookies.get(SESSION_COOKIE)
        with self.lock:
            account = self.sessions.get(token) if token else None
        if account is None:
            raise self.unauthorized(entry, "invalid" if token else "missing", "Sign in with POST /v1/login first.")
        entry["auth"] = "ok"
        return account

    def unauthorized(self, entry, reason, message) -> Failure:
        with self.lock:
            self.auth_failures[reason] += 1
        entry["auth"] = reason
        challenge = {"api_key": f'ApiKey realm="{SERVICE}"', "signed": f'CedarSignature realm="{SERVICE}"',
                     "cookie": f'Cookie realm="{SERVICE}"'}[self.config.auth]
        if self.config.auth == "api_key" and self.config.api_key_placement == "bearer":
            challenge = f'Bearer realm="{SERVICE}"' + (', error="invalid_token"' if reason == "invalid" else "")
        return Failure(401, "unauthorized", message, {"WWW-Authenticate": challenge})

    def presented_key(self, request: Request) -> str | None:
        placement, name = self.config.api_key_placement, self.config.api_key_name
        if placement == "header":
            return request.headers.get(name) or None
        if placement == "bearer":
            value = request.headers.get("authorization", "")
            return (value[7:].strip() or None) if value[:7].lower() == "bearer " else None
        return parse_qs(request.url.query).get(name, [None])[0] or None

    def elsewhere(self, request: Request) -> bool:
        """Something that looks like an API key in another common place (Authorization, X-API-Key, ?api_key)."""
        headers = request.headers
        return bool(headers.get("authorization") or headers.get("x-api-key") or headers.get("api-key")
                    or parse_qs(request.url.query).get("api_key") or (
                        self.config.api_key_placement != "header" and headers.get(self.config.api_key_name)))

    def verify_signature(self, request: Request, body: bytes, entry: dict) -> str:
        headers = request.headers
        key_id, stamp, signature = (headers.get(name) for name in ("x-cedar-key-id", "x-cedar-timestamp",
                                                                  "x-cedar-signature"))
        if not (key_id and stamp and signature):
            raise self.unauthorized(entry, "missing", "Signed requests need X-Cedar-Key-Id, X-Cedar-Timestamp and "
                                                      "X-Cedar-Signature.")
        found = self.signing.get(key_id)
        try:
            fresh = abs(int(stamp) - int(self.clock.now().timestamp())) <= SIGNATURE_SKEW
        except ValueError:
            fresh = False
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        message = "\n".join([request.method, target, stamp, hashlib.sha256(body).hexdigest()])
        if found is None or not fresh or not hmac.compare_digest(
                hmac.new(found[0].encode(), message.encode(), hashlib.sha256).hexdigest(), signature.lower()):
            raise self.unauthorized(entry, "invalid", "The request signature is invalid or stale.")
        entry["auth"] = "ok"
        return found[1]

    def login(self, body: bytes, entry: dict) -> Response:
        payload = self.json(body)
        username, password = payload.get("username"), payload.get("password")
        expected = self.passwords.get(username) if isinstance(username, str) else None
        if not (expected and isinstance(password, str)
                and secrets.compare_digest(expected.encode(), password.encode())):
            failure = self.unauthorized(entry, "invalid", "Wrong username or password.")
            self.count("POST", "/v1/login", 401, entry)
            return JSONResponse(failure.payload, status_code=401, headers=failure.headers)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[token] = username
        entry["auth"] = "ok"
        self.count("POST", "/v1/login", 200, entry)
        response = JSONResponse({"signed_in": True, "username": username})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    # Endpoints (the backend's tools) -------------------------------------------------------
    @staticmethod
    def json(body: bytes) -> dict:
        try:
            payload = loads(body.decode("utf-8"), json_only=True) if body.strip() else None
        except (LabError, UnicodeDecodeError):
            raise Failure(400, "invalid_request", "The body is not valid JSON.") from None
        if not isinstance(payload, dict):
            raise Failure(400, "invalid_request", "The body must be a JSON object.")
        return payload

    def call(self, tool, account, **arguments):
        with self.lock:
            try:
                return self.hotel.call(tool, account, None, arguments)
            except ToolFailure as failure:
                details = {key: value for key, value in failure.payload.items() if key not in ("error", "message")}
                raise Failure(STATUS.get(failure.payload["error"], 400), failure.payload["error"],
                              failure.payload["message"], **details) from None

    @staticmethod
    def valid(model, data):
        try:
            return model.model_validate(data)
        except ValidationError as error:
            raise Failure(400, "invalid_request", describe(error)) from None

    @staticmethod
    def json_only(request: Request):
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json" and not kind.endswith("+json"):
            raise Failure(415, "unsupported_media_type", "Send JSON with Content-Type: application/json.")

    def me(self, account, request, body):
        holder = self.accounts[account]
        return 200, {"username": account, "name": holder["name"], "email": holder["email"]}

    def rooms(self, account, request, body):
        query = {key: values[-1] for key, values in parse_qs(request.url.query).items()
                 if key != self.config.api_key_name or self.config.api_key_placement != "query"}
        found = self.valid(RoomQuery, query)
        return 200, self.call("search_rooms", account, **found.model_dump(exclude_none=True))

    def quote(self, account, request, body):
        self.json_only(request)
        found = self.valid(QuoteBody, self.json(body))
        return 201, self.call("get_quote", account, **found.model_dump())

    def book(self, account, request, body):
        self.json_only(request)
        found = self.valid(BookingBody, self.json(body))
        key = request.headers.get("idempotency-key")
        if key is not None:
            key = self.valid(IdempotencyHeader, {"key": key}).key
        email = found.guest_email or self.accounts[account]["email"]
        if email is None:
            raise Failure(400, "invalid_request", "guest_email is required for bookings without an account.")
        result = self.call("create_booking", account, quote_id=found.quote_id, guest_name=found.guest_name,
                           guest_email=email, idempotency_key=key)
        return (200 if result.get("replayed") else 201), result

    def booking(self, account, request, body, booking_id):
        found = self.valid(BookingPath, {"booking_id": booking_id})
        return 200, self.call("get_booking", account, booking_id=found.booking_id)

    def cancel(self, account, request, body, booking_id):
        found = self.valid(BookingPath, {"booking_id": booking_id})
        return 200, self.call("cancel_booking", account, booking_id=found.booking_id)

    # Private oracle -------------------------------------------------------------------------
    async def oracle(self, request: Request) -> Response:
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1") or request.headers.get("origin") \
                or request.headers.get("host") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return error_response(403, "forbidden", "The oracle is private to the local host")
        token = self.config.oracle_token
        if token and not secrets.compare_digest(token.encode(), request.headers.get("x-oracle-token", "").encode()):
            return error_response(403, "forbidden", "Oracle token required")
        action = request.path_params["action"]
        if request.method == "GET":
            return JSONResponse(self.state()) if action == "state" else error_response(404, "not_found",
                                                                                         "Unknown oracle resource")
        if action != "reset":
            return error_response(404, "not_found", "Unknown oracle action")
        try:
            raw = (await request.body()).decode()
            command = OracleReset.model_validate(loads(raw, json_only=True) if raw.strip() else {})
            return JSONResponse(self.reset(clock=command.clock))
        except (LabError, ValidationError, UnicodeDecodeError) as error:
            return error_response(400, "invalid_request", describe(error))

    # Lifecycle ------------------------------------------------------------------------------
    def start(self) -> "RunningRest":
        if self.thread is not None:
            raise LabError("This REST fixture is already running")
        listener = bind(self.config.port)
        self.server = http_server(self.build(listener.getsockname()[1]))
        self.thread = threading.Thread(target=lambda: asyncio.run(serve_until_stopped(self.server, listener)),
                                       name=f"rest-fixture-{self.port}", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                listener.close()
                raise LabError("REST fixture failed to start")
            time.sleep(0.01)
        return RunningRest(self.base_url, self.oracle_url, self.reset_url, self.port, self,
                           copy.deepcopy(self.generated))

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)


@dataclass(frozen=True)
class RunningRest:
    base_url: str
    oracle_url: str
    reset_url: str
    port: int
    controls: RestFixture
    credentials: dict


@contextmanager
def run_rest(config: RestConfig | None = None, *, clock: Callable[[], datetime] | None = None) -> Iterator[RunningRest]:
    """Serve on a background thread (port 0 picks a free port); usable from sync code."""
    fixture = RestFixture(config, clock=clock)
    running = fixture.start()
    try:
        yield running
    finally:
        fixture.stop()


@asynccontextmanager
async def serve_rest(config: RestConfig | None = None, *,
                     clock: Callable[[], datetime] | None = None) -> AsyncIterator[RunningRest]:
    """Async twin of run_rest; the server keeps its own thread and event loop."""
    fixture = RestFixture(config, clock=clock)
    running = await asyncio.to_thread(fixture.start)
    try:
        yield running
    finally:
        await asyncio.to_thread(fixture.stop)


def signed_headers(method: str, target: str, body: bytes, *, key_id: str, secret: str, timestamp: int) -> dict:
    """The signed variant's headers for one request (what a client holding the secret would send)."""
    message = "\n".join([method, target, str(timestamp), hashlib.sha256(body).hexdigest()])
    return {"X-Cedar-Key-Id": key_id, "X-Cedar-Timestamp": str(timestamp),
            "X-Cedar-Signature": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}


def configure(args: argparse.Namespace, environ) -> RestConfig:
    data = load(Path(args.config)) if args.config else {}
    if not isinstance(data, dict):
        raise LabError("REST fixture config must be a mapping")
    flags = {"port": args.port, "variant": args.variant, "auth": args.auth, "docs": args.docs, "clock": args.clock,
             "api_key_placement": args.api_key_placement, "api_key_name": args.api_key_name}
    data |= {key: value for key, value in flags.items() if value is not None}
    for option, field in ((args.api_key_env, "api_keys"), (args.oracle_token_env, "oracle_token")):
        if option:
            if not environ.get(option):
                raise LabError(f"Environment variable {option} is not set")
            data[field] = {environ[option]: GUESTS[0][0]} if field == "api_keys" else environ[option]
    return RestConfig.model_validate(data)


def main(argv: list[str] | None = None, environ=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m consumer_agent_testing_tool.rest_fixture",
        description="Serve the loopback Cedar Stays REST API until SIGTERM. Prints one JSON line with its URLs "
                    "(and any generated credentials) when ready. --write-docs DIR writes the documentation bundle "
                    f"(base URL as {SERVICE_URL}) and exits.")
    parser.add_argument("--port", type=int, help="TCP port on 127.0.0.1; 0 picks a free port")
    parser.add_argument("--variant", choices=VARIANTS)
    parser.add_argument("--auth", choices=RestAuth.__args__)
    parser.add_argument("--docs", choices=DocsVariant.__args__)
    parser.add_argument("--config", help="JSON or YAML RestConfig file")
    parser.add_argument("--clock", help="Fixed start time, ISO 8601 with timezone")
    parser.add_argument("--api-key-placement", choices=["header", "bearer", "query"])
    parser.add_argument("--api-key-name", help="Header or query parameter that carries the key (X-Cedar-Key)")
    parser.add_argument("--api-key-env", help="Environment variable holding the API key to accept")
    parser.add_argument("--oracle-token-env", help="Environment variable holding an oracle token")
    parser.add_argument("--write-docs", metavar="DIR", help="Write the documentation bundle and exit")
    args = parser.parse_args(argv)
    try:
        config = configure(args, os.environ if environ is None else environ)
        if args.write_docs:
            for path in write_docs(Path(args.write_docs), config):
                print(path)
            return 0
        fixture = RestFixture(config)
        listener = bind(config.port)
        server = http_server(fixture.build(listener.getsockname()[1]))
    except (LabError, ValidationError, OSError) as error:
        print(f"rest fixture: {describe(error)}", file=sys.stderr)
        return 2

    async def serve():
        task = asyncio.create_task(server.serve(sockets=[listener]))  # The process exit cleans up.
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if server.started:
            try:
                print(json.dumps(fixture.announcement()), flush=True)
            except OSError:
                pass  # A launcher that closed stdout still owns the process lifetime.
        await task
    asyncio.run(serve())
    return 0 if server.started else 2


__all__ = ["DOC_TYPES", "SERVICE_URL", "RestConfig", "RestFixture", "RunningRest", "api_reference", "docs_bundle",
           "key_location", "llms_txt", "openapi_document", "run_rest", "serve_rest", "signed_headers", "write_docs"]

if __name__ == "__main__":
    raise SystemExit(main())
