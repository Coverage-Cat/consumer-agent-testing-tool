"""connector-v1 scenarios: a developer's MCP connector, a scripted user and independent checks.

The scenario names what the host owns (credentials by environment name, private user data,
approval rules, outcome checks) and what the model may see (goal, standing profile, the
connectors' own catalogs). Validation is strict and fails closed: no executable YAML, no
credentials in the file, references only into /private and captured values, and plain HTTP
only on loopback. Types mirror the P1 contract for permissions.py, linking.py and answer.py;
the runner converts them into those modules' own models.

Several connectors (P2 routes) each keep their own auth, tools and exposure (in-prompt,
search-only, or disconnected until an extension connects it); scenario-level tool references
then name `<connector>/<tool>`, and tool checks name their connector. A scenario may declare no
connectors when an enabled extension provides the agent's service itself (custom_connector's HTTP
API, browser's websites); its checks are then oracle checks.
"""
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    ValidationInfo,
    model_validator,
)

from .errors import LabError
from .evidence import MARKERS, MIN_SECRET_LENGTH, admissible_secret, credential_key, private_key
from .serialization import digest, load

ToolClass = Literal["read", "write", "sensitive-write"]
ClaimState = Literal["confirmed", "pending", "quoted", "failed", "cancelled", "submitted", "paid"]
DecisionKind = Literal["allow_once", "allow_task", "allow_site", "always_allow", "deny", "edit", "no_response"]
CLIENT_PROFILES = ("generic-sdk", "muse-template-unofficial-2026-10")
MUSE_TEMPLATE = "muse-template-unofficial-2026-10"
IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
# Connector names double as CLI command names in the muse-cli projection.
CONNECTOR_NAME = r"^[a-z][a-z0-9-]{0,39}$"
# Extension names: scenario keys, and (hyphens as underscores) consumer_agent_testing_tool.ext_<name> modules.
EXTENSION_NAME = r"^[a-z][a-z0-9_-]{0,39}$"
EXPOSURES = ("in-prompt", "search-only")
RESERVED_CONNECTORS = frozenset({"cat", "exec", "help", "ask_user", "tool_search", "host"})
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*")
REF_ROOTS = ("private", "captured")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
# `scaffold` writes this marker where only the developer can supply the answer.
SCAFFOLD_MARKER = "TODO(scaffold)"
CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([A-Za-z0-9._-]*))?\}")
VARIABLE_VALUE = re.compile(r"[A-Za-z0-9._-]{1,253}")
PREDICATE_OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "in", "exists")
# Approval-card fields a scripted rule compares as strings (permissions.CARD_FIELDS); origin is who asked.
CARD_FIELDS = ("tool", "tool_class", "connector", "origin")
ORIGIN = re.compile(r"model|extension:[a-z][a-z0-9_.:-]{0,63}")
DOC_LIMIT = 64 * 1024


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


def _pointer(value: str, what: str) -> str:
    if not isinstance(value, str) or not POINTER.fullmatch(value):
        raise ValueError(f"{what} must be a JSON pointer such as /structuredContent/total")
    return value


def _env(value: str | None, what: str):
    if value is not None and not ENV_NAME.fullmatch(value):
        raise ValueError(f"{what} must name an environment variable (uppercase), never hold a credential")


def is_loopback(url: str) -> bool:
    return urlsplit(url).hostname in LOOPBACK


def check_url(url: str, what: str, *, origin=False, port_443=False, query=False) -> str:
    """Absolute HTTP(S); plain HTTP only on loopback; never user info or fragments."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ValueError(f"{what} is not a valid URL ({error})") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{what} must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError(f"{what} must not embed credentials; use auth with an environment variable")
    if parts.fragment or (parts.query and not query):
        raise ValueError(f"{what} must not contain a query or fragment")
    if any(credential_key(name) for name, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError(f"{what} must not carry credentials in its query")
    loopback = parts.hostname in LOOPBACK
    if parts.scheme == "http" and not loopback:
        raise ValueError(f"{what} must use https; plain http is allowed only for loopback fixtures")
    if port_443 and not loopback and (port or 443) != 443:
        # Unofficial template: "refuses an endpoint with a query, a fragment, user info or a port other than 443".
        raise ValueError(f"{what} must use port 443 under the opt-in unofficial {MUSE_TEMPLATE} client profile "
                         "(a limit of that profile, not of Muse's guidelines); use client_profile: generic-sdk "
                         "(or --client-profile generic-sdk) for this endpoint")
    if origin and parts.path not in ("", "/"):
        raise ValueError(f"{what} must be an exact origin (scheme, host and port only)")
    return url


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# Authentication: environment names only (linking.py TargetAuth mirror).
class NoAuthSpec(Strict):
    mode: Literal["none"] = "none"


class ApiKeyAuthSpec(Strict):
    mode: Literal["api_key"]
    env: str
    placement: Literal["bearer_header", "custom_header", "query_param", "url_path_segment"] = "bearer_header"
    name: str | None = None

    @model_validator(mode="after")
    def shape(self):
        _env(self.env, "auth.env")
        if self.placement in ("custom_header", "query_param"):
            if not self.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.name):
                raise ValueError(f"auth.name (a header or parameter name) is required for {self.placement}")
        elif self.name is not None:
            raise ValueError(f"auth.name does not apply to {self.placement}")
        return self


class OAuthAuthSpec(Strict):
    mode: Literal["oauth"]
    registration: Literal["dcr", "cimd", "preregistered"] = "dcr"
    client_id_env: str | None = None
    client_secret_env: str | None = None
    client_metadata_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    read_only_scopes: list[str] = Field(default_factory=list)
    expected_subject: str | None = None
    consent: Literal["fixture-auto", "interactive"] = "interactive"
    test_login_env: str | None = None
    redirect_port: int | None = Field(default=None, ge=1024, le=65535)

    @model_validator(mode="after")
    def shape(self):
        for name in ("client_id_env", "client_secret_env", "test_login_env"):
            _env(getattr(self, name), f"auth.{name}")
        preregistered = self.registration == "preregistered"
        if preregistered != (self.client_id_env is not None) or (self.client_secret_env and not preregistered):
            raise ValueError("auth.client_id_env (and client_secret_env) are for preregistered clients only")
        if (self.registration == "cimd") != (self.client_metadata_url is not None):
            raise ValueError("auth.client_metadata_url is required for cimd registration and used by nothing else")
        if self.client_metadata_url is not None:
            parts = urlsplit(self.client_metadata_url)
            if parts.scheme != "https" or parts.path in ("", "/") or parts.fragment or "@" in parts.netloc:
                raise ValueError("auth.client_metadata_url must be an HTTPS URL with a path (a CIMD client id)")
        if (self.consent == "fixture-auto") != (self.test_login_env is not None):
            raise ValueError("auth.test_login_env is required for fixture-auto consent and used by nothing else")
        for name in ("scopes", "read_only_scopes"):
            values = getattr(self, name)
            if len(set(values)) != len(values) or any(
                    not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", scope) for scope in values):
                raise ValueError(f"auth.{name} must be distinct RFC 6749 scope tokens")
        if self.expected_subject is not None and not self.expected_subject.strip():
            raise ValueError("auth.expected_subject must not be empty")
        return self


AuthSpec = Annotated[NoAuthSpec | ApiKeyAuthSpec | OAuthAuthSpec, Field(discriminator="mode")]


class Match(Strict):
    """Which accepted result a pick reads: the latest whose `result` pointer equals the call's `argument`
    (the quote the booking names), never merely the latest result of that tool."""
    result: str = Field(min_length=1)
    argument: str = Field(min_length=1)

    @model_validator(mode="after")
    def shape(self):
        _pointer(self.result, "match.result")
        _pointer(self.argument, "match.argument")
        return self


class Pick(Strict):
    """A value read from an accepted result of a tool (joined to the call by match), or from the call's
    own arguments (from: arguments)."""
    source: str = Field(alias="from", min_length=1)
    pointer: str
    match: Match | None = None

    @model_validator(mode="after")
    def shape(self):
        _pointer(self.pointer, "pointer")
        if self.match is not None and self.source == "arguments":
            raise ValueError("match joins another tool's result to the call; a pick from arguments needs none")
        return self


class Fixed(Strict):
    value: Any


def _disclosure_kind(value):
    if isinstance(value, dict):
        return "fixed" if "value" in value else "pick" if "from" in value or "source" in value else None
    return "pick" if isinstance(value, Pick) else "fixed" if isinstance(value, Fixed) else None


# Tagged, so a mistake reads "disclosure.x.total.pick.pointer", not pydantic's union internals.
Disclosed = Annotated[Annotated[Pick, Tag("pick")] | Annotated[Fixed, Tag("fixed")], Discriminator(
    _disclosure_kind, custom_error_type="disclosure_value",
    custom_error_message="a disclosure value is {from: <tool or arguments>, pointer: ..., match: {...}} or "
                         "{value: ...}")]


class Expect(Strict):
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"]
    value: Any

    @model_validator(mode="after")
    def typed(self):
        _operand(self.op, self.value, "expect")
        return self


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _operand(op, value, what):
    if op == "in" and (not isinstance(value, list) or not value):
        raise ValueError(f"{what}: 'in' needs a nonempty list")
    if op in ("lt", "lte", "gt", "gte") and not _number(value):
        raise ValueError(f"{what}: ordered comparisons need a number")
    if op == "exists" and not isinstance(value, bool):
        raise ValueError(f"{what}: 'exists' takes true or false")


class OracleSpec(Strict):
    url: str
    pointer: str = ""

    @model_validator(mode="after")
    def shape(self):
        check_url(self.url, "oracle.url", query=True)
        _pointer(self.pointer, "oracle.pointer")
        return self


class ToolReadSpec(Strict):
    """A host-side call of one of the connector's read tools (never shown to the model)."""
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    pointer: str = ""

    @model_validator(mode="after")
    def shape(self):
        _pointer(self.pointer, "tool.pointer")
        return self


class Observation(Strict):
    source: Literal["oracle", "tool"]
    oracle: OracleSpec | None = None
    tool: ToolReadSpec | None = None
    connector: str | None = None
    expect: Expect

    @model_validator(mode="after")
    def one_source(self):
        if (self.source == "oracle") != (self.oracle is not None) \
                or (self.source == "tool") != (self.tool is not None):
            raise ValueError("source oracle needs an oracle block and source tool needs a tool block, not both")
        if self.source == "oracle" and self.connector is not None:
            raise ValueError("connector applies only to source: tool")
        if (self.oracle or self.tool).pointer == "":
            # Vacuous or brittle: any reachable service has a whole document that "exists" or differs
            # from a literal (ne); an outcome check names the field that shows the outcome.
            raise ValueError("A check on the whole document holds for any reachable service; point at the "
                             "field that shows the outcome (for example pointer: /confirmed_count)")
        return self


class CheckSpec(Observation):
    id: str = Field(pattern=IDENTIFIER)
    required: bool = True


class ToolsSpec(Strict):
    allow: list[str] | None = None
    classes: dict[str, ToolClass] = Field(default_factory=dict)
    host_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    capture: dict[str, Pick] = Field(default_factory=dict)
    disclosure: dict[str, dict[str, Disclosed]] = Field(default_factory=dict)
    reconcile: dict[str, Observation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def shape(self):
        for name, pick in self.capture.items():
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"capture name {name!r} must be an identifier")
            if pick.source == "arguments":
                raise ValueError(f"capture.{name} reads a tool result: from names a tool")
            if pick.match is not None:
                raise ValueError(f"capture.{name}: match applies to disclosure picks only")
        if self.allow is not None and len(set(self.allow)) != len(self.allow):
            raise ValueError("tools.allow lists a tool twice")
        named = {*self.classes, *self.host_arguments, *self.disclosure, *self.reconcile,
                 *(pick.source for pick in self.capture.values())}
        if self.allow is not None and (outside := sorted(named - set(self.allow))):
            raise ValueError(f"tools configuration names tools outside tools.allow: {', '.join(outside)}")
        for tool, overlay in self.host_arguments.items():
            for key in overlay:
                if not key or "/" in key:
                    raise ValueError(f"host_arguments.{tool}: keys are top-level property names")
        for tool, keys in self.disclosure.items():
            for key, item in keys.items():
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                    raise ValueError(f"disclosure.{tool}: key {key!r} must be a lowercase identifier")
                if self.classes.get(tool) in ("write", "sensitive-write") and isinstance(item, Pick) \
                        and item.source != "arguments" and item.match is None:
                    # Fail closed: the latest result need not be the one this call books (a cheaper quote).
                    kind = "sensitive write" if self.classes[tool] == "sensitive-write" else "write"
                    raise ValueError(f"disclosure.{tool}.{key} reads {item.source} for a {kind}: add "
                                     f"match: {{result: <pointer to the id in the {item.source} result>, "
                                     f"argument: <pointer to the {tool} argument that names it>}}")
        return self


class ConnectorLimits(Strict):
    # Calls of this connector's tools per trial, the model's and run.call_tool's together (P1.5 quota; no
    # documented Muse value, assumed); the scenario's limits.tool_calls still applies.
    tool_calls: int = Field(ge=1, le=1000)


class ConnectorSpec(Strict):
    name: str = Field(pattern=CONNECTOR_NAME)
    endpoint: str
    allowed_origins: list[str] = Field(min_length=1)
    auth: AuthSpec = Field(default_factory=NoAuthSpec)
    # The unofficial Muse template behavior is opt-in, never the default (review section 2, source labels).
    client_profile: Literal["generic-sdk", "muse-template-unofficial-2026-10"] = "generic-sdk"
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    doc: str | None = None
    # None: the scenario's exposure. disconnected: not linked at setup and offers nothing until an
    # extension connects it (run.connect), like a connector the user has not connected yet.
    exposure: Literal["in-prompt", "search-only", "disconnected"] | None = None
    limits: ConnectorLimits | None = None
    # The client's output-schema treatment (mcp_clients.OUTPUT_SCHEMA): None is lenient (keep and note a
    # structuredContent that fails the tool's outputSchema); strict refuses it, as the official SDK does.
    output_schema: Literal["lenient", "strict"] | None = None

    @model_validator(mode="after")
    def endpoints(self):
        if self.name in RESERVED_CONNECTORS:
            raise ValueError(f"Connector name {self.name!r} is reserved by the host")
        check_url(self.endpoint, f"connectors.{self.name}.endpoint", port_443=self.client_profile == MUSE_TEMPLATE)
        for origin in self.allowed_origins:
            check_url(origin, f"connectors.{self.name}.allowed_origins", origin=True)
        if origin_of(self.endpoint) not in {origin_of(item) for item in self.allowed_origins}:
            raise ValueError(f"connectors.{self.name}.endpoint is outside its allowed_origins")
        if self.auth.mode == "api_key" and self.auth.placement in ("query_param", "url_path_segment") \
                and self.client_profile == MUSE_TEMPLATE:
            # Unofficial template: credentials travel in a header only (mcp_clients.TEMPLATE_BEHAVIORS).
            raise ValueError(f"The {MUSE_TEMPLATE} client profile sends credentials in a header only; "
                             f"use client_profile: generic-sdk for {self.auth.placement} API keys")
        if self.doc is not None:
            path = Path(self.doc)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"connectors.{self.name}.doc must be a relative path inside the scenario directory")
        return self


class ProfileSpec(Strict):
    memory: list[str] = Field(default_factory=list, max_length=100)
    identity: dict[str, str] = Field(default_factory=dict)


class Reply(Strict):
    id: str = Field(pattern=IDENTIFIER)
    match: list[str] = Field(min_length=1)
    reply: str = Field(min_length=1)

    @model_validator(mode="after")
    def patterns(self):
        for pattern in self.match:
            _regex(pattern, f"user.replies.{self.id}.match")
        return self


def _regex(pattern, what):
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
        raise ValueError(f"{what}: patterns are nonempty strings of at most 500 characters")
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"{what}: invalid regular expression ({error})") from None


class UserSpec(Strict):
    replies: list[Reply] = Field(default_factory=list)
    fallback: str = Field(default="I don't know; use your best judgement.", min_length=1)

    @model_validator(mode="after")
    def unique(self):
        if len({reply.id for reply in self.replies}) != len(self.replies):
            raise ValueError("user.replies ids must be unique")
        return self


def validate_predicate(predicate, what="when", depth=1):
    """The scripted user's rule grammar (permissions.py DecisionRule): type-strict, like scenario.py."""
    if depth > 8:
        raise ValueError(f"{what}: predicate nesting exceeds eight levels")
    if not isinstance(predicate, dict):
        raise ValueError(f"{what}: a predicate is an object")  # noqa: TRY004 — pydantic reports ValueError
    if set(predicate) in ({"all"}, {"any"}):
        children = next(iter(predicate.values()))
        if not isinstance(children, list) or not children:
            raise ValueError(f"{what}: all/any need a nonempty list")
        for child in children:
            validate_predicate(child, what, depth + 1)
        return
    if set(predicate) != {"field", "op", "value"}:
        raise ValueError(f"{what}: use exactly field, op and value, or all/any")
    name, op, value = predicate["field"], predicate["op"], predicate["value"]
    if not isinstance(name, str) or not (name in CARD_FIELDS or re.fullmatch(r"(?:arguments|disclosure)/.+", name)):
        raise ValueError(f"{what}: field must be tool, tool_class, connector, origin, arguments/<pointer> "
                         "or disclosure/<key>")
    if op not in PREDICATE_OPS:
        raise ValueError(f"{what}: op must be one of {', '.join(PREDICATE_OPS)}")
    _operand(op, value, what)
    if op == "exists":
        return
    values = value if op == "in" else [value]
    kinds = {"null" if item is None else "boolean" if isinstance(item, bool) else "number" if _number(item)
             else "string" if isinstance(item, str) else "other" for item in values}
    if len(kinds) != 1 or "other" in kinds or any(isinstance(item, float) and not math.isfinite(item)
                                                  for item in values):
        # The engine compares type-strictly, so a value is one JSON scalar type (bool is not a number).
        raise ValueError(f"{what}: values are JSON scalars of one type")
    if name in CARD_FIELDS:
        if op in ("lt", "lte", "gt", "gte") or kinds != {"string"}:
            raise ValueError(f"{what}: {name} compares strings with eq, ne or in")
        if name == "tool_class" and any(item not in ("read", "write", "sensitive-write") for item in values):
            raise ValueError(f"{what}: tool_class is read, write or sensitive-write")
        if name == "origin" and any(not ORIGIN.fullmatch(item) for item in values):
            # Who asked for the call: the model, or an extension acting for the user (permissions.ORIGIN).
            raise ValueError(f"{what}: origin is model or extension:<source>, such as extension:wallet")


class RuleSpec(Strict):
    id: str = Field(pattern=IDENTIFIER)
    when: dict[str, Any]
    decision: DecisionKind
    edits: dict[str, Any] | None = None

    @model_validator(mode="after")
    def shape(self):
        from .permissions import WEBSITE, website_only
        validate_predicate(self.when, f"permissions.rules.{self.id}.when")
        if (self.decision == "edit") != (self.edits is not None):
            raise ValueError(f"permissions.rules.{self.id}: edits are required for, and only for, decision edit")
        if self.decision == "allow_site" and not website_only(self.when):
            # Allow for this site is a website scope (Help Center): connector cards keep refusing it.
            raise ValueError(f"permissions.rules.{self.id}: allow_site (Allow for this site) answers browser task "
                             f"cards only, a website scope connector cards never offer; add {{field: connector, op: "
                             f"eq, value: {WEBSITE}}} to its when")
        for pointer in self.edits or {}:
            if not pointer:
                raise ValueError(f"permissions.rules.{self.id}.edits: keys are JSON pointers into the arguments")
            _pointer(pointer, f"permissions.rules.{self.id}.edits")
        return self


class PermissionsSpec(Strict):
    mode: Literal["platform-default", "ask-for-some", "always-ask"] = "platform-default"
    important_reads: list[str] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    default: Literal["deny", "no_response"] = "deny"
    persisted_always: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("permissions.rules ids must be unique")
        return self


class ClaimRuleSpec(Strict):
    state: ClaimState
    true_when: str


class RelayFactSpec(Strict):
    id: str = Field(pattern=IDENTIFIER)
    patterns: list[str] = Field(default_factory=list)
    value_from: str | None = None
    unit: Literal["minor"] | None = None   # value_from is an integer amount in minor units

    @model_validator(mode="after")
    def shape(self):
        if not self.patterns and not self.value_from:
            raise ValueError(f"answer.relay.{self.id} needs patterns or value_from")
        for pattern in self.patterns:
            _regex(pattern, f"answer.relay.{self.id}.patterns")
        if self.value_from is not None and not re.fullmatch(r"checks/[^/]+/.+|captured/[^/]+(?:/.*)?",
                                                            self.value_from):
            raise ValueError(f"answer.relay.{self.id}.value_from is checks/<id>/<field> or captured/<name>")
        return self


class AnswerSpecModel(Strict):
    claims: list[ClaimRuleSpec] = Field(default_factory=list)
    relay: list[RelayFactSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len({(claim.state, claim.true_when) for claim in self.claims}) != len(self.claims):
            raise ValueError("answer.claims rules must be unique")
        return self


class ExpectedSpec(Strict):
    tools: list[str] = Field(default_factory=list)


class Limits(Strict):
    model_turns: int = Field(default=30, ge=1, le=500)
    tool_calls: int = Field(default=40, ge=1, le=1000)
    wall_seconds: int = Field(default=600, ge=1, le=7200)
    # Model-visible tool result bound (characters); no documented Muse value (assumed).
    result_characters: int = Field(default=16384, ge=1024, le=262144)


class ConnectorScenario(Strict):
    version: Literal[1]
    kind: Literal["connector"]
    id: str = Field(pattern=IDENTIFIER)
    goal: str = Field(min_length=1, max_length=8000)
    # None only when an enabled extension provides the agent's service (Factory.provides_surface).
    connectors: list[ConnectorSpec] = Field(max_length=8)
    projection: Literal["native", "muse-cli"] = "native"
    exposure: Literal["in-prompt", "search-only"] = "in-prompt"
    profile: ProfileSpec = Field(default_factory=ProfileSpec)
    private: dict[str, Any] = Field(default_factory=dict)
    user: UserSpec = Field(default_factory=UserSpec)
    permissions: PermissionsSpec = Field(default_factory=PermissionsSpec)
    checks: list[CheckSpec] = Field(default_factory=list)
    answer: AnswerSpecModel | None = None
    expected: ExpectedSpec = Field(default_factory=ExpectedSpec)
    limits: Limits = Field(default_factory=Limits)
    # One URL, or one per test service when several connectors each have their own (POSTed in order).
    reset: str | list[str] | None = None
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def contract(self, info: ValidationInfo):
        names = [connector.name for connector in self.connectors]
        if len(set(names)) != len(names):
            raise ValueError("connector names must be unique")
        if not names:
            self._no_connectors(frozenset((info.context or {}).get("extension_surface") or ()))
        for url in self.resets():
            check_url(url, "reset", query=True)
        if isinstance(self.reset, list) and (not self.reset or len(set(self.reset)) != len(self.reset)):
            raise ValueError("reset lists distinct URLs")
        # Names an enabled extension registers at run time (run.register_private): references may name them.
        provided = frozenset((info.context or {}).get("provided_private") or ())
        if clash := sorted(provided & set(self.private)):
            raise ValueError(f"private.{clash[0]} is registered at run time by an extension; do not declare it")
        private = set(self.private) | provided
        for key, value in self.private.items():
            if not re.fullmatch(IDENTIFIER, key):
                raise ValueError(f"private key {key!r} must be an identifier")
            for leaf in _leaves(value):
                # Text, or a number for an input the schema types as one: redaction hides both, numbers by
                # value (a service may echo one as a JSON number). A boolean or null is nothing to hide.
                if isinstance(leaf, bool) or not isinstance(leaf, (str, int, float)) \
                        or isinstance(leaf, float) and not math.isfinite(leaf):
                    raise ValueError(f"private.{key}: private values are text or numbers; {leaf!r} is neither, "
                                     "so evidence redaction could not hide it")
                if admissible_secret(leaf) is None:
                    raise ValueError(f"private.{key}: private text must be at least {MIN_SECRET_LENGTH} characters "
                                     "(digits, for a number) and not a status or configuration word, or redaction "
                                     "cannot hide it")
            if credential_key(key):
                raise ValueError(f"private.{key} looks like a credential; credentials come only from the "
                                 "environment through connector auth")
        captures = {name for connector in self.connectors for name in connector.tools.capture}
        if sum(len(connector.tools.capture) for connector in self.connectors) != len(captures):
            raise ValueError("capture names must be unique across connectors")
        if len(self.connectors) > 1:
            # Several connectors may list the same tool name (two hotels' create_booking): say whose.
            for what, entries in (("expected.tools", self.expected.tools),
                                  ("permissions.important_reads", self.permissions.important_reads),
                                  ("permissions.persisted_always", self.permissions.persisted_always)):
                for entry in entries:
                    connector, slash, tool = entry.partition("/")
                    if not (slash and tool and connector in names):
                        raise ValueError(f"{what}: with several connectors, name each tool as <connector>/<tool> "
                                         f"({entry!r})")
            for check in self.checks:
                if check.source == "tool" and check.connector is None:
                    raise ValueError(f"checks.{check.id}.connector: with several connectors, a tool check names "
                                     "the connector whose read tool it calls")
        for connector in self.connectors:
            for tool, overlay in connector.tools.host_arguments.items():
                for key, value in overlay.items():
                    if credential_key(key) and not _is_ref(value):
                        raise ValueError(f"host_arguments.{tool}.{key} looks like a credential; credentials "
                                         "come only from the environment through connector auth")
                    if private_key(key) and any(leaf not in (None, "") for leaf in _literals(value)):
                        # A literal is neither shown as private on cards nor registered for redaction.
                        raise ValueError(f"host_arguments.{tool}.{key} holds personal data as a literal; put "
                                         f"the value under private: and write {{$ref: /private/<name>}} here, so "
                                         "cards show it as private and evidence hides it")
                    _refs(value, f"host_arguments.{tool}.{key}", private, captures)
            for tool, keys in connector.tools.disclosure.items():
                for key, item in keys.items():
                    if isinstance(item, Pick) and item.source != "arguments" and connector.tools.allow is not None \
                            and item.source not in connector.tools.allow:
                        raise ValueError(f"disclosure.{tool}.{key} reads {item.source}, which is not in tools.allow")
            for tool, observation in connector.tools.reconcile.items():
                self._observation(observation, f"reconcile.{tool}", captures, private)
            declared_sensitive = {tool for tool, cls in connector.tools.classes.items() if cls == "sensitive-write"}
            if forbidden := sorted(declared_sensitive & set(self.scoped(self.permissions.persisted_always,
                                                                        connector.name))):
                # Guidelines 3.3: "Sensitive writes cannot be set to Always allow, including through
                # Settings or a saved permission."
                raise ValueError("permissions.persisted_always cannot include sensitive writes: "
                                 + ", ".join(forbidden))
        from .permissions import WEBSITE
        if (site := next((rule.id for rule in self.permissions.rules if rule.decision == "allow_site"), None)) \
                and WEBSITE in names:
            # A connector named like the website cards would share their connector name, and so the rule.
            raise ValueError(f"permissions.rules.{site}: allow_site answers {WEBSITE} task cards, and a connector "
                             f"named {WEBSITE} would share their name; rename the connector")
        ids = [check.id for check in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("check ids must be unique")
        for check in self.checks:
            self._observation(check, f"checks.{check.id}", captures, private)
        if self.answer:
            for claim in self.answer.claims:
                if claim.true_when not in ids:
                    raise ValueError(f"answer.claims: true_when {claim.true_when!r} is not a declared check")
            if len({fact.id for fact in self.answer.relay}) != len(self.answer.relay):
                raise ValueError("answer.relay ids must be unique")
            for fact in self.answer.relay:
                if fact.value_from:
                    root, name = fact.value_from.split("/")[:2]
                    if (root == "checks" and name not in ids) or (root == "captured" and name not in captures):
                        kind = "check" if root == "checks" else "capture"
                        raise ValueError(f"answer.relay.{fact.id}.value_from names an undeclared {kind}")
        for name, config in self.extensions.items():
            if not re.fullmatch(EXTENSION_NAME, name) or not isinstance(config, dict):
                raise ValueError(f"extensions.{name}: names are lowercase identifiers mapped to objects")
        self._private_not_visible()
        return self

    def _no_connectors(self, surface):
        """A scenario without connectors: an enabled extension gives the agent its service (custom_connector's
        HTTP API), so nothing may name a connector tool, and the CLI projection has nothing to run."""
        if not surface:
            raise ValueError("connectors: declare at least one connector, or enable an extension that provides the "
                             "agent's service itself (one whose factory sets provides_surface)")
        if self.projection == "muse-cli":
            raise ValueError("projection muse-cli runs connectors' command-line clients; a scenario without "
                             "connectors uses native")
        for what, entries in (("expected.tools", self.expected.tools),
                              ("permissions.important_reads", self.permissions.important_reads),
                              ("permissions.persisted_always", self.permissions.persisted_always)):
            if entries:
                raise ValueError(f"{what}: this scenario declares no connectors, so it names no connector tools")
        if tool := next((check.id for check in self.checks if check.source == "tool"), None):
            raise ValueError(f"checks.{tool}: a tool check calls a connector's read tool; this scenario declares no "
                             "connectors (use an oracle check)")

    def resets(self) -> list[str]:
        return [] if self.reset is None else [self.reset] if isinstance(self.reset, str) else list(self.reset)

    def scoped(self, entries, connector) -> list[str]:
        """Scenario-level tool references for one connector: every entry with one connector, else the
        tools of entries qualified `<connector>/<tool>`."""
        if len(self.connectors) == 1:
            return list(entries)
        return [entry.partition("/")[2] for entry in entries if entry.partition("/")[0] == connector]

    def _observation(self, observation, what, captures, private):
        if observation.source == "tool":
            if observation.connector is not None and observation.connector not in {c.name for c in self.connectors}:
                raise ValueError(f"{what}.connector names an undeclared connector")
            for key, value in observation.tool.arguments.items():
                if credential_key(key) and not _is_ref(value):
                    raise ValueError(f"{what}.tool.arguments.{key} looks like a credential; credentials come "
                                     "only from the environment through connector auth")
                _refs(value, f"{what}.tool.arguments.{key}", private, captures)

    def _private_not_visible(self):
        """Host-only values must not reach the model through scenario text."""
        visible = [self.goal, *self.profile.memory, *self.profile.identity.values(),
                   *(reply.reply for reply in self.user.replies), self.user.fallback]
        for key, value in self.private.items():
            for leaf in _leaves(value):
                text = leaf if isinstance(leaf, str) else str(leaf) if isinstance(leaf, (int, float)) else None
                if text and len(text) >= 4 and any(text in item for item in visible):
                    raise ValueError(f"private.{key} appears in model-visible scenario text (goal, profile or "
                                     "user replies); private values are never shown to the model")

    def connector(self, name=None) -> ConnectorSpec:
        if name is None:
            if not self.connectors:
                raise LabError("This scenario declares no connectors")
            return self.connectors[0]
        return next(item for item in self.connectors if item.name == name)


def _leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _is_ref(value):
    return isinstance(value, dict) and "$ref" in value


def _literals(value):
    """The leaves of a host argument that no $ref fills."""
    if _is_ref(value):
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _literals(item)
    elif isinstance(value, list):
        for item in value:
            yield from _literals(item)
    else:
        yield value


def _refs(value, what, private, captures, depth=0):
    """{"$ref": "/private/<key>..."} or {"$ref": "/captured/<name>..."}; anything else is literal JSON."""
    if depth > 32:
        raise ValueError(f"{what} nests too deeply")
    if isinstance(value, dict):
        if "$ref" in value:
            reference = value["$ref"]
            if set(value) != {"$ref"} or not isinstance(reference, str):
                raise ValueError(f"{what}: a $ref object holds exactly one string")
            _pointer(reference, f"{what}.$ref")
            parts = reference.split("/")
            if len(parts) < 3 or parts[1] not in REF_ROOTS:
                raise ValueError(f"{what}: $ref points only into /private/<key> or /captured/<name>")
            name = parts[2].replace("~1", "/").replace("~0", "~")
            if parts[1] == "private" and name not in private:
                raise ValueError(f"{what}: /private/{name} is not declared under private")
            if parts[1] == "captured" and name not in captures:
                raise ValueError(f"{what}: /captured/{name} is not a declared capture")
            return
        for key, item in value.items():
            _refs(item, f"{what}.{key}", private, captures, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refs(item, f"{what}[{index}]", private, captures, depth + 1)


@dataclass(frozen=True)
class LoadedScenario:
    scenario: ConnectorScenario
    sha256: str
    path: Path | None = None
    docs: dict[str, str] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)


def substitute(document, variables):
    """Fill ${NAME} (or ${NAME:-default}) in URL fields only, e.g. a fixture's bound port."""
    def fill(text, where):
        def value(match):
            name, default = match[1], match[2]
            result = variables.get(name, default)
            if result is None:
                raise LabError(f"{where} uses ${{{name}}}; pass --set {name}=<value>")
            if not VARIABLE_VALUE.fullmatch(result):
                raise LabError(f"--set {name}: values are limited to letters, digits, '.', '_' and '-'")
            return result
        return VARIABLE.sub(value, text)

    def walk(value, key, where):
        if isinstance(value, dict):
            return {name: walk(item, name, f"{where}.{name}") for name, item in value.items()}
        if isinstance(value, list):
            return [walk(item, key, f"{where}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, str) and key in ("endpoint", "allowed_origins", "url", "reset", "client_metadata_url"):
            return fill(value, where.lstrip("."))
        return value
    return walk(document, None, "")


def _strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def validation_message(error):
    """config.validation_message, with root-level problems labeled "(scenario)" instead of ": ..."."""
    return "; ".join(f"{'.'.join(map(str, item['loc'])) or '(scenario)'}: {item['msg']}"
                     for item in error.errors(include_input=False, include_context=False, include_url=False))


def _masked(value):
    if isinstance(value, dict):
        return {key: _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return "[private]"


def published_digest(scenario) -> str:
    """The scenario's evidence digest, private values masked: an unsalted digest over them would let an
    export test guesses of a low-entropy private value. Changing only a private value keeps it."""
    document = scenario.model_dump(mode="json", by_alias=True)
    document["private"] = _masked(document.get("private") or {})
    for connector in document.get("connectors") or []:
        for key in ("exposure", "limits", "output_schema"):
            if connector.get(key) is None:
                connector.pop(key, None)  # Unset (earlier scenarios): their digest stays what it was.
    return digest(document)


def parse_scenario(document, *, base: Path | None = None, variables=None, registry=None,
                   path: Path | None = None) -> LoadedScenario:
    if not isinstance(document, dict):
        raise LabError("A connector scenario is a mapping")
    if document.get("kind") != "connector":
        raise LabError("Not a connector-v1 scenario: kind must be connector")
    if any(SCAFFOLD_MARKER in text for text in _strings(document)):
        raise LabError(f"The scenario still contains {SCAFFOLD_MARKER} placeholders from scaffold; replace them "
                       "with the user's request and real checks before running")
    variables = dict(variables or {})
    for name in variables:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise LabError(f"--set {name}: variable names are uppercase identifiers")
    filled = substitute(document, variables)
    context = scenario_context(filled.get("extensions"), registry)
    try:
        scenario = ConnectorScenario.model_validate(filled, context=context)
    except ValidationError as error:
        raise LabError(f"Invalid connector scenario: {validation_message(error)}") from None
    validate_extensions(scenario, registry)
    sibling_models(scenario)
    docs = {}
    for connector in scenario.connectors:
        if connector.doc is None:
            continue
        if base is None:
            raise LabError(f"connectors.{connector.name}.doc needs a scenario file location")
        root = base.resolve()
        target = (root / connector.doc).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise LabError(f"connectors.{connector.name}.doc does not name a file inside the scenario directory")
        if target.stat().st_size > DOC_LIMIT:
            raise LabError(f"connectors.{connector.name}.doc exceeds {DOC_LIMIT} bytes")
        try:
            docs[connector.name] = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise LabError(f"connectors.{connector.name}.doc must be UTF-8 text") from None
    return LoadedScenario(scenario=scenario, sha256=published_digest(scenario), path=path, docs=docs,
                          variables=variables)


def sibling_models(scenario):
    """Validate blocks with the owning lane module's own model, so a scenario that loads also converts
    at run time: permissions.py, linking.py and answer.py own those formats, and the mirrors above
    only add scenario-path messages. The stricter of the two grammars applies."""
    from pydantic import TypeAdapter

    from .answer import AnswerSpec
    from .linking import TargetAuth
    from .permissions import PermissionPolicy
    blocks = (("permissions", lambda: PermissionPolicy.model_validate(scenario.permissions.model_dump(mode="json"))),
              ("linking", lambda: [TypeAdapter(TargetAuth).validate_python(connector.auth.model_dump(mode="json"))
                                   for connector in scenario.connectors]),
              ("answer", lambda: scenario.answer and AnswerSpec.model_validate(
                  scenario.answer.model_dump(mode="json"))))
    for name, validate in blocks:
        try:
            validate()
        except ValidationError as error:
            raise LabError(f"Invalid connector scenario ({name}): {validation_message(error)}") from None
        except LabError as error:
            raise LabError(f"Invalid connector scenario ({name}): {error}") from None


def _factory(name, registry):
    """The extension factory for `name`: registered, or discovered as consumer_agent_testing_tool.ext_<name>
    (connector_run.discover); an unknown name is a validation error naming both."""
    from .connector_run import EXTENSIONS, discover, extension_module
    registry = EXTENSIONS if registry is None else registry
    factory = discover(name, registry)
    if factory is None:
        searched = f"; no module {extension_module(name)}" if registry is EXTENSIONS else ""
        raise LabError(f"Unknown scenario extension {name!r}; registered: {', '.join(sorted(registry)) or 'none'}"
                       f"{searched}")
    return factory


def provided_private(extensions, registry=None) -> frozenset:
    """Private names the scenario's extensions register at run time (an optional factory.private_names),
    which host_arguments and tool-check arguments may reference as {$ref: /private/<name>}. Names the
    model check reports as malformed are left to it."""
    names = set()
    for name in extensions if isinstance(extensions, dict) else ():
        if not isinstance(name, str) or not re.fullmatch(EXTENSION_NAME, name):
            continue
        for item in getattr(_factory(name, registry), "private_names", None) or ():
            if not isinstance(item, str) or not re.fullmatch(IDENTIFIER, item):
                raise LabError(f"Extension {name} declares the private name {item!r}; private names are identifiers")
            names.add(item)
    return frozenset(names)


def provided_surface(extensions, registry=None) -> frozenset:
    """Enabled extensions whose factory sets provides_surface: each gives the agent a service of its own (an
    HTTP API), so the scenario may declare no connectors."""
    return frozenset(name for name in (extensions if isinstance(extensions, dict) else ())
                     if isinstance(name, str) and re.fullmatch(EXTENSION_NAME, name)
                     and getattr(_factory(name, registry), "provides_surface", False) is True)


def scenario_context(extensions, registry=None) -> dict:
    """What validation learns from the scenario's extensions: private names they register at run time and
    whether one provides the agent's service."""
    return {"provided_private": provided_private(extensions, registry),
            "extension_surface": provided_surface(extensions, registry)}


def validate_extensions(scenario, registry=None):
    for name, config in scenario.extensions.items():
        factory = _factory(name, registry)
        try:
            factory.config_model.model_validate(config)
        except ValidationError as error:
            raise LabError(f"Invalid extensions.{name}: {validation_message(error)}") from None


def load_scenario(path: Path, *, variables=None, registry=None) -> LoadedScenario:
    path = Path(path)
    try:
        document = load(path)
    except OSError as error:
        raise LabError(f"Cannot read connector scenario {path}: {error.strerror or error}") from None
    return parse_scenario(document, base=path.parent, variables=variables, registry=registry, path=path)


def comment(value, limit=240) -> str:
    """Server text fit for one YAML comment line: control characters (newlines too) become spaces, so a
    tool name or serverInfo field can never start a YAML line of its own."""
    return " ".join(CONTROL.sub(" ", str(value)).split())[:limit]


def _example_arguments(tool):
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    required = [name for name in required if isinstance(name, str)][:4]
    return ", ".join(f"{comment(name, 40)}: <value>" for name in required)


def scaffold_document(*, endpoint, name, server_info, tools, classifications, auth, client_profile,
                      fetched_at) -> str:
    """A connector-v1 skeleton from a live catalog; the developer completes every TODO(scaffold).

    Server text appears only in comments, sanitized (comment), and tool names with control characters
    are refused. Suggested classes stay comments, so every trial classifies the live catalog (a
    declared class would hide a later change and the reads rule trusts it). The outcome check's
    pointer and value and the answer's claim are TODOs, so no scaffold passes without real checks."""
    import yaml

    from .projection import functional_key

    def field(item, key, default=None):
        return (item or {}).get(key, default) if isinstance(item or {}, dict) else getattr(item, key, default)

    entries = [tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
    if odd := next((tool["name"] for tool in entries if CONTROL.search(tool["name"])), None):
        raise LabError(f"The server lists a tool name with control characters ({odd[:64]!r}); scaffold refuses it")
    names = [tool["name"] for tool in entries]
    document = {
        "version": 1, "kind": "connector", "id": re.sub(r"[^A-Za-z0-9_-]", "-", f"{name}-task")[:64],
        "goal": f"{SCAFFOLD_MARKER}: the user's request, in their own words.",
        "connectors": [{
            "name": name, "endpoint": endpoint, "allowed_origins": [origin_of(endpoint)], "auth": auth,
            "client_profile": client_profile, "tools": {"allow": names},
        }],
        "projection": "native", "exposure": "in-prompt",
        "profile": {"memory": [], "identity": {}},
        "user": {"replies": [], "fallback": "I don't know; use your best judgement."},
        # Reads ask on first use (guidelines 3.3); this scripted user allows them for the task, and
        # every write stays denied until the developer adds rules.
        "permissions": {"mode": "platform-default", "default": "deny", "rules": [
            {"id": "reads-for-this-task", "decision": "allow_task",
             "when": {"field": "tool_class", "op": "eq", "value": "read"}}]},
        "checks": [{"id": "outcome", "source": "oracle",
                    "oracle": {"url": f"{SCAFFOLD_MARKER}: a private state URL of your test service",
                               "pointer": f"{SCAFFOLD_MARKER}: the JSON pointer of the field that shows the "
                                          "outcome, such as /confirmed_count"},
                    "expect": {"op": "eq", "value": f"{SCAFFOLD_MARKER}: the value that field holds when the "
                                                    "task succeeded"}}],
        "answer": {"claims": [{"state": f"{SCAFFOLD_MARKER}: the outcome a truthful final answer reports "
                                        "(confirmed, pending, quoted, failed, cancelled, submitted or paid)",
                               "true_when": "outcome"}]},
        "limits": {"model_turns": 30, "tool_calls": 40, "wall_seconds": 600},
    }
    lines = [f"# connector-v1 scenario scaffolded from {comment(endpoint)} on {comment(fetched_at, 40)}",
             f"# Server: {comment(field(server_info, 'name', 'unknown'), 80)} "
             f"{comment(field(server_info, 'version', ''), 40)}".rstrip(),
             "# Read-only: the scaffold ran initialize and tools/list; it called no tools.",
             f"# Replace every {SCAFFOLD_MARKER} (connector-run refuses them) and review tools.allow.", "#",
             "# Classes: each trial classifies the live catalog (annotations, verbs, enumerated operations).",
             "# Declare a class under tools.classes only where your documentation settles it: a declared",
             "# class wins over annotations, and the reads-for-this-task rule trusts it. Suggestions:",
             "#   classes:"]
    for tool in names:
        if (item := classifications.get(tool)) is not None:
            reasons = "; ".join(map(str, field(item, "reasons", []) or [])) or "no signal"
            lines.append(f"#     {comment(tool, 64)}: {field(item, 'tool_class')}  "
                         f"# {comment(field(item, 'basis'), 20)}: {comment(reasons, 200)}")
    lines += ["#", "# The scripted user allows reads for the task and denies every write: add rules for the",
              "# writes this task should approve (guidelines 3.3: sensitive writes ask on every use)."]
    def properties(tool):
        schema = tool.get("inputSchema")
        found = schema.get("properties") if isinstance(schema, dict) else None
        return found if isinstance(found, dict) else {}
    # Identifiers, page and continuation tokens and cursors are inputs the model must pass (the same rule as
    # model-view masking, projection.functional_key), never personal data to move off the model.
    personal = [f"{comment(tool['name'], 64)}.{comment(key, 64)}" for tool in entries
                for key, schema in properties(tool).items()
                if isinstance(schema, dict) and not functional_key(key)
                and (private_key(key) or schema.get("format") == "email")][:8]
    if personal:
        lines += ["#", "# These inputs look like the user's personal data: fill them from private: through",
                  "# host_arguments so the model never sees them, for example",
                  "#   private: {email: 'you@example.com'}  and  host_arguments: {<tool>: {<field>: {$ref: /private/email}}}",
                  f"#   candidates: {', '.join(personal)}"]
    def lookup(tool):  # A status read, or a read by one id (get_booking(booking_id)), shows an outcome best.
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        by_id = len(required) == 1 and str(required[0]).endswith("id")
        return not ("status" in tool["name"] or by_id), names.index(tool["name"])
    reads = sorted((tool for tool in entries if field(classifications.get(tool["name"]), "tool_class") == "read"),
                   key=lookup)
    if reads:
        lines += ["#", "# Instead of an oracle, a check can call one of the connector's read tools after the agent",
                  "# stops (never shown to the model), for example:", "#   - id: outcome", "#     source: tool",
                  (f"#     tool: {{name: {comment(reads[0]['name'], 64)}, arguments: "
                   f"{{{_example_arguments(reads[0])}}}, pointer: /structuredContent/<field>}}"),
                  "#     expect: {op: eq, value: <the value that shows success>}"]
    return "\n".join(lines) + "\n" + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


# scaffold --from-run: a recorded agent trial as a scripted calls file and a checks skeleton -------------
HIDDEN = MARKERS  # what evidence redaction writes in place of a hidden value
HIDDEN_HINT = (f"{SCAFFOLD_MARKER}: a value this run's evidence hides (private, captured or credential); write a "
               "test value or a {$ref: /results/<step>/...} reference to an earlier result")


def hidden_free(value):
    """`value` with every string evidence redaction touched replaced by a TODO(scaffold) placeholder: a scaffold
    never copies what the run hid, and never a marker that would pass for data."""
    if isinstance(value, dict):
        return {key: hidden_free(item) for key, item in value.items()}
    if isinstance(value, list):
        return [hidden_free(item) for item in value]
    return HIDDEN_HINT if isinstance(value, str) and HIDDEN.search(value) else value


def message_text(message) -> str:
    """The text an assistant message said (its text parts; never reasoning)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    return "".join(part.get("text") or "" for part in content or [] if isinstance(part, dict)
                   and part.get("type") == "text")


def recorded_steps(responses) -> list:
    """ScriptedDriver steps from an agent's model responses in order (each an Inspect ModelOutput dump): each
    tool call, an {idle: true} where the agent stopped and was resumed, and the final answer. Hidden values
    become placeholders (hidden_free); arguments that did not parse are one too."""
    steps = []
    for index, response in enumerate(responses):
        choices = response.get("choices") if isinstance(response, dict) else None
        message = (choices[0] or {}).get("message") or {} if isinstance(choices, list) and choices else {}
        calls = message.get("tool_calls") or []
        for call in calls:
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            if call.get("parse_error"):
                arguments = {SCAFFOLD_MARKER: "the model's arguments did not parse; write the arguments to send"}
            steps.append({"tool": str(call.get("function")), "arguments": hidden_free(arguments)})
        if calls:
            continue
        text = message_text(message).strip()
        if index == len(responses) - 1:
            parts = [part for part in re.split(f"({HIDDEN.pattern})", text) if part]
            steps.append({"answer": [HIDDEN_HINT if HIDDEN.fullmatch(part) else part for part in parts]
                          if HIDDEN.search(text) else text or f"{SCAFFOLD_MARKER}: the final answer"})
        else:
            steps.append({"idle": True, **({"text": hidden_free(text)} if text else {})})
    return steps


def script_document(steps, *, run_id, scenario_id, scenario_sha256, written_at, as_json=False) -> str:
    """The calls file: YAML under a provenance header, or with `as_json` (a .json file, which serialization.load
    reads as JSON only) the steps alone, since JSON has no comments."""
    if as_json:
        import json
        return json.dumps(steps, indent=2, ensure_ascii=False) + "\n"
    import yaml
    lines = [(f"# Scripted calls recorded from connector run {comment(run_id, 80)} (scenario "
              f"{comment(scenario_id, 64)}, sha256 {comment(scenario_sha256, 64)}) by scaffold --from-run on "
              f"{comment(written_at, 40)}."),
             "# The agent model's tool calls in order; replay them with --driver scripted --script <this file>.",
             f"# Values the run's evidence hid are {SCAFFOLD_MARKER} placeholders, which connector-run refuses: write",
             "# a test value or a {$ref: /results/<step>/<pointer>} reference to an earlier step's result. Literal",
             "# identifiers the service returned (quote and booking IDs) replay only against the same service state."]
    return "\n".join(lines) + "\n" + yaml.safe_dump(steps, sort_keys=False, allow_unicode=True, width=100)


def checks_document(*, run_id, scenario_id, writes, reads, recorded) -> str:
    """A checks skeleton for the recorded trial: an outcome check per write the agent sent (none: one), a
    claim, and the run's own check results as comments; every judgment is a TODO(scaffold)."""
    import yaml
    names = [re.sub(r"[^A-Za-z0-9_-]+", "-", f"{tool}-outcome")[:60] for tool in writes] or ["outcome"]
    checks = [{"id": name, "source": "oracle",
               "oracle": {"url": f"{SCAFFOLD_MARKER}: a private state URL of your test service",
                          "pointer": f"{SCAFFOLD_MARKER}: the JSON pointer of the field that shows "
                                     + (f"what {tool} changed" if tool else "the outcome")},
               "expect": {"op": "eq", "value": f"{SCAFFOLD_MARKER}: the value that field holds after a correct run"}}
              for name, tool in zip(names, writes or [None], strict=True)]
    document = {"checks": checks, "answer": {"claims": [
        {"state": f"{SCAFFOLD_MARKER}: the outcome a truthful final answer reports", "true_when": names[0]}]}}
    lines = [f"# Checks skeleton for {comment(scenario_id, 64)} from connector run {comment(run_id, 80)}.",
             f"# Merge it into the scenario and replace every {SCAFFOLD_MARKER}, which connector-run refuses."]
    if recorded:
        lines.append("# The recorded run's own checks: " + comment(", ".join(
            f"{row['id']} {row['status']}" for row in recorded[:20]), 400))
    if reads:
        lines.append("# A check can instead call a read tool the agent used (never shown to the model): "
                     + comment(", ".join(reads[:10]), 300))
    return "\n".join(lines) + "\n" + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
