"""MCP clients for the connector lane: an unofficial Muse template mirror and the official SDK.

``MuseTemplateClient`` reproduces the observable behavior of the unofficial Muse custom-connector
MCP client template (mirrors pinned in ``TEMPLATE_MIRRORS``; the mirrors are unlicensed, so the
behavior is described and cited, never copied). ``SdkClient`` is the official Python SDK baseline.
Both run host-side: credentials are applied here and never reach the model, and nothing here
claims equivalence with Muse.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import itertools
import json
import re
import time
from collections.abc import Collection
from contextlib import aclosing, asynccontextmanager
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx2
import mcp_types as types
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    KNOWN_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    MODERN_PROTOCOL_VERSIONS,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import __version__
from .config import is_loopback
from .errors import LabError, Unsupported
from .http_streams import StreamingTransport
from .linking import Credential, LinkError

TEMPLATE_BASIS = ("unofficial mirror rallat/muse-skills@3b5ae2c160 and "
                  "sneakers-the-rat/muse-skills@25b2494c, first seen 2026-10-15")
# Byte-identical copies, both unlicensed and first seen 2026-10-15 (rallat 00:00Z, sneakers 19:43Z).
TEMPLATE_MIRRORS = (
    {"repository": "rallat/muse-skills", "commit": "3b5ae2c160", "first_seen": "2026-10-15", "license": None,
     "files": ["custom-connector-setup/templates/mcp_client.py",
               "custom-connector-setup/references/mcp-cli.md"]},
    {"repository": "sneakers-the-rat/muse-skills", "commit": "25b2494c", "first_seen": "2026-10-15",
     "license": None, "files": ["workflow-skills/custom-connector-setup/templates/mcp_client.py",
                                "workflow-skills/custom-connector-setup/references/mcp-cli.md"]},
)
# Every template behavior and its basis: "unofficial" cites the pinned mirrors (line numbers of the
# pinned mcp_client.py, sha256 c7173c0e..., and mcp-cli.md, checked 2026-10-15 and 2026-10-17);
# "assumed" marks harness choices, including deliberate deviations from the cited lines.
TEMPLATE_BEHAVIORS = {
    "post-only": ("unofficial", ("mcp_client.py:65 sends POST; the template opens no GET stream "
                                 "and sends no DELETE")),
    "protocol-versions": ("unofficial", "mcp_client.py:19; mcp-cli.md:40 (offers 2025-11-25)"),
    "capabilities": ("unofficial", "mcp_client.py:123 declares empty client capabilities"),
    "tools-capability": ("unofficial", "mcp_client.py:128-134 requires the tools capability"),
    "no-redirects": ("unofficial", "mcp_client.py:26-28 NoRedirect; mcp-cli.md:46"),
    "endpoint-rules": ("unofficial", ("mcp-cli.md:19; mcp_client.py:42 (no query, fragment or "
                                      "user info; port 443)")),
    "timeout": ("unofficial", "mcp_client.py:68 timeout=45; mcp-cli.md:40"),
    "no-retries": ("unofficial", "mcp-cli.md:40,46"),
    "header-credentials": ("unofficial", ("the template sends its credential (an hsurr surrogate) in a "
                                          "header; custom-connector-setup/bin/dynamic_credentials.py:3-5")),
    "tool-allowlist": ("unofficial", "the template enforces an ALLOWED_TOOLS allowlist"),
    "session-per-invocation": ("unofficial", ("mcp-cli.md:40 (keeps the server's session header for one "
                                              "invocation only); mcp_client.py:47,69-72,154-155: one Client per "
                                              "CLI run through muse.exec (mcp-cli.md:3)")),
    "initialized-notification": ("unofficial", ("mcp_client.py:136 sends notifications/initialized (params {}) "
                                                "and :73-76 accepts only HTTP 202 for it ('MCP server did not "
                                                "accept the initialization notification' otherwise); "
                                                "mcp-cli.md:40")),
    "pagination": ("unofficial", ("mcp_client.py:142-143,157: list-tools takes --cursor and sends one tools/list "
                                  "(params {} without a cursor) per invocation, after its own initialize; "
                                  "mcp-cli.md:27: pass the returned nextCursor back with --cursor")),
    "server-requests-unanswered": ("assumed", ("with empty capabilities and POST-only replies, "
                                               "server-initiated requests go unanswered")),
    "loopback-allowance": ("assumed", ("harness allowance for local fixtures; the template itself "
                                       "refuses ports other than 443 (mcp-cli.md:19)")),
    "client-identity": ("assumed", ("deliberate deviation: the template sends clientInfo {name: "
                                    "muse-custom-connector, version: 1} (mcp_client.py:124); the surrogate names the "
                                    "harness instead, so no server or log takes it for Muse. The template's "
                                    "User-Agent (urllib's default) is not shown in the cited lines")),
    "host-credential-refresh": ("assumed", ("not template behavior: the template retries nothing "
                                            "(mcp-cli.md:46); the host's stand-in for authd refreshes the "
                                            "credential once and resends once after a 401, which the server "
                                            "refused before running the request")),
    "invocation-deadline": ("assumed", ("harness choice: the template's urlopen(timeout=45) "
                                        "(mcp_client.py:68) bounds each socket operation; the surrogate also "
                                        "ends a whole invocation after the same timeout, so a server that "
                                        "drips its reply cannot hold a trial open")),
    "session-hint": ("assumed", ("a tool error that names the MCP session (or Mcp-Session-Id) suggests "
                                 "state from an earlier session; a hint, not protocol evidence")),
}
# Only HTTP-level or JSON-RPC evidence from the client protocol proves that a server needs an earlier
# session; tool-error text is a hint (session-hint), because "session" is ordinary domain vocabulary.
SESSION_TEXT = re.compile(r"\bMCP[ -]session\b|\bMcp-Session-Id\b", re.IGNORECASE)
# The whole error message of a Streamable HTTP transport rejecting a session (the Python SDK's "Bad Request:
# Missing session ID", "Session not found", "Not Found: Invalid or expired session ID"; the TypeScript SDK's
# "Bad Request: Mcp-Session-Id header is required", "No valid session ID provided"), never a domain message
# such as "Invalid params: session s-100 is full".
TRANSPORT_SESSION = re.compile(
    r"\s*(?:(?:bad request|not found|error)\s*:\s*)?(?:session not found"
    r"|(?:missing|invalid|unknown|expired|no valid)(?: or (?:invalid|unknown|expired))? (?:mcp[ -])?session[ -]?id"
    r"(?: provided| header)?(?: is (?:required|missing))?|mcp-session-id header is (?:required|missing))\s*\.?\s*",
    re.IGNORECASE)
SDK_VERSION = importlib.metadata.version("mcp")
MAX_PAGES = 100
# Server error text reaching the host and the model is bounded, as the template bounds it.
ERROR_MESSAGE_LIMIT = 500
# Bounded reads: a hostile or broken server cannot exhaust memory through one response.
RESPONSE_LIMIT = 32 * 1024 * 1024
# The SDK keeps an idle GET stream open; its default read timeout for streams is 300 s.
SSE_READ_SECONDS = 300.0
ErrorCategory = Literal["protocol", "transport", "timeout", "http", "redirect", "authentication",
                        "allowlist", "unsupported", "result"]
# What a client does with a successful result whose structuredContent is missing or fails the tool's outputSchema
# (P1.5): a treatment of the client profile, recorded with it in the run lock. Muse's own handling is not public.
OUTPUT_SCHEMA = {
    "lenient": {"basis": "assumed", "detail": "The result is kept as received and the problem noted "
                                              "(output-schema-mismatch, -missing or -invalid); the default."},
    "strict": {"basis": "documented", "source": f"mcp=={importlib.metadata.version('mcp')} mcp/client/session.py "
                                                "ClientSession.validate_tool_result",
               "detail": "The official SDK's ClientSession raises on a missing or invalid structuredContent (or an "
                         "invalid outputSchema) and discards the result: the call's outcome is refused and noted "
                         "(output-schema-refused). Not a Muse behavior."},
}


def _basis(behavior: str) -> dict:
    basis, source = TEMPLATE_BEHAVIORS[behavior]
    return {"basis": basis, "source": source}


class ClientProfile(BaseModel):
    """Observable MCP client behavior; ``basis`` says where it comes from."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["generic-sdk", "muse-template-unofficial-2026-10"]
    protocol_versions: list[str] = Field(min_length=1)  # the first is offered
    timeout_seconds: float = Field(gt=0, le=600)
    retries: int = Field(ge=0)
    follow_redirects: bool
    require_https_443: bool
    session_per_call: bool
    post_only: bool
    basis: str = Field(min_length=1)
    output_schema: Literal["lenient", "strict"] = "lenient"  # OUTPUT_SCHEMA

    @field_validator("protocol_versions")
    @classmethod
    def known_versions(cls, versions):
        if len(set(versions)) != len(versions) or not set(versions) <= set(KNOWN_PROTOCOL_VERSIONS):
            raise ValueError(f"protocol_versions must be distinct MCP revisions from {KNOWN_PROTOCOL_VERSIONS}")
        return versions


GENERIC_PROFILE = ClientProfile(
    name="generic-sdk", protocol_versions=list(reversed(KNOWN_PROTOCOL_VERSIONS)), timeout_seconds=30.0,
    retries=0, follow_redirects=True, require_https_443=False, session_per_call=False, post_only=False,
    basis=f"official SDK mcp=={SDK_VERSION} Client: dual-era negotiation (server/discover, then "
          "initialize), same-origin 307/308 redirects only, GET stream and DELETE on close; "
          "not a Muse behavior")
MUSE_TEMPLATE_PROFILE = ClientProfile(
    name="muse-template-unofficial-2026-10", protocol_versions=["2025-11-25", "2025-06-18", "2025-03-26"],
    timeout_seconds=45.0, retries=0, follow_redirects=False, require_https_443=True,
    session_per_call=True, post_only=True, basis=TEMPLATE_BASIS)
PROFILES = {profile.name: profile for profile in (GENERIC_PROFILE, MUSE_TEMPLATE_PROFILE)}


class McpCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: dict | None  # CallToolResult JSON as received (content, structuredContent, isError, _meta)
    error: dict | None   # {code, message, category}, plus status (HTTP) or kind (linking) when known
    uncertain: bool      # the request may have reached the server but no response arrived; never retried
    elapsed_ms: int = Field(ge=0)
    # tools/call params.arguments and params.name exactly as this client serialized them for the wire;
    # None when no tools/call request was written (refused, or the handshake failed first).
    sent_arguments: dict | None = None
    sent_name: str | None = None

    @model_validator(mode="after")
    def one_outcome(self):
        if (self.result is None) == (self.error is None):
            raise ValueError("A tool call has exactly one of result or error")
        if self.uncertain and self.error is None:
            raise ValueError("Only a call without a response can be uncertain")
        return self


@runtime_checkable
class ConnectorClient(Protocol):
    profile: ClientProfile
    server_info: dict | None
    protocol_version: str | None
    capabilities: dict | None
    instructions: str | None
    observations: list[dict]

    async def connect(self) -> None: ...
    async def list_tools(self) -> list[dict]: ...
    async def call_tool(self, name: str, arguments: dict) -> McpCallResult: ...
    async def close(self) -> None: ...


def make_client(profile: ClientProfile, endpoint: str, credential: Credential, *,
                allowed_tools: Collection[str] | None = None, allow_loopback: bool = True) -> ConnectorClient:
    """The client implementing ``profile``. ``allowed_tools`` mirrors the template's reviewed
    allowlist (calls to other tools are refused before any traffic); ``allow_loopback`` is the
    recorded harness allowance for loopback fixtures under the template's port-443 rule."""
    implementation = MuseTemplateClient if profile.name == MUSE_TEMPLATE_PROFILE.name else SdkClient
    return implementation(profile, endpoint, credential, allowed_tools=allowed_tools,
                          allow_loopback=allow_loopback)


def _supports(profile: ClientProfile, implementation: str):
    template = implementation == "MuseTemplateClient"
    if (profile.name == MUSE_TEMPLATE_PROFILE.name) != template:
        raise LabError(f"Client profile {profile.name} is not implemented by {implementation}")
    rules = {"retries": profile.retries == 0}
    if template:
        rules |= {"post_only": profile.post_only, "follow_redirects": not profile.follow_redirects,
                  "protocol_versions": set(profile.protocol_versions) <= set(HANDSHAKE_PROTOCOL_VERSIONS)}
    else:
        # The SDK transport opens its GET stream and follows same-origin redirects unconditionally.
        rules |= {"post_only": not profile.post_only, "follow_redirects": profile.follow_redirects}
    if unsupported := [name for name, ok in rules.items() if not ok]:
        raise LabError(f"Client profile {profile.name} sets {', '.join(unsupported)} beyond what "
                       f"{implementation} implements")


def _display(url: str) -> str:
    """A URL fit for evidence: no user info, query or fragment, which can carry credentials."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return urlunsplit((parts.scheme, host + (f":{parts.port}" if parts.port else ""), parts.path, "", ""))


def _endpoint_notes(profile: ClientProfile, endpoint: str, *, template: bool, allow_loopback: bool) -> list[dict]:
    refuse = Unsupported if template else LabError
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError as error:
        raise LabError(f"Invalid MCP endpoint: {error}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LabError("The MCP endpoint must be an absolute HTTP(S) URL")
    if "@" in parts.netloc or parts.fragment:
        raise refuse("The MCP endpoint must not contain user info or a fragment"
                     + (" (the unofficial Muse template refuses both)" if template else ""))
    if template and parts.query:
        raise Unsupported("The unofficial Muse MCP template refuses an endpoint with a query string")
    loopback = is_loopback(endpoint)
    if parts.scheme == "http" and not loopback:
        raise LabError("Remote MCP endpoints require HTTPS; plain HTTP is allowed only for loopback fixtures")
    notes = []
    if profile.require_https_443 and (parts.scheme != "https" or port not in (None, 443)):
        if not loopback:
            raise Unsupported(f"Client profile {profile.name} requires HTTPS on port 443; the unofficial "
                              "Muse template refuses any other port")
        if not allow_loopback:
            raise Unsupported(f"Client profile {profile.name} requires HTTPS on port 443 and the loopback "
                              "fixture allowance is disabled")
        notes.append({"kind": "loopback-allowance", "endpoint": _display(endpoint), "count": 1,
                      **_basis("loopback-allowance")})
    return notes


def _ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _reject_constant(value):
    raise ValueError(f"non-standard JSON constant {value}")


class _Failure(Exception):
    """A request that failed; ``uncertain`` when it may have reached the server unanswered."""

    def __init__(self, category: str, message: str, *, code: int | None = None, status: int | None = None,
                 uncertain: bool = False, kind: str | None = None):
        super().__init__(message)
        self.category, self.message, self.code, self.status = category, message, code, status
        self.uncertain, self.kind = uncertain, kind

    def error(self) -> dict:
        error = {"code": self.code, "message": self.message, "category": self.category}
        if self.status is not None:
            error["status"] = self.status
        if self.kind is not None:
            error["kind"] = self.kind
        return error


class _Unauthorized(Exception):
    def __init__(self, status: int, challenge: dict):
        super().__init__(status)
        self.status, self.challenge = status, challenge


def _challenge(header: str) -> dict:
    return {key.lower(): quoted or bare for key, quoted, bare in
            re.findall(r'([A-Za-z_]+)=(?:"([^"]*)"|([^\s,]+))', header or "")}


def _auth_failure(credential, status: int, challenge: dict, refreshed: bool) -> _Failure:
    # The credential knows why a refresh failed (expired, revoked, wrong account); a static key is
    # simply rejected, and with no credential at all the server requires one.
    if (credential.describe() or {}).get("mode") == "none":
        oauth = "resource_metadata" in challenge
        return _Failure("authentication", f"the connector requires authorization (HTTP {status})"
                        + (" and advertises OAuth protected resource metadata; declare auth: {mode: oauth, ...}"
                           if oauth else "; declare connector auth (mode oauth or api_key)"),
                        status=status, kind="required")
    kind = getattr(credential, "failure", None) or (
        "expired" if "expired" in challenge.get("error_description", "").lower() else "rejected")
    note = "after a credential refresh" if refreshed else "and could not be refreshed"
    return _Failure("authentication", f"the connector rejected the credential (HTTP {status}) {note}",
                    status=status, kind=kind)


def _transport_failure(error: httpx2.HTTPError, timeout: float) -> _Failure:
    if isinstance(error, (httpx2.ConnectTimeout, httpx2.PoolTimeout)):
        return _Failure("timeout", f"no connection within {timeout:g} s")
    if isinstance(error, httpx2.TimeoutException):
        return _Failure("timeout", f"no response within {timeout:g} s; the request may have taken effect "
                                   "(not retried)", uncertain=True)
    if isinstance(error, (httpx2.ConnectError, httpx2.LocalProtocolError, httpx2.UnsupportedProtocol)):
        return _Failure("transport", f"{type(error).__name__}: the request could not be sent")
    return _Failure("transport", f"{type(error).__name__}: the connection failed before a response; "
                                 "the request may have taken effect (not retried)", uncertain=True)


def _parse(data: bytes | str):
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise _Failure("protocol", f"invalid JSON in the response ({type(error).__name__})", uncertain=True) from None


async def _read(response: httpx2.Response) -> bytes:
    chunks, total = [], 0
    # Close the byte stream in this task even when leaving early: one left to the garbage collector
    # closes httpx2's connection from another task, which anyio refuses.
    async with aclosing(response.aiter_bytes()) as stream:
        async for chunk in stream:
            total += len(chunk)
            if total > RESPONSE_LIMIT:
                raise _Failure("protocol", f"the response exceeds {RESPONSE_LIMIT} bytes", uncertain=True)
            chunks.append(chunk)
    return b"".join(chunks)


async def _lines(response: httpx2.Response):
    """Event-stream lines (CR, LF or CRLF endings) within the response byte limit."""
    pending, total = b"", 0
    async with aclosing(response.aiter_bytes()) as stream:  # see _read
        async for chunk in stream:
            total += len(chunk)
            if total > RESPONSE_LIMIT:
                raise _Failure("protocol", f"the event stream exceeds {RESPONSE_LIMIT} bytes", uncertain=True)
            pending += chunk
            held = b"\r" if pending.endswith(b"\r") else b""  # a CR may be the first half of CRLF
            *complete, rest = re.split(rb"\r\n|\r|\n", pending[:-1] if held else pending)
            pending = rest + held
            for line in complete:
                yield line.decode("utf-8", "replace")
    if pending := pending.rstrip(b"\r"):
        yield pending.decode("utf-8", "replace")


async def _pages(fetch) -> list[dict]:
    tools, cursor, seen = [], None, set()
    for _ in range(MAX_PAGES):
        page = await fetch(cursor)
        entries = page.get("tools") if isinstance(page, dict) else None
        if not isinstance(entries, list) or not all(
                isinstance(tool, dict) and isinstance(tool.get("name"), str) for tool in entries):
            raise LabError("MCP tools/list returned malformed tool entries", code=1, category="tool/API")
        tools.extend(entries)
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or cursor in seen:
            raise LabError("MCP pagination repeated a cursor", code=1, category="tool/API")
        seen.add(cursor)
    else:
        raise LabError(f"MCP pagination exceeded {MAX_PAGES} pages", code=1, category="tool/API")
    if len({tool["name"] for tool in tools}) != len(tools):
        raise LabError("MCP published duplicate tool names", code=1, category="tool/API")
    return tools


def output_problem(schema: dict, result: dict) -> tuple[str, str] | None:
    """(note kind, where) when a successful result does not meet its tool's outputSchema, judged as the official
    SDK's ClientSession.validate_tool_result judges it: no structuredContent, content that fails the schema, or a
    schema that is not one. Never the offending value, which may be private."""
    from jsonschema import SchemaError
    from jsonschema.exceptions import best_match
    from jsonschema.validators import validator_for
    from referencing import Registry
    if not isinstance(schema, dict):  # The SDK refuses such a listing: a Tool's outputSchema is an object or null.
        return "output-schema-invalid", "/outputSchema"
    if result.get("structuredContent") is None:
        return "output-schema-missing", "/structuredContent"
    try:
        implementation = validator_for(schema)
        implementation.check_schema(schema)
        error = best_match(implementation(schema, registry=Registry()).iter_errors(result["structuredContent"]))
    except (SchemaError, RecursionError, Exception):  # noqa: BLE001 — the server's schema is untrusted input
        return "output-schema-invalid", "/outputSchema"
    if error is None:
        return None
    where = "".join(f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in error.absolute_path)
    return "output-schema-mismatch", f"/structuredContent{where} ({str(error.validator)[:40]})"[:200]


def _refused(name: str) -> McpCallResult:
    return McpCallResult(result=None, uncertain=False, elapsed_ms=0, sent_arguments=None, sent_name=None, error={
        "code": None, "category": "allowlist",
        "message": f"Tool {name!r} is not in the reviewed allowlist; the client refused the call"})


class _Observed:
    """Deduplicated protocol notes; credential values never enter them."""
    credential: Credential
    observations: list[dict]
    profile: ClientProfile
    _schemas: dict  # tool -> outputSchema, from the last tools/list

    def _scrub(self, value):
        if isinstance(value, str):
            for secret in sorted((s for s in self.credential.secrets if isinstance(s, str) and len(s) >= 4),
                                 key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
        return value

    def _note(self, kind: str, **fields):
        fields = {key: self._scrub(value) for key, value in fields.items() if value is not None}
        for item in self.observations:
            if item["kind"] == kind and {k: v for k, v in item.items() if k not in ("kind", "count")} == fields:
                item["count"] += 1
                return
        self.observations.append({"kind": kind, **fields, "count": 1})

    def _require(self):
        if not self._connected:
            raise LabError("Connect the MCP client before using it")

    def _sent(self) -> dict:
        """What the last tools/call request put on the wire (params.name and params.arguments)."""
        name, arguments = self._wire or (None, None)
        return {"sent_arguments": arguments, "sent_name": name}

    def _check_output(self, name: str, result: dict) -> _Failure | None:
        """A successful result against its tool's listed outputSchema (OUTPUT_SCHEMA): a problem is noted, and the
        strict treatment refuses the result: an unreadable outcome, as an invalid CallToolResult is."""
        schema = self._schemas.get(name)
        # As validate_tool_result: only a missing (null) schema skips the check; {} still needs structuredContent.
        if schema is None or result.get("isError"):
            return None
        if (problem := output_problem(schema, result)) is None:
            return None
        kind, where = problem
        if self.profile.output_schema != "strict":
            self._note(kind, tool=name)
            return None
        self._note("output-schema-refused", tool=name, problem=kind, at=where, treatment="strict",
                   basis=OUTPUT_SCHEMA["strict"]["basis"], source=OUTPUT_SCHEMA["strict"]["source"])
        return _Failure("result", f"the {name} result's structuredContent does not meet the tool's outputSchema "
                                  f"({kind.removeprefix('output-schema-')} at {where}); the strict output-schema "
                                  "treatment refuses it", uncertain=True)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()


# Not the template's {"name": "muse-custom-connector", "version": "1"} (mcp_client.py:124): a deliberate deviation
# (TEMPLATE_BEHAVIORS["client-identity"]), so no server or log mistakes the surrogate for Muse.
_TEMPLATE_CLIENT_INFO = {"name": "consumer-agent-testing-tool/muse-template-surrogate", "version": __version__}
_SSL_CONTEXT = None


def _ssl_context():
    # One verified context for every invocation; building one per call costs a certificate load.
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx2.create_ssl_context()
    return _SSL_CONTEXT


class MuseTemplateClient(_Observed):
    """The unofficial Muse custom-connector template: POST-only, one session per invocation.

    Each ``call_tool`` and each page of ``list_tools`` is one CLI-like invocation: initialize
    (offering 2025-11-25, empty capabilities), notifications/initialized (answered with 202, or the
    template refuses the server), then the request, on a fresh HTTP client; the session id lives for
    that invocation only and no DELETE is sent. A page's nextCursor goes to the next invocation, as
    ``list-tools --cursor`` does, so a cursor bound to its session fails there (paging-needs-session).
    Redirects are refused, nothing is retried, and a call whose response never arrived is reported
    uncertain. The template itself never checks a result against its outputSchema; this client notes a
    mismatch (lenient) or refuses the result (strict), the profile's output-schema treatment.
    """

    def __init__(self, profile: ClientProfile, endpoint: str, credential: Credential, *,
                 allowed_tools: Collection[str] | None = None, allow_loopback: bool = True):
        _supports(profile, "MuseTemplateClient")
        self.profile, self.endpoint, self.credential = profile, endpoint, credential
        self.allowed_tools = None if allowed_tools is None else frozenset(allowed_tools)
        self.server_info = self.protocol_version = self.capabilities = self.instructions = None
        self.observations = _endpoint_notes(profile, endpoint, template=True, allow_loopback=allow_loopback)
        placement = (credential.describe() or {}).get("placement")
        if placement in ("query_param", "url_path_segment"):
            raise Unsupported(f"The unofficial Muse MCP template sends credentials in a header only; "
                              f"the {placement} placement needs the generic-sdk profile")
        self._persistent, self._connected, self._first, self._wire = None, False, None, None
        self._schemas = {}

    def _deadline(self):
        """One invocation ends after the profile timeout (TEMPLATE_BEHAVIORS["invocation-deadline"])."""
        return asyncio.timeout(self.profile.timeout_seconds)

    def _expired(self, stage: str) -> _Failure:
        self._note("invocation-deadline", stage=stage, **_basis("invocation-deadline"))
        return _Failure("timeout", f"no complete response within {self.profile.timeout_seconds:g} s; the request "
                                   "may have taken effect (not retried)", uncertain=True)

    async def connect(self):
        if self._connected:
            return
        exchange = _Exchange(self)
        try:
            async with self._deadline():
                await exchange.handshake()
        except _Failure as failure:
            await exchange.aclose()
            raise self._error(failure, "initialize") from None
        except TimeoutError:
            await exchange.aclose()
            raise self._error(self._expired("initialize"), "initialize") from None
        except BaseException:
            await exchange.aclose()
            raise
        if self.profile.session_per_call:
            await exchange.aclose()
        else:
            self._persistent = exchange
        self._connected = True

    async def list_tools(self) -> list[dict]:
        """Every page, as the template's list-tools command reads them (TEMPLATE_BEHAVIORS["pagination"]):
        one invocation per page, the previous page's nextCursor passed to the next. A server that refuses a
        cursor in the new session after an earlier page succeeded keeps its paging state in the MCP session,
        which the template cannot follow: recorded as paging-needs-session and raised as Unsupported."""
        self._require()
        pages = 0

        async def page(cursor):
            nonlocal pages
            pages += 1
            try:
                async with self._deadline(), self._invocation() as exchange:
                    return await exchange.request("tools/list", {"cursor": cursor} if cursor is not None else {})
            except _Failure as failure:
                # A definite refusal (a JSON-RPC error or an HTTP 4xx answer), not a lost or unreadable reply.
                if cursor is not None and self._persistent is None and failure.category in ("protocol", "http") \
                        and not failure.uncertain:
                    self._note("paging-needs-session", page=pages, status=failure.status,
                               **_basis("pagination"))
                    raise Unsupported(
                        f"MCP tools/list failed: page {pages}'s cursor was refused in a new session "
                        f"({self._scrub(failure.message)[:200]}); the unofficial Muse template lists each page "
                        "in its own invocation (list-tools --cursor), so a cursor must not depend on the session "
                        "that issued it") from None
                raise
        try:
            tools = await _pages(page)
        except _Failure as failure:
            raise self._error(failure, "tools/list") from None
        except TimeoutError:
            raise self._error(self._expired("tools/list"), "tools/list") from None
        self._schemas = {tool["name"]: tool.get("outputSchema") for tool in tools}
        return tools

    async def call_tool(self, name: str, arguments: dict) -> McpCallResult:
        self._require()
        started = time.monotonic()
        if self.allowed_tools is not None and name not in self.allowed_tools:
            self._note("allowlist-refused", tool=name, **_basis("tool-allowlist"))
            return _refused(name)
        self._wire = None  # set when the tools/call body is written (_Exchange._post)
        try:
            async with self._deadline(), self._invocation() as exchange:
                result = await exchange.request("tools/call", {"name": name, "arguments": arguments})
        except (_Failure, TimeoutError) as raised:
            failure = raised if isinstance(raised, _Failure) else self._expired("tools/call")
            error = failure.error() | {"message": self._scrub(failure.message)}
            # A failed handshake never sent the tools/call, so it changed nothing.
            return McpCallResult(result=None, error=error, uncertain=failure.uncertain and self._wire is not None,
                                 elapsed_ms=_ms(started), **self._sent())
        except LabError as error:  # a later handshake no longer matches the profile
            return McpCallResult(result=None, uncertain=False, elapsed_ms=_ms(started), error={
                "code": None, "message": self._scrub(str(error)), "category": "unsupported"}, **self._sent())
        if result.get("isError") and SESSION_TEXT.search(json.dumps(
                [result.get("content"), result.get("structuredContent")])[:100_000]):
            # Every invocation is a new session, so state bound to an earlier one is gone. Tool text is a
            # hint only (a "session" can be a yoga class); protocol rejections record needs-session.
            self._note("session-hint", tool=name, signal="a tool error names the MCP session",
                       **_basis("session-hint"))
        if refused := self._check_output(name, result):
            return McpCallResult(result=None, error=refused.error(), uncertain=refused.uncertain,
                                 elapsed_ms=_ms(started), **self._sent())
        return McpCallResult(result=result, error=None, uncertain=False, elapsed_ms=_ms(started), **self._sent())

    async def close(self):
        if self._persistent is not None:
            await self._persistent.aclose()
        self._persistent, self._connected = None, False

    @asynccontextmanager
    async def _invocation(self):
        if self._persistent is not None:
            yield self._persistent
            return
        exchange = _Exchange(self)
        try:
            await exchange.handshake()
            yield exchange
        finally:
            await exchange.aclose()

    def _absorb(self, result: dict, version: str):
        snapshot = {"protocol_version": version, "server_info": result.get("serverInfo"),
                    "capabilities": result.get("capabilities")}
        if self._first is None:
            self._first = snapshot
            self.protocol_version = version
            self.server_info = snapshot["server_info"] if isinstance(snapshot["server_info"], dict) else None
            self.capabilities = snapshot["capabilities"]
            self.instructions = result["instructions"] if isinstance(result.get("instructions"), str) else None
        elif changed := sorted(key for key in snapshot if snapshot[key] != self._first[key]):
            # Each invocation re-initializes, so a server can change between calls.
            self._note("handshake-changed", fields=changed)

    def _error(self, failure: _Failure, stage: str) -> LabError:
        message = f"MCP {stage} failed: {self._scrub(failure.message)}"
        if failure.category == "authentication":
            return LinkError(message, kind=failure.kind or "rejected")
        if failure.category in ("redirect", "unsupported"):
            return Unsupported(message)
        if failure.category in ("timeout", "transport"):
            return LabError(message, category="setup/infrastructure")
        return LabError(message, code=1, category="tool/API")


class _Exchange:
    """One template invocation: its own HTTP client, session id and request ids."""

    def __init__(self, owner: MuseTemplateClient):
        self.owner = owner
        self.http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(owner.profile.timeout_seconds), follow_redirects=False, trust_env=False,
            transport=StreamingTransport(verify=_ssl_context(), trust_env=False),
            headers={"User-Agent": f"consumer-agent-testing-tool/{__version__} ({owner.profile.name})"})
        self.session_id = self.version = None
        self.ids = itertools.count(1)

    async def aclose(self):
        await self.http.aclose()  # no DELETE: the template never terminates its session

    async def handshake(self):
        owner = self.owner
        offered = owner.profile.protocol_versions[0]
        accepted = owner.profile.protocol_versions
        try:
            result = await self.request("initialize", {"protocolVersion": offered, "capabilities": {},
                                                       "clientInfo": _TEMPLATE_CLIENT_INFO})
        except _Failure as failure:
            if failure.category == "protocol" and (failure.code == types.UNSUPPORTED_PROTOCOL_VERSION
                                                   or "version" in failure.message.lower()):
                owner._note("unsupported-version", offered=offered, server_error=failure.message[:200],
                            **_basis("protocol-versions"))
                raise Unsupported(f"The server refused MCP {offered}; the unofficial Muse template "
                                  f"negotiates only {', '.join(accepted)}") from None
            raise
        version = result.get("protocolVersion")
        if version not in accepted:
            owner._note("unsupported-version", offered=offered, negotiated=str(version)[:64],
                        **_basis("protocol-versions"))
            raise Unsupported(f"The server negotiated MCP {str(version)[:64]!r}; the unofficial Muse "
                              f"template accepts only {', '.join(accepted)}")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            owner._note("missing-tools-capability", **_basis("tools-capability"))
            raise Unsupported("The server does not advertise the tools capability, which the unofficial "
                              "Muse template requires")
        self.version = version
        await self.notify("notifications/initialized")
        owner._absorb(result, version)

    async def request(self, method: str, params: dict | None = None) -> dict:
        body = {"jsonrpc": "2.0", "id": next(self.ids), "method": method}
        if params is not None:
            body["params"] = params
        return await self._post(body)

    async def notify(self, method: str):
        # Every template message carries params, {} for notifications/initialized (mcp_client.py:54,136).
        await self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    def _headers(self) -> tuple[str, dict]:
        endpoint = self.owner.endpoint
        url, credential_headers = self.owner.credential.apply(endpoint, {})
        if url != endpoint:
            raise LabError("The credential changed the endpoint URL; the unofficial Muse template sends "
                           "credentials in headers only", category="authentication")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return url, {**credential_headers, **headers}  # protocol headers always win

    async def _post(self, body: dict):
        content = json.dumps(body).encode()
        if body.get("method") == "tools/call":
            # What goes on the wire, parsed back: the runner compares it with the approved tool and arguments.
            params = json.loads(content)["params"]
            self.owner._wire = (params.get("name"), params.get("arguments"))
        for attempt in range(2):
            url, headers = self._headers()
            try:
                async with self.http.stream("POST", url, content=content, headers=headers) as response:
                    return await self._receive(response, body)
            except _Unauthorized as unauthorized:
                # Host-side credential upkeep (Muse's authd/Sentinel role), not a template retry: the
                # server rejected the request before running it, so one resend cannot duplicate it.
                if attempt == 0 and await self.owner.credential.refresh():
                    self.owner._note("credential-refreshed", status=unauthorized.status,
                                     **_basis("host-credential-refresh"))
                    continue
                raise _auth_failure(self.owner.credential, unauthorized.status, unauthorized.challenge,
                                    attempt == 1) from None
            except httpx2.HTTPError as error:
                raise _transport_failure(error, self.owner.profile.timeout_seconds) from None
        raise AssertionError("unreachable")

    async def _receive(self, response: httpx2.Response, body: dict):
        owner, status, method = self.owner, response.status_code, body["method"]
        if 300 <= status < 400:
            location = response.headers.get("location")
            owner._note("redirect-refused", status=status, method=method,
                        location=_display(urljoin(str(response.request.url), location)) if location else None,
                        **_basis("no-redirects"))
            raise _Failure("redirect", f"the endpoint redirected (HTTP {status}); the unofficial Muse "
                                       "template follows no redirects", status=status)
        if status == 401:
            raise _Unauthorized(status, _challenge(response.headers.get("www-authenticate", "")))
        if status == 403:  # a refusal, not a lapsed credential: nothing to refresh
            scope = _challenge(response.headers.get("www-authenticate", "")).get("error") == "insufficient_scope"
            raise _Failure("authentication", "the connector requires a scope this link was not granted" if scope
                           else "the connector refused the request (HTTP 403)", status=status,
                           kind="insufficient-scope" if scope else "rejected")
        if method == "initialize" and (session := response.headers.get("mcp-session-id")):
            if not re.fullmatch(r"[\x21-\x7e]{1,512}", session):
                raise _Failure("protocol", "the server issued an invalid session id")
            self.session_id = session
            owner._note("session-issued", **_basis("session-per-invocation"))
        if "id" not in body:
            if status == 202:
                return None
            if 200 <= status < 300:
                # The template returns only on 202 and refuses the server otherwise (mcp_client.py:73-76), where
                # other clients accept any success: a template incompatibility, not a server error.
                owner._note("notification-refused", method=method, status=status,
                            **_basis("initialized-notification"))
                raise _Failure("unsupported", f"the server answered {method} with HTTP {status}; the unofficial "
                                              "Muse template accepts only 202 Accepted for a notification",
                               status=status)
            raise _Failure("http", f"the server rejected {method} with HTTP {status}", status=status)
        kind = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if status >= 400:
            data = await _read(response)
            message = _jsonrpc_error(data)
            text = (message["message"] if message else data[:2000].decode("utf-8", "replace")).lower()
            # Protocol evidence only: the transport's own rejection, or a session rejected after this client
            # sent one the server issued. A stateless server's domain error ("session s-100 is full") is none.
            sent = self.session_id is not None
            if status in (400, 404) and (TRANSPORT_SESSION.fullmatch(text) or sent and "session" in text):
                owner._note("needs-session", status=status, method=method, sent_session=sent,
                            **_basis("session-per-invocation"))
            if message:
                raise _Failure("protocol", message["message"], code=message["code"], status=status)
            raise _Failure("http", f"the server answered {method} with HTTP {status}", status=status,
                           uncertain=status >= 500)
        if kind == "application/json":
            return self._match(_parse(await _read(response)), body["id"])
        if kind == "text/event-stream":
            return await self._events(response, body["id"])
        raise _Failure("protocol", f"unexpected response content type {kind or 'none'!r} for {method}",
                       status=status, uncertain=True)

    async def _events(self, response: httpx2.Response, request_id: int) -> dict:
        event, data = "message", []
        async with aclosing(_lines(response)) as lines:  # the response usually arrives before the stream ends
            async for line in lines:
                if line:
                    if not line.startswith(":"):
                        field, _, value = line.partition(":")
                        value = value.removeprefix(" ")
                        if field == "event":
                            event = value
                        elif field == "data":
                            data.append(value)
                    continue
                if data and event == "message" and (result := self._event("\n".join(data), request_id)) is not None:
                    return result
                event, data = "message", []
        if data and event == "message" and (result := self._event("\n".join(data), request_id)) is not None:
            return result
        raise _Failure("transport", "the event stream ended without a response; the request may have taken "
                                    "effect (not retried)", uncertain=True)

    def _event(self, text: str, request_id: int) -> dict | None:
        try:
            message = _parse(text)
        except _Failure:
            self.owner._note("invalid-event")
            return None
        if isinstance(message, dict) and isinstance(message.get("method"), str):
            method = message["method"][:100]
            if "id" in message:  # the template neither answers nor declares capabilities for these
                self.owner._note("server-request", method=method, **_basis("server-requests-unanswered"))
            else:
                self.owner._note("server-notification", method=method)
            return None
        if isinstance(message, dict) and type(message.get("id")) is int and message["id"] == request_id:
            return self._match(message, request_id)
        self.owner._note("unmatched-response")
        return None

    @staticmethod
    def _match(message, request_id: int) -> dict:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or type(message.get("id")) is not int \
                or message["id"] != request_id:
            raise _Failure("protocol", "the response is not a JSON-RPC reply to this request", uncertain=True)
        if isinstance(error := message.get("error"), dict):
            code = error.get("code")
            raise _Failure("protocol", str(error.get("message") or "JSON-RPC error")[:500],
                           code=code if type(code) is int else None)
        if not isinstance(result := message.get("result"), dict):
            raise _Failure("protocol", "the JSON-RPC reply carries no result object", uncertain=True)
        return result


def _jsonrpc_error(data: bytes) -> dict | None:
    try:
        message = json.loads(data, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    error = message.get("error") if isinstance(message, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        code = error.get("code")
        return {"code": code if type(code) is int else None, "message": error["message"][:500]}
    return None


def _leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from _leaves(child)
    else:
        yield error


def _cause(error: BaseException) -> BaseException:
    """The informative leaf of an SDK task-group failure."""
    from mcp.shared.exceptions import MCPError
    leaves = list(_leaves(error))
    for kind in (LinkError, MCPError, httpx2.HTTPError, LabError, ValidationError):
        for leaf in leaves:
            if isinstance(leaf, kind):
                return leaf
    return leaves[0] if leaves else error


class _Closed(Exception):
    pass


class _SessionTask:
    """One SDK session owned by its own task: a transport failure cancels that task, never the caller."""

    def __init__(self, opener):
        self._opener, self._queue = opener, asyncio.Queue()
        self._task = self._ready = self._current = None

    async def start(self):
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._main())
        await self._ready

    async def run(self, operation):
        if self._task is None or self._task.done():
            raise _Closed("the SDK session is closed")
        future = asyncio.get_running_loop().create_future()
        await self._queue.put((operation, future))
        return await future

    async def stop(self):
        if self._task is None or self._task.done():
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 10)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _main(self):
        failure: BaseException = _Closed("the SDK session is closed")
        try:
            async with self._opener() as client:
                self._ready.set_result(None)
                while (item := await self._queue.get()) is not None:
                    operation, future = item
                    # Stays set if the session dies mid-operation, so the finally below fails it.
                    self._current = future
                    try:
                        value = await operation(client)
                    except Exception as error:  # noqa: BLE001 - the caller maps it
                        if not future.done():
                            future.set_exception(error)
                    else:
                        if not future.done():
                            future.set_result(value)
                    self._current = None
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - the session died; its waiters get the cause
            failure = error
        finally:
            waiters = [self._current, self._ready] + [item[1] for item in self._drain()]
            for future in waiters:
                if future is not None and not future.done():
                    future.set_exception(failure)

    def _drain(self):
        while not self._queue.empty():
            if (item := self._queue.get_nowait()) is not None:
                yield item


class _Bounded(httpx2.AsyncByteStream):
    """A response body within RESPONSE_LIMIT, so a hostile or broken server cannot exhaust memory."""

    def __init__(self, stream, owner: SdkClient):
        self.stream, self.owner, self.total = stream, owner, 0

    async def __aiter__(self):
        chunks = self.stream.__aiter__()
        try:
            async for chunk in chunks:
                self.total += len(chunk)
                if self.total > RESPONSE_LIMIT:
                    self.owner._oversized = True
                    raise httpx2.ReadError(f"the response exceeds {RESPONSE_LIMIT} bytes")
                yield chunk
        finally:
            # Closing the transport alone does not unwind a suspended iterator's task-owned scopes.
            if close := getattr(chunks, "aclose", None):
                await close()

    async def aclose(self):
        await self.stream.aclose()


class _Delivery(httpx2.AsyncBaseTransport):
    """Counts MCP POSTs that may have reached the server: only those can make a call uncertain.

    The SDK reports a dead connection as CONNECTION_CLOSED whether or not the request left.
    Every response body is bounded (RESPONSE_LIMIT), as the template bounds its reads."""

    def __init__(self, owner: SdkClient):
        self.owner, self.inner = owner, StreamingTransport(trust_env=False)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        counted = int(request.method == "POST" and SdkClient._mcp_traffic(request))
        self.owner._delivered += counted  # in flight: from here on it may reach the server
        try:
            response = await self.inner.handle_async_request(request)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.UnsupportedProtocol):
            self.owner._delivered -= counted  # it never left
            raise
        response.stream = _Bounded(response.stream, self.owner)
        return response

    async def aclose(self):
        await self.inner.aclose()


class _CredentialAuth(httpx2.Auth):
    """Adds the host-side credential to SDK requests; a 401 gets one refresh and resend."""

    def __init__(self, owner: SdkClient):
        self.owner = owner

    async def async_auth_flow(self, request):
        self._apply(request)
        response = yield request
        if response.status_code == 401 and await self.owner.credential.refresh():
            self.owner._note("credential-refreshed", status=401, **_basis("host-credential-refresh"))
            self._apply(request)
            yield request

    def _apply(self, request: httpx2.Request):
        url, headers = self.owner.credential.apply(str(request.url), {})
        request.url = httpx2.URL(url)
        request.headers.update(headers)


class SdkClient(_Observed):
    """The official MCP Python SDK client: dual-era negotiation, one persistent session by default.

    ``mode="auto"`` probes server/discover (2026-07-28) and falls back to the initialize handshake.
    Results are returned as received: under the lenient output-schema treatment (the default) a
    structuredContent that fails its output schema is kept and noted; the strict one refuses it, as
    ``ClientSession.call_tool`` would (OUTPUT_SCHEMA).
    """

    def __init__(self, profile: ClientProfile, endpoint: str, credential: Credential, *,
                 allowed_tools: Collection[str] | None = None, allow_loopback: bool = True):
        _supports(profile, "SdkClient")
        self.profile, self.endpoint, self.credential = profile, endpoint, credential
        self.allowed_tools = None if allowed_tools is None else frozenset(allowed_tools)
        self.server_info = self.protocol_version = self.capabilities = self.instructions = None
        self.observations = _endpoint_notes(profile, endpoint, template=False, allow_loopback=allow_loopback)
        # A credential may bring its own httpx2 auth (the SDK OAuth provider); others are applied here.
        self._auth = getattr(credential, "httpx_auth", None) or _CredentialAuth(self)
        self._mode = "auto" if set(profile.protocol_versions) & set(MODERN_PROTOCOL_VERSIONS) else "legacy"
        self._schemas, self._task, self._connected, self._status, self._delivered = {}, None, False, None, 0
        self._wire, self._oversized, self._challenge = None, False, {}
        # The credential's own request guards (linking: HTTPS, pinned authorization origins) apply to the
        # SDK OAuth provider's discovery and token requests during a run, not only while linking.
        hooks = getattr(credential, "event_hooks", None)
        self._hooks = hooks() if callable(hooks) else {}

    async def connect(self):
        if self._connected:
            return
        try:
            if self.profile.session_per_call:
                await self._once(lambda client: asyncio.sleep(0))
            else:
                task = _SessionTask(self._session)
                await task.start()
                self._task = task
        except Exception as error:  # noqa: BLE001 - normalized for the runner
            raise self._error(error, "initialize") from None
        self._connected = True

    async def list_tools(self) -> list[dict]:
        self._require()

        async def fetch(client):
            async def page(cursor):
                listed = await client.list_tools(cursor=cursor, cache_mode="bypass")
                return listed.model_dump(mode="json", by_alias=True, exclude_unset=True)
            return await _pages(page)
        try:
            tools = await self._run(fetch)
        except Exception as error:  # noqa: BLE001 - normalized for the runner
            raise self._error(error, "tools/list") from None
        self._schemas = {tool["name"]: tool.get("outputSchema") for tool in tools}
        return tools

    async def call_tool(self, name: str, arguments: dict) -> McpCallResult:
        self._require()
        started = time.monotonic()
        if self.allowed_tools is not None and name not in self.allowed_tools:
            self._note("allowlist-refused", tool=name)
            return _refused(name)

        # Per call, before the session runs it: a closed session never reaches call() below.
        self._status, self._delivered, self._wire, self._oversized = None, 0, None, False

        async def call(client):
            self._status, self._delivered, self._wire, self._oversized = None, 0, None, False
            request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
            result = await client.session.send_request(request, types.CallToolResult)
            return result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        try:
            result = await self._run(call)
        except Exception as error:  # noqa: BLE001 - every failure becomes a call outcome
            failure = self._call_failure(_cause(error))
            return McpCallResult(result=None, error=failure.error() | {"message": self._scrub(failure.message)},
                                 uncertain=failure.uncertain, elapsed_ms=_ms(started), **self._sent())
        if refused := self._check_output(name, result):
            return McpCallResult(result=None, error=refused.error(), uncertain=refused.uncertain,
                                 elapsed_ms=_ms(started), **self._sent())
        return McpCallResult(result=result, error=None, uncertain=False, elapsed_ms=_ms(started), **self._sent())

    async def close(self):
        if self._task is not None:
            await self._task.stop()
        self._task, self._connected = None, False

    async def _run(self, operation):
        return await (self._task.run(operation) if self._task is not None else self._once(operation))

    async def _once(self, operation):
        async def main():
            async with self._session() as client:
                return await operation(client)
        return await asyncio.create_task(main())

    @asynccontextmanager
    async def _session(self):
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        timeout = self.profile.timeout_seconds
        async with httpx2.AsyncClient(
                auth=self._auth, timeout=httpx2.Timeout(timeout, read=max(timeout, SSE_READ_SECONDS)),
                follow_redirects=False, trust_env=False, transport=_Delivery(self),
                headers={"User-Agent": f"consumer-agent-testing-tool/{__version__} ({self.profile.name})"},
                event_hooks={"request": [self._on_request, *self._hooks.get("request", ())],
                             "response": [self._on_response, *self._hooks.get("response", ())]}) as http:
            transport = streamable_http_client(self.endpoint, http_client=http,
                                               terminate_on_close=not self.profile.post_only)
            client = Client(transport, mode=self._mode, read_timeout_seconds=timeout, cache=None,
                            client_info=types.Implementation(name="consumer-agent-testing-tool/generic-sdk",
                                                             version=__version__),
                            message_handler=self._on_message)
            async with client:
                self._absorb(client)
                yield client

    def _absorb(self, client):
        version = client.protocol_version
        if version not in self.profile.protocol_versions:
            self._note("unsupported-version", negotiated=version)
            raise Unsupported(f"The server negotiated MCP {version}, outside client profile {self.profile.name} "
                              f"({', '.join(self.profile.protocol_versions)})")
        self.protocol_version = version
        info = client.server_info
        self.server_info = info.model_dump(mode="json", by_alias=True, exclude_none=True) if info else None
        self.capabilities = client.server_capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
        self.instructions = client.instructions
        self._note("negotiated", version=version,
                   era="modern" if version in MODERN_PROTOCOL_VERSIONS else "handshake")
        if "tools" not in self.capabilities:
            self._note("missing-tools-capability")

    @staticmethod
    def _mcp_traffic(request: httpx2.Request) -> bool:
        # MCP transport requests accept event streams; discovery and token requests from an auth flow do not.
        return "text/event-stream" in request.headers.get("accept", "")

    async def _on_request(self, request: httpx2.Request):
        if not self._mcp_traffic(request):
            return
        if request.method == "GET":
            self._note("get-stream-opened")
        elif request.method == "DELETE":
            self._note("session-delete-sent")
        elif request.method == "POST":
            try:
                body = json.loads(request.content)
            except (httpx2.RequestNotRead, ValueError):
                body = None
            if isinstance(body, dict) and "method" not in body and "id" in body:
                self._note("server-request-answered", outcome="error" if "error" in body else "result")
            if isinstance(body, dict) and body.get("method") == "tools/call":
                # What goes on the wire: the runner compares it with the approved tool and arguments.
                params = body.get("params")
                self._wire = (params.get("name"), params.get("arguments")) if isinstance(params, dict) else None

    async def _on_response(self, response: httpx2.Response):
        request = response.request
        if not self._mcp_traffic(request):
            return
        if request.method == "POST":
            self._status = response.status_code
            if response.status_code == 401:
                self._challenge = _challenge(response.headers.get("www-authenticate", ""))
        if response.has_redirect_location:
            location = urljoin(str(request.url), response.headers["location"])
            followed = response.status_code in (307, 308) and _display(location).split("/")[:3] == \
                _display(str(request.url)).split("/")[:3]
            self._note("redirect", status=response.status_code, location=_display(location), followed=followed)
        if request.method == "POST" and response.headers.get("mcp-session-id"):
            self._note("session-issued")

    async def _on_message(self, message):
        if isinstance(message, Exception):
            self._note("transport-exception", type=type(message).__name__)
        else:
            self._note("server-notification", method=getattr(message, "method", type(message).__name__))

    def _call_failure(self, leaf: BaseException) -> _Failure:
        from mcp.shared.exceptions import MCPError
        status = self._status
        if isinstance(leaf, LinkError):
            return _Failure("authentication", str(leaf), kind=leaf.kind)
        if isinstance(leaf, _Closed):
            return _Failure("transport", str(leaf))
        sent = self._delivered > 0
        if self._oversized:
            return _Failure("protocol", f"the response exceeds {RESPONSE_LIMIT} bytes", uncertain=sent)
        if isinstance(leaf, MCPError):
            message = str(leaf.message)[:ERROR_MESSAGE_LIMIT]
            # -32000 and -32001 are also ordinary server error codes: only the SDK's own messages mean
            # that no answer arrived (mcp 2.2.0 jsonrpc_dispatcher).
            if leaf.code == types.REQUEST_TIMEOUT and re.fullmatch(r"Request '.*' timed out", message):
                return _Failure("timeout", f"no response within {self.profile.timeout_seconds:g} s"
                                + ("; the request may have taken effect (not retried)" if sent else ""),
                                code=leaf.code, uncertain=sent)
            if leaf.code == types.CONNECTION_CLOSED and message == "Connection closed":
                return _Failure("transport", message + ("; the request may have taken effect (not retried)"
                                                        if sent else "; the request was not sent"),
                                code=leaf.code, uncertain=sent)
            if status in (401, 403):
                return _auth_failure(self.credential, status, self._challenge, False)
            if status is not None and 300 <= status < 400:
                return _Failure("redirect", message, code=leaf.code, status=status)
            # The SDK stands in this message for an HTTP error without a JSON-RPC body.
            synthesized = message == "Server returned an error response"
            failed = status if status is not None and status >= 400 else None
            return _Failure("http" if synthesized else "protocol", message, code=leaf.code, status=failed,
                            uncertain=synthesized and failed is not None and failed >= 500)
        if isinstance(leaf, httpx2.HTTPError):
            failure = _transport_failure(leaf, self.profile.timeout_seconds)
            failure.uncertain = failure.uncertain and sent
            return failure
        if isinstance(leaf, ValidationError):
            return _Failure("result", "the tools/call result is not a valid CallToolResult", uncertain=True)
        if isinstance(leaf, LabError):
            return _Failure("unsupported", str(leaf))
        return _Failure("transport", f"{type(leaf).__name__}: the call failed without a response", uncertain=sent)

    def _error(self, error: BaseException, stage: str) -> LabError:
        from mcp.shared.exceptions import MCPError
        leaf = _cause(error)
        if self._oversized:
            return LabError(f"MCP {stage} failed: the response exceeds {RESPONSE_LIMIT} bytes", code=1,
                            category="tool/API")
        if isinstance(leaf, LabError):
            return leaf
        if isinstance(leaf, RuntimeError) and "protocol version" in str(leaf).lower():
            self._note("unsupported-version", offered=LATEST_HANDSHAKE_VERSION)
            return Unsupported(f"MCP {stage} failed: {self._scrub(str(leaf))}")
        if isinstance(leaf, MCPError) and self._status in (401, 403):
            failure = _auth_failure(self.credential, self._status, self._challenge, False)
            return LinkError(f"MCP {stage} failed: {failure.message}", kind=failure.kind)
        if isinstance(leaf, (MCPError, ValidationError)):
            return LabError(f"MCP {stage} failed: {self._scrub(str(leaf))[:500]}", code=1, category="tool/API")
        return LabError(f"MCP {stage} failed: {type(leaf).__name__}", category="setup/infrastructure")


__all__ = [
    "GENERIC_PROFILE",
    "MUSE_TEMPLATE_PROFILE",
    "OUTPUT_SCHEMA",
    "PROFILES",
    "TEMPLATE_BASIS",
    "TEMPLATE_BEHAVIORS",
    "TEMPLATE_MIRRORS",
    "ClientProfile",
    "ConnectorClient",
    "McpCallResult",
    "MuseTemplateClient",
    "SdkClient",
    "make_client",
    "output_problem",
]
