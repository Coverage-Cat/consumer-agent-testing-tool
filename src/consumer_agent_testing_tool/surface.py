"""General consumer-surface workflows. Private host state never becomes planner input."""
import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .browser_network import EmbeddedBrowserWorker, UIRequestGrant, validate_network_policy
from .config import is_loopback
from .errors import LabError, normalize_error
from .evidence import Redactor, environment_secrets
from .linking import ApiKeyAuth, link
from .serialization import digest, load

# The agent driver offers scenario-authored steps as one enum-constrained tool. Say so wherever
# a run is recorded: it is not evidence that a model can use the service's own tools.
STEP_MENU = "scripted action menu"
STEP_MENU_GAP = ("Service tool surface not exposed to the model: the agent chose among scenario-authored "
                 "actions and the host resolved every tool argument")
RESOURCE_LINK_CHECK = "mcp-apps-resource-link"
MASKED = "[REDACTED]"
# What tools_sha256 covers: the catalog as the pinned MCP client SDK parses tools/list.
TOOLS_DIGEST_BASIS = ("tools/list as parsed by the MCP client SDK: MCP-defined tool fields, full input "
                      "and output schemas and _meta; unknown top-level and annotation fields are dropped")
# Values public evidence recorded before they became secrets; read by export re-redaction.
LATE_SECRETS = Path("private/late-secrets.private.json")
# The embedded-app lane's host contract: generic-host evidence for ChatGPT- or Claude-style hosts.
# Muse documents no third-party UI (review 2026-10-15 §2.2), so none of it is Muse evidence.
MCP_APPS_HOST = {"evidence": "generic MCP Apps host; not Muse evidence",
                 "specification": "MCP Apps 2026-01-26 (SEP-1865, io.modelcontextprotocol/ui)",
                 # Absence of evidence, not proof that Muse lacks MCP Apps support.
                 "scope_basis": {"label": "assumed", "source": "review 2026-10-15 §2.2: Muse's guidelines never "
                                 "mention UI, widgets or templates; no Meta client in the MCP extension matrix"}}
# The view's latest ui/update-model-context, as the model sees it in later turns (bounded).
APP_CONTEXT_LIMIT = 8000
# ui/message entries waiting for the agent's next observation; each is bounded like app context.
APP_MESSAGE_LIMIT = 20
APP_MESSAGE_QUEUED = "queued for the agent's next observation as an app message"
# The native renderer's policy for a view's own HTTP writes (review M6): no grant mechanism exists
# there, so each write is recorded before it is sent and the report counts them.
NATIVE_WRITES = ("native renderer: a view's direct HTTP writes to browser_origins are sent without host consent; "
                 "the coordinator records each one first as a host_action event (consent: none). The isolated "
                 "renderer requires a one-use grant per write")
# A callback resource is an absolute URI: a scheme, then up to 2,000 visible characters.
RESOURCE_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S{1,2000}")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Check(Strict):
    id: str
    layer: Literal["expectation", "agent", "host", "service"]
    path: str
    expected: Any


class Reconcile(Strict):
    tool: str
    arguments: dict = Field(default_factory=dict)
    path: str
    expected: Any


class ToolPolicy(Strict):
    effect: Literal["read", "write"]
    arguments_schema: dict = Field(default_factory=lambda: {
        "type": "object", "properties": {}, "additionalProperties": False})
    host_arguments: dict = Field(default_factory=dict)
    capture: dict[str, str] = Field(default_factory=dict)
    error_paths: list[str] = Field(default_factory=lambda: ["/structuredContent/error"])
    reconcile: Reconcile | None = None
    consent: dict | None = None


class UIAction(Strict):
    kind: Literal["fill", "click", "expect_value", "expect_text"]
    selector: str
    frame_url: str | None = None
    value: Any = None


class UI(Strict):
    source: str
    resource_uri: str | None = None
    bridge: Literal["mcp-app", "legacy-openai"] = "mcp-app"
    ready_selector: str
    actions: list[UIAction] = Field(default_factory=list)
    callback_tools: list[str] = Field(default_factory=list)
    # Exact resource URIs the view may read through the host (resources/read; resources/list shows
    # only these). Left out of the scenario digest while empty, so earlier digests hold.
    callback_resources: list[str] = Field(default_factory=list, max_length=100, exclude_if=lambda value: not value)
    callback_required: bool = False
    # Observe a documented window property; never execute scenario JavaScript.
    observations: dict[str, str] = Field(default_factory=dict)
    resource_file: str | None = None
    network_grants: list[dict] = Field(default_factory=list, max_length=100)
    network_reconcile: Reconcile | None = None

    @model_validator(mode="after")
    def readable(self):
        if self.callback_resources:
            if self.bridge != "mcp-app":
                raise ValueError("callback_resources need the mcp-app bridge; window.openai reads no resources")
            if len(set(self.callback_resources)) != len(self.callback_resources) or not all(
                    RESOURCE_URI.fullmatch(uri) for uri in self.callback_resources):
                raise ValueError("callback_resources must be distinct absolute resource URIs")
        return self

    @model_validator(mode="after")
    def grants(self):
        for grant in self.network_grants:
            if set(grant) == {"$ref"}:
                if not isinstance(grant["$ref"], str) or not grant["$ref"].startswith(
                        ("/fixture/", "/secrets/", "/replies/")):
                    raise ValueError("Network grant references must use trusted fixture, secret or reply inputs")
            else:
                UIRequestGrant.model_validate(grant)
        return self


class Step(Strict):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    description: str
    kind: Literal["tool", "ui", "user", "assert"]
    after: list[str] = Field(default_factory=list)
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)
    ui: UI | None = None
    reply_key: str | None = None
    checks: list[Check] = Field(default_factory=list)
    expose: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def shape(self):
        required = {"tool": self.tool, "ui": self.ui, "user": self.reply_key}
        if self.kind in required and required[self.kind] is None:
            raise ValueError(f"{self.kind} step is missing its configuration")
        if any(any(part in path.lower() for part in (
                "_meta", "secret", "token", "password", "card_number", "cvc"))
               for path in self.expose.values()):
            raise ValueError("Private metadata cannot be exposed to the planner")
        return self


class SurfaceScenario(Strict):
    version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    goal: str
    endpoint: str
    allowed_origins: list[str]
    browser_origins: list[str] = Field(default_factory=list)
    browser_worker: EmbeddedBrowserWorker | None = None
    test_only: Literal[True] = True
    fixture: dict = Field(default_factory=dict)
    tools: dict[str, ToolPolicy]
    steps: list[Step] = Field(min_length=1)
    treatment: Literal["baseline", "observation", "workaround"] = "baseline"
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_model_turns: int = Field(default=100, ge=1, le=1000)
    wall_seconds: int = Field(default=600, ge=1, le=3600)
    # "auto" probes server/discover (MCP 2026-07-28) before falling back to initialize; the default
    # keeps the plain 2025 handshake. Generic dual-era host coverage (review P3.1), not a Muse need.
    version_negotiation: Literal["legacy", "auto"] = "legacy"
    # The MCP connection's API key (review F3): named by a coordinator environment variable and sent
    # only to the endpoint, as a bearer token, a custom header or a query parameter. Never in
    # evidence. Left out of the digest while unset.
    auth: ApiKeyAuth | None = Field(default=None, exclude_if=lambda value: value is None)

    def digest(self):
        """The scenario digest; a field added later enters it only when set, so earlier digests hold."""
        unset = {name for name in ("version_negotiation",) if getattr(self, name) == "legacy"}
        return hashlib.sha256(self.model_dump_json(exclude=unset).encode()).hexdigest()

    @model_validator(mode="after")
    def contract(self):
        if any(name.startswith("__surface_") for name in self.tools):
            raise ValueError("Reserved private host callback name")
        if self.browser_worker:
            validate_network_policy(self.browser_origins, self.browser_worker.rules, self.browser_worker)
            if set(self.browser_origins) & {"https://surface-host.invalid", "https://surface-app.invalid"}:
                raise ValueError("Embedded host origins are reserved")
        for origin in self.allowed_origins + self.browser_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.hostname \
                    or parsed.username or parsed.password or parsed.query or parsed.fragment \
                    or parsed.path not in ("", "/"):
                raise ValueError("Allowlist entries must be exact HTTP(S) origins")
        parsed = urlsplit(self.endpoint)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Endpoint cannot contain credentials, a query, or a fragment")
        if f"{parsed.scheme}://{parsed.netloc}" not in self.allowed_origins:
            raise ValueError("Endpoint is outside the explicit origin allowlist")
        if self.auth:
            if self.auth.placement == "url_path_segment":
                raise ValueError("surface-run sends an API key as a bearer token, a custom header or a query parameter")
            if parsed.scheme != "https" and not is_loopback(self.endpoint):
                raise ValueError("An API key travels only over HTTPS or to a loopback endpoint")
        seen = set()
        for step in self.steps:
            if step.id in seen or any(dependency not in seen for dependency in step.after):
                raise ValueError("Steps need unique IDs and dependencies on earlier steps")
            seen.add(step.id)
            if any(check.id == RESOURCE_LINK_CHECK for check in step.checks):
                raise ValueError(f"Check id {RESOURCE_LINK_CHECK} is reserved for the host's automatic check")
            if step.tool and step.tool not in self.tools:
                raise ValueError(f"Undeclared tool: {step.tool}")
            if step.ui:
                if (step.ui.network_grants or step.ui.network_reconcile) and not self.browser_worker:
                    raise ValueError("UI network grants require the isolated browser worker")
                if step.ui.network_reconcile:
                    lookup = self.tools.get(step.ui.network_reconcile.tool)
                    if not lookup or lookup.effect != "read":
                        raise ValueError("UI network reconciliation requires a declared read tool")
                if step.ui.source not in seen - {step.id}:
                    raise ValueError("UI source must be an earlier step")
                if step.ui.source not in step.after:
                    raise ValueError("UI must depend on its source step")
                if any(name not in self.tools for name in step.ui.callback_tools):
                    raise ValueError("UI callback tools must be declared")
                if step.ui.resource_file and self.treatment != "workaround":
                    raise ValueError("Resource replacement requires workaround treatment")
                if step.ui.observations and self.treatment == "baseline":
                    raise ValueError("Runtime property observations require observation treatment")
        for policy in self.tools.values():
            Draft202012Validator.check_schema(policy.arguments_schema)
            def check_refs(value):
                if isinstance(value, dict):
                    if "$ref" in value and not value["$ref"].startswith("#"):
                        raise ValueError("Tool schemas may only use local references")
                    for item in value.values():
                        check_refs(item)
                elif isinstance(value, list):
                    for item in value:
                        check_refs(item)
            check_refs(policy.arguments_schema)
            if policy.reconcile:
                read = self.tools.get(policy.reconcile.tool)
                if not read or read.effect != "read":
                    raise ValueError("Reconciliation must use a declared read tool")
        return self


def inside(root: Path, value: str | Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise LabError(f"Path escapes the workspace: {value} resolves outside {root.resolve()}; "
                       "keep runs, scenarios and inputs inside the current directory")
    return path


def pointer(value, path):
    if path == "":
        return value
    if not path.startswith("/"):
        raise LabError("Evidence paths must be JSON pointers")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def secret_positions(template, value):
    """Mask what a template fills from /secrets; redaction matches strings only, so numbers would leak."""
    if isinstance(template, dict) and set(template) == {"$ref"}:
        reference = str(template["$ref"])
        return MASKED if reference == "/secrets" or reference.startswith("/secrets/") else value
    if isinstance(template, dict) and isinstance(value, dict):
        return {key: secret_positions(template.get(key), item) for key, item in value.items()}
    if isinstance(template, list) and isinstance(value, list) and len(template) == len(value):
        return [secret_positions(*pair) for pair in zip(template, value, strict=True)]
    return value


def atomic_json(path, value, *, indent=None):
    # The one-shot C encoder: json.dump (and any indent before Python 3.14) recurses in Python,
    # so a deeply nested raw service result would raise RecursionError. Encoded before the
    # temporary file opens, so a failure never truncates anything. Committed, shallow records
    # pass an indent and end with a newline, as the other release recorders write them.
    text = json.dumps(value) if indent is None else json.dumps(value, indent=indent) + "\n"
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Paused(Exception):
    """A resumable stop; not evidence of success."""

    def __init__(self, message, *, category="waiting"):
        super().__init__(message)
        self.category = category


class ViewReply(Exception):
    """How an embedded view's tools/call ends when it is not a plain accepted result.

    `result` is the service's own CallToolResult (isError included) for the view;
    `stop` ends the active UI step promptly instead of waiting out its callback timeout.
    """

    def __init__(self, detail, *, result=None, stop=False):
        super().__init__(detail)
        self.result, self.stop = result, stop


def brief(value, limit=120):
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit - 3] + "..."


class SurfaceRun:
    def __init__(self, root, scenario, host, *, secrets=None, replies=None, resume=None,
                 planner=None, model_lock=None):
        self.root = Path(root).resolve()
        self.scenario, self.host, self.planner = scenario, host, planner
        self.replies = replies or {}
        # Coordinator credentials a service echoes back must not reach this run's own events.
        self.redactor = Redactor([*environment_secrets(), *(secrets or {}).values()])
        self.digest = scenario.digest()
        import platform

        from .serialization import file_digest, implementation_lock
        self.run_lock = {
            "scenario": self.digest, "python": platform.python_version(), "model": model_lock,
            "driver": "agent" if planner else "scripted",
            "surface_core": file_digest(Path(__file__)),
            "browser_host": file_digest(Path(__file__).parent / "data/surface_host/host.mjs"),
            "harness": implementation_lock(self.root),
        }
        self.path = inside(self.root, resume or Path("runs") / f"surface-{uuid.uuid4().hex}")
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock_id = uuid.uuid4().hex
        self.evidence_profile = {"name": "consumer-surface-public-constraint-surrogate",
            "surface": "mcp", "label": "consumer surface surrogate"}
        self.state = {"scenario_sha256": self.digest, "steps": {}, "operations": {}, "ui_attempts": {},
                      "secrets": secrets or {}, "checks": [], "capabilities": {
                          stage: "not-exercised" for stage in (
                              "discovery", "negotiation", "resource", "rendering", "interaction", "completion")},
                      "events": 0,
                      "driver": "agent" if planner else "scripted"}
        self.state["run_lock"] = self.run_lock
        if resume:
            self.state = json.loads((self.path / "checkpoint.private.json").read_text())
            if self.state["scenario_sha256"] != self.digest:
                raise LabError("Scenario changed; resume requires the same scenario")
            if self.state["driver"] != ("agent" if planner else "scripted"):
                raise LabError("Cannot change driver during a resumed run")
            # Measured during execution, not configured: carried over, then re-verified.
            for measured in ("browser_runtime", "server"):
                if measured in self.state.get("run_lock", {}):
                    self.run_lock[measured] = self.state["run_lock"][measured]
            if self.state.get("run_lock") != self.run_lock:
                raise LabError("Harness or model lock changed; use an explicitly labeled new run")
            self.state["run_lock"] = self.run_lock
            if not self.state.get("terminal"):
                self.state["secrets"].update(secrets or {})
        self.redactor.add(self.state.get("secrets", {}).values())
        if (late := self.path / LATE_SECRETS).is_file():
            self.redactor.add(json.loads(late.read_text()).get("secrets", []))
        self.host.callback = self.callback
        self.host.observe = self.observe_host
        self.host.completes = self.view_completes
        self.host.credential = None  # Resolved at connect: the endpoint's API key (scenario auth).
        self.active_ui = None
        self.ui_callback_count = 0
        self.ui_reads = 0
        self.ui_interruption = None
        self.active_network_grants = []
        # View tools/call requests are serialized so duplicate writes are recognized reliably.
        self.callback_lock = asyncio.Lock()
        self.stage = None
        self.outcome = None

    def persist(self):
        # A terminal run keeps only what refuses a replay and rebuilds its report.
        atomic_json(self.path / "checkpoint.private.json",
                    self.scrubbed() if self.state.get("terminal") else self.state)

    def scrubbed(self):
        return {
            "scenario_sha256": self.state["scenario_sha256"], "driver": self.state["driver"],
            "run_lock": self.state["run_lock"], "events": self.state["events"],
            "terminal": self.state["terminal"],
            "private_state": "scrubbed after a terminal outcome: no secrets, arguments or raw results",
            "steps": {key: {"status": value["status"]} for key, value in self.state["steps"].items()},
            "checks": self.redactor.clean(self.state["checks"]),
            "capabilities": self.state["capabilities"],
            "agent_runs": self.state.get("agent_runs", []),
            "model_preflight_runs": self.state.get("model_preflight_runs", []),
            "active_step": self.state.get("active_step"),
            "late_secrets": self.state.get("late_secrets", 0),
            "unconsented_network_writes": self.state.get("unconsented_network_writes", 0),
        }

    def require_run_lock(self, expected):
        # A rerun may meet a different server build; diff then reports it under /server.
        measured = ("browser_runtime", "server")
        if {k: v for k, v in expected.items() if k not in measured} != {
                k: v for k, v in self.run_lock.items() if k not in measured}:
            raise LabError("Behavioral rerun lock mismatch; use a new run with declared diff treatments")
        if runtime := expected.get("browser_runtime"):
            from .browser_execution import validate_identity
            if not self.scenario.browser_worker:
                raise LabError("A locked browser worker cannot become a native browser")
            validate_identity(self.scenario.browser_worker, runtime)
            if self.run_lock.get("browser_runtime") not in (None, runtime):
                raise LabError("Behavioral rerun browser runtime differs from the retained runtime")
            self.state["expected_browser_runtime"] = runtime
            self.persist()

    def event(self, kind, value):
        from .sealed_events import record
        self.state["events"] += 1
        self.persist()  # Reserve the sequence before append; crashes may leave gaps, never reused IDs.
        row = record(self.path, self.redactor, self.state["events"], kind, value,
            scenario_id=self.scenario.id, profile=self.evidence_profile, clock_id=self.clock_id,
            driver=self.state["driver"])
        with (self.path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    def observe_host(self, value):
        self.state["capabilities"].update(self.public_capabilities(value))
        if self.active_ui and value.get("kind") == "ui_resource":
            self.record_ui_resource(self.active_ui.id, value)
        if self.active_ui and value.get("kind") == "ui_model_context":
            self.record_app_context(self.active_ui.id, value)
        if value.get("kind") == "ui_message":
            value = {**value, "disposition": self.record_app_message(value)}
        self.event("host_observation", value)
        self.persist()
        return value

    def record_app_message(self, value):
        """A view's ui/message, which MCP Apps 2026-01-26 hosts SHOULD add to the conversation with its role.

        An agent run delivers it once, in the next observation a model reads, as an app message. A
        scripted run has no conversation, and no model turn follows the workflow's last step. Returns
        the recorded disposition; the view is answered isError unless the message was queued.
        """
        if self.state["driver"] != "agent" or not self.active_ui:
            return "recorded; not delivered to a conversation"
        if all(self.state["steps"].get(step.id, {}).get("status") == "completed"
               for step in self.scenario.steps if step.id != self.active_ui.id):
            # Once this step completes nothing is ready, so the driver asks the model nothing more.
            return "recorded; not delivered: no model turn follows the workflow's last step"
        pending = self.state.setdefault("app_messages", [])
        if len(pending) >= APP_MESSAGE_LIMIT:
            return f"recorded; not delivered: {APP_MESSAGE_LIMIT} app messages were already waiting"
        message = {"step": self.active_ui.id, "source": value.get("source"), "role": value.get("role"),
                   "content": value.get("content")}
        if len(json.dumps(message, default=str)) > APP_CONTEXT_LIMIT:
            message = {**message, "content": None,
                       "omitted": f"app message larger than {APP_CONTEXT_LIMIT} characters"}
        pending.append(message)
        return APP_MESSAGE_QUEUED

    def undelivered_app_messages(self, messages, reason, *, requeued):
        """Record app messages that no model read. Requeued ones go first in the next observation
        a model reads (a resumed run's first), so the queued disposition still holds."""
        if requeued:
            self.state["app_messages"] = [*messages, *self.state.get("app_messages", [])]
        self.event("app_messages_undelivered", {"count": len(messages), "reason": reason, "requeued": requeued,
                                                "steps": sorted({str(item.get("step")) for item in messages})})

    def record_app_context(self, step_id, value):
        """Each ui/update-model-context replaces the view's previous one; an empty update clears it.

        MCP Apps 2026-01-26: the host SHOULD give the model only the latest update in future turns.
        It persists across steps and resumes, as the context of a conversation would.
        """
        if value.get("cleared"):
            self.state["app_context"] = None
            return
        self.state["app_context"] = {"step": step_id, "source": value.get("source"), **{
            key: value[key] for key in ("content", "structuredContent", "omitted") if value.get(key) is not None}}

    def record_server(self, connected):
        """Pin what identifies the server build, so diff can tell two builds apart."""
        catalog = connected.pop("tools", None)
        identity = {
            "server_info": connected.get("server_info"),
            "protocol_version": connected.get("protocol_version"),
            # "modern" (2026-07-28, server/discover) or "legacy" (initialize), as negotiated.
            "protocol_era": connected.get("protocol_era"),
            "instructions_sha256": digest(connected["instructions"])
                if connected.get("instructions") is not None else None,
            "capabilities_sha256": digest(connected["server"]) if connected.get("server") is not None else None,
            # Names, descriptions, schemas, annotations and _meta, in the server's order.
            "tools_sha256": digest(catalog) if catalog is not None else None,
            "tool_count": len(catalog) if catalog is not None else None,
        }
        previous = self.run_lock.get("server")
        if previous is not None and {key: previous.get(key) for key in identity} != identity:
            raise LabError("The MCP server identity or tool catalog changed since this run started; "
                           "start a new run to test the changed build")
        self.run_lock["server"] = {**(previous or {}), **identity, "tools_digest_basis": TOOLS_DIGEST_BASIS}
        self.state["run_lock"] = self.run_lock
        return catalog

    def record_ui_resource(self, step_id, value):
        self.state.setdefault("ui_links", {})[step_id] = {key: value.get(key) for key in (
            "uri", "source", "tool", "descriptor_uri", "descriptor_source", "descriptor_invalid", "reason")}
        if isinstance(value.get("sha256"), str):
            server = self.run_lock.setdefault("server", {})
            server.setdefault("ui_resources", {})[step_id] = {"uri": value.get("uri"), "sha256": value["sha256"]}
            self.state["run_lock"] = self.run_lock

    @staticmethod
    def public_capabilities(value):
        result = {key: item for key, item in value.items()
                if key in ("discovery", "negotiation", "resource", "rendering", "interaction", "completion")
                and isinstance(item, str) and item in ("passed", "unavailable", "not-exercised", "failed")}
        if value.get("bridge") in ("mcp-app", "legacy-openai"):
            result["bridge"] = value["bridge"]
        return result

    def resolve(self, value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                try:
                    return pointer({"fixture": self.scenario.fixture, "steps": self.state["steps"],
                                    "secrets": self.state["secrets"], "replies": self.replies}, value["$ref"])
                except (KeyError, IndexError, TypeError, ValueError):
                    raise self.missing_reference(value["$ref"]) from None
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        return value

    @staticmethod
    def missing_reference(reference):
        root = reference.split("/")[1] if reference.count("/") > 1 else reference.strip("/")
        if root == "secrets":
            return LabError(f"Scenario reference {reference} has no value; supply it with --secrets-file "
                            "or capture it from an earlier tool result")
        if root == "replies":
            return Paused(f"User reply {reference} is required; resume with --replies")
        if root == "steps":
            return LabError(f"Scenario reference {reference} is missing from the recorded results",
                            code=1, category="tool/API")
        return LabError(f"Scenario reference {reference} does not resolve")

    def view_arguments(self, origin):
        """The source call's arguments as they were sent, for MCP Apps tool-input.

        Taken from the recorded operation, never re-resolved (a resume need not repeat earlier
        replies). Host arguments were never part of them; secret positions and any string that
        holds a registered secret are masked, because the view is service-controlled code.
        """
        def secrets(value, depth=0):
            if depth > 64:
                return MASKED  # Fail closed rather than recurse through hostile nesting.
            if isinstance(value, dict):
                return {key: secrets(item, depth + 1) for key, item in value.items()}
            if isinstance(value, list):
                return [secrets(item, depth + 1) for item in value]
            if isinstance(value, str) and any(secret in value for secret in self.redactor.secrets):
                return MASKED
            return value
        sent = self.state["operations"].get(origin.id, {}).get("arguments", {})
        return secrets(secret_positions(origin.arguments, sent))

    @staticmethod
    def captured(policy, result):
        values = {}
        for key, path in policy.capture.items():
            try:
                values[key] = pointer(result, path)
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return values

    @staticmethod
    def unredactable(value):
        """Whether a captured value holds a number: the redactor can only find strings."""
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                return True
        return False

    def rejected(self, result, policy):
        return not isinstance(result, dict) or bool(result.get("isError")) \
            or any(self.has_error(result, path) for path in policy.error_paths)

    async def invoke(self, name, arguments, operation, *, reject=True, durable=True):
        """Cross the service boundary once per logical operation.

        `reject=False` returns a service-rejected result instead of raising; `durable=False`
        (view reads) neither deduplicates nor records the operation.
        """
        policy = self.scenario.tools[name]
        if errors := list(Draft202012Validator(policy.arguments_schema).iter_errors(arguments)):
            raise LabError(f"Arguments violate the declared policy for {name}: {errors[0].validator}")
        effective = {**arguments, **self.resolve(policy.host_arguments)}
        previous = self.state["operations"].get(operation) if durable else None
        # Nothing crossed the service boundary while approval was outstanding.
        waiting = previous is not None and previous["status"] == "awaiting-consent"
        if previous and not waiting and previous.get("arguments") != arguments:
            raise Paused(f"Arguments changed for {operation}; the previous operation was not repeated")
        if previous and previous["status"] == "completed":
            return previous["result"]
        if previous and not waiting and policy.effect == "write":
            if not policy.reconcile:
                raise Paused(f"Ambiguous write {operation}; no reconciliation rule. It was not repeated.")
            rule = policy.reconcile
            # Reads may be repeated; the original write is never blindly replayed.
            result = await self.invoke(rule.tool, self.resolve(rule.arguments),
                                       f"reconcile-{operation}-{uuid.uuid4().hex}")
            try:
                confirmed = pointer(result, rule.path) == self.resolve(rule.expected)
            except (KeyError, IndexError, TypeError):
                confirmed = False
            if not confirmed:
                raise Paused(f"Write {operation} is not confirmed; retry requires a new test decision")
            self.state["operations"][operation] = {"status": "completed", "result": result,
                                                  "reconciled": True, "arguments": arguments}
            self.persist()
            return result
        if policy.consent:
            from .consent import ConsentBroker, effective_arguments
            declaration = self.resolve(policy.consent)
            effective = effective_arguments(declaration["schema"], effective)
            reply = declaration["reply_key"]
            if reply not in self.replies:
                if durable:
                    # Resuming with the approval sends exactly this call; nothing was sent yet.
                    self.state["operations"][operation] = {"status": "awaiting-consent", "tool": name,
                                                          "arguments": arguments}
                    self.persist()
                raise Paused(f"Host approval required for {reply}")
            broker = ConsentBroker(self.path / "consent.private.sqlite")
            grant = broker.issue(declaration["envelope"], declaration["schema"], effective,
                decision=self.replies[reply], now=declaration["now"], expires_at=declaration["expires_at"])
            broker.consume(grant["id"], declaration["envelope"], declaration["schema"], effective,
                           now=declaration["now"])
            self.event("consent_consumed", {"operation": operation, "binding": grant["binding"]})
        if durable:
            self.state["operations"][operation] = {"status": "pending", "tool": name,
                                                  "arguments": arguments}
            self.persist()  # Durable intent BEFORE crossing the service boundary.
        recorded = {**effective, **{key: secret_positions(template, effective[key])
                                    for key, template in policy.host_arguments.items() if key in effective}}
        self.event("host_action", {"operation": operation, "tool": name,
                                  "argument_keys": sorted(effective), "arguments": recorded,
                                  "effect": policy.effect})
        result = await self.host.call(name, effective)
        # Register captured and token-like values before anything about this response is recorded.
        # learn() is iterative and takes any name that mentions a credential (password_reset_url),
        # but not counts or types (max_tokens, token_type) or descriptive fields of a credential object.
        captured = self.captured(policy, result)
        self.redactor.learn(result)
        self.redactor.add(captured.values())
        if unredactable := sorted(key for key, value in captured.items() if self.unredactable(value)):
            # Evidence redaction matches strings only: withhold the response instead of leaking it.
            self.event("service_response", {"operation": operation, "result": None, "withheld":
                                            f"captured {', '.join(unredactable)} holds a number"})
            raise LabError(f"Captured value {unredactable[0]} from {name} holds a number, which evidence "
                           "redaction cannot hide; the response was withheld. Capture string values only",
                           category="setup/infrastructure")
        self.event("service_response", {"operation": operation, "result": result})
        if self.rejected(result, policy):
            if durable:
                self.state["operations"][operation] = {"status": "completed", "result": result,
                                                      "arguments": arguments, "rejected": True}
                self.persist()
            if reject:
                raise LabError(f"Service rejected {name}; see the redacted response",
                               code=1, category="tool/API")
            return result
        if missing := [path for key, path in policy.capture.items() if key not in captured]:
            raise LabError(f"Service result for {name} has no value at capture path {missing[0]}",
                           code=1, category="tool/API")
        self.state["secrets"].update(captured)
        if durable:
            self.state["operations"][operation] = {"status": "completed", "result": result,
                                                  "arguments": arguments}
            self.persist()
        return result

    @staticmethod
    def has_error(value, path):
        try:
            return bool(pointer(value, path))
        except (KeyError, IndexError, TypeError):
            return False

    async def callback(self, name, arguments):
        if name == "__surface_browser_identity":
            from .browser_execution import validate_identity
            if not self.active_ui or not self.scenario.browser_worker:
                raise LabError("Unexpected browser worker identity")
            validate_identity(self.scenario.browser_worker, arguments)
            previous = self.run_lock.get("browser_runtime") or self.state.get("expected_browser_runtime")
            if previous is not None and previous != arguments:
                raise LabError("Browser worker runtime changed; UI continuation refused")
            self.run_lock["browser_runtime"] = arguments
            self.state["run_lock"] = self.run_lock
            self.event("browser_runtime", arguments)
            return {"accepted": True}
        if name == "__surface_app_message":
            # A view's ui/message, recorded as observed; the view learns whether a conversation gets it.
            if not self.active_ui or not isinstance(arguments, dict) \
                    or set(arguments) != {"kind", "source", "role", "content"} or arguments["kind"] != "ui_message":
                raise LabError("Unexpected app message")
            return {"queued": self.observe_host(arguments)["disposition"] == APP_MESSAGE_QUEUED}
        if name == "__surface_network_write":
            # A view's own HTTP write in the native renderer, which has no grant mechanism: recorded
            # here, durably, as an unconsented host action before the browser sends it (review M6).
            if not self.active_ui or self.scenario.browser_worker \
                    or set(arguments) != {"url", "method", "body_sha256", "body_bytes"}:
                raise LabError("Unexpected browser network write")
            count = self.state["unconsented_network_writes"] = self.state.get("unconsented_network_writes", 0) + 1
            self.event("host_action", {"operation": f"ui:{self.active_ui.id}:network-write-{count}", "tool": None,
                                       "channel": "ui-network", "effect": "write", "consent": "none",
                                       "renderer": "native", **arguments})
            return {"recorded": True}
        if name == "__surface_browser_request":
            if not self.active_ui or not self.scenario.browser_worker \
                    or set(arguments) != {"url", "method", "body_sha256"}:
                raise LabError("Unexpected browser network request")
            used = self.state.setdefault("network_grants_used", {}).setdefault(self.active_ui.id, [])
            for index, grant in enumerate(self.active_network_grants):
                if index not in used and all(getattr(grant, key) == value for key, value in arguments.items()) \
                        and datetime.fromisoformat(grant.expires_at) > datetime.now(UTC):
                    used.append(index)
                    self.event("browser_network_authorized", {"step": self.active_ui.id, **arguments})
                    return {"authorized": True}
            raise LabError("No unused, unexpired host grant for this exact browser request")
        async with self.callback_lock:
            return await self.view_call(name, arguments)

    async def view_call(self, name, arguments):
        step = self.active_ui
        if not step or name not in step.ui.callback_tools:
            self.event("ui_callback_refused", {"step": step.id if step else None, "tool": name,
                                               "reason": "outside the UI step's callback allowlist"})
            raise LabError("UI attempted a tool outside its callback allowlist", code=1, category="interaction")
        if not self.visible(name)["app"]:
            # MCP Apps: a host MUST reject a view's tools/call for a tool whose visibility lacks "app".
            self.event("ui_callback_refused", {"step": step.id, "tool": name,
                                               "reason": "_meta.ui.visibility excludes app"})
            raise LabError(f"UI attempted {name}, which the server does not make visible to apps",
                           code=1, category="interaction")
        policy = self.scenario.tools[name]
        if errors := list(Draft202012Validator(policy.arguments_schema).iter_errors(arguments)):
            self.event("ui_callback_refused", {"step": step.id, "tool": name,
                                               "reason": f"arguments violate the declared policy: {errors[0].validator}"})
            raise LabError(f"UI arguments violate the declared policy for {name}: {errors[0].validator}",
                           code=1, category="interaction")
        # Identical writes within one UI step are one logical write; reads reach the service every
        # time, so a polling view never sees a frozen result.
        write = policy.effect == "write"
        if not write:
            self.ui_reads += 1
        operation = f"ui:{step.id}:{name}" if write else f"ui:{step.id}:{name}:read-{self.ui_reads}"
        try:
            result = await self.invoke(name, arguments, operation, reject=False, durable=write)
        except Exception as error:
            # A pause (approval, changed arguments, ambiguous write) or host failure ends the UI
            # step visibly; the view only learns that its call failed.
            self.ui_interruption = self.ui_interruption or error
            raise ViewReply(str(error), stop=True) from error
        rejected = self.rejected(result, policy)
        completes = not rejected and self.view_completes(name, step)
        if completes and not write:
            # Reads leave no operation record; a resume needs to know one completed this step.
            self.state.setdefault("ui_read_completions", {})[step.id] = name
        self.event("ui_callback", {"step": step.id, "tool": name, "operation": operation,
                                   "effect": policy.effect, "is_error": rejected})  # Persists the marker.
        if rejected:
            # The view receives the service's CallToolResult (isError: true), not a JSON-RPC failure.
            # A rejected write cannot complete the step: identical retries are deduplicated.
            if write:
                self.ui_interruption = self.ui_interruption or LabError(
                    f"Service rejected {name} from UI {step.id}; see the redacted response",
                    code=1, category="tool/API")
            raise ViewReply(f"Service rejected {name}", result=result, stop=write)
        if completes:
            self.ui_callback_count += 1
        return result

    def visible(self, name):
        """A tool's _meta.ui.visibility as the host measured it at connect (default: model and app)."""
        measured = (self.state.get("tool_visibility") or {}).get(name) or {}
        return {"model": measured.get("model") is not False, "app": measured.get("app") is not False}

    def refuse_app_only_steps(self):
        """An MCP Apps host never offers the model a tool whose visibility lacks "model"
        (MCP Apps 2026-01-26), so no agent-side step may call one; host reconciliation reads may."""
        for step in self.scenario.steps:
            if step.kind == "tool" and not self.visible(step.tool)["model"]:
                measured = (self.state.get("tool_visibility") or {}).get(step.tool, {})
                declared = measured.get("declared")
                if measured.get("invalid"):
                    # Not an array: the server's defect, and the host fails closed for views as well, so no
                    # scenario change helps (host.mjs passes the declared value already serialized).
                    raise LabError(f"Tool {step.tool} declares _meta.ui.visibility {str(declared)[:200]}, which is "
                                   "not an array of \"model\" and \"app\"; the host offers it to neither the model "
                                   f"nor a view, so step {step.id} cannot call it", code=1, category="tool/API")
                raise LabError(f"Tool {step.tool} is not visible to the model (_meta.ui.visibility "
                               f"{brief(declared)}); MCP Apps hosts never offer it to the model, so step "
                               f"{step.id} cannot call it. Use it only as a UI callback tool",
                               category="setup/infrastructure")

    def view_completes(self, name, step=None):
        """Whether an accepted view call completes its UI step: a write does, and a read only
        when the step declares no write callback (polling never stands in for a submission)."""
        step = step or self.active_ui
        if not step or name not in step.ui.callback_tools:
            return False
        return self.scenario.tools[name].effect == "write" or all(
            self.scenario.tools[tool].effect == "read" for tool in step.ui.callback_tools)

    def check(self, step):
        if link := self.state.get("ui_links", {}).get(step.id):
            self.record_check(step, self.resource_link_check(step, link))
        for check in step.checks:
            expected = self.resolve(check.expected)
            try:
                actual = pointer(self.state["steps"], check.path)
                status = "passed" if actual == expected and type(actual) is type(expected) else "failed"
            except (KeyError, IndexError, TypeError, ValueError):
                actual, status = None, "observation-gap"
            self.record_check(step, {"id": check.id, "step": step.id, "layer": check.layer, "status": status,
                                     "expected": expected, "actual": actual})

    @staticmethod
    def resource_link_check(step, link):
        # A scenario URI or result metadata can render an app that no real host would find.
        expected, actual = link.get("uri"), link.get("descriptor_uri")
        # The OpenAI Apps SDK key is a legacy link for that bridge only; MCP Apps hosts never read it.
        legacy = actual is not None and link.get("descriptor_source") == "openai-output-template" \
            and step.ui.bridge != "legacy-openai"
        passed = bool(expected) and actual == expected and not legacy
        item = {"id": RESOURCE_LINK_CHECK, "step": step.id, "layer": "host", "automatic": True,
                "status": "passed" if passed else "failed", "expected": expected,
                "actual": actual if actual is not None else link.get("descriptor_invalid"),
                "source": link.get("source"), "descriptor_source": link.get("descriptor_source"),
                "basis": "MCP Apps hosts find a tool's UI through its tools/list definition "
                         "(_meta.ui.resourceUri); a scenario URI or result metadata cannot stand in for it"}
        if not passed:
            item["detail"] = ("Only openai/outputTemplate links this UI; MCP Apps hosts read _meta.ui.resourceUri"
                              if legacy else link.get("reason") or "The tool definition does not link this UI")
        return item

    def record_check(self, step, item):
        self.state["checks"] = [c for c in self.state["checks"]
                                if (c["step"], c["id"]) != (step.id, item["id"])] + [item]
        self.event("assertion", item)
        if item["status"] == "failed":
            self.persist()
            raise LabError(f"{item['layer']} assertion {item['id']} failed: expected {brief(item['expected'])}, "
                           f"actual {brief(item['actual'])}" + (f" ({item['detail']})" if item.get("detail") else ""),
                           code=1, category="assertion")
        if item["status"] != "passed":
            self.persist()
            # Missing evidence is never a pass, but it is not a demonstrated failure either.
            raise Paused(f"{item['layer']} assertion {item['id']}: {item['status']}", category="observation-gap")

    async def execute_step(self, step):
        if step.kind == "tool":
            result = await self.invoke(step.tool, self.resolve(step.arguments), step.id)
        elif step.kind == "user":
            if step.reply_key not in self.replies:
                raise Paused(f"User input required for {step.reply_key}: {step.description}")
            result = {"reply": self.replies[step.reply_key]}
        elif step.kind == "ui":
            if self.state["ui_attempts"].get(step.id) == "pending":
                if self.state.get("network_grants_used", {}).get(step.id):
                    lookup = step.ui.network_reconcile
                    if not lookup:
                        raise Paused(f"UI {step.id} used a direct write grant; independent read reconciliation is required")
                    result = await self.invoke(lookup.tool, self.resolve(lookup.arguments),
                        f"ui-network-reconcile:{step.id}:{self.state['events']}")
                    actual, expected = pointer(result, lookup.path), self.resolve(lookup.expected)
                    if type(actual) is not type(expected) or actual != expected:
                        raise Paused(f"UI {step.id} direct write remains unconfirmed")
                    self.event("ui_network_reconciled", {"step": step.id, "tool": lookup.tool})
                # A click can have effects before its tool callback. Never replay it. Every write
                # callback must have been recorded; a read-only step needs its completing read.
                writes = [name for name in step.ui.callback_tools if self.scenario.tools[name].effect == "write"]
                operations = [f"ui:{step.id}:{name}" for name in writes]
                recorded = all(operation in self.state["operations"] for operation in operations) if writes \
                    else step.id in self.state.get("ui_read_completions", {})
                if not step.ui.callback_required or not recorded:
                    raise Paused(f"UI {step.id} was interrupted; cannot safely repeat its interactions")
                for name, operation in zip(writes, operations, strict=True):
                    previous = self.state["operations"][operation]
                    if previous.get("rejected"):
                        raise LabError(f"Service rejected {name} from UI {step.id}; see the redacted response",
                                       code=1, category="tool/API")
                    await self.invoke(name, previous.get("arguments", {}), operation)
                result = {"reconciled": True, "capabilities": self.state["capabilities"]}
                self.state["steps"][step.id] = {"result": result, "status": "observed"}
                self.check(step)
                self.state["steps"][step.id]["status"] = "completed"
                self.state["ui_attempts"][step.id] = "completed"
                self.persist()
                return
            self.active_ui, self.ui_callback_count, self.ui_reads, self.ui_interruption = step, 0, 0, None
            ui = self.resolve(step.ui.model_dump())
            self.active_network_grants = [UIRequestGrant.model_validate(grant) for grant in ui["network_grants"]]
            # Browser receives no authority objects; every write asks the private host.
            ui.pop("network_grants")
            ui.pop("network_reconcile")
            origin = next(item for item in self.scenario.steps if item.id == step.ui.source)
            # MCP Apps tool-input carries the call's own arguments; host arguments never reach the view.
            ui["source_tool"] = origin.tool if origin.kind == "tool" else None
            ui["tool_input"] = self.view_arguments(origin) if origin.kind == "tool" else {}
            source = self.state["steps"][step.ui.source]["result"]
            self.state["ui_attempts"][step.id] = "pending"
            self.persist()
            try:
                try:
                    result = await self.host.render(ui, source, self.path)
                except Exception as error:
                    # The host stops a UI step its callback interrupted; report the cause.
                    if self.ui_interruption is not None:
                        raise self.ui_interruption from None
                    link = self.state.get("ui_links", {}).get(step.id) or {}
                    if not link.get("uri") and link.get("descriptor_invalid") is not None:
                        # The tool definition links a UI no host can load: a server defect, not missing
                        # coverage. Nothing rendered; the failed host check (exit 1) says why.
                        self.state["ui_attempts"].pop(step.id, None)
                        self.record_check(step, self.resource_link_check(step, link))
                    if isinstance(error, Paused) and error.category == "unsupported":
                        # No UI was found, so nothing was clicked: a resume may try again.
                        self.state["ui_attempts"].pop(step.id, None)
                    raise
                if self.ui_interruption is not None:
                    raise self.ui_interruption
                if step.ui.callback_required and not self.ui_callback_count:
                    raise LabError(f"UI {step.id} did not complete its required callback",
                                   code=1, category="interaction")
                self.state["ui_attempts"][step.id] = "completed"
            finally:
                self.active_ui = None
                self.active_network_grants = []
        else:
            result = {}
        self.state["steps"][step.id] = {"result": result, "status": "observed"}
        self.persist()
        self.check(step)
        self.state["steps"][step.id]["status"] = "completed"
        self.persist()

    def observation(self, ready):
        # Model-visible: the goal, ready actions, completed steps and declared result fields.
        # Never raw tool output, fixture or private state, UI fields, metadata, check outcomes
        # or host capabilities: harness and oracle state must not steer the evaluated model.
        # The view-supplied parts are app_context, the context a view asked the host to give the
        # model (ui/update-model-context), and app_messages, the messages a view sent to the
        # conversation (ui/message), each delivered once; both masked and bounded.
        public = {}
        for step in self.scenario.steps:
            stored = self.state["steps"].get(step.id)
            if stored and stored["status"] == "completed":
                public[step.id] = {}
                for key, path in step.expose.items():
                    try:
                        public[step.id][key] = pointer(stored.get("result"), path)
                    except (KeyError, IndexError, TypeError, ValueError):
                        public[step.id][key] = None
        observation = {
            "goal": self.scenario.goal,
            "available_actions": [{"id": s.id, "description": s.description, "kind": s.kind}
                                  for s in ready],
            "completed": [key for key, value in self.state["steps"].items()
                          if value["status"] == "completed"],
            # Credentials and secure host inputs stay hidden; the user's own data does not (P0.5).
            # Recorded copies still get the full evidence redaction.
            "public_results": self.redactor.mask_credentials(public),
        }
        if context := self.app_context():
            observation["app_context"] = context
        if messages := self.state.pop("app_messages", None):
            observation["app_messages"] = self.redactor.mask_credentials(messages)
        return observation

    def app_context(self):
        """The app context: the view's latest model-context update, which MCP Apps hosts give the
        model in later turns. Service-controlled content, so credentials are masked and size bounded."""
        context = self.state.get("app_context")
        if not context:
            return None
        if len(json.dumps(context, default=str)) > APP_CONTEXT_LIMIT:
            context = {"step": context.get("step"), "source": context.get("source"),
                       "omitted": f"app context larger than {APP_CONTEXT_LIMIT} characters"}
        return self.redactor.mask_credentials(context)

    def ready_steps(self):
        remaining = [step for step in self.scenario.steps
                     if self.state["steps"].get(step.id, {}).get("status") != "completed"]
        ready = [step for step in remaining if all(
            self.state["steps"].get(dependency, {}).get("status") == "completed"
            for dependency in step.after)]
        if remaining and not ready:
            raise LabError("No executable scenario step")
        return ready

    async def execute_action(self, identifier):
        """Trusted execution boundary shared by scripted and Inspect-driven runs."""
        ready = self.ready_steps()
        if identifier not in {step.id for step in ready}:
            raise LabError("Agent proposed an unavailable action", code=1, category="interaction")
        step = next(step for step in ready if step.id == identifier)
        self.state["active_step"] = step.id
        self.event("step_started", {"id": step.id, "driver": self.state["driver"]})
        if self.state["steps"].get(step.id, {}).get("status") == "observed":
            self.check(step)
            self.state["steps"][step.id]["status"] = "completed"
            self.persist()
        else:
            try:
                await asyncio.wait_for(self.execute_step(step), self.scenario.timeout_seconds + 5)
            except TimeoutError:
                raise LabError(f"Step {step.id} did not finish within its host timeout; reconcile pending "
                               "operations before resuming", category="integration-failure") from None

    async def run(self):
        if terminal := self.state.get("terminal"):
            return self.decline_resume(terminal)
        status, detail, category, code = "completed", None, None, 0
        try:
            # A missing or shared endpoint key is a connect failure found before any model call.
            self.stage = "connect"
            credential = self.host.credential = await self.endpoint_credential()
            if self.planner is not None and hasattr(self.planner, "preflight"):
                # A provider without native tool calls must fail before any live service action.
                self.stage = "model-preflight"
                await self.planner.preflight(self)
            self.stage = "connect"
            capabilities = await self.host.connect(self.scenario)
            if credential is not None:
                described = credential.describe()
                capabilities["endpoint_auth"] = {key: described[key] for key in ("mode", "placement", "name", "env")}
            catalog = self.record_server(capabilities)
            self.state["capabilities"].update(self.public_capabilities(capabilities))
            self.state["tool_visibility"] = capabilities.get("tool_visibility") or {}
            self.event("capabilities", capabilities)
            if catalog is not None:
                self.event("server_catalog", {"tools_sha256": self.run_lock["server"]["tools_sha256"],
                                              "tools": catalog})
            self.persist()
            self.refuse_app_only_steps()
            self.stage = None
            if self.planner:
                await self.planner.run_workflow(self)
            else:
                while ready := self.ready_steps():
                    await self.execute_action(ready[0].id)
            if self.scenario.browser_worker and any(
                    step.kind == "ui" and self.state["steps"].get(step.id, {}).get("status") == "completed"
                    for step in self.scenario.steps) and not self.run_lock.get("browser_runtime"):
                raise LabError("No measured browser worker identity for completed UI execution")
        except Paused as error:
            status, detail, category, code = "blocked", str(error), error.category, 3
        except (asyncio.CancelledError, KeyboardInterrupt):
            status, detail, category, code = (
                "blocked", "Execution interrupted; reconcile pending operations before resuming", "interrupted", 3)
        except Exception as error:  # noqa: BLE001 — preserve evidence and pending-write state
            # Spec exit codes: 1 a failed assertion, service or agent outcome; 2 setup,
            # infrastructure or integration; 3 blocked or unsupported coverage.
            failure = normalize_error(error)
            code = failure.code if failure.code in (1, 2, 3) else 2
            status, category = ("blocked" if code == 3 else "failed"), failure.category
            detail = str(error) if isinstance(error, LabError) else f"{type(error).__name__}: {error}"
        finally:
            try:
                await self.host.close()
            except Exception as error:  # noqa: BLE001 — report cleanup failures without losing evidence
                self.event("cleanup_failure", {"detail": str(error) or type(error).__name__})
                if status == "completed":
                    status, detail, category, code = "failed", "Host cleanup failed", "setup/infrastructure", 2
            outcome = {"status": status, "exit_code": code, "failure_class": category,
                       "failure_stage": (self.stage or self.state.get("active_step")) if status != "completed" else None,
                       "detail": self.redactor.clean(detail)}
            # Completed and failed-outcome runs are final; blocked and setup failures stay resumable.
            # A write without a valid response (e.g. one that violates its output schema) may have
            # committed: keep its private state so a resume reconciles it instead of guessing.
            uncertain = sorted(key for key, operation in self.state["operations"].items()
                               if operation.get("status") == "pending"
                               and getattr(self.scenario.tools.get(operation.get("tool")), "effect", None) == "write")
            if uncertain:
                self.event("uncertain_writes", {"operations": uncertain})
            if (status == "completed" or code == 1) and not uncertain:
                self.state["terminal"] = outcome
            if self.state.get("terminal") and (left := self.state.pop("app_messages", None)):
                self.undelivered_app_messages(left, "the run ended before a model read them", requeued=False)
            self.retain_late_secrets()
            self.persist()
            if self.state.get("terminal"):
                self.discard_private_inputs()
            result = self.report(status, detail, outcome)
        if result["reporting_status"] != "completed":
            return 2
        return code

    async def endpoint_credential(self):
        """The MCP endpoint's API key from the coordinator environment, registered with the redactor
        before anything is sent; it must differ from the selected model's key. None without auth."""
        if not self.scenario.auth:
            return None
        model = self.run_lock.get("model") or {}
        selected = SimpleNamespace(auth=SimpleNamespace(env=(model.get("auth") or {}).get("env"))) if model else None
        # An API key links at once: no token store or HTTP policy is used.
        return await link(self.scenario.auth, self.scenario.endpoint, store=self.path, http_policy=None,
                          redactor=self.redactor, model=selected)

    def discard_private_inputs(self):
        # Approval bindings include host-injected arguments; a finished run never consumes them.
        for name in ("consent.private.sqlite", "consent.private.sqlite-journal"):
            (self.path / name).unlink(missing_ok=True)

    def retain_late_secrets(self):
        """Keep exports redacting values public evidence recorded before they became secrets.

        A session handle may appear in a response before a later step captures it. Sealed events
        are not rewritten, and a scrubbed checkpoint no longer lists the run's secrets, so the
        values found in public files are kept in private/ for export re-redaction (they are already
        in this run's local files) and the report names the gap.
        """
        leaked = set()
        for item in sorted(self.path.rglob("*")):
            parts = item.relative_to(self.path).parts
            if not item.is_file() or item.is_symlink() or item.suffix in (".tmp", ".sqlite") \
                    or any(part == "private" or ".private" in part or part == ".active" for part in parts):
                continue
            try:
                data = item.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue  # Binary (zipped Inspect logs): their text is also in the events scanned here.
            text = data.decode(errors="ignore")
            # JSON files hold escaped text: also look for each secret's escaped forms.
            leaked.update(secret for secret in self.redactor.secrets if any(form in text for form in (
                secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1])))
        self.state["late_secrets"] = len(leaked)
        if leaked:
            (self.path / LATE_SECRETS).parent.mkdir(mode=0o700, exist_ok=True)
            atomic_json(self.path / LATE_SECRETS, {"secrets": sorted(leaked)})

    def decline_resume(self, terminal):
        """A finished run is never replayed; resuming only re-seals its recorded outcome."""
        self.event("resume_declined", {"status": terminal["status"],
                                       "reason": "The run already reached a final outcome; nothing was repeated"})
        result = self.report(terminal["status"], terminal["detail"], terminal)
        return 2 if result["reporting_status"] != "completed" else terminal["exit_code"]

    def summary(self):
        """One line for the terminal: outcome, exit code, failure stage and reason."""
        outcome = self.outcome or {}
        line = f"Surface run {outcome.get('status', 'unknown')} (exit {outcome.get('exit_code', 2)})"
        if outcome.get("failure_stage"):
            line += f" at {outcome['failure_stage']}"
        if outcome.get("failure_class"):
            line += f" [{outcome['failure_class']}]"
        if outcome.get("detail"):
            reason = " ".join(str(outcome["detail"]).split())
            line += ": " + (reason if len(reason) <= 300 else reason[:297] + "...")
        return line

    def report(self, status, detail, outcome=None):
        from .sealed_events import metadata
        outcome = outcome or {"status": status, "detail": detail, "failure_class": None,
                              "exit_code": {"completed": 0, "blocked": 3}.get(status, 1),
                              "failure_stage": self.state.get("active_step") if status != "completed" else None}
        menu = self.state["driver"] == "agent"
        recorded = metadata(self.path, scenario_id=self.scenario.id, profile=self.evidence_profile,
            driver=self.state["driver"], locations={
                "inference": "none" if self.state["driver"] == "scripted" else "selected adapter; see model lock",
                "browser": "local isolated worker" if self.scenario.browser_worker else "local native",
                "target": self.scenario.allowed_origins, "telemetry": "disabled",
                "egress": "declared MCP and browser destinations"})
        if menu:
            # The planner picks among scenario-authored steps; it never sees the service's tools.
            recorded.update(lane="action-menu", evidence_label=STEP_MENU,
                            observation_gaps=[*recorded["observation_gaps"], STEP_MENU_GAP])
        if late := self.state.get("late_secrets"):
            recorded["observation_gaps"] = [*recorded["observation_gaps"], (
                f"{late} secret value(s) appeared in evidence recorded before the run learned them; "
                "local run files keep them, exports re-redact them")]
        # The host's automatic checks are no assertion about the service: they cannot make a pass alone.
        authored = [check for check in self.state["checks"] if not check.get("automatic")]
        report = self.redactor.clean_evidence({
            **recorded,
            "schema_version": 1, "scenario_id": self.scenario.id,
            "scenario_sha256": self.digest, "status": status, "detail": detail,
            "failure_class": outcome["failure_class"], "failure_stage": outcome["failure_stage"],
            "resumable": status != "completed" and not self.state.get("terminal"),
            # Until a final outcome the private checkpoint keeps credentials and raw results.
            "private_state": "scrubbed" if self.state.get("terminal") else "retained for resume",
            "progress": {"completed": sum(s["status"] == "completed"
                                         for s in self.state["steps"].values()),
                         "total": len(self.scenario.steps)},
            "driver": self.state["driver"], "surface": "local consumer-surface surrogate",
            "run_lock": self.state["run_lock"],
            "agent_runs": self.state.get("agent_runs", []),
            "model_preflight_runs": self.state.get("model_preflight_runs", []),
            "actual_target_surface_validated": False, "treatment": self.scenario.treatment,
            # Service and host checks keep their outcomes, but only a scripted baseline run is
            # compatibility evidence; a one-choice menu adds no agent evidence to it.
            "compatibility_pass": status == "completed" and self.scenario.treatment == "baseline"
                and bool(authored) and not menu,
            "compatibility_scope": "scripted baseline runs only",
            "mcp_apps_host": {**MCP_APPS_HOST, "renderer": "isolated single app iframe"
                              if self.scenario.browser_worker else "native, sandbox proxy for mcp-app views",
                              "version_negotiation": self.scenario.version_negotiation,
                              **({} if self.scenario.browser_worker else {"network_writes": {
                                  "unconsented": self.state.get("unconsented_network_writes", 0),
                                  "policy": NATIVE_WRITES}})},
            "capabilities": self.state["capabilities"], "checks": self.state["checks"],
            "steps": {key: {"status": value["status"]} for key, value in self.state["steps"].items()},
            "layers": {
                "expectation": {"scenario_validated": True},
                "agent": {"driver": self.state["driver"], "exercised": False,
                          "interface": STEP_MENU if menu else None,
                          "observation_gaps": [STEP_MENU_GAP] if menu else []},
                "host": self.state["capabilities"],
                "service": {"checks": [c for c in self.state["checks"] if c["layer"] == "service"],
                            "independent_backend_verification": False},
            },
            "evidence_limits": ["Service responses are not independent backend verification.",
                                "Scripted runs do not establish agent behavior.",
                                ("Agent runs choose among scenario-authored actions with host-resolved "
                                 "arguments; they do not test use of the service's tools."),
                                ("Embedded-app results are generic MCP Apps host evidence (ChatGPT- or "
                                 "Claude-style hosts), not Muse evidence: Muse documents no third-party UI."),
                                "Surrogate runs do not certify an actual consumer-agent product."],
        })
        from .sealed_report import finalize
        result = finalize(self.path, report)
        reported = result["reporting_status"] == "completed"
        self.outcome = {**outcome, "status": result["status"], "detail": report["detail"],
                        "exit_code": outcome["exit_code"] if reported else 2}
        if not reported:
            self.outcome["failure_class"] = "reporting"
        return result


def read_surface(path):
    return SurfaceScenario.model_validate(load(Path(path)))
