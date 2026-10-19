"""Personal Agent Protocol ("Poppy") draft 0.1 readiness: check URL --suite poppy plays the personal agent
against a company that publishes /.well-known/poppy.json.

Discovery (read-only, any target): poppy.json (draft section 3), the issuer's OAuth Authorization Server
Metadata (3.2) and each listed MCP API's protected resource metadata (6, RFC 9728). Probes (opt-in with
--manifest, because they act): DPoP-bound Session Tokens and their refusals (4.2, 4.3), the company's handling
of client metadata documents (4.1, CIMD), the browser-session join form (5), invalid_token, sign_in_required and
insufficient_scope (6), Account Tokens (4.8), and the operations extension's at-most-once confirm (Operations
5-7). Probes start sessions; a declared test account is signed in to and one declared operation is confirmed,
which performs it.

Basis: draft 0.1 as published at personalagentprotocol.org (SPEC_COMMIT of its source, retrieved 2026-10-16),
every rule quoting the sentence it rests on. Muse's only evidence is a demo (sierra.ai/blog/poppy, 2026-10-09:
"Muse went to Rocket's website, found Rocket's agent through Poppy"), so every result is labeled "draft 0.1; no
production Muse evidence".
"""
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import LabError, Unsupported, normalize_error
from .gadget import StaticRun, ignored_options, outcome_line, summary, workspace_path
from .serialization import digest, load

SPEC = "https://personalagentprotocol.org/docs/spec"
OPERATIONS_SPEC = "https://personalagentprotocol.org/docs/extensions/operations"
SPEC_COMMIT = "e910c866f4c7ea45f169b435a470027d39b9c075"  # personal-agent-protocol/personal-agent-protocol
SPEC_SOURCES = {"content/spec.mdx": "e7c63e8ad3a96922e59c7da571d79ce96b61ef0690a8bdbbb2c3c2e272e129a1",
                "content/operations.mdx": "4b07bcb5f10994bfda5919d6e8776dab231693bdc40ae532920151558b2c1d29"}
# A partner's demo, not Meta documentation: unofficial, and no production Muse evidence.
MUSE_EVIDENCE = {"basis": "unofficial", "source": "https://sierra.ai/blog/poppy", "checked_at": "2026-10-16",
                 "quote": "Muse went to Rocket's website, found Rocket's agent through Poppy",
                 "note": "a Sierra demo (2026-10-09); no production Muse evidence"}
DRAFT = "0.1"
SUITE = "poppy"
LABEL = "draft 0.1; no production Muse evidence"
INTERFACE = {"name": "personal-agent-protocol-draft-0.1", "surface": "poppy-company", "label": LABEL}
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CLIENT_ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
CORE_SCOPES = {"poppy:read", "poppy:write"}
EXTENSIONS = {"operations"}  # Plain names are the protocol's own; draft 0.1 defines this one.
API_TYPES = {"openapi", "mcp"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 1024 * 1024
OPERATION_STATES = {"proposed", "in_progress", "succeeded", "failed", "cancelled", "expired"}
FINAL_STATES = {"succeeded", "failed", "cancelled", "expired"}
TOKEN_FIELDS = ("access_token", "refresh_token", "id_token", "device_code")  # secrets a company returns

# rule: (section, quote). Every rule rests on a draft 0.1 sentence or table row, quoted verbatim; a table row
# reads "field (Required column): ..." or "code (status): meaning".
RULES = {
    "3-https": ("3", ("A Company MUST publish its poppy.json document at https://{domain}/.well-known/poppy.json over "
                "HTTPS, where {domain} is the Company's domain or a subdomain it controls.")),
    "3-redirects": ("3", "Personal Agents MUST follow redirects, and every redirect MUST be to an HTTPS URL."),
    "3.1-protocol-version": ("3.1", ("A Personal Agent MUST NOT use a document whose major version it doesn't "
                             "support.")),
    "3.1-organization": ("3.1", ("domain MUST be the host that serves the document, after any redirects. Domains are "
                         "compared ignoring case, and nothing else.")),
    "3.1-auth": ("3.1", "issuer is the Company's OAuth issuer identifier, an HTTPS URL."),
    "3.1-surfaces": ("3.1", "agent, apis, web (Required: One of agent, apis, or web)"),
    "3.1-sign-in": ("3.1", ("It also contains direct, device, and mediated for the types of sign-in the Company "
                    "supports (4.4), if any.")),
    "3.1-agent": ("3.1", ("protocols lists its supported conversation protocols, each with a type and HTTPS endpoint "
                  "(section 7).")),
    "3.1-web": ("3.1", ("The optional browser_session_endpoint is where the Personal Agent's browser joins a Session "
                "(section 5).")),
    "3.3-extensions": ("3.3", ("Extensions defined with this protocol have plain names, such as operations. Anyone "
                       "else MUST start an extension's name with a domain they control, such as "
                       "example.com/gift-wrap.")),
    "4.4-scopes": ("4.4", ("Names starting with poppy: are reserved for this protocol, so a Company's existing OAuth "
                   "scopes can't clash with them.")),
    "6-apis": ("6", "A Company can list APIs in its poppy.json (section 3). Each API's type MUST be one of these"),
    "6-mcp-metadata": ("6", ("MCP clients find where to get a token from the server's protected resource metadata "
                       "(RFC 9728), and its authorization_servers MUST include auth.issuer.")),
    "3.2-metadata": ("3.2", ("Before using a Company's poppy.json, the Personal Agent fetches the metadata and MUST "
                     "check that its issuer exactly matches auth.issuer and that poppy_domains includes "
                     "organization.domain, ignoring case.")),
    "3.2-endpoints": ("3.2", "token_endpoint issues Session and Account Tokens (4.2, 4.8). Required."),
    "3.2-dpop-algs": ("3.2", ("dpop_signing_alg_values_supported lists the algorithms the Company accepts for DPoP "
                      "Proofs (4.3), as RFC 9449 §5.1 defines. Companies MUST publish it, and it MUST include "
                      "ES256.")),
    "4.1-client-metadata": ("4.1", "client_id MUST equal the URL the document was fetched from."),
    "4.1-assertion-audience": ("4.1", ("Companies MUST reject a client assertion with any other aud, including their "
                               "token endpoint URL.")),
    "4.2-start-session": ("4.2", ("The Company verifies both assertions against the Personal Agent's jwks_uri, checks "
                          "that the assertion's iss is the authenticated client_id, rejects a reused jti, and "
                          "returns a new Session")),
    "4.2-token-response": ("4.2", ("expires_in is the token's lifetime in seconds. The Company chooses the value, and "
                           "it MUST be greater than 0.")),
    "4.2-assertion-replay": ("4.2", ("The Company rejects one it has already seen, so a copied assertion can't be used "
                             "again.")),
    "4.2-renew": ("4.2", ("The Company checks that the Session belongs to the same client_id and User ID as the "
                  "assertion, and returns a new signed-out Session Token for it.")),
    "4.3-proof-replay": ("4.3", ("At the token endpoint, the Company MUST reject a proof whose jti it has already "
                         "accepted within the acceptance window, because a replayed proof there could get a "
                         "new token.")),
    "4.3-dpop-required": ("4.3", ("Companies MUST accept Bearer Session Tokens only on their MCP APIs (section 6), and "
                          "MUST require DPoP everywhere else.")),
    "4.3-proof-binding": ("4.3", ("On requests with a Session Token, the key is the one the token is bound to, and "
                          "ath matches the token.")),
    "4.3-token-in-url": ("4.3", ("Personal Agents MUST NOT send tokens in a URL, and Companies MUST NOT accept them "
                         "from one.")),
    "4.3-nonce": ("4.3", ("A Company MAY require proofs to include a nonce it issued, as RFC 9449 describes, so proofs "
                  "can't be made in advance. Personal Agents MUST support nonces.")),
    "6-invalid-token": ("6", ("invalid_token (401): The token is missing or expired. The Personal Agent gets a new "
                        "one (4.2, 4.8).")),
    "6-sign-in-required": ("6", "sign_in_required (403): The request needs a signed-in token."),
    "6-insufficient-scope": ("6", ("insufficient_scope (403): The token lacks required account scopes. The response "
                             "names them in the scope attribute.")),
    "5-browser-join": ("5", ("If every check passes, the Company sets a cookie for the Session and responds with 303 "
                       "See Other to return_to. If not, it sets no cookie and returns an error page with HTTP "
                       "400.")),
    "5-browser-checks": ("5", ("It hasn't accepted the same jti before. It only needs to remember a jti until the "
                         "assertion expires.")),
    "5-cookie": ("5", "MUST be Secure and HttpOnly, and use SameSite=Lax or None, not Strict."),
    "4.7-mediated": ("4.7", ("A Company that offers Mediated Sign-In MUST rate-limit attempts, MUST NOT return "
                     "credentials in any response, and SHOULD require a one-time code when a sign-in looks "
                     "unusual.")),
    "4.8-account-token": ("4.8", ("With session_id, it gets a token for that Session, signing it in if it was signed "
                          "out.")),
    "4.9-sign-out": ("4.8", "When the Account Token has expired or been revoked, the Company returns invalid_grant."),
    "7.13-conversation-not-found": ("7.13", ("conversation_not_found (404): The Conversation ID is unknown or "
                                    "belongs to a different Personal Agent, User, or account (7.2). All "
                                    "return the same error.")),
    "ops-proposal": ("Operations 3", ("Proposing an operation doesn't perform it, and the Company MUST NOT start the "
                     "action before it is confirmed.")),
    "ops-user-approval": ("Operations 5.2", ("approved_by is allowed for this operation. If not, it returns "
                          "user_approval_required.")),
    "ops-terms-changed": ("Operations 5.2", ("revision is the latest revision. If not, it returns terms_changed with "
                          "the current operation.")),
    "ops-confirm": ("Operations 5.2", ("If it is in any other state, the Company returns the operation as it is and "
                    "does nothing else, so a repeated confirm is safe.")),
    "ops-at-most-once": ("Operations 6", ("The Company MUST perform each operation at most once, however many times "
                         "and through whichever channels it is confirmed.")),
    "ops-cancel": ("Operations 7", "A final operation is returned as it is."),
    "ops-not-found": ("Operations 9", ("operation_not_found (404): The operation is unknown, or the Session can't use "
                      "it (3.2).")),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PoppyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule: str
    severity: Literal["fail", "warn", "info", "blocked"]
    subject: str | None = None  # the document, endpoint or probe the finding is about
    evidence: str
    section: str
    quote: str
    basis: Literal["documented"] = "documented"
    source: str = SPEC


def finding(rule, severity, evidence, *, subject=None) -> PoppyFinding:
    section, quote = RULES[rule]
    return PoppyFinding(rule=rule, severity=severity, subject=subject, evidence=evidence[:800],
                        section=f"§{section}", quote=quote,
                        source=OPERATIONS_SPEC if section.startswith("Operations") else SPEC)


# --- The probe manifest -----------------------------------------------------------------------------

class Request(Strict):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=1)
    json_body: dict | list | None = Field(default=None, alias="json")


class Identity(Strict):
    """loopback: this checker serves its client metadata on 127.0.0.1, so only a loopback test deployment
    can fetch it (and must accept an http client_id there). hosted: a document you publish over HTTPS whose
    jwks_uri holds the public half of the key in private_key_env (a PEM or JWK), under key_id."""
    identity: Literal["loopback", "hosted"] = "loopback"
    client_id: str | None = None
    private_key_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    key_id: str | None = None


class SignIn(Strict):
    """A test account for Mediated Sign-In; values come from the environment and never reach evidence."""
    type: Literal["mediated"] = "mediated"
    credentials_env: dict[str, str] = Field(min_length=1)
    scope: str = Field(min_length=1)


class Requests(Strict):
    sign_in_required: Request | None = None  # answered sign_in_required for a signed-out Session Token
    insufficient_scope: Request | None = None  # needs a scope beyond sign_in.scope


class Effects(Strict):
    url: str  # a test oracle: GET returns JSON holding the count of performed actions
    pointer: str = Field(pattern=r"^(/[^/]*)*$")


class Operations(Strict):
    propose: Request  # a request that proposes exactly one operation (spec: HTTP 202 with "operation")
    scope: str | None = None  # sign in with these scopes for it (default: sign_in.scope)
    approved_by: Literal["user", "standing_permission"] = "user"
    effects: Effects | None = None


class Browser(Strict):
    return_to: str | None = None


class ProbeManifest(Strict):
    version: Literal[1] = 1
    kind: Literal["poppy-probes"] = "poppy-probes"
    agent: Identity = Field(default_factory=Identity)
    user_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$")
    sign_in: SignIn | None = None
    requests: Requests = Field(default_factory=Requests)
    operations: Operations | None = None
    browser: Browser = Field(default_factory=Browser)


def load_manifest(path: Path) -> ProbeManifest:
    try:
        return ProbeManifest.model_validate(load(Path(path)))
    except FileNotFoundError:
        raise LabError(f"Poppy probe manifest not found: {path}") from None
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, row['loc'])) or '(root)'}: {row['msg']}"
                             for row in error.errors(include_input=False))
        raise LabError(f"Invalid Poppy probe manifest {path}: {problems}") from None


# --- URLs and documents -----------------------------------------------------------------------------

def secure_url(url, *, loopback_ok) -> bool:
    """HTTPS, or plain HTTP on loopback when the target itself is a loopback test deployment."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return bool(parts.hostname) and (parts.scheme == "https" or loopback_ok and parts.scheme == "http"
                                     and parts.hostname in LOOPBACK)


def origin_of(target: str) -> str:
    """TARGET as an origin: "example.com" means https://example.com."""
    text = target if re.match(r"https?://", target or "") else f"https://{target}"
    parts = urlsplit(text)
    # A dotless name ("booking-service", the configured-target default) is no company domain.
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/") or "." not in parts.hostname and parts.hostname not in LOOPBACK:
        raise LabError(f"--suite poppy needs the company's origin as TARGET, such as https://example.com "
                       f"(got {target!r})")
    if parts.scheme != "https" and parts.hostname not in LOOPBACK:
        raise LabError("Poppy discovery is HTTPS only; plain HTTP is accepted for a loopback test deployment")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def field(document, *path):
    """A nested member, or None when any level is missing or not an object: documents are the company's."""
    for key in path:
        document = document.get(key) if isinstance(document, dict) else None
    return document


def metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1: the well-known segment goes between the host and any issuer path."""
    parts = urlsplit(issuer)
    return urlunsplit((parts.scheme, parts.netloc, "/.well-known/oauth-authorization-server" +
                       parts.path.rstrip("/"), "", ""))


def resource_metadata_url(resource: str) -> str:
    parts = urlsplit(resource)
    return urlunsplit((parts.scheme, parts.netloc, "/.well-known/oauth-protected-resource" +
                       parts.path.rstrip("/"), "", ""))


def validate_poppy_json(document, *, served_url: str, loopback_ok: bool) -> list[PoppyFinding]:
    """Section 3 rules for one poppy.json as served from served_url (after redirects)."""
    rows, subject = [], "poppy.json"

    def url_rule(rule, value, where):
        if not secure_url(value, loopback_ok=loopback_ok):
            rows.append(finding(rule, "fail", f"{where} is not an HTTPS URL: {str(value)[:200]!r}", subject=subject))

    if not isinstance(document, dict):
        return [finding("3-https", "fail", "poppy.json is not a JSON object", subject=subject)]
    version = document.get("protocol_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version):
        rows.append(finding("3.1-protocol-version", "fail", f"protocol_version {version!r} is not major.minor",
                            subject=subject))
    elif version.split(".")[0] != DRAFT.split(".")[0]:
        rows.append(finding("3.1-protocol-version", "blocked", f"protocol_version {version}: this checker supports "
                            f"major version {DRAFT.split('.')[0]} only and must not use the document", subject=subject))
    elif version != DRAFT:
        rows.append(finding("3.1-protocol-version", "info", f"protocol_version {version} is a newer minor version; "
                            f"checked against draft {DRAFT}", subject=subject))
    organization = document.get("organization")
    host = urlsplit(served_url).hostname or ""
    if not isinstance(organization, dict) or not isinstance(organization.get("name"), str) \
            or not isinstance(organization.get("domain"), str):
        rows.append(finding("3.1-organization", "fail", "organization needs a name and a domain", subject=subject))
    elif organization["domain"].lower() != host.lower():
        rows.append(finding("3.1-organization", "fail", f"organization.domain {organization['domain']!r} is not the "
                            f"host that served the document ({host!r})", subject=subject))
    surfaces = [key for key in ("agent", "apis", "web") if key in document]
    if not surfaces:
        rows.append(finding("3.1-surfaces", "fail", "None of agent, apis or web is present", subject=subject))
    auth = document.get("auth")
    web = document.get("web") if isinstance(document.get("web"), dict) else {}
    needs_auth = "agent" in document or "apis" in document or "browser_session_endpoint" in web
    if auth is None:
        if needs_auth:
            rows.append(finding("3.1-auth", "fail", "auth is required when agent, apis or "
                                "web.browser_session_endpoint is present", subject=subject))
    elif not isinstance(auth, dict):
        rows.append(finding("3.1-auth", "fail", "auth is not an object", subject=subject))
    else:
        url_rule("3.1-auth", auth.get("issuer"), "auth.issuer")
        declared = set()
        for kind in ("direct", "device", "mediated"):
            entry = auth.get(kind)
            if entry is None:
                continue
            scopes = entry.get("scopes") if isinstance(entry, dict) else None
            if not isinstance(scopes, list) or not scopes or not all(isinstance(item, str) and item
                                                                     for item in scopes):
                rows.append(finding("3.1-sign-in", "fail", f"auth.{kind}.scopes is not a list of scopes",
                                    subject=subject))
                continue
            declared.update(scopes)
            if kind == "mediated":
                url_rule("3.1-sign-in", entry.get("endpoint"), "auth.mediated.endpoint")
                fields = entry.get("fields")
                if not isinstance(fields, list) or not fields or not all(
                        isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(
                            item.get("label"), str) and isinstance(item.get("secret"), bool) for item in fields):
                    rows.append(finding("3.1-sign-in", "fail", "auth.mediated.fields needs name, label and secret "
                                        "for every credential", subject=subject))
        if not any(kind in auth for kind in ("direct", "device", "mediated")):
            rows.append(finding("3.1-sign-in", "info", "No sign-in type: only signed-out Sessions are offered",
                                subject=subject))
        custom = auth.get("custom_scopes") if isinstance(auth.get("custom_scopes"), dict) else {}
        for scope in sorted(declared - CORE_SCOPES):
            if scope.startswith("poppy:"):
                rows.append(finding("4.4-scopes", "fail", f"{scope!r} uses the reserved poppy: prefix",
                                    subject=subject))
            elif not isinstance(custom.get(scope), str):
                rows.append(finding("4.4-scopes", "warn", f"Custom scope {scope!r} has no description in "
                                    "auth.custom_scopes", subject=subject))
    agent = document.get("agent")
    if agent is not None:
        protocols = agent.get("protocols") if isinstance(agent, dict) else None
        if not isinstance(protocols, list) or not protocols:
            rows.append(finding("3.1-agent", "fail", "agent.protocols is not a nonempty list", subject=subject))
        else:
            for index, entry in enumerate(protocols):
                if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
                    rows.append(finding("3.1-agent", "fail", f"agent.protocols[{index}] has no type", subject=subject))
                    continue
                url_rule("3.1-agent", entry.get("endpoint"), f"agent.protocols[{index}].endpoint")
                if entry["type"] != "poppy":
                    rows.append(finding("3.1-agent", "info", f"Conversation protocol {entry['type']!r} is not "
                                        "defined by the draft; personal agents skip it", subject=subject))
    apis = document.get("apis")
    if apis is not None:
        if not isinstance(apis, list):
            rows.append(finding("6-apis", "fail", "apis is not a list", subject=subject))
        for index, entry in enumerate(apis if isinstance(apis, list) else []):
            kind = entry.get("type") if isinstance(entry, dict) else None
            if kind not in API_TYPES:
                rows.append(finding("6-apis", "info" if isinstance(kind, str) else "fail",
                                    f"apis[{index}] type {kind!r} is not openapi or mcp; personal agents skip it",
                                    subject=subject))
                continue
            url_rule("6-apis", entry.get("url"), f"apis[{index}].url")
            if not isinstance(entry.get("description"), str) or not entry["description"].strip():
                rows.append(finding("6-apis", "warn", f"apis[{index}] has no description to help a personal agent "
                                    "pick it", subject=subject))
    if "web" in document:
        if not isinstance(document["web"], dict):
            rows.append(finding("3.1-web", "fail", "web is not an object", subject=subject))
        elif "browser_session_endpoint" in web:
            url_rule("3.1-web", web["browser_session_endpoint"], "web.browser_session_endpoint")
    extensions = document.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, dict):
            rows.append(finding("3.3-extensions", "fail", "extensions is not an object keyed by name", subject=subject))
        for name, entry in (extensions if isinstance(extensions, dict) else {}).items():
            if "/" not in name and name not in EXTENSIONS:
                rows.append(finding("3.3-extensions", "warn", f"Extension {name!r} has a plain name the draft does "
                                    "not define; other parties' names start with a domain they control",
                                    subject=subject))
            if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
                rows.append(finding("3.3-extensions", "fail", f"Extension {name!r} has no version", subject=subject))
            elif name == "operations":
                url_rule("3.3-extensions", entry.get("endpoint"), "extensions.operations.endpoint")
    return rows


def validate_as_metadata(metadata, document, *, loopback_ok) -> list[PoppyFinding]:
    """3.2: the issuer's RFC 8414 metadata, checked against poppy.json."""
    rows, subject = [], "authorization server metadata"
    if not isinstance(metadata, dict):
        return [finding("3.2-metadata", "fail", "The metadata is not a JSON object", subject=subject)]
    issuer, domain, auth = field(document, "auth", "issuer"), field(document, "organization", "domain"), \
        field(document, "auth") or {}
    if metadata.get("issuer") != issuer:
        rows.append(finding("3.2-metadata", "fail", f"issuer {metadata.get('issuer')!r} does not exactly match "
                            f"auth.issuer {issuer!r}", subject=subject))
    domains = metadata.get("poppy_domains")
    if not isinstance(domains, list) or str(domain or "").lower() not in [str(item).lower() for item in domains]:
        rows.append(finding("3.2-metadata", "fail", f"poppy_domains {domains!r} does not include "
                            f"organization.domain {domain!r}", subject=subject))
    required = {"token_endpoint": True, "revocation_endpoint": True, "authorization_endpoint": "direct" in auth,
                "device_authorization_endpoint": "device" in auth}
    for name, needed in required.items():
        if needed and not secure_url(metadata.get(name), loopback_ok=loopback_ok):
            rows.append(finding("3.2-endpoints", "fail", f"{name} is missing or not an HTTPS URL", subject=subject))
    algorithms = metadata.get("dpop_signing_alg_values_supported")
    if not isinstance(algorithms, list) or "ES256" not in algorithms:
        rows.append(finding("3.2-dpop-algs", "fail", f"dpop_signing_alg_values_supported {algorithms!r} does not "
                            "include ES256", subject=subject))
    return rows


def validate_client_metadata(document, *, fetched_url: str, loopback_ok=False) -> list[PoppyFinding]:
    """4.1: a personal agent's client metadata document (CIMD) as fetched from fetched_url."""
    rows, subject = [], "client metadata"
    if not isinstance(document, dict):
        return [finding("4.1-client-metadata", "fail", "The client metadata is not a JSON object", subject=subject)]
    client_id = document.get("client_id")
    if client_id != fetched_url:
        rows.append(finding("4.1-client-metadata", "fail", f"client_id {client_id!r} is not the URL it was fetched "
                            f"from ({fetched_url!r})", subject=subject))
    if not secure_url(fetched_url, loopback_ok=loopback_ok):
        rows.append(finding("4.1-client-metadata", "fail", "client_id is not an HTTPS URL", subject=subject))
    host = urlsplit(fetched_url).hostname
    for name, value in [("jwks_uri", document.get("jwks_uri")),
                        *((f"redirect_uris[{index}]", item) for index, item in
                          enumerate(document.get("redirect_uris") or []))]:
        if not secure_url(value, loopback_ok=loopback_ok) or urlsplit(value).hostname != host:
            rows.append(finding("4.1-client-metadata", "fail", f"{name} is not an HTTPS URL on the client_id's "
                                f"domain ({host})", subject=subject))
    if document.get("token_endpoint_auth_method") != "private_key_jwt":
        rows.append(finding("4.1-client-metadata", "fail", "token_endpoint_auth_method is not private_key_jwt",
                            subject=subject))
    if not isinstance(document.get("client_name"), str):
        rows.append(finding("4.1-client-metadata", "warn", "No client_name for the company's sign-in page to show",
                            subject=subject))
    return rows


def private_members(jwks) -> list[str]:
    """Key ids holding private members: "The key set contains only public keys." """
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    return [str(key.get("kid")) for key in keys or [] if isinstance(key, dict)
            and {"d", "p", "q", "dp", "dq", "qi", "k"} & set(key)]


# --- Keys, assertions and DPoP proofs ---------------------------------------------------------------

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_jti() -> str:
    return b64(secrets.token_bytes(18))  # at least 128 random bits (4.2)


class Key:
    """One ES256 key; the public JWK goes in a JWKS (signing keys) or a proof header (DPoP keys)."""

    def __init__(self, kid: str, private=None):
        from cryptography.hazmat.primitives.asymmetric import ec
        from jwt.algorithms import ECAlgorithm
        self.kid = kid
        self.private = private or ec.generate_private_key(ec.SECP256R1())
        public = ECAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        self.public = {name: public[name] for name in ("kty", "crv", "x", "y")}

    @classmethod
    def load(cls, text: str, kid: str) -> "Key":
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from jwt.algorithms import ECAlgorithm
        try:
            private = (load_pem_private_key(text.encode(), password=None) if "-----BEGIN" in text
                       else ECAlgorithm.from_jwk(text))
        except (ValueError, TypeError) as error:
            raise LabError(f"The probe identity key is not an EC private key: {type(error).__name__}") from None
        if not isinstance(private, ec.EllipticCurvePrivateKey) or private.curve.name != "secp256r1":
            raise LabError("The probe identity key must be a P-256 private key (ES256)")
        return cls(kid, private)

    @property
    def jwk(self) -> dict:
        return {**self.public, "kid": self.kid, "alg": "ES256", "use": "sig"}

    def thumbprint(self) -> str:  # RFC 7638
        return b64(hashlib.sha256(json.dumps(self.public, sort_keys=True, separators=(",", ":")).encode()).digest())

    def sign(self, claims: dict, **headers) -> str:
        import jwt
        return jwt.encode(claims, self.private, algorithm="ES256", headers=headers)


def ath(token: str) -> str:
    return b64(hashlib.sha256(token.encode()).digest())


def htu(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def proof(key: Key, method: str, url: str, *, token=None, nonce=None, overrides=None) -> str:
    """A DPoP proof (RFC 9449 §4.2); overrides break one claim on purpose."""
    claims = {"jti": new_jti(), "htm": method, "htu": htu(url), "iat": int(time.time())}
    if token is not None:
        claims["ath"] = ath(token)
    if nonce:
        claims["nonce"] = nonce
    claims.update(overrides or {})
    return key.sign(claims, typ="dpop+jwt", jwk=key.public)


# --- The loopback identity ---------------------------------------------------------------------------

class LoopbackIdentity:
    """Serve this checker's client metadata documents on 127.0.0.1 and count each fetch, so a probe can
    show the company fetched (and refused) them. Never reachable from outside this machine."""

    def __init__(self, key: Key, *, extensions: dict):
        self.key, self.extensions, self.fetches, self.server = key, extensions, {}, None
        self.base = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def client_id(self):
        return f"{self.base}/agent.json"

    def document(self, name: str) -> dict | None:
        if not self.base:
            return None
        port = self.base.rsplit(":", 1)[1]
        documents = {
            "/agent.json": self.metadata("/agent.json"),
            # client_id names another document than the one fetched (4.1: it MUST equal the URL).
            "/mismatch.json": self.metadata("/agent.json"),
            # The key set is on another domain than the client_id (4.1: the same domain is required).
            "/offsite.json": {**self.metadata("/offsite.json"), "jwks_uri": f"http://localhost:{port}/jwks.json"},
            "/jwks.json": {"keys": [self.key.jwk]},
        }
        return documents.get(name)

    def metadata(self, path) -> dict:
        return {"client_id": f"{self.base}{path}", "client_name": "Consumer Agent Testing Tool Poppy probe",
                "jwks_uri": f"{self.base}/jwks.json", "redirect_uris": [f"{self.base}/oauth/callback"],
                "token_endpoint_auth_method": "private_key_jwt",
                **({"extensions": self.extensions} if self.extensions else {})}

    async def _serve(self, reader, writer):
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode("latin-1")
            while (await asyncio.wait_for(reader.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass  # headers are not needed
            parts = line.split()
            path = urlsplit(parts[1]).path if len(parts) >= 2 else ""
            body = self.document(path) if parts and parts[0] == "GET" else None
            self.fetches[path] = self.fetches.get(path, 0) + 1
            data = json.dumps(body).encode() if body is not None else b'{"error": "not_found"}'
            status = "200 OK" if body is not None else "404 Not Found"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                         "Connection: close\r\nCache-Control: no-store\r\n\r\n".encode() + data)
            await writer.drain()
        except (TimeoutError, ConnectionError, IndexError):
            pass
        finally:
            writer.close()


# --- HTTP with evidence -------------------------------------------------------------------------------

AUTH_PARAM = re.compile(r"""\s*(?:([!#$%&'*+.^_`|~0-9A-Za-z-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]+)|"""
                        r"""([!#$%&'*+.^_`|~0-9A-Za-z-]+))\s*,?""")


def challenges(header: str | None) -> list[tuple[str, dict]]:
    """WWW-Authenticate challenges (RFC 9110 §11.6.1) as (scheme, params)."""
    found = []
    for name, value, scheme in AUTH_PARAM.findall(header or ""):
        if scheme:
            found.append((scheme, {}))
        elif found:
            found[-1][1][name.lower()] = value[1:-1].replace('\\"', '"') if value.startswith('"') else value
    return found


def error_code(response: httpx.Response) -> str | None:
    """The OAuth error code: in WWW-Authenticate at APIs (section 6), in the JSON body elsewhere."""
    for _scheme, params in challenges(response.headers.get("www-authenticate")):
        if params.get("error"):
            return params["error"]
    body = body_json(response)
    return body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else None


def body_json(response: httpx.Response):
    try:
        return response.json() if response.content and len(response.content) <= MAX_BODY else None
    except ValueError:
        return None


def internal_address(address) -> bool:
    """Anything but a globally routable unicast address: private, loopback, link-local (cloud metadata),
    shared (CGNAT), reserved, multicast or unspecified, also as an IPv4-mapped IPv6 address."""
    address = getattr(address, "ipv4_mapped", None) or address
    return not address.is_global or address.is_multicast


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class Egress:
    """Contact only the target, its organization's domain and subdomains, the issuer's host and hosts the
    probe manifest declares; anything else a document names is refused before a request is sent.

    A host the developer did not name (the issuer, an API, the organization's other hosts) must not be an
    internal address when the target is not one: an address literal is checked as written, a name is
    resolved (when a resolver is given: the real one on the network) and the request goes to the address
    vetted, so a later resolution cannot rebind it."""

    def __init__(self, origin: str, *, resolver=None):
        host = urlsplit(origin).hostname.lower()
        self.loopback = host in LOOPBACK
        # Before poppy.json names its domain, the well-known URL may redirect within the target's own site
        # ("from example.com to www.example.com", section 3).
        self.hosts, self.domains = {host}, {host, host.removeprefix("www.")}
        self.target, self.declared, self.resolver = host, {host}, resolver
        self.vetted, self.internal_target = {}, None

    def allow(self, url=None, *, domain=None, declared=False):
        """Reach `url`'s host (and `domain` with its subdomains); `declared`: the developer named it."""
        if domain:
            self.domains.add(domain.lower())
        if isinstance(url, str) and urlsplit(url).hostname:
            self.hosts.add(urlsplit(url).hostname.lower())
            if declared:
                self.declared.add(urlsplit(url).hostname.lower())

    def check(self, url: str):
        host = (urlsplit(url).hostname or "").lower()
        if self.loopback:
            if host not in LOOPBACK:
                raise Unsupported(f"A loopback test deployment named {host!r}; the probes stay on loopback")
        elif host not in self.hosts and not any(host == item or host.endswith("." + item) for item in self.domains):
            raise Unsupported(f"{host!r} is outside the company's declared hosts; the checker does not contact it")
        if not secure_url(url, loopback_ok=self.loopback):
            raise Unsupported(f"Refusing a non-HTTPS request to {url[:120]!r}")

    async def addresses(self, host: str, port: int) -> list | None:
        """The host's addresses: itself as a literal, else resolved (None without a resolver)."""
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            pass
        if self.resolver is None:
            return None
        try:
            async with asyncio.timeout(10):
                found = await self.resolver(host, port)
            return [ipaddress.ip_address(str(item).split("%")[0]) for item in found]
        except (OSError, TimeoutError, ValueError):
            raise Unsupported(f"{host!r} did not resolve; the checker does not contact it") from None

    async def vet(self, url: str) -> str | None:
        """The address to send `url` to (None: as named): a host the developer did not name never reaches an
        internal address, unless the target itself is one (an internal deployment)."""
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port or 443
        if self.loopback or host in self.declared:
            return None
        if self.internal_target is None:
            found = await self.addresses(self.target, port)
            self.internal_target = bool(found) and any(internal_address(item) for item in found)
        if host not in self.vetted:
            found = await self.addresses(host, port)
            if found is not None and (not found or not self.internal_target
                                      and any(internal_address(item) for item in found)):
                raise Unsupported(f"{host!r} is an internal address, which the company's documents named; the "
                                  "checker does not contact it")
            literal = found is not None and len(found) == 1 and str(found[0]) == host.strip("[]")
            self.vetted[host] = None if found is None or literal else str(found[0])
        return self.vetted[host]


class Http:
    """One client: no redirects followed implicitly, DPoP nonces remembered per origin and retried once,
    and every exchange recorded with registered secrets masked."""

    def __init__(self, client: httpx.AsyncClient, run: StaticRun, egress: Egress):
        self.client, self.run, self.egress, self.nonces, self.nonce_required = client, run, egress, {}, False
        self.exchanges = 0

    def secret(self, *values):
        self.run.redactor.add([value for value in values if isinstance(value, str)])

    async def send(self, probe: str, method: str, url: str, *, headers=None, retry_nonce=True, **kwargs):
        """headers may be a function of the current nonce, so a retried request gets fresh proofs."""
        self.egress.check(url)
        address = await self.egress.vet(url)
        origin = urlunsplit(urlsplit(url)[:2] + ("", "", ""))
        for attempt in range(2):
            built = headers(self.nonces.get(origin)) if callable(headers) else dict(headers or {})
            self.secret(built.get("Authorization"), built.get("DPoP"))  # never Accept and the like
            try:
                response = await self.bounded(method, url, built, kwargs, address)
            except httpx.HTTPError as error:
                raise LabError(f"{method} {urlsplit(url).netloc}{urlsplit(url).path} failed: "
                               f"{type(error).__name__}: {error}") from None
            self.exchanges += 1
            if nonce := response.headers.get("dpop-nonce"):
                self.nonces[origin] = nonce
            self.record(probe, method, url, built, kwargs, response)
            if attempt == 0 and retry_nonce and nonce and error_code(response) == "use_dpop_nonce":
                self.nonce_required = True
                continue
            return response
        return response

    async def bounded(self, method, url, headers, kwargs, address=None) -> httpx.Response:
        if address is not None:
            # To the vetted address, keeping the Host header and the TLS server name (Egress.vet).
            parts = urlsplit(url)
            url = str(httpx.URL(url).copy_with(host=address))
            headers = {"Host": parts.netloc.rpartition("@")[2], **headers}
            kwargs = {**kwargs, "extensions": {**kwargs.get("extensions", {}), "sni_hostname": parts.hostname}}
        from .ext_custom_connector import read_bounded
        async with self.client.stream(method, url, headers=headers, **kwargs) as response:
            # Decoded a bounded step at a time (gzip, deflate, zstd): a few compressed kilobytes never expand in
            # memory past MAX_BODY.
            read = await read_bounded(response, MAX_BODY)
        where = f"{urlsplit(url).netloc}{urlsplit(url).path}"
        if read["truncated"]:
            raise LabError(f"{where} answered more than {MAX_BODY} bytes")
        if read["undecoded"]:
            raise LabError(f"{where} answered in {read['encoding']} encoding that does not decode")
        # The body is decoded already: keep every header (Set-Cookie included) but the encoding ones.
        headers = [(name, value) for name, value in response.headers.multi_items()
                   if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(response.status_code, headers=headers, content=read["body"], request=response.request)

    def record(self, probe, method, url, headers, kwargs, response):
        body = body_json(response)
        if isinstance(body, dict):
            # Registered before the event is written. Not Redactor.learn: it would take the issuer metadata's
            # token_endpoint and authorization_endpoint URLs for credentials.
            self.secret(*(body.get(name) for name in TOKEN_FIELDS))
        self.run.event("http_exchange", {
            "probe": probe, "request": {"method": method, "url": url, "headers": sorted(headers),
                                        "form": sorted(kwargs.get("data") or {}),
                                        "json": kwargs.get("json")},
            "response": {"status": response.status_code,
                         "headers": {name: response.headers.get(name) for name in (
                             "www-authenticate", "dpop-nonce", "location", "cache-control", "retry-after",
                             "content-type") if response.headers.get(name)},
                         "set_cookie": [cookie_attributes(value) for value in response.headers.get_list("set-cookie")],
                         "body": body if body is not None else response.text[:1000]}})


def cookie_attributes(header: str) -> dict:
    """A Set-Cookie header's name and attributes, never its value."""
    name, _, rest = header.partition("=")
    attributes = {}
    for part in rest.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        attributes[key.lower()] = value or True
    return {"name": name.strip(), "attributes": attributes}


# --- The personal agent -------------------------------------------------------------------------------

class Agent:
    """The probe's personal agent: client assertions and Session assertions signed with its identity key,
    DPoP proofs with per-session keys, and token requests to the company's token endpoint."""

    def __init__(self, http: Http, *, client_id: str, key: Key, issuer: str, token_endpoint: str, user_id: str):
        self.http, self.client_id, self.key, self.issuer = http, client_id, key, issuer
        self.token_endpoint, self.user_id = token_endpoint, user_id

    def claims(self, sub, aud=None, **extra):
        now = int(time.time())
        return {"iss": self.client_id, "sub": sub, "aud": aud or self.issuer, "iat": now, "exp": now + 60,
                "jti": new_jti(), **extra}

    def client_assertion(self, *, aud=None, key=None, client_id=None):
        """private_key_jwt (RFC 7523): iss and sub are the client_id, aud the issuer as one string."""
        client_id, key = client_id or self.client_id, key or self.key
        return key.sign({**self.claims(client_id, aud), "iss": client_id}, kid=key.kid)

    def session_assertion(self, **extra):
        return self.key.sign(self.claims(self.user_id, **extra), kid=self.key.kid)

    async def token(self, probe, fields, *, dpop: Key | None, client_id=None, client_key=None, aud=None,
                    reuse_proof=None):
        form = {**fields, "client_id": client_id or self.client_id, "client_assertion_type": CLIENT_ASSERTION}

        def headers(nonce):
            # A fresh client assertion per attempt; the replay probe reuses one proof on purpose.
            form["client_assertion"] = self.client_assertion(aud=aud, key=client_key, client_id=client_id)
            self.http.secret(form["client_assertion"], fields.get("assertion"), fields.get("refresh_token"))
            if reuse_proof:
                return {"DPoP": reuse_proof}
            return {"DPoP": proof(dpop, "POST", self.token_endpoint, nonce=nonce)} if dpop else {}
        response = await self.http.send(probe, "POST", self.token_endpoint, headers=headers, data=form,
                                        retry_nonce=reuse_proof is None)
        body = body_json(response)
        if isinstance(body, dict):
            self.http.secret(body.get("access_token"), body.get("refresh_token"))
        return response, body if isinstance(body, dict) else {}

    async def start_session(self, probe, dpop: Key):
        return await self.token(probe, {"grant_type": JWT_BEARER, "assertion": self.session_assertion()}, dpop=dpop)

    async def call(self, probe, request: Request | tuple, token: str | None, dpop: Key | None, *, scheme="DPoP",
                   overrides=None, proof_key=None, query_token=False):
        method, url, body = ((request.method, request.url, request.json_body) if isinstance(request, Request)
                             else request)
        if query_token and token:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode({'access_token': token})}"

        def headers(nonce):
            built = {} if token is None or query_token else {"Authorization": f"{scheme} {token}"}
            if dpop is not None and not query_token:
                built["DPoP"] = proof(proof_key or dpop, method, url, token=token, nonce=nonce, overrides=overrides)
            return built
        response = await self.http.send(probe, method, url, headers=headers,
                                        **({"json": body} if body is not None else {}))
        return response, body_json(response)


def expect(response, status: int, code: str | None) -> tuple[str, dict]:
    """"pass" on the documented status and code, "warn" on the right status with another code, else "fail"."""
    observed = {"status": response.status_code, "error": error_code(response)}
    if response.status_code != status:
        return "fail", observed
    return ("pass" if code is None or observed["error"] == code else "warn"), observed


class Probes:
    """The active half: every probe records what it sent and saw, and adds findings."""

    def __init__(self, http, agent, document, metadata, identity, manifest):
        self.http, self.agent, self.document, self.metadata = http, agent, document, metadata
        self.identity, self.manifest = identity, manifest
        self.findings, self.ran, self.sessions = [], [], {}
        self.mediated = field(document, "auth", "mediated") if isinstance(field(document, "auth", "mediated"),
                                                                          dict) else None
        # The company asked for a one-time code: only the person can finish, and every later sign-in of the
        # test account would send them another code, so none is attempted.
        self.code_required = False
        self.accounts = []  # every signed-in Session, so no Account Token the probes got outlives them

    def add(self, rule, severity, evidence, subject=None):
        self.findings.append(finding(rule, severity, evidence, subject=subject))

    def grade(self, rule, outcome, observed, expected, subject):
        """Record one expectation: a pass adds nothing but the probe name; warn and fail say what was seen."""
        self.ran.append(f"{rule}:{subject}")
        if outcome != "pass":
            self.add(rule, outcome, f"expected {expected}; observed {json.dumps(observed)}", subject)

    async def session(self, name="signed-out"):
        """A fresh signed-out Session with its own DPoP key; None if the company refused to start one."""
        key = Key(f"dpop-{name}")
        response, body = await self.agent.start_session(f"session:{name}", key)
        token = body.get("access_token")
        if response.status_code != 200 or not isinstance(token, str):
            return None
        self.sessions[name] = {"key": key, "token": token, "session_id": body.get("session_id")}
        return self.sessions[name]

    # -- 4.1 / 4.2 / 4.3 at the token endpoint ---------------------------------------------------------
    async def token_endpoint(self):
        key = Key("dpop-main")
        assertion = self.agent.session_assertion()
        fetched = dict(self.identity.fetches) if isinstance(self.identity, LoopbackIdentity) else None
        response, body = await self.agent.token("start-session", {"grant_type": JWT_BEARER, "assertion": assertion},
                                                dpop=key)
        self.ran.append("4.2-start-session")
        if response.status_code != 200 or not isinstance(body.get("access_token"), str):
            self.add("4.2-start-session", "fail", f"The token endpoint did not start a Session: HTTP "
                     f"{response.status_code}, error {error_code(response)!r}", "token endpoint")
            return None
        problems = []
        if str(body.get("token_type", "")).lower() != "dpop":
            problems.append(f"token_type {body.get('token_type')!r} is not DPoP")
        if type(body.get("expires_in")) is not int or body["expires_in"] <= 0:
            problems.append(f"expires_in {body.get('expires_in')!r} is not a positive integer")
        if not isinstance(body.get("session_id"), str) or not body["session_id"]:
            problems.append("no session_id")
        if body.get("signed_in") is not False:
            problems.append(f"signed_in {body.get('signed_in')!r} for a new Session (it starts signed out)")
        if body.get("scope", "") != "":
            problems.append("a signed-out token carries account scopes")
        if "refresh_token" in body:
            problems.append("a signed-out Session got an Account Token")
        for problem in problems:
            self.add("4.2-token-response", "fail", problem, "token endpoint")
        if "no-store" not in response.headers.get("cache-control", "").lower():
            self.add("4.2-token-response", "warn", "The token response is not Cache-Control: no-store (RFC 6749 "
                     "§5.1, which the draft's example follows)", "token endpoint")
        unread = [name for name in ("/agent.json", "/jwks.json") if fetched is not None
                  and self.identity.fetches.get(name, 0) == fetched.get(name, 0)]
        if unread:  # A fresh port per run: nothing could have been cached.
            self.add("4.2-start-session", "fail", f"The company issued a Session Token without fetching "
                     f"{' or '.join(unread)}, so it verified neither the client nor its signature", "token endpoint")
        main = self.sessions["main"] = {"key": key, "token": body["access_token"], "session_id": body["session_id"]}
        # The same assertion again (fresh client assertion and proof): its jti was seen.
        response, _ = await self.agent.token("assertion-replay", {"grant_type": JWT_BEARER, "assertion": assertion},
                                             dpop=Key("dpop-replay"))
        self.grade("4.2-assertion-replay", *expect(response, 400, "invalid_grant"), "400 invalid_grant",
                   "token endpoint")
        # One proof twice at the token endpoint: the second use MUST be refused.
        reused = proof(Key("dpop-proof"), "POST", self.agent.token_endpoint,
                       nonce=self.http.nonces.get(urlunsplit(urlsplit(self.agent.token_endpoint)[:2] + ("", "", ""))))
        first, _ = await self.agent.token("proof-first-use", {"grant_type": JWT_BEARER,
                                                               "assertion": self.agent.session_assertion()},
                                          dpop=None, reuse_proof=reused)
        if first.status_code == 200:
            response, _ = await self.agent.token("proof-replay", {"grant_type": JWT_BEARER,
                                                                  "assertion": self.agent.session_assertion()},
                                                 dpop=None, reuse_proof=reused)
            self.grade("4.3-proof-replay", *expect(response, 400, "invalid_dpop_proof"), "400 invalid_dpop_proof",
                       "token endpoint")
        else:  # A refused first use proves nothing about the replay.
            self.add("4.3-proof-replay", "info", f"Not evaluated: the proof's first use got HTTP {first.status_code}",
                     "token endpoint")
        # A client assertion addressed to the token endpoint instead of the issuer.
        response, _ = await self.agent.token("assertion-audience", {"grant_type": JWT_BEARER,
                                                                    "assertion": self.agent.session_assertion()},
                                             dpop=Key("dpop-aud"), aud=self.agent.token_endpoint)
        self.grade("4.1-assertion-audience", *expect(response, 401, "invalid_client"), "401 invalid_client",
                   "token endpoint")
        # Renewing: the same Session, a new signed-out token.
        response, body = await self.agent.token("renew", {"grant_type": JWT_BEARER,
                                                          "assertion": self.agent.session_assertion(),
                                                          "session_id": main["session_id"]}, dpop=key)
        self.ran.append("4.2-renew")
        if response.status_code != 200 or body.get("session_id") != main["session_id"] \
                or body.get("access_token") in (None, main["token"]) or body.get("signed_in") is not False:
            self.add("4.2-renew", "fail", f"Renewal answered HTTP {response.status_code} without a new signed-out "
                     "token for the same Session", "token endpoint")
        elif isinstance(body.get("access_token"), str):
            main["token"] = body["access_token"]  # "it MUST use the new token from then on"
        if isinstance(self.identity, LoopbackIdentity):
            await self.client_metadata_refusals()
        return main

    async def client_metadata_refusals(self):
        """4.1, the company's side of CIMD: refuse a document whose client_id is not its URL, a key set on
        another domain, and an assertion signed by a key outside the key set."""
        cases = [("client_id-mismatch", f"{self.identity.base}/mismatch.json", None),
                 ("offsite-jwks", f"{self.identity.base}/offsite.json", None),
                 ("unknown-key", self.identity.client_id, Key("rogue"))]
        for name, client_id, rogue in cases:
            agent = Agent(self.http, client_id=client_id, key=rogue or self.agent.key, issuer=self.agent.issuer,
                          token_endpoint=self.agent.token_endpoint, user_id=self.agent.user_id)
            response, _ = await agent.token(f"client-metadata:{name}", {"grant_type": JWT_BEARER,
                                                                         "assertion": agent.session_assertion()},
                                            dpop=Key(f"dpop-{name}"), client_key=rogue)
            self.grade("4.1-client-metadata", *expect(response, 401, "invalid_client"), "401 invalid_client",
                       f"token endpoint ({name})")

    # -- 4.3 / 6 at a DPoP resource --------------------------------------------------------------------
    def resource(self):
        """A read-only DPoP resource every Poppy company defines: an unknown operation or conversation."""
        endpoint = field(self.document, "extensions", "operations", "endpoint")
        if isinstance(endpoint, str):
            return ("GET", f"{endpoint.rstrip('/')}/op_{new_jti()}", None), "operation_not_found", \
                "operations endpoint"
        protocols = field(self.document, "agent", "protocols")
        for entry in protocols if isinstance(protocols, list) else []:
            if isinstance(entry, dict) and entry.get("type") == "poppy" and isinstance(entry.get("endpoint"), str):
                return ("GET", f"{entry['endpoint'].rstrip('/')}/cnv_{new_jti()}/events", None), \
                    "conversation_not_found", "conversation endpoint"
        return None, None, None

    async def token_use(self, main):
        request, not_found, subject = self.resource()
        if request is None:
            self.add("4.3-dpop-required", "info", "No operations or poppy conversation endpoint to probe; DPoP token "
                     "use was not exercised", "resources")
            return
        token, key = main["token"], main["key"]
        rule = "ops-not-found" if not_found == "operation_not_found" else "7.13-conversation-not-found"
        response, _ = await self.agent.call("dpop-accepted", request, token, key)
        self.grade(rule, *expect(response, 404, not_found), f"404 {not_found}", subject)
        if response.status_code in (400, 401, 403):
            # Every refusal below would pass vacuously when even the valid token is refused.
            self.add("4.3-proof-binding", "fail", f"A valid DPoP-bound Session Token was refused (HTTP "
                     f"{response.status_code}, {error_code(response)!r}); the refusal probes were not run", subject)
            return
        response, _ = await self.agent.call("no-token", request, None, None)
        outcome, observed = expect(response, 401, "invalid_token")
        has_dpop = any(scheme.lower() == "dpop" for scheme, _ in challenges(response.headers.get("www-authenticate")))
        self.grade("6-invalid-token", "warn" if outcome == "pass" and not has_dpop else outcome,
                   {**observed, "dpop_challenge": has_dpop}, "401 with a DPoP challenge, error=invalid_token", subject)
        response, _ = await self.agent.call("bearer-scheme", request, token, None, scheme="Bearer")
        self.grade("4.3-dpop-required", "fail" if response.status_code != 401 else "pass",
                   {"status": response.status_code}, "401: DPoP is required outside MCP", subject)
        response, _ = await self.agent.call("no-proof", request, token, None)
        self.grade("4.3-dpop-required", *expect(response, 401, "invalid_dpop_proof"), "401 invalid_dpop_proof",
                   f"{subject} (no proof)")
        for name, extra in (("unbound-key", {"proof_key": Key("dpop-other")}),
                            ("wrong-ath", {"overrides": {"ath": ath(token + "x")}}),
                            ("wrong-htu", {"overrides": {"htu": htu(urljoin(request[1], "/elsewhere"))}}),
                            ("wrong-htm", {"overrides": {"htm": "DELETE"}})):
            response, _ = await self.agent.call(name, request, token, key, **extra)
            self.grade("4.3-proof-binding", *expect(response, 401, "invalid_dpop_proof"), "401 invalid_dpop_proof",
                       f"{subject} ({name})")
        response, _ = await self.agent.call("token-in-url", request, token, None, query_token=True)
        self.grade("4.3-token-in-url", "fail" if response.status_code not in (400, 401) else "pass",
                   {"status": response.status_code}, "the token in the URL refused (400 or 401)", subject)

    # -- 5: the browser join ---------------------------------------------------------------------------
    async def browser(self, main):
        endpoint = field(self.document, "web", "browser_session_endpoint")
        if not isinstance(endpoint, str):
            return
        domain = str(field(self.document, "organization", "domain") or "")
        origin = urlunsplit(urlsplit(endpoint)[:2] + ("", "", ""))
        return_to = self.manifest.browser.return_to or f"{origin}/"

        def assertion(**changes):
            claims = self.agent.claims(self.agent.user_id, endpoint, session_id=main["session_id"],
                                       return_to=return_to)
            typ = changes.pop("typ", "poppy-browser+jwt")
            claims.update(changes)
            signed = self.agent.key.sign(claims, kid=self.agent.key.kid, typ=typ)
            self.http.secret(signed)
            return signed

        async def post(name, value):
            return await self.http.send(f"browser:{name}", "POST", endpoint, data={"assertion": value})
        joined = assertion()
        response = await post("join", joined)
        self.ran.append("5-browser-join")
        cookies = [cookie_attributes(value) for value in response.headers.get_list("set-cookie")]
        location = urljoin(endpoint, response.headers.get("location", ""))
        if response.status_code != 303 or location != return_to or not cookies:
            self.add("5-browser-join", "fail", f"Join answered HTTP {response.status_code}, Location "
                     f"{response.headers.get('location')!r}, {len(cookies)} cookie(s); expected 303 to "
                     f"{return_to} with the Session's cookie; the refusal probes were not run",
                     "browser session endpoint")
            return
        for cookie in cookies:
            attributes = cookie["attributes"]
            same_site = str(attributes.get("samesite", "")).lower()
            if not attributes.get("secure") or not attributes.get("httponly") or same_site not in ("lax", "none"):
                self.add("5-cookie", "fail", f"Cookie {cookie['name']!r} is not Secure, HttpOnly and SameSite=Lax or "
                         f"None (attributes: {sorted(attributes)}, SameSite={same_site or 'unset'})",
                         "browser session endpoint")
        now = int(time.time())
        refusals = [("replay", joined), ("session-assertion-typ", assertion(typ="JWT")),
                    ("foreign-return_to", assertion(return_to=f"https://elsewhere.invalid/{domain}")),
                    ("long-lived", assertion(iat=now, exp=now + 120)),
                    ("wrong-aud", assertion(aud=self.agent.issuer))]
        for name, value in refusals:
            response = await post(name, value)
            refused = response.status_code == 400 and not response.headers.get_list("set-cookie")
            self.grade("5-browser-checks", "pass" if refused else "fail",
                       {"status": response.status_code, "cookies": len(response.headers.get_list("set-cookie"))},
                       "400 and no cookie", f"browser session endpoint ({name})")

    # -- 4.7 / 4.8 / 6: signed-in Sessions --------------------------------------------------------------
    def credentials(self):
        sign_in = self.manifest.sign_in
        values = {name: os.environ.get(variable) for name, variable in sign_in.credentials_env.items()}
        missing = sorted(variable for name, variable in sign_in.credentials_env.items() if not values[name])
        if missing:
            raise LabError(f"Set the test account credentials in {', '.join(missing)}")
        self.http.secret(*values.values())
        return values

    async def sign_in(self, session, scope, name):
        """Mediated Sign-In for a Session; the signed-in token replaces the Session's token."""
        endpoint = self.mediated.get("endpoint") if self.mediated else None
        if not isinstance(endpoint, str):
            self.add("4.7-mediated", "blocked", "The probe manifest declares a mediated test account, but poppy.json "
                     "offers no auth.mediated.endpoint", "mediated sign-in")
            return None
        credentials = self.credentials()
        response, body = await self.agent.call(f"sign-in:{name}", ("POST", endpoint, {
            "scope": scope, "credentials": credentials}), session["token"], session["key"])
        self.ran.append(f"4.7-mediated:{name}")
        body = body if isinstance(body, dict) else {}
        self.http.secret(body.get("access_token"), body.get("refresh_token"))
        echoed = [field for field, value in credentials.items() if value and value in response.text]
        if echoed:
            self.add("4.7-mediated", "fail", f"The sign-in response returned the {', '.join(echoed)} credential",
                     "mediated sign-in")
        if body.get("status") == "code_required":
            self.code_required = True
            self.add("4.7-mediated", "blocked", "The company asked for a one-time code; the probes cannot finish this "
                     "sign-in without the person", "mediated sign-in")
            return None
        if response.status_code != 200 or body.get("status") != "complete" or body.get("signed_in") is not True \
                or not isinstance(body.get("access_token"), str) or not isinstance(body.get("refresh_token"), str):
            self.add("4.7-mediated", "fail", f"Mediated Sign-In answered HTTP {response.status_code}, status "
                     f"{body.get('status')!r}; expected complete with a signed-in token and an Account Token",
                     "mediated sign-in")
            return None
        if body.get("session_id") != session["session_id"]:
            self.add("4.7-mediated", "fail", "Sign-in signed in another Session than the one that asked",
                     "mediated sign-in")
        granted = set(str(body.get("scope", "")).split())
        if not granted <= set(scope.split()):
            self.add("4.7-mediated", "fail", f"Sign-in granted {sorted(granted - set(scope.split()))} beyond the "
                     "requested scopes", "mediated sign-in")
        signed_in = {**session, "token": body["access_token"], "account": body["refresh_token"], "scope": granted,
                     "name": name}
        self.accounts.append(signed_in)
        return signed_in

    async def account(self):
        sign_in, declared = self.manifest.sign_in, self.manifest.requests
        signed_out = await self.session("account-signed-out")
        if signed_out and declared.sign_in_required:
            response, _ = await self.agent.call("sign-in-required:signed-out", declared.sign_in_required,
                                                signed_out["token"], signed_out["key"])
            self.grade("6-sign-in-required", *expect(response, 403, "sign_in_required"), "403 sign_in_required",
                       "declared request")
        if sign_in is None:
            return None
        session = await self.session("account")
        signed_in = session and await self.sign_in(session, sign_in.scope, "account")
        if not signed_in:
            return None
        if declared.sign_in_required:
            response, _ = await self.agent.call("sign-in-required:signed-in", declared.sign_in_required,
                                                signed_in["token"], signed_in["key"])
            self.grade("6-sign-in-required", "pass" if response.status_code < 400 else "fail",
                       {"status": response.status_code, "error": error_code(response)}, "success when signed in",
                       "declared request (signed in)")
        if declared.insufficient_scope:
            response, _ = await self.agent.call("insufficient-scope", declared.insufficient_scope,
                                                signed_in["token"], signed_in["key"])
            outcome, observed = expect(response, 403, "insufficient_scope")
            named = next((params.get("scope") for _s, params in challenges(response.headers.get("www-authenticate"))
                          if params.get("error") == "insufficient_scope"), None)
            self.grade("6-insufficient-scope", "warn" if outcome == "pass" and not named else outcome,
                       {**observed, "scope_attribute": named}, "403 insufficient_scope naming the scopes",
                       "declared request")
        # 4.8: the Account Token signs in the same Session again, then a revoked one is refused.
        response, body = await self.agent.token("account-token", {
            "grant_type": "refresh_token", "refresh_token": signed_in["account"],
            "session_id": signed_in["session_id"]}, dpop=signed_in["key"])
        self.ran.append("4.8-account-token")
        if response.status_code != 200 or body.get("signed_in") is not True \
                or body.get("session_id") != signed_in["session_id"]:
            self.add("4.8-account-token", "fail", f"The Account Token answered HTTP {response.status_code} without a "
                     "signed-in token for the Session", "token endpoint")
        else:
            signed_in["account"] = body.get("refresh_token") or signed_in["account"]  # rotation: use the new one
            if isinstance(body.get("access_token"), str):
                # The Session's newest token from here on, so a company that retires superseded tokens is not
                # blamed for the probe's stale one.
                signed_in["token"] = body["access_token"]
        return signed_in

    async def revoke(self, signed_in, probe):
        """RFC 7009 revocation of one Account Token the probes obtained."""
        form = {"token": signed_in["account"], "token_type_hint": "refresh_token", "client_id": self.agent.client_id,
                "client_assertion_type": CLIENT_ASSERTION, "client_assertion": self.agent.client_assertion()}
        self.http.secret(form["client_assertion"])
        response = await self.http.send(probe, "POST", self.metadata["revocation_endpoint"], data=form)
        signed_in["revocation"] = response.status_code
        return response

    async def release(self):
        """Revoke each Account Token no probe revoked (another sign-in's, or every one after a failed probe), so
        the test account is not left signed in. Best effort: the probes' own outcome stands either way."""
        if not isinstance(self.metadata.get("revocation_endpoint"), str):
            return
        for signed_in in self.accounts:
            if "revocation" not in signed_in:
                try:
                    await self.revoke(signed_in, f"sign-out:{signed_in['name']}")
                except LabError:  # a refused host or a failed request; the exchange is recorded as it happened
                    pass

    async def sign_out(self, signed_in):
        revocation = self.metadata.get("revocation_endpoint")
        if not signed_in or not isinstance(revocation, str):
            return
        response = await self.revoke(signed_in, "sign-out")
        if response.status_code != 200:
            self.add("4.9-sign-out", "fail", f"Revocation answered HTTP {response.status_code} (RFC 7009: 200)",
                     "revocation endpoint")
            return
        response, _ = await self.agent.token("revoked-account-token", {
            "grant_type": "refresh_token", "refresh_token": signed_in["account"],
            "session_id": signed_in["session_id"]}, dpop=signed_in["key"])
        self.grade("4.9-sign-out", *expect(response, 400, "invalid_grant"), "400 invalid_grant", "token endpoint")

    # -- Operations 3-7 ---------------------------------------------------------------------------------
    async def effects(self):
        declared = self.manifest.operations.effects
        if declared is None:
            return None
        response = await self.http.send("operations:effects", "GET", declared.url)
        value = body_json(response)
        for part in declared.pointer.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            value = value.get(part) if isinstance(value, dict) else value[int(part)] \
                if isinstance(value, list) and part.isdigit() and int(part) < len(value) else None
        if type(value) is not int:
            raise LabError(f"The effects oracle {declared.url} gave no integer at {declared.pointer}")
        return value

    async def operations(self, signed_in):
        declared = self.manifest.operations
        endpoint = field(self.document, "extensions", "operations", "endpoint")
        if declared is None:
            return
        if not isinstance(endpoint, str):
            self.add("ops-proposal", "blocked", "The manifest declares an operation, but poppy.json lists no "
                     "operations extension", "operations")
            return
        endpoint = endpoint.rstrip("/")
        if self.manifest.sign_in:
            session = signed_in
            if declared.scope and (not signed_in or set(declared.scope.split()) - signed_in.get("scope", set())):
                fresh = None if self.code_required else await self.session("operations")
                session = fresh and await self.sign_in(fresh, declared.scope, "operations")
            if not session:  # A failed proposal without the account would blame the company for the probe.
                self.add("ops-proposal", "blocked", "Not run: the test account's sign-in did not complete",
                         "operations")
                return
        else:
            session = await self.session("operations-signed-out")
            if not session:
                return
        token, key = session["token"], session["key"]
        before = await self.effects()
        response, body = await self.agent.call("operations:propose", declared.propose, token, key)
        operation = body.get("operation") if isinstance(body, dict) else None
        self.ran.append("ops-proposal")
        problems = [] if response.status_code in (200, 201, 202) else [f"HTTP {response.status_code}"]
        if not isinstance(operation, dict):
            problems.append("no operation object")
        else:
            if not isinstance(operation.get("operation_id"), str) or type(operation.get("revision")) is not int:
                problems.append("operation_id or revision missing")
            if operation.get("state") != "proposed":
                problems.append(f"state {operation.get('state')!r}")
            if not isinstance(operation.get("summary"), str) or not operation["summary"].strip():
                problems.append("no summary")
            if operation.get("confirmation") is not None or operation.get("result") is not None:
                problems.append("confirmation or result already set")
        if problems:
            self.add("ops-proposal", "fail", f"The proposal is not a proposed operation: {'; '.join(problems)}",
                     "operations")
            return
        if before is not None and await self.effects() != before:
            self.add("ops-proposal", "fail", "Proposing changed the effects oracle before any confirm", "operations")
        url = f"{endpoint}/{operation['operation_id']}"
        revision = operation["revision"]
        response, body = await self.agent.call("operations:stale-revision", ("POST", f"{url}/confirm", {
            "revision": revision + 1, "approved_by": declared.approved_by}), token, key)
        outcome, observed = expect(response, 409, "terms_changed")
        if outcome == "pass" and not isinstance((body or {}).get("operation"), dict):
            outcome = "warn"
        self.grade("ops-terms-changed", outcome, observed, "409 terms_changed with the current operation",
                   "operations")
        approved_by = declared.approved_by
        if operation.get("user_approval_required") is True:
            response, _ = await self.agent.call("operations:standing-permission", ("POST", f"{url}/confirm", {
                "revision": revision, "approved_by": "standing_permission"}), token, key)
            self.grade("ops-user-approval", *expect(response, 403, "user_approval_required"),
                       "403 user_approval_required", "operations")
            approved_by = "user"  # A personal agent asks the person for this revision.
        confirm = ("POST", f"{url}/confirm", {"revision": revision, "approved_by": approved_by})
        # Two confirms at once (a retry racing the original), then one more: one record, one effect.
        (first, one), (second, two) = await asyncio.gather(
            self.agent.call("operations:confirm", confirm, token, key),
            self.agent.call("operations:confirm-concurrently", confirm, token, key))
        third, three = await self.agent.call("operations:confirm-again", confirm, token, key)
        self.ran.append("ops-confirm")
        answers = [(first, one), (second, two), (third, three)]
        confirmed = [body for response, body in answers if response.status_code == 200 and isinstance(body, dict)
                     and body.get("state") in OPERATION_STATES - {"proposed"}
                     and (body.get("confirmation") or {}).get("revision") == revision]
        if not confirmed:
            self.add("ops-confirm", "fail", f"Confirm answered HTTP {first.status_code}, state "
                     f"{(one or {}).get('state')!r}", "operations")
            return
        if len(confirmed) != 3 or len({(body.get("operation_id"), (body.get("confirmation") or {}).get(
                "confirmed_at")) for body in confirmed}) != 1:
            self.add("ops-confirm", "fail", "Repeated confirms did not all return the one operation as it was "
                     f"(HTTP {[response.status_code for response, _ in answers]})", "operations")
        final, second = confirmed[-1], third
        for _ in range(10):
            if final.get("state") in FINAL_STATES:
                break
            wait = second.headers.get("retry-after", "1")
            await asyncio.sleep(min(float(wait), 5) if wait.replace(".", "", 1).isdigit() else 1)
            second, final = await self.agent.call("operations:track", ("GET", url, None), token, key)
            final = final if isinstance(final, dict) else {}
        if before is not None:
            performed = await self.effects() - before
            once = performed <= 1 and (final.get("state") != "succeeded" or performed == 1)
            self.grade("ops-at-most-once", "pass" if once else "fail",
                       {"performed": performed, "state": final.get("state")},
                       "one effect for three confirms, two of them concurrent (none beyond one if it did not "
                       "succeed)", "operations")
        else:
            self.add("ops-at-most-once", "info", "No effects oracle declared: at-most-once rests on the repeated "
                     "confirm's answer only", "operations")
        response, after = await self.agent.call("operations:cancel-final", ("POST", f"{url}/cancel", None),
                                                token, key)
        if final.get("state") in FINAL_STATES:
            unchanged = isinstance(after, dict) and after.get("state") == final.get("state")
            self.grade("ops-cancel", "pass" if response.status_code == 200 and unchanged else "fail",
                       {"status": response.status_code, "state": (after or {}).get("state")},
                       f"200 with the final state {final.get('state')}", "operations")


# --- The run ------------------------------------------------------------------------------------------

async def fetch_json(http: Http, probe: str, url: str, *, follow: bool, loopback_ok: bool):
    """GET a JSON document, following up to five redirects when asked; returns (status, body, final_url,
    findings). Every redirect must stay on HTTPS (section 3)."""
    rows = []
    for _ in range(6):
        response = await http.send(probe, "GET", url, headers={"Accept": "application/json"})
        if follow and response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
            target = urljoin(url, response.headers["location"])
            if not secure_url(target, loopback_ok=loopback_ok):
                rows.append(finding("3-redirects", "fail", f"A redirect leaves HTTPS: {target[:200]!r}",
                                    subject="poppy.json"))
                return response.status_code, None, url, rows
            url = target
            continue
        return response.status_code, body_json(response), url, rows
    rows.append(finding("3-redirects", "fail", "More than five redirects", subject="poppy.json"))
    return None, None, url, rows


async def discovery(http: Http, origin: str, egress: Egress) -> dict:
    loopback = egress.loopback
    status, document, served, rows = await fetch_json(http, "discovery", f"{origin}/.well-known/poppy.json",
                                                      follow=True, loopback_ok=loopback)
    result = {"findings": rows, "document": None, "metadata": None, "served_url": served}
    if status != 200 or document is None:
        if not rows:
            rows.append(finding("3-https", "fail", f"{origin}/.well-known/poppy.json answered HTTP {status} without "
                                "a JSON document", subject="poppy.json"))
        return result
    result["document"] = document
    rows += validate_poppy_json(document, served_url=served, loopback_ok=loopback)
    if not isinstance(document, dict):
        return result
    domain = field(document, "organization", "domain")
    # The document speaks only for the host that served it (section 3): a domain it merely claims widens nothing.
    vouched = isinstance(domain, str) and domain.lower() == (urlsplit(served).hostname or "").lower()
    egress.allow(served, domain=domain if vouched else None)
    issuer = field(document, "auth", "issuer")
    if not secure_url(issuer, loopback_ok=loopback):
        return result
    # The issuer's host only (Egress.vet keeps it off internal addresses); never its subdomains.
    egress.allow(issuer)
    try:
        status, metadata, _, _ = await fetch_json(http, "issuer-metadata", metadata_url(issuer), follow=False,
                                                  loopback_ok=loopback)
    except Unsupported as error:
        rows.append(finding("3.2-metadata", "blocked", str(error), subject="authorization server metadata"))
        return result
    if status != 200 or metadata is None:
        rows.append(finding("3.2-metadata", "fail", f"{metadata_url(issuer)} answered HTTP {status} without RFC 8414 "
                            "metadata", subject="authorization server metadata"))
        return result
    result["metadata"] = metadata
    rows += validate_as_metadata(metadata, document, loopback_ok=loopback)
    for index, entry in enumerate(document["apis"] if isinstance(document.get("apis"), list) else []):
        if isinstance(entry, dict) and entry.get("type") == "mcp" and secure_url(entry.get("url"),
                                                                                  loopback_ok=loopback):
            try:
                status, resource, _, _ = await fetch_json(http, f"mcp-resource-metadata:{index}",
                                                          resource_metadata_url(entry["url"]), follow=False,
                                                          loopback_ok=loopback)
            except Unsupported as error:
                rows.append(finding("6-mcp-metadata", "blocked", str(error), subject=f"apis[{index}]"))
                continue
            servers = resource.get("authorization_servers") if isinstance(resource, dict) else None
            if status != 200 or not isinstance(servers, list) or issuer not in servers:
                rows.append(finding("6-mcp-metadata", "fail", f"Protected resource metadata (HTTP {status}) does not "
                                    f"list auth.issuer in authorization_servers", subject=f"apis[{index}]"))
    return result


async def run_check(target: str, *, manifest_path: Path | None, output: Path, transport=None, resolver=None) -> int:
    """Discovery always; the probes when a manifest is given. Exit 0 no fail finding, 1 fail findings,
    2 setup error, 3 blocked (an unsupported major version, a refused host, a sign-in needing a person).
    The report's path goes to stdout, a one-line outcome to stderr."""
    run = StaticRun(Path(output), SUITE, INTERFACE)
    code, detail, findings, ran, stage = 0, None, [], [], "manifest"
    document = metadata = manifest = None
    nonce_required, exchanges = False, 0
    try:
        run.event("specification", {"spec": SPEC, "operations": OPERATIONS_SPEC, "commit": SPEC_COMMIT,
                                    "sources": SPEC_SOURCES, "muse": MUSE_EVIDENCE, "label": LABEL})
        manifest = load_manifest(manifest_path) if manifest_path else None
        origin = origin_of(target)
        # On the network the real resolver vets the hosts documents name; an injected transport (a test) brings
        # its own resolver or none.
        egress = Egress(origin, resolver=resolver if resolver is not None or transport is not None else resolve)
        for request in [*(item for item in ((manifest.requests.sign_in_required, manifest.requests.insufficient_scope)
                                            if manifest else ()) if item),
                        *((manifest.operations.propose,) if manifest and manifest.operations else ()),
                        *((manifest.operations.effects,) if manifest and manifest.operations and
                          manifest.operations.effects else ())]:
            egress.allow(request.url, declared=True)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=20, transport=transport,
                                     headers={"User-Agent": "consumer-agent-testing-tool poppy-check"}) as client:
            http = Http(client, run, egress)
            stage = "discovery"
            found = await discovery(http, origin, egress)
            findings += found["findings"]
            document, metadata = found["document"], found["metadata"]
            # A document that does not speak for its claimed domain (3.1) or whose issuer is not bound to that
            # domain (3.2) could steer the probes at another company: neither is used.
            unusable = [row for row in findings if row.rule in ("3.1-organization", "3.2-metadata")
                        and row.severity == "fail" or row.rule == "3.1-protocol-version" and row.severity == "blocked"]
            if manifest and document is not None and metadata is not None and not unusable:
                stage = "probes"
                probed = await probe(http, manifest, document, metadata, egress)
                findings += probed.findings
                ran = probed.ran
            elif manifest and unusable:
                findings.append(finding(unusable[0].rule, "info", "Probes not run: a personal agent must not use "
                                        f"this poppy.json ({unusable[0].evidence})", subject="probes"))
            elif manifest:
                findings.append(finding("4.2-start-session", "blocked", "Discovery did not yield poppy.json and its "
                                        "issuer metadata, so no probe ran", subject="probes"))
            nonce_required, exchanges = http.nonce_required, http.exchanges
        if nonce_required:
            findings.append(finding("4.3-nonce", "info", "The company required DPoP nonces; the probes supplied them",
                                    subject="DPoP"))
        stage = None
        code = 1 if any(row.severity == "fail" for row in findings) else 3 if any(
            row.severity == "blocked" for row in findings) else 0
    except Exception as error:  # noqa: BLE001 — a setup failure is still reported and sealed
        failure = normalize_error(error)
        code = 3 if failure.code == 3 else 2
        detail = run.redactor.text(str(failure))
        run.event("setup_error", {"stage": stage, "class": failure.category, "detail": detail})
    counts = summary(findings)
    counts["blocked"] = sum(row.severity == "blocked" for row in findings)
    status = {0: "completed", 1: "failed", 2: "failed", 3: "blocked"}[code]
    if code == 1:
        detail = f"{counts['fail']} finding(s) at severity fail against draft {DRAFT}"
    elif code == 3 and detail is None:
        detail = next(row.evidence for row in findings if row.severity == "blocked")
    run.event("run_finished", {"status": status, "exit_code": code, "counts": counts})
    report = {
        "lane": "readiness", "evidence_label": LABEL, "schema_version": 1, "kind": "poppy-check", "suite": SUITE,
        "fidelity": "a personal agent's discovery" + (" and session probes" if manifest else "") +
                    f" against Personal Agent Protocol draft {DRAFT}",
        "label": LABEL, "status": status, "detail": detail, "exit_code": code,
        "failure_class": None if code == 0 else {1: "poppy", 2: "setup/infrastructure", 3: "unsupported"}[code],
        "failure_stage": stage if code else None,
        "execution_status": "failed" if code == 2 else "blocked" if code == 3 else "completed",
        "compatibility_pass": False,
        "specification": {"draft": DRAFT, "spec": SPEC, "operations": OPERATIONS_SPEC, "commit": SPEC_COMMIT,
                          "sources": SPEC_SOURCES, "retrieved": "2026-10-16"},
        "muse": MUSE_EVIDENCE,
        "target": {"origin": target, "probes": manifest is not None,
                   "identity": manifest.agent.identity if manifest else None,
                   "manifest": {"path": str(manifest_path), "sha256": digest(manifest.model_dump(mode="json"))}
                   if manifest else None},
        "company": {"poppy_json": document, "authorization_server_metadata": metadata},
        "probes_run": ran, "http_exchanges": exchanges, "findings": [row.model_dump() for row in findings],
        "summary": counts, "checks": rule_checks(findings, ran) if code != 2 else [],
        "run_lock": {"specification_commit": SPEC_COMMIT, "rules_sha256": digest(RULES),
                     "poppy_json_sha256": digest(document) if document is not None else None},
        "evidence_limits": [
            "Draft 0.1 with no production Muse evidence: Muse appeared in a Sierra demo only.",
            ("Discovery reads documents; probes act as one personal agent and see only the company's answers. "
             "The browser join form is posted by the checker's HTTP client, not by a browser navigation."),
            ("A loopback identity is an http://127.0.0.1 client_id, which production companies must refuse; it "
             "only fits a loopback test deployment that opts in."),
            ("Confirms assert approved_by for a test account; no person approved the operation. The token-in-URL "
             "probe sends a signed-out test Session Token in a query string on purpose, which a personal agent "
             "never does."),
            ("Not covered: Direct and Device Sign-In (a person signs in), conversations (section 7), MCP sessions "
             "with Bearer Session Tokens, payments and push (open topics).")],
    }
    result = run.finish(report, locations={"inference": "none", "browser": "none", "target": target,
                                           "telemetry": "disabled", "egress": "the company's declared hosts only"})
    error = "setup" if code == 2 else None
    if result["reporting_status"] != "completed":
        code, error = 2, error or "reporting"
    print(run.path)
    failed = [row for row in findings if row.severity == "fail"]
    failing = [f"{row.rule} ({row.subject})" if row.subject else row.rule for row in failed]
    # The first failing finding's evidence leads ("poppy.json answered HTTP 404"), then each rule and subject.
    print(outcome_line(SUITE, result["status"], counts, LABEL, failing, detail if code in (2, 3) else None,
                       run.path, error=error, first=failed[0].evidence if failed else None), file=sys.stderr)
    return code


async def probe(http: Http, manifest: ProbeManifest, document: dict, metadata: dict, egress: Egress) -> "Probes":
    """Start the identity, then run the probe groups in order; each group's findings stand on their own."""
    declared = manifest.agent
    extensions = {"operations": {"version": "1"}} if manifest.operations else {}
    user_id = manifest.user_id or f"usr_{new_jti()}"  # opaque and per run: never derived from personal data
    issuer, token_endpoint = field(document, "auth", "issuer"), metadata.get("token_endpoint")  # equal after 3.2
    if not isinstance(token_endpoint, str):
        raise LabError("The issuer metadata names no token_endpoint, so no Session can start")
    if declared.identity == "loopback":
        if not egress.loopback:
            raise Unsupported("A loopback probe identity is reachable from a loopback test deployment only; declare "
                              "agent.identity: hosted with a client metadata document you publish over HTTPS")
        identity = LoopbackIdentity(Key("catt-probe"), extensions=extensions)
    else:
        if not (declared.client_id and declared.private_key_env and declared.key_id):
            raise LabError("A hosted identity needs agent.client_id, agent.private_key_env and agent.key_id")
        text = os.environ.get(declared.private_key_env)
        if not text:
            raise LabError(f"Set the probe identity's private key in {declared.private_key_env}")
        http.secret(text)
        identity = None
    if identity is not None:
        async with identity:
            problems = validate_client_metadata(identity.document("/agent.json"), fetched_url=identity.client_id,
                                                loopback_ok=True)
            if problems:  # This checker's own document must be valid before any company sees it.
                raise LabError(f"The probe client metadata is invalid: {problems[0].evidence}")
            agent = Agent(http, client_id=identity.client_id, key=identity.key, issuer=issuer,
                          token_endpoint=token_endpoint, user_id=user_id)
            return await run_probes(http, agent, document, metadata, identity, manifest, egress)
    key = Key.load(text, declared.key_id)
    egress.allow(declared.client_id, declared=True)
    status, client_metadata, _, _ = await fetch_json(http, "own-client-metadata", declared.client_id, follow=False,
                                                     loopback_ok=False)
    problems = validate_client_metadata(client_metadata, fetched_url=declared.client_id)
    jwks_uri = client_metadata.get("jwks_uri") if isinstance(client_metadata, dict) else None
    if status != 200 or problems or not isinstance(jwks_uri, str):
        raise LabError(f"The hosted client metadata at {declared.client_id} is not usable: "
                       f"{problems[0].evidence if problems else f'HTTP {status}'}")
    _, jwks, _, _ = await fetch_json(http, "own-jwks", jwks_uri, follow=False, loopback_ok=False)
    if private_members(jwks) or key.public not in [{name: item.get(name) for name in ("kty", "crv", "x", "y")}
                                                   for item in (jwks or {}).get("keys", []) if isinstance(item, dict)]:
        raise LabError("The hosted key set must hold only public keys, including the public half of "
                       f"{declared.private_key_env}")
    agent = Agent(http, client_id=declared.client_id, key=key, issuer=issuer, token_endpoint=token_endpoint,
                  user_id=user_id)
    return await run_probes(http, agent, document, metadata, None, manifest, egress)


async def run_probes(http, agent, document, metadata, identity, manifest, egress) -> Probes:
    probes = Probes(http, agent, document, metadata, identity, manifest)
    try:
        main = await probes.token_endpoint()
        if main:
            await probes.token_use(main)
            await probes.browser(main)
            signed_in = await probes.account()
            await probes.operations(signed_in)
            await probes.sign_out(signed_in)
    except Unsupported as error:  # a host outside the declared ones: the rest of the probes are blocked
        probes.add("4.2-start-session", "blocked", str(error), "probes")
    except Exception:
        await probes.release()
        raise
    await probes.release()
    return probes


def rule_checks(findings, ran) -> list[dict]:
    """One row per finding; a rule with no finding is "no-finding" if a probe or document exercised it,
    never "passed"."""
    rows = []
    for rule, (section, _quote) in RULES.items():
        hits = [row for row in findings if row.rule == rule]
        exercised = any(item == rule or item.startswith(f"{rule}:") for item in ran)
        if not hits:
            rows.append({"id": rule, "layer": "poppy", "status": "no-finding", "required": False,
                         "detail": "no finding (exercised)" if exercised else "no finding; not an admission",
                         "basis": f"draft {DRAFT} §{section}"})
        for row in hits:
            rows.append({"id": f"{rule}/{row.subject}" if row.subject else rule, "layer": "poppy",
                         "status": {"fail": "failed", "warn": "warning", "info": "info", "blocked": "blocked"}[
                             row.severity], "required": row.severity in ("fail", "blocked"),
                         "detail": row.evidence, "basis": row.section})
    return rows


async def check_from_cli(args) -> int:
    """check URL --suite poppy [--manifest probes.yaml]."""
    stray = [flag for flag, value in (("--endpoint", args.endpoint), ("--auth-from", args.auth_from),
                                      ("--auth-env", args.auth_env), ("--auth-placement", args.auth_placement),
                                      ("--auth-name", args.auth_name), ("--client-profile", args.client_profile),
                                      *ignored_options(args)) if value]
    if stray:
        raise LabError(f"{', '.join(stray)} does not apply to --suite poppy, which runs here; probe settings go "
                       "in --manifest")
    if args.offline:
        raise Unsupported("--offline does not apply to --suite poppy, which connects to the company")
    root = Path.cwd().resolve()
    manifest = workspace_path(root, args.manifest, "--manifest") if args.manifest else None
    return await run_check(args.target, manifest_path=manifest, output=workspace_path(root, args.output, "--output"))
