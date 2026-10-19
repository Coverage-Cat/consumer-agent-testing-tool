"""Host-side connector linking: no auth, API keys, and OAuth 2.1 through the official SDK provider.

The host plays Muse's trusted control plane (authd and Sentinel; documented in Meta's security
post, research.meta.ai "How We Built Safety Into Muse", 2026-09-08): it obtains, stores and
applies credentials, so the model never sees one. Every value that could disclose a credential is
listed in ``Credential.secrets`` for the Redactor, and httpx2 URL logs are scrubbed of them.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from secrets import token_urlsafe
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from . import __version__
from .config import is_loopback
from .errors import LabError
from .evidence import admissible_secret
from .http_streams import StreamingTransport
from .serialization import digest

if TYPE_CHECKING:
    from .mcp_clients import ClientProfile

LinkKind = Literal["missing", "required", "rejected", "revoked", "expired", "wrong-account", "insufficient-scope",
                   "discovery", "registration", "authorization"]
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 token
QUERY_NAME = re.compile(r"[A-Za-z0-9._~-]+")
SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")  # RFC 6749 scope-token
# Names a credential header must not take over: transport framing and MCP protocol headers, and
# Cookie (cookie auth is not a Muse connector path; unofficial mirrors decline it).
RESERVED_HEADERS = frozenset({"host", "content-length", "content-type", "transfer-encoding", "connection",
                              "accept", "cookie", "user-agent", "mcp-session-id", "mcp-protocol-version",
                              "last-event-id"})
PATH_PLACEHOLDER = "{api_key}"
_PLACEHOLDER = re.compile(r"\{api_key\}|%7Bapi_key%7D", re.IGNORECASE)
MIN_KEY_LENGTH = 8  # shorter keys are refused: the Redactor ignores short environment values
INTERACTIVE_TIMEOUT = 300.0
MAX_CONSENT_STEPS = 10
STORE_VERSION = 1
RUN_MARKERS = ("events.jsonl", "result.json", "manifest.json", "checkpoint.private.json")
CLIENT_NAME = "Consumer Agent Testing Tool (connector surrogate)"
USER_AGENT = f"consumer-agent-testing-tool/{__version__} (linking)"
# Any authenticated request reveals whether the endpoint challenges for OAuth; a ping changes nothing.
_PROBE = {"jsonrpc": "2.0", "id": "catt-link-probe", "method": "ping"}
_PROBE_VERSION = "2025-11-25"  # a handshake revision, so the SDK sends the RFC 8707 resource


class LinkError(LabError):
    """A credential problem the developer can act on; category "authentication".

    kind: missing (credential variable unset), required (the target requires a credential and none is
    declared), rejected (the target refused the credential), revoked, expired, wrong-account,
    insufficient-scope, discovery (RFC 9728/8414 metadata or PKCE support),
    registration (DCR, CIMD or preregistered client), authorization (authorization request, login or
    token exchange).
    """

    def __init__(self, message: str, *, kind: str, code: int = 2):
        super().__init__(message, code=code, category="authentication")
        self.kind = kind


class _StaleClient(LinkError):
    """The authorization server no longer knows the client a link registered (a restarted fixture, a deleted
    registration): _link_oauth discards a stored client and registers again, once."""

    def __init__(self, message: str):
        super().__init__(message, kind="registration")


# An authorization endpoint that does not know the client answers with an error and no redirect (RFC 6749
# section 4.1.2.1): invalid_client, or invalid_request naming the client id as unknown.
_UNKNOWN_CLIENT = re.compile(r"\bclient(?:[ _]?id)?\b.{0,80}\b(?:not found|unknown|not registered|does not exist|"
                             r"invalid)\b|\b(?:unknown|invalid|unregistered)\s+client\b", re.IGNORECASE)


def _unknown_client(response: httpx2.Response) -> bool:
    """Whether an authorization or token endpoint answered that it does not know the client."""
    if response.status_code not in (400, 401):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    error, description = body.get("error"), str(body.get("error_description") or "")[:500]
    return error == "invalid_client" or error == "invalid_request" and bool(_UNKNOWN_CLIENT.search(description))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _env_name(value: str | None, field: str):
    if value is not None and not ENV_NAME.fullmatch(value):
        raise ValueError(f"{field} must name an uppercase environment variable; scenarios never hold credentials")


class NoAuth(_Strict):
    mode: Literal["none"] = "none"


class ApiKeyAuth(_Strict):
    """A static key read from a coordinator environment variable. url_path_segment replaces the
    endpoint's ``{api_key}`` path segment, or appends the key as the last segment."""
    mode: Literal["api_key"]
    env: str
    placement: Literal["bearer_header", "custom_header", "query_param", "url_path_segment"] = "bearer_header"
    name: str | None = None  # header or query parameter name; required for custom_header/query_param

    @model_validator(mode="after")
    def references(self):
        _env_name(self.env, "env")
        named = self.placement in ("custom_header", "query_param")
        if named != (self.name is not None):
            raise ValueError(f"name is {'required' if named else 'not used'} for placement {self.placement}")
        if self.placement == "custom_header" and (
                not HEADER_NAME.fullmatch(self.name) or self.name.lower() in RESERVED_HEADERS):
            raise ValueError("name must be an HTTP header name outside the protocol and cookie headers")
        if self.placement == "query_param" and not QUERY_NAME.fullmatch(self.name):
            raise ValueError("name must be a plain query parameter name")
        return self


class OAuthAuth(_Strict):
    mode: Literal["oauth"]
    registration: Literal["dcr", "cimd", "preregistered"] = "dcr"
    client_id_env: str | None = None      # preregistered
    client_secret_env: str | None = None  # preregistered confidential client
    client_metadata_url: str | None = None  # cimd
    scopes: list[str] = []
    read_only_scopes: list[str] = []      # guidelines 3.1: offer read-only linking where possible
    expected_subject: str | None = None   # wrong-account detection
    consent: Literal["fixture-auto", "interactive"] = "interactive"
    test_login_env: str | None = None     # fixture-auto: "user:password" for the loopback fixture AS
    redirect_port: int | None = Field(default=None, ge=1024, le=65535)  # fixed loopback redirect port

    @model_validator(mode="after")
    def references(self):
        for field in ("client_id_env", "client_secret_env", "test_login_env"):
            _env_name(getattr(self, field), field)
        prereg, cimd = self.registration == "preregistered", self.registration == "cimd"
        if prereg != (self.client_id_env is not None) or (self.client_secret_env and not prereg):
            raise ValueError("client_id_env (and optional client_secret_env) are for preregistered clients only")
        if cimd != (self.client_metadata_url is not None):
            raise ValueError("client_metadata_url is required for cimd and used by nothing else")
        if cimd:
            parts = urlsplit(self.client_metadata_url)
            if parts.scheme != "https" or parts.path in ("", "/") or parts.fragment or "@" in parts.netloc:
                raise ValueError("client_metadata_url must be an HTTPS URL with a path (a CIMD client id)")
        if (self.consent == "fixture-auto") != (self.test_login_env is not None):
            raise ValueError("test_login_env is required for fixture-auto consent and used by nothing else")
        for field in ("scopes", "read_only_scopes"):
            values = getattr(self, field)
            if len(set(values)) != len(values) or not all(SCOPE_TOKEN.fullmatch(value) for value in values):
                raise ValueError(f"{field} must be distinct RFC 6749 scope tokens")
        if self.expected_subject is not None and not self.expected_subject.strip():
            raise ValueError("expected_subject must not be empty")
        return self


TargetAuth = Annotated[NoAuth | ApiKeyAuth | OAuthAuth, Field(discriminator="mode")]
_TARGET_AUTH = TypeAdapter(TargetAuth)


@runtime_checkable
class Credential(Protocol):
    """A host-side credential. ``secrets`` lists every value to register with the Redactor."""
    secrets: list[str]

    def apply(self, url: str, headers: dict) -> tuple[str, dict]: ...  # adds the real credential
    async def refresh(self) -> bool: ...  # after a 401; False when the connector must be relinked
    def describe(self) -> dict: ...       # redacted: mode, placement, scopes, subject, expires_at


class _LogGuard(logging.Filter):
    """httpx2 logs every request URL at INFO, and the SDK logs transport failures with their exception
    (whose text names the URL), which would show query and path credentials. Every record any logger
    creates is scrubbed: its message, its exception text and its stack."""

    def __init__(self):
        super().__init__()
        self.secrets = set()

    def add(self, values):
        self.secrets.update(value for value in values if isinstance(value, str) and len(value) >= 4)

    def scrub(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def filter(self, record):
        if not self.secrets:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # a malformed record is the logging module's to report
            message = None
        if message is not None and (text := self.scrub(message)) != message:
            record.msg, record.args = text, None
        if record.exc_info and not record.exc_text:
            formatted = logging.Formatter().formatException(record.exc_info)
            if (scrubbed := self.scrub(formatted)) != formatted:
                # Handlers that render exc_info themselves would show the original: keep the text only.
                record.exc_info, record.exc_text = None, scrubbed
        elif record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)
        return True


_LOG_GUARD = _LogGuard()
for _logger in ("httpx2", "httpx"):
    logging.getLogger(_logger).addFilter(_LOG_GUARD)
_RECORD_FACTORY = logging.getLogRecordFactory()


def _guarded_record(*args, **kwargs):
    """A logger filter sees only records created on its own logger; the factory sees them all."""
    record = _RECORD_FACTORY(*args, **kwargs)
    _LOG_GUARD.filter(record)
    return record


if not getattr(_RECORD_FACTORY, "catt_guarded", False):
    _guarded_record.catt_guarded = True
    logging.setLogRecordFactory(_guarded_record)


def _register(values, redactor=None) -> list[str]:
    values = [value for value in values if isinstance(value, str) and len(value) >= 4]
    _LOG_GUARD.add(values)
    if redactor is not None and values:
        redactor.add(values)
    return values


def _origin(url: str) -> tuple:
    parts = urlsplit(str(url))
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or {"https": 443, "http": 80}.get(scheme)


def _loopback_host(host: str | None) -> bool:
    return bool(host) and is_loopback(f"http://{'[' + host + ']' if ':' in host else host}/")


def _environment(name: str, purpose: str, model=None) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise LinkError(f"Set the {purpose} environment variable {name}; scenarios name the variable, "
                        "never the value", kind="missing")
    from .destinations import target_credential
    try:  # the same separation rule as every other target credential, including the selected model's key
        target_credential(SimpleNamespace(auth=SimpleNamespace(mode="api_key", env=name)), model)
    except LabError as error:
        raise LinkError(str(error), kind="rejected") from None
    return value


def _check_endpoint(endpoint: str, auth):
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LabError("The connector endpoint must be an absolute HTTP(S) URL")
    if "@" in parts.netloc or parts.fragment:
        raise LabError("The connector endpoint must not contain user info or a fragment")
    if parts.scheme != "https" and not is_loopback(endpoint):
        raise LabError("Remote connector endpoints require HTTPS; credentials never travel in cleartext")
    if _PLACEHOLDER.search(endpoint):
        if not (isinstance(auth, ApiKeyAuth) and auth.placement == "url_path_segment"):
            raise LabError("The endpoint has an {api_key} placeholder, but the key is not placed in the path")
        if f"/{PATH_PLACEHOLDER}/" not in parts.path + "/":
            raise LabError("The {api_key} placeholder must be a whole path segment")


class NoCredential:
    @property
    def secrets(self) -> list[str]:
        return []

    def apply(self, url: str, headers: dict) -> tuple[str, dict]:
        return url, dict(headers)

    async def refresh(self) -> bool:
        return False

    def describe(self) -> dict:
        return {"mode": "none", "placement": None, "scopes": [], "subject": None, "expires_at": None}


class ApiKeyCredential:
    def __init__(self, auth: ApiKeyAuth, endpoint: str, key: str):
        self.auth, self.endpoint, self._key = auth, endpoint, key
        self._origin = _origin(endpoint)

    @property
    def secrets(self) -> list[str]:
        return list(dict.fromkeys([self._key, quote(self._key, safe="")]))

    def apply(self, url: str, headers: dict) -> tuple[str, dict]:
        headers = dict(headers)
        if _origin(url) != self._origin:
            return url, headers  # never add the key to a request for another origin
        placement = self.auth.placement
        if placement == "bearer_header":
            headers["Authorization"] = f"Bearer {self._key}"
        elif placement == "custom_header":
            headers[self.auth.name] = self._key
        elif placement == "query_param":
            parts = urlsplit(url)
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != self.auth.name]
            url = urlunsplit(parts._replace(query=urlencode([*query, (self.auth.name, self._key)], quote_via=quote)))
        else:
            segment = quote(self._key, safe="")
            if _PLACEHOLDER.search(url):
                url = _PLACEHOLDER.sub(lambda _: segment, url)
            elif url.rstrip("/") == self.endpoint.rstrip("/"):
                parts = urlsplit(url)
                url = urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/" + segment))
        return url, headers

    async def refresh(self) -> bool:
        return False  # a static key cannot be refreshed; the developer rotates it

    def describe(self) -> dict:
        return {"mode": "api_key", "placement": self.auth.placement, "name": self.auth.name, "env": self.auth.env,
                "scopes": [], "subject": None, "expires_at": None}


def _link_api_key(auth: ApiKeyAuth, endpoint: str, redactor, model=None) -> ApiKeyCredential:
    key = _environment(auth.env, "API key", model)
    if re.search(r"[\s\x00-\x1f\x7f]", key):
        raise LinkError(f"The API key in {auth.env} contains whitespace or control characters", kind="rejected")
    if len(key) < MIN_KEY_LENGTH or admissible_secret(key) is None:
        raise LinkError(f"The API key in {auth.env} is too short to redact reliably "
                        f"({MIN_KEY_LENGTH} characters minimum)", kind="rejected")
    credential = ApiKeyCredential(auth, endpoint, key)
    _register(credential.secrets, redactor)
    return credential


def _write_private(path: Path, value: dict):
    text = json.dumps(value, sort_keys=True)
    temporary = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _store_directory(store: Path) -> Path:
    """A private directory inside the workspace, outside the shareable runs/ and exports/ trees."""
    root = Path.cwd().resolve()
    path = (root / store).resolve()
    if not path.is_relative_to(root):
        raise LabError("The token store must be inside the workspace")
    if path.relative_to(root).parts[:1] in (("runs",), ("exports",)):
        raise LabError("The token store must not be inside runs/ or exports/, which hold shareable evidence")
    for parent in (path, *path.parents):
        if not parent.is_relative_to(root):
            break
        if any((parent / marker).is_file() for marker in RUN_MARKERS):
            raise LabError("The token store must not be inside a run directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o022:
        raise LabError("The token store directory must not be writable by group or others")
    return path


class FileTokenStore:
    """The SDK's TokenStorage on one 0600 JSON file: tokens, client registration and discovery."""

    def __init__(self, path: Path, register):
        self.path, self._register, self.fixed_client = path, register, None
        self.record = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        if self.path.stat().st_mode & 0o077:
            raise LabError(f"Token store file {self.path.name} is readable by others; remove it and relink")
        try:
            data = json.loads(self.path.read_text())
        except ValueError:
            return {}
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return {}
        tokens, client = data.get("tokens") or {}, data.get("client_info") or {}
        self._register([tokens.get("access_token"), tokens.get("refresh_token"), client.get("client_secret")])
        return {key: value for key, value in data.items() if key != "version"}

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        try:
            return OAuthToken.model_validate(self.record["tokens"]) if self.record.get("tokens") else None
        except ValidationError:
            return None

    async def set_tokens(self, tokens):
        self._register([tokens.access_token, tokens.refresh_token])
        self.record["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self.record["expires_at"] = time.time() + int(tokens.expires_in) if tokens.expires_in else None
        self.save()

    async def get_client_info(self):
        if self.fixed_client is not None:
            return self.fixed_client
        from mcp.shared.auth import OAuthClientInformationFull
        try:
            data = self.record.get("client_info")
            return OAuthClientInformationFull.model_validate(data) if data else None
        except ValidationError:
            return None

    async def set_client_info(self, client_info):
        if self.fixed_client is not None:
            return  # a preregistered client comes from the coordinator's environment, never the store
        self._register([client_info.client_secret])
        self.record["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self.save()

    def forget(self, *, client: bool = False):
        for key in ("tokens", "expires_at", "subject") + (("client_info",) if client else ()):
            self.record.pop(key, None)
        self.save()

    def discard(self):
        self.record = {}
        self.path.unlink(missing_ok=True)

    def save(self):
        _write_private(self.path, {"version": STORE_VERSION, **self.record})


class _RedirectListener:
    """The loopback redirect_uri (RFC 8252 section 7.3); it receives authorization responses."""
    PATH = "/callback"

    def __init__(self):
        self.results, self.server, self.port = asyncio.Queue(), None, None

    @classmethod
    async def start(cls, port: int | None = None, *, fixed: bool = False):
        listener = cls()
        try:
            listener.server = await asyncio.start_server(listener._handle, "127.0.0.1", port or 0)
        except OSError:
            if fixed or not port:
                raise LinkError(f"The loopback redirect port {port} is unavailable", kind="authorization") from None
            listener.server = await asyncio.start_server(listener._handle, "127.0.0.1", 0)
        listener.port = listener.server.sockets[0].getsockname()[1]
        return listener

    @property
    def uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.PATH}"

    def matches(self, url: httpx2.URL) -> bool:
        return (url.scheme, url.host, url.port, url.path) == ("http", "127.0.0.1", self.port, self.PATH)

    async def _handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            parts = urlsplit(target)
            params = dict(parse_qsl(parts.query))
            if method == "GET" and parts.path == self.PATH and {"code", "error"} & params.keys():
                self.results.put_nowait(params)
                status, text = "200 OK", "Linked. You can close this window." if "code" in params else \
                    "Authorization was not granted. You can close this window."
            else:
                status, text = "404 Not Found", "Not found."
            body = f"<!doctype html><title>Consumer Agent Testing Tool</title><p>{text}</p>".encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store"
                         f"\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    async def close(self):
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 2)
        except TimeoutError:
            pass


_APPROVE = re.compile(r"approve|allow|accept|grant|consent|authori[sz]e|continue|sign.?in|log.?in|submit|yes",
                      re.IGNORECASE)
_DENY = re.compile(r"deny|cancel|reject|decline|refuse|\bno\b", re.IGNORECASE)


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self._form = [], None

    def handle_starttag(self, tag, attrs):
        attributes = {name.lower(): value or "" for name, value in attrs}
        if tag == "form":
            self._form = {"action": attributes.get("action", ""), "method": attributes.get("method", "get"),
                          "fields": []}
            self.forms.append(self._form)
        elif self._form is not None and tag in ("input", "button"):
            attributes["type"] = (attributes.get("type") or ("submit" if tag == "button" else "text")).lower()
            self._form["fields"].append(attributes)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def _login_form(html: str, user: str, password: str) -> tuple[str, str, list] | None:
    """The fixture's login or consent form, filled with the test login: (action, method, fields)."""
    parser = _Forms()
    parser.feed(html)
    for form in sorted(parser.forms, key=lambda form: all(f["type"] != "password" for f in form["fields"])):
        data, submit, named_user = [], None, False
        for field in form["fields"]:
            kind, name = field["type"], field.get("name")
            if kind in ("submit", "image"):
                label = " ".join(field.get(key, "") for key in ("name", "value", "id"))
                if submit is None and name and _APPROVE.search(label) and not _DENY.search(label):
                    submit = (name, field.get("value", ""))
            elif name and kind == "password":
                data.append((name, password))
            elif name and kind == "hidden":
                data.append((name, field.get("value", "")))
            elif name and kind in ("text", "email") and not named_user:
                data.append((name, user))
                named_user = True
            elif name and kind in ("checkbox", "radio") and "checked" in field:
                data.append((name, field.get("value", "on")))
        if submit or any(field["type"] == "password" for field in form["fields"]) or len(parser.forms) == 1:
            return form["action"], form["method"].upper(), data + ([submit] if submit else [])
    return None


def _id_token_subject(token) -> str | None:
    """The unverified ``sub`` claim of an OIDC id_token: a wrong-account hint, not an authorization."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims["sub"] if isinstance(claims.get("sub"), str) else None
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


class OAuthCredential:
    """OAuth 2.1 through the SDK's OAuthClientProvider: PRM (RFC 9728), AS metadata (RFC 8414), DCR
    (RFC 7591), CIMD or a preregistered client, authorization code with PKCE S256, and the RFC 8707
    resource indicator. The host keeps the tokens (authd's role); a lapsed link is reported, never
    silently re-authorized during a run."""

    def __init__(self, auth: OAuthAuth, endpoint: str, policy: ClientProfile, redactor=None):
        self.auth, self.endpoint, self.policy, self._redactor = auth, endpoint, policy, redactor
        self.provider = self.store = self.listener = None
        self.subject = self.failure = None
        self.observations: list[dict] = []
        self._seen, self._hints, self._login = [], {}, ""
        self._linked, self._authorizations, self._origin = False, 0, _origin(endpoint)
        self._pinned: set = set()  # origins this link used; the provider may not leave them during a run
        # A client registered by an earlier link (the token store's): checked before authorizing with it.
        self._stored_client, self._registered_again = False, False

    @property
    def secrets(self) -> list[str]:
        return list(self._seen)

    @property
    def httpx_auth(self):
        """The SDK provider, for SDK transports: proactive refresh, step-up and resend on 401."""
        return self.provider

    def event_hooks(self) -> dict:
        """httpx2 hooks for an SDK client that uses httpx_auth: the provider's discovery, token and
        refresh requests during a run get linking's guards (HTTPS off loopback, fixture logins on
        loopback, only the origins this link used) and its token capture."""
        return {"request": [self._run_guard], "response": [self._capture]}

    def apply(self, url: str, headers: dict) -> tuple[str, dict]:
        headers = dict(headers)
        tokens = self.provider.context.current_tokens if self.provider else None
        if tokens and tokens.access_token and _origin(url) == self._origin:
            headers["Authorization"] = f"Bearer {tokens.access_token}"
        return url, headers

    async def refresh(self) -> bool:
        from mcp.client.auth import OAuthFlowError
        context = self.provider.context
        async with context.lock:
            if not context.can_refresh_token():
                self.failure = "expired" if self._expired() else "revoked"
                return False
            self._hints.pop("sub", None)
            try:
                # Pinned SDK internals (mcp 2.2.0): the provider's own refresh request and handling,
                # so the resource indicator and client authentication match the SDK flow exactly.
                request = await self.provider._refresh_token()
                async with self._http() as http:
                    response = await http.send(request)
            except (httpx2.HTTPError, OAuthFlowError, LinkError):
                # The authorization server was unreachable; the grant may still be good, so keep it.
                self.failure = "expired" if self._expired() else "revoked"
                self._note("refresh-unreachable")
                return False
            try:
                described = str(response.json().get("error_description") or "") if response.status_code != 200 else ""
            except (ValueError, AttributeError):
                described = ""
            refreshed = await self.provider._handle_refresh_response(response)
        if not refreshed:
            # The authorization server no longer honors the grant (RFC 6749 invalid_grant).
            self.failure = "expired" if "expired" in described.lower() else "revoked"
            self.store.forget()
            self._note("refresh-refused", status=response.status_code)
            return False
        if self.subject and self._hints.get("sub", self.subject) != self.subject:
            self.failure = "wrong-account"
            return False
        if not set(self.auth.scopes) <= set((context.current_tokens.scope or "").split()):
            self.failure = "insufficient-scope"
            return False
        self.failure = None
        self._note("token-refreshed")
        return True

    def describe(self) -> dict:
        context = self.provider.context if self.provider else None
        metadata = context.oauth_metadata if context else None
        resource = context.protected_resource_metadata if context else None
        tokens = context.current_tokens if context else None
        offered = None
        if metadata and metadata.scopes_supported is not None or resource and resource.scopes_supported is not None:
            offered = set((metadata and metadata.scopes_supported) or ()) | set(
                (resource and resource.scopes_supported) or ())
        expiry = context.token_expiry_time if context and context.token_expiry_time else (
            self.store.record.get("expires_at") if self.store else None)
        return {
            "mode": "oauth", "placement": "bearer_header", "registration": self.auth.registration,
            "consent": self.auth.consent, "scopes": sorted((tokens.scope or "").split()) if tokens else [],
            "requested_scopes": list(self.auth.scopes), "read_only_scopes": list(self.auth.read_only_scopes),
            "read_only_supported": set(self.auth.read_only_scopes) <= offered
            if self.auth.read_only_scopes and offered is not None else None,
            "subject": self.subject,
            "expires_at": datetime.fromtimestamp(expiry, UTC).isoformat() if expiry else None,
            "issuer": str(metadata.issuer) if metadata else None,
            "resource": context.get_resource_url() if context else None,
            "protected_resource_metadata": resource is not None, "pkce": "S256",
            "refreshable": bool(tokens and tokens.refresh_token), "reused_stored_token": self._authorizations == 0,
            **({"client_registered_again": True} if self._registered_again else {}),
        }

    def _note(self, kind: str, **fields):
        note = {"kind": kind, **fields}
        if note not in self.observations:
            self.observations.append(note)

    def _register(self, values):
        fresh = [value for value in _register(values, self._redactor) if value not in self._seen]
        self._seen.extend(fresh)

    def _expired(self) -> bool:
        expiry = self.provider.context.token_expiry_time if self.provider else None
        if expiry is None and self.store is not None:
            expiry = self.store.record.get("expires_at")
        return bool(expiry) and time.time() >= expiry  # as in the SDK, a zero expiry means none

    def _http(self, *, link: bool = False, hooks: bool = True) -> httpx2.AsyncClient:
        events = {"request": [self._guard] + ([self._registration_guard] if link else []),
                  "response": [self._capture]} if hooks else {}
        return httpx2.AsyncClient(timeout=httpx2.Timeout(self.policy.timeout_seconds), follow_redirects=False,
                                  transport=StreamingTransport(trust_env=False),
                                  trust_env=False, headers={"User-Agent": USER_AGENT}, event_hooks=events)

    async def _run_guard(self, request: httpx2.Request):
        await self._guard(request)
        if self._linked and self._pinned and _origin(str(request.url)) not in self._pinned:
            # A 401 lets the server name new metadata; a refresh token (and a client secret) must not
            # follow it to an authorization server this link never used.
            raise LinkError(f"The OAuth flow tried to reach {request.url.host}, which this link did not use; "
                            "relink the connector", kind="discovery")

    def _pin(self):
        context, urls = self.provider.context, [self.endpoint, self.provider.context.auth_server_url]
        if (resource := context.protected_resource_metadata) is not None:
            urls += [str(resource.resource), *map(str, resource.authorization_servers or ())]
        if (metadata := context.oauth_metadata) is not None:
            urls += [str(value) for key, value in metadata if value is not None and (
                key == "issuer" or key == "jwks_uri" or key.endswith("_endpoint"))]
        self._pinned = {_origin(url) for url in urls if url}

    async def _guard(self, request: httpx2.Request):
        loopback = _loopback_host(request.url.host)
        if self.auth.consent == "fixture-auto" and not loopback:
            # Test logins and fixture registrations never leave the machine.
            raise LinkError(f"fixture-auto linking stays on loopback; {request.url.host} is not a local fixture "
                            "(use consent: interactive)", kind="authorization")
        if request.url.scheme != "https" and not loopback:
            raise LinkError(f"OAuth traffic to {request.url.host} must use HTTPS", kind="discovery")

    async def _registration_guard(self, request: httpx2.Request):
        context = self.provider.context
        metadata = context.oauth_metadata
        endpoint = metadata.registration_endpoint if metadata else None
        fallback = urlunsplit((*urlsplit(self.endpoint)[:2], "/register", "", ""))
        if request.method != "POST" or str(request.url) not in (str(endpoint) if endpoint else None, fallback):
            return
        if metadata is None:
            raise LinkError("Authorization server metadata (RFC 8414) was not discovered; refusing the legacy "
                            "/register fallback", kind="discovery")
        if self.auth.registration != "dcr":
            reason = ("does not advertise client_id_metadata_document_supported"
                      if self.auth.registration == "cimd" else "would register a new client")
            raise LinkError(f"The authorization server {reason}; refusing dynamic client registration for a "
                            f"{self.auth.registration} link", kind="registration")

    async def _capture(self, response: httpx2.Response):
        # The SDK's OAuthToken drops non-standard fields; the token response may name the subject.
        metadata = self.provider.context.oauth_metadata if self.provider else None
        if not (metadata and response.request.method == "POST" and response.status_code == 200
                and str(response.request.url) == str(metadata.token_endpoint)):
            return
        try:
            data = json.loads(await response.aread())
        except ValueError:
            return
        if isinstance(data, dict):
            self._register([data.get(key) for key in ("access_token", "refresh_token", "id_token")])
            subject = data.get("sub") if isinstance(data.get("sub"), str) else _id_token_subject(data.get("id_token"))
            if subject:
                self._hints["sub"] = subject

    async def _redirect(self, authorization_url: str):
        if self._linked:
            # The SDK re-authorizes on a 401; during a run that is a lapsed link the user must renew.
            kind = "expired" if self._expired() else "revoked"
            raise LinkError(f"The connector link is {kind}; relink it (re-authorization is never automatic "
                            "during a run)", kind=kind)
        metadata = self.provider.context.oauth_metadata
        if metadata is None:
            raise LinkError("Authorization server metadata (RFC 8414) was not discovered; refusing to guess "
                            "authorization endpoints", kind="discovery")
        if "S256" not in (metadata.code_challenge_methods_supported or ()):
            raise LinkError("The authorization server does not advertise PKCE S256 "
                            "(code_challenge_methods_supported)", kind="discovery")
        url = self._requested_scope(authorization_url)
        self._authorizations += 1
        if self._stored_client:
            # Before a person (or the fixture login) authorizes with a client an earlier link registered: an
            # authorization server that forgot it (a restarted fixture) answers with an error, never a login.
            try:
                async with self._http() as http:
                    response = await http.get(url)
            except httpx2.HTTPError as error:
                raise LabError(f"The authorization server is unreachable ({type(error).__name__})") from None
            if _unknown_client(response):
                raise _StaleClient("The authorization server no longer knows the client this link registered")
        if self.auth.consent == "interactive":
            print(f"Link {urlsplit(self.endpoint).hostname}: open this URL, sign in and approve access "
                  f"(waiting {INTERACTIVE_TIMEOUT:g} s):\n{url}", file=sys.stderr, flush=True)
            return
        await self._fixture_consent(url)

    def _requested_scope(self, url: str) -> str:
        """Declared scopes win over the SDK's scope selection (WWW-Authenticate, then PRM)."""
        if not self.auth.scopes:
            return url
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        chosen = dict(query).get("scope", "").split()
        scope = " ".join([*self.auth.scopes, *(["offline_access"] if "offline_access" in chosen
                                               and "offline_access" not in self.auth.scopes else [])])
        self.provider.context.client_metadata.scope = scope  # what the token records if the AS omits scope
        query = [(key, value) for key, value in query if key not in ("scope", "prompt")] + [("scope", scope)]
        if "offline_access" in scope.split():
            query.append(("prompt", "consent"))  # OIDC requires it with offline_access
        return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))

    async def _fixture_consent(self, url: str):
        """Drive a loopback fixture's login and consent pages with the test login, then deliver the
        redirect to the loopback redirect_uri (the listener captures the code)."""
        target = httpx2.URL(url)
        if not _loopback_host(target.host):
            raise LinkError("fixture-auto consent only runs against a loopback authorization server; use "
                            "consent: interactive", kind="authorization")
        user, _, password = self._login.partition(":")
        origin = (target.scheme, target.host, target.port)
        method, data, basic, submitted = "GET", None, False, set()
        async with self._http(hooks=False) as http:
            for _ in range(MAX_CONSENT_STEPS):
                posted = data is not None and method != "GET"
                response = await http.request(
                    method, target.copy_merge_params(data) if data and not posted else target,
                    auth=httpx2.BasicAuth(user, password) if basic else None,
                    content=urlencode(data).encode() if posted else None,
                    headers={"Content-Type": "application/x-www-form-urlencoded"} if posted else None)
                if response.has_redirect_location:
                    location = target.join(response.headers["location"])
                    if self.listener.matches(location):
                        self._register([location.params.get("code")])
                        await http.get(location)
                        return
                    if (location.scheme, location.host, location.port) != origin:
                        raise LinkError("The fixture authorization server redirected outside itself and the "
                                        "loopback redirect URI", kind="authorization")
                    target, method, data = location, "GET", None
                    continue
                challenge = response.headers.get("www-authenticate", "").lower()
                if response.status_code == 401 and not basic and challenge.startswith("basic"):
                    basic = True
                    continue
                if _unknown_client(response):
                    raise _StaleClient("The fixture authorization server does not know the client")
                form = _login_form(response.text, user, password) if response.status_code == 200 \
                    and "html" in response.headers.get("content-type", "") else None
                if form is None:
                    raise LinkError(f"The fixture authorization server answered HTTP {response.status_code} without "
                                    "a login form or a redirect", kind="authorization")
                action = target.join(form[0]) if form[0] else target
                if (action.scheme, action.host, action.port) != origin:
                    raise LinkError("The fixture login form posts outside the authorization server",
                                    kind="authorization")
                attempt = (str(action.copy_with(query=None)), tuple(name for name, _ in form[2]))
                if attempt in submitted:
                    raise LinkError("The fixture authorization server rejected the test login", kind="authorization")
                submitted.add(attempt)
                target, method, data = action, form[1], form[2]
        raise LinkError(f"The fixture authorization did not finish within {MAX_CONSENT_STEPS} steps",
                        kind="authorization")

    async def _callback(self):
        from mcp.client.auth import AuthorizationCodeResult
        timeout = INTERACTIVE_TIMEOUT if self.auth.consent == "interactive" else self.policy.timeout_seconds
        try:
            params = await asyncio.wait_for(self.listener.results.get(), timeout)
        except TimeoutError:
            raise LinkError(f"No authorization redirect arrived within {timeout:g} s", kind="authorization") from None
        if error := params.get("error"):
            raise LinkError(f"The authorization server returned {error[:64]!r}",
                            kind="insufficient-scope" if error == "invalid_scope" else "authorization")
        self._register([params.get("code")])
        return AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=params.get("iss"))

    async def _probe(self):
        from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
        from mcp.client.auth.utils import extract_field_from_www_auth
        context = self.provider.context
        try:
            async with self._http(link=True) as http:
                response = await http.post(self.endpoint, json=_PROBE, auth=self.provider, headers={
                    "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": _PROBE_VERSION})
        except LinkError:
            raise
        except OAuthRegistrationError as error:
            raise LinkError(f"Client registration failed: {self._scrub(error)}", kind="registration") from None
        except OAuthTokenError as error:
            raise LinkError(f"The token exchange failed: {self._scrub(error)}", kind="authorization") from None
        except OAuthFlowError as error:
            raise LinkError(f"OAuth failed: {self._scrub(error)}",
                            kind="discovery" if context.oauth_metadata is None else "authorization") from None
        except httpx2.HTTPError as error:
            raise LabError(f"The connector or its authorization server is unreachable ({type(error).__name__})") \
                from None
        if response.status_code == 401:
            raise LinkError("The connector rejected the token its authorization server issued", kind="rejected")
        if response.status_code == 403:
            if extract_field_from_www_auth(response, "error") == "insufficient_scope":
                raise LinkError("The linked account lacks a scope the connector requires", kind="insufficient-scope")
            raise LinkError("The connector refused the linked account (HTTP 403)", kind="rejected")
        if not (context.current_tokens and context.current_tokens.access_token):
            raise LinkError("The connector accepted an unauthenticated request (no HTTP 401 challenge), so it does "
                            "not use OAuth; declare auth mode none or api_key", kind="discovery")
        if context.protected_resource_metadata is None:
            self._note("legacy-discovery", detail="no RFC 9728 protected resource metadata")

    async def _verify(self):
        context = self.provider.context
        granted = set((context.current_tokens.scope or "").split())
        if missing := [scope for scope in self.auth.scopes if scope not in granted]:
            raise LinkError(f"The authorization server did not grant: {', '.join(missing)}",
                            kind="insufficient-scope")
        self.subject = self._hints.get("sub") or (self.store.record.get("subject") if not self._authorizations
                                                  else None) or await self._introspect()
        if self.auth.expected_subject is not None:
            if self.subject is None:
                raise LinkError("The linked account cannot be checked against expected_subject: the authorization "
                                "server reported no subject", kind="wrong-account")
            if self.subject != self.auth.expected_subject:
                raise LinkError("The linked account differs from expected_subject", kind="wrong-account")

    async def _introspect(self) -> str | None:
        """RFC 7662 introspection, when advertised, for a subject the token response did not carry."""
        context = self.provider.context
        endpoint = context.oauth_metadata.introspection_endpoint if context.oauth_metadata else None
        if endpoint is None:
            return None
        data = {"token": context.current_tokens.access_token, "token_type_hint": "access_token"}
        if context.client_info:
            data["client_id"] = context.client_info.client_id
        data, headers = context.prepare_token_auth(data, {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            async with self._http() as http:
                response = await http.post(str(endpoint), data=data, headers=headers)
            body = response.json() if response.status_code == 200 else {}
        except (httpx2.HTTPError, ValueError):
            return None
        active = isinstance(body, dict) and body.get("active") is True
        return body["sub"] if active and isinstance(body.get("sub"), str) else None

    def _scrub(self, error) -> str:
        text = str(error)[:500]
        for secret in sorted(self._seen, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def _restore(self):
        from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
        record, context = self.store.record, self.provider.context
        context.token_expiry_time = record.get("expires_at")
        context.auth_server_url = record.get("auth_server_url")
        for field, model in (("oauth_metadata", OAuthMetadata),
                             ("protected_resource_metadata", ProtectedResourceMetadata)):
            try:
                setattr(context, field, model.model_validate(record[field]) if record.get(field) else None)
            except ValidationError:
                setattr(context, field, None)

    def _persist(self):
        context = self.provider.context
        self.store.record.update(
            subject=self.subject, redirect_port=self.listener.port, auth_server_url=context.auth_server_url,
            oauth_metadata=context.oauth_metadata.model_dump(mode="json", exclude_none=True)
            if context.oauth_metadata else None,
            protected_resource_metadata=context.protected_resource_metadata.model_dump(mode="json", exclude_none=True)
            if context.protected_resource_metadata else None)
        self.store.save()


@functools.cache
def _declared_scope_metadata():
    from mcp.shared.auth import OAuthClientMetadata

    class DeclaredScopeMetadata(OAuthClientMetadata):
        """Client metadata whose scope always keeps the declared scopes. The SDK swaps in the
        challenged scope before dynamic registration, which would register the client for less
        than it then requests (and an AS that checks registered scopes refuses the request)."""
        _declared: tuple[str, ...] = PrivateAttr(default=())

        def __setattr__(self, name, value):
            if name == "scope" and self._declared:
                value = " ".join(dict.fromkeys([*(value or "").split(), *self._declared]))
            super().__setattr__(name, value)
    return DeclaredScopeMetadata


def _store_path(store: Path, auth: OAuthAuth, endpoint: str) -> Path:
    from mcp.shared.auth_utils import resource_url_from_server_url
    identity = auth.model_dump(mode="json", exclude={"read_only_scopes", "redirect_port"})
    identity["resource"] = resource_url_from_server_url(endpoint)
    return _store_directory(store) / f"oauth-{digest(identity)[:32]}.json"


async def _link_oauth(auth: OAuthAuth, endpoint: str, store: Path, policy, redactor, model=None) -> OAuthCredential:
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl
    credential = OAuthCredential(auth, endpoint, policy, redactor)
    if auth.consent == "fixture-auto" and not is_loopback(endpoint):
        raise LinkError("fixture-auto consent only links loopback fixtures; use consent: interactive for "
                        f"{urlsplit(endpoint).hostname}", kind="authorization")
    if auth.consent == "fixture-auto":
        credential._login = _environment(auth.test_login_env, "test login", model)
        if not credential._login.partition(":")[0] or ":" not in credential._login:
            raise LinkError(f"{auth.test_login_env} must hold user:password for the fixture login", kind="missing")
        credential._register([credential._login, credential._login.partition(":")[2]])
    tokens = credential.store = FileTokenStore(_store_path(store, auth, endpoint), credential._register)
    listener = credential.listener = await _RedirectListener.start(
        auth.redirect_port or tokens.record.get("redirect_port"), fixed=auth.redirect_port is not None)
    try:
        stored = await tokens.get_client_info()
        registered = {str(uri) for uri in (stored.redirect_uris or ())} if stored else set()
        if auth.registration == "dcr" and stored and listener.uri not in registered:
            tokens.forget(client=True)  # registered for another redirect port: register again
            stored = None
        scope = " ".join(auth.scopes) or None
        if auth.registration == "preregistered":
            secret = _environment(auth.client_secret_env, "client secret", model) if auth.client_secret_env else None
            credential._register([secret])
            tokens.fixed_client = OAuthClientInformationFull(
                client_id=_environment(auth.client_id_env, "client id", model), client_secret=secret,
                token_endpoint_auth_method="client_secret_post" if secret else "none",
                redirect_uris=[AnyUrl(listener.uri)], grant_types=["authorization_code", "refresh_token"],
                response_types=["code"], scope=scope)
        metadata = _declared_scope_metadata()(
            redirect_uris=[AnyUrl(listener.uri)], token_endpoint_auth_method="none", client_name=CLIENT_NAME,
            grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope=scope)
        metadata._declared = tuple(auth.scopes)
        # A dynamically registered client from an earlier link may be one the server forgot: discarded and
        # registered again once (recorded), never in a loop.
        retry = auth.registration == "dcr" and stored is not None
        for attempt in range(2):
            credential.provider = OAuthClientProvider(
                endpoint, metadata, tokens, credential._redirect, credential._callback,
                client_metadata_url=auth.client_metadata_url if auth.registration == "cimd" else None)
            credential._restore()
            credential._stored_client = retry and attempt == 0
            try:
                await credential._probe()
                await credential._verify()
            except _StaleClient as error:
                if not retry or attempt:
                    hint = {"dcr": "though this link registered it just now; check the authorization server's "
                                   "dynamic client registration",
                            "preregistered": "check the preregistered client id in auth.client_id_env",
                            "cimd": "check the client metadata document at auth.client_metadata_url"}
                    raise LinkError(f"{error} ({hint[auth.registration]})", kind="registration") from None
                tokens.forget(client=True)
                credential._hints, credential._registered_again = {}, True
                credential._note("client-registered-again", registration="dcr",
                                 reason="the authorization server no longer knew the stored client")
                continue
            except LinkError as error:
                if error.kind in ("wrong-account", "insufficient-scope", "rejected"):
                    tokens.forget()  # never reuse tokens for the wrong account or scope set
                raise
            break
        credential._stored_client = False
        credential._persist()
    finally:
        await listener.close()
    credential._pin()
    credential._linked = True
    return credential


async def link(auth: TargetAuth, endpoint: str, *, store: Path, http_policy: ClientProfile,
               redactor=None, model=None) -> Credential:
    """Link one connector host-side. none and api_key are immediate; oauth runs the SDK provider flow,
    with tokens kept under ``store`` (0600, workspace-local, never under runs/ or exports/). Every
    credential value is registered with ``redactor`` (when given) as soon as it exists. ``model`` (the
    selected config.ModelProfile) keeps the connector's credentials apart from the model's key."""
    auth = _TARGET_AUTH.validate_python(auth) if isinstance(auth, dict) else auth
    _check_endpoint(endpoint, auth)
    if isinstance(auth, NoAuth):
        return NoCredential()
    if isinstance(auth, ApiKeyAuth):
        return _link_api_key(auth, endpoint, redactor, model)
    return await _link_oauth(auth, endpoint, Path(store), http_policy, redactor, model)


SURROGATE = re.compile(r"catt-surr-[A-Za-z0-9_-]{32}")
_SURROGATE_BYTES = re.compile(SURROGATE.pattern.encode())
SURROGATE_PLACEMENTS = {"header": "header", "bearer_header": "header", "custom_header": "header",
                        "query": "query", "query_param": "query", "path": "path", "url_path_segment": "path",
                        "body": "body"}
# A body swap is a literal replacement, so it must stay valid inside JSON strings and form values.
_BODY_SAFE = re.compile(r"[A-Za-z0-9._~:/=@!$*,;-]+")


class SurrogateVault:
    """Surrogate credentials for agent-written HTTP: the agent only ever sees ``catt-surr-...`` values.

    ``swap`` puts the real secret back on a request to a registered host, in the placement it was
    issued for; a surrogate addressed anywhere else stops the request. Basis: documented (Meta's
    security post: code in the runtime cell only sees a surrogate minted by authd, which Sentinel
    replaces with the real credential at the network boundary); the hsurr form is unofficial
    (workflow-skills/custom-connector-setup/bin/dynamic_credentials.py:3-5, pinned mirrors).
    """

    def __init__(self):
        self._entries: dict[str, dict] = {}
        self.swaps: list[dict] = []  # audit trail; surrogates are not secrets

    @property
    def secrets(self) -> list[str]:
        return [entry["secret"] for entry in self._entries.values()]

    def issue(self, secret: str, *, hosts: list[str], placement: str) -> str:
        if not isinstance(secret, str) or admissible_secret(secret) is None \
                or re.search(r"[\s\x00-\x1f\x7f]", secret):
            raise LabError("A surrogate needs a real secret of at least 4 visible characters")
        if placement not in SURROGATE_PLACEMENTS:
            raise LabError(f"Unknown surrogate placement {placement!r}")
        placement = SURROGATE_PLACEMENTS[placement]
        if placement == "body" and not _BODY_SAFE.fullmatch(secret):
            raise LabError("A body surrogate's secret must be JSON- and form-safe (no quotes, backslashes, &, +, %)")
        if not hosts:
            raise LabError("A surrogate must name at least one host")
        allowed = set()
        for host in hosts:
            try:
                parts = urlsplit(f"//{host}")
                port = parts.port
            except (TypeError, ValueError):
                raise LabError(f"Invalid surrogate host {host!r}") from None
            if not parts.hostname or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
                raise LabError(f"Surrogate hosts are host or host:port, not URLs: {host!r}")
            allowed.add((parts.hostname.lower(), port))
        surrogate = f"catt-surr-{token_urlsafe(24)}"
        self._entries[surrogate] = {"secret": secret, "hosts": allowed, "placement": placement}
        _register([secret, quote(secret, safe="")])
        return surrogate

    def swap(self, method: str, url: str, headers: dict, body: bytes | None, *,
             redirect: bool = False) -> tuple[str, dict, bytes | None]:
        """Swap surrogates in an outbound request. A redirect hop is never swapped: only the request
        the user approved carries the real credential."""
        parts = urlsplit(url)
        if "catt-surr-" in parts.netloc.rpartition("@")[2].lower():
            # Hostnames fold case, so a surrogate there is refused whatever its form: it would reach DNS.
            raise LabError("A surrogate credential appeared in the request host; the request was not sent",
                           code=1, category="policy/approval")
        found = [(match, location) for text, location in (
            (parts.path, "path"), (parts.query, "query"), (parts.fragment, "fragment"),
            (parts.netloc.rpartition("@")[0], "userinfo"),
            *((str(name), "header-name") for name in headers),
            *((str(value), "header") for value in headers.values()))
            for match in SURROGATE.findall(text)]
        found += [(match.decode(), "body") for match in _SURROGATE_BYTES.findall(body or b"")]
        known = [(surrogate, location) for surrogate, location in found if surrogate in self._entries]
        if not known:
            return url, dict(headers), body
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
        port = parts.port or {"https": 443, "http": 80}.get(scheme)
        for surrogate, location in known:
            entry = self._entries[surrogate]
            if not any(name == host and (allowed or {"https": 443, "http": 80}.get(scheme)) == port
                       for name, allowed in entry["hosts"]):
                raise LabError("A surrogate credential was addressed to an unregistered host; the request was not "
                               "sent", code=1, category="policy/approval")
            if scheme != "https" and not _loopback_host(host):
                raise LabError("A surrogate credential would travel in cleartext; the request was not sent",
                               code=1, category="policy/approval")
            if location != entry["placement"]:
                raise LabError(f"A surrogate credential appeared in the request {location}, outside its "
                               f"{entry['placement']} placement; the request was not sent", code=1,
                               category="policy/approval")
            if location == "query" and any(surrogate in piece.partition("=")[0] for piece in parts.query.split("&")):
                # Only a parameter's value carries a query credential: never a parameter name, which logs and
                # service-side scrubbing keyed on the granted parameter would miss.
                raise LabError("A surrogate credential appeared in a query parameter name, outside its query "
                               "placement; the request was not sent", code=1, category="policy/approval")
        stated = [value for name, value in headers.items() if str(name).lower() == "host"]
        if stated and any(str(value).lower() != parts.netloc.rpartition("@")[2].lower() for value in stated):
            raise LabError("The Host header does not match the request URL; the request was not sent", code=1,
                           category="policy/approval")
        for surrogate, location in known:
            self.swaps.append({"surrogate": surrogate, "method": method, "host": host, "placement": location,
                               "swapped": not redirect})
        if redirect:
            return url, dict(headers), body
        path, query, headers = parts.path, parts.query, dict(headers)
        for surrogate in {surrogate for surrogate, _ in known}:
            secret = self._entries[surrogate]["secret"]
            encoded = quote(secret, safe="")
            # In the query, only parameter values are swapped (names were checked above), each pair as written.
            path, query = path.replace(surrogate, encoded), "&".join(
                f"{name}={value.replace(surrogate, encoded)}" if equals else piece
                for piece in query.split("&") for name, equals, value in (piece.partition("="),)) if query else query
            headers = {name: value.replace(surrogate, secret) if isinstance(value, str) else value
                       for name, value in headers.items()}
            body = body.replace(surrogate.encode(), secret.encode()) if body else body
        return urlunsplit(parts._replace(path=path, query=query)), headers, body


__all__ = ["ApiKeyAuth", "ApiKeyCredential", "Credential", "FileTokenStore", "LinkError", "NoAuth", "NoCredential",
           "OAuthAuth", "OAuthCredential", "SurrogateVault", "TargetAuth", "link"]
