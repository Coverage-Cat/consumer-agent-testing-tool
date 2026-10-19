"""Host-owned browser policy with snapshot-only agent observations."""
import json
import re
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .browser_agent import BrowserActionRefused, entered, run_browser_agent
from .browser_diagnostics import DeveloperDiagnostics, DiagnosticPolicy
from .browser_network import EGRESS, BrowserWorkerPolicy, NetworkRule, validate_network_policy
from .consent import ConsentBroker
from .errors import LabError, LimitExhausted, Unsupported, normalize_error
from .evidence import Redactor, admissible_secret, environment_secrets
from .serialization import digest, file_digest, implementation_lock, load
from .surface import Paused, Strict, inside
from .surface_host import BrowserHost

# Ordinary outcomes returned to the agent: nothing (more) was executed or retried.
REFUSED = frozenset({"stale-reference", "denied", "invalid-action", "action-failed"})
# P3.4 (basis: assumed): the lane browser_mcp.mjs labels WEBMCP_LABEL. Meta documents a WebMCP host for web apps
# on its AI glasses (a Meta product, not Muse; see WEBMCP_BASIS there); this lane does not reproduce that host's
# limits, so the label names no product.
WEBMCP_LANE = "generic WebMCP page tools, not Muse"
WEBMCP_LIMIT = (f"WebMCP page tools were enabled (policy webmcp: true), the '{WEBMCP_LANE}' lane (basis: "
                "assumed): page-registered tools, their listings and their outputs are untrusted page content, and "
                "nothing here is Muse evidence.")


class BrowserPolicy(Strict):
    url: str
    origins: list[str]
    rules: list[NetworkRule]
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_actions: int = Field(default=30, ge=1, le=200)
    max_model_turns: int = Field(default=40, ge=1, le=500)
    wall_seconds: int = Field(default=180, ge=1, le=3600)
    principal: str = "synthetic-user"
    task_id: str = "browser-test"
    uploads: dict[str, str] = Field(default_factory=dict)
    max_file_bytes: int = Field(default=3*1024*1024, ge=1, le=10*1024*1024)
    allowed_upload_extensions: list[str] = Field(default_factory=lambda: [".txt", ".pdf", ".json"])
    diagnostics: DiagnosticPolicy = Field(default_factory=DiagnosticPolicy)
    worker: BrowserWorkerPolicy | None = None
    # Snapshot text per observation; longer pages are paged with a snapshot offset.
    snapshot_max_chars: int = Field(default=20000, ge=1000, le=2_000_000)
    # Snapshot observations (pages or continued slices) kept in full in the model
    # conversation; older ones become stubs naming their generation and range.
    history_snapshots: int = Field(default=2, ge=1, le=20)
    # JavaScript dialogs: surfaced to the agent, or answered by this declared default.
    dialogs: Literal["agent", "accept", "dismiss"] = "agent"
    # P3.4: list the tools a page registers through WebMCP in the snapshot (browse_open passes it to the
    # backend, browser_mcp.mjs) and let the agent call them (browser_action kind=webmcp, browser.mjs).
    # Labeled "generic WebMCP page tools, not Muse" (assumed; Meta AI glasses' own WebMCP host limits are not
    # reproduced, see WEBMCP_LANE); off by default, and never on for the connector lane's browser tasks.
    webmcp: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def destinations(self):
        validate_network_policy(self.origins, self.rules, self.worker)
        return self


REQUEST_SCHEMA = {"type": "object", "properties": {name: {"type": "string"}
    for name in ("url", "method", "body", "rule")},
    "required": ["url", "method", "body", "rule"], "additionalProperties": False}


def shown_forms(secret):
    """A secret and the forms a page commonly shows a long number in (a card or account number grouped
    by spaces or dashes; assumed), so a field that formats a filled value cannot reveal it."""
    digits = re.sub(r"[ -]", "", secret)
    if not re.fullmatch(r"[0-9]{12,19}", digits):
        return [secret]
    groups = [[digits[index:index + 4] for index in range(0, len(digits), 4)]]
    if len(digits) == 15:  # 4-6-5, as American Express numbers are shown.
        groups.append([digits[:4], digits[4:10], digits[10:]])
    return list(dict.fromkeys([secret, digits, *(mark.join(group) for group in groups for mark in " -")]))


def mask(value, secrets):
    """Declared secure values stay hidden from the model; nothing else is rewritten."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: mask(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item, secrets) for item in value]
    return value


class BrowserBroker:
    def __init__(self, root, policy, host=None, decisions=None, secrets=(), path=None):
        import uuid
        self.root = Path(root).resolve()
        self.policy = policy
        self.path = inside(self.root, path or Path("runs") / f"browser-{uuid.uuid4().hex}")
        self.path.mkdir(parents=True)
        # Scenario children commonly share a directory basename such as
        # "browser"; that basename cannot serve as a globally unique run ID.
        self.evidence_run_id = f"browser-{uuid.uuid4().hex}" if path else self.path.name
        self.clock_id = uuid.uuid4().hex
        self.evidence_profile = {"name": "muse-browser-public-2026-09",
            "surface": "browser", "label": "public-constraint surrogate"}
        self.host = host or BrowserHost(self.root)
        self.host.callback = self.authorize
        self.host.observe = self.observe
        # Evidence redaction learns typed values as they are entered. The model view
        # masks only host-supplied secrets (and the facade masks password fields): the
        # agent must see the page as rendered, including its own typed text.
        self.redactor = Redactor([*environment_secrets(), *secrets])
        self.secure = self.redactor.credentials  # Environment contact details are the user's own data.
        self.decisions = decisions or {}
        self.broker = ConsentBroker(self.path / "consent.private.sqlite")
        self.events, self.count, self.sequence = [], 0, 0
        self.backend_checks = []
        self.model_lock, self.browser_version, self.browser_backend = None, None, None
        self.worker_runtime = None
        self.agent_runs, self.preflight_runs = [], []
        self.refusals, self.truncated, self.captured, self.page_tool_calls = {}, 0, None, 0
        self.harness_lock = implementation_lock(self.root)
        self.diagnostics = DeveloperDiagnostics(self.path, policy.diagnostics)
        self.diagnostics_stopped = False

    def observe(self, value):
        """Developer-only boundary diagnostics, never returned as an agent snapshot."""
        if value.get("kind") == "browser_worker":
            if not self.policy.worker or value.get("image") != self.policy.worker.image \
                    or value.get("webrtc_loopback_probe") is not False:
                raise LabError("Browser worker identity does not match the selected policy")
            self.worker_runtime = {key: item for key, item in value.items() if key != "kind"}
        if value.get("kind") in {
                "network_denied", "browser_backend", "browser_backend_call", "browser_backend_error",
                "browser_worker", "browser_http", "browser_dialog", "browser_file_chooser",
                "browser_tab_load"}:
            self.record(value["kind"], {key: item for key, item in value.items() if key != "kind"})

    async def authorize(self, name, arguments):
        try:
            return await self._authorize(name, arguments)
        except Exception as error:
            self.record("network_denied", {"rule": arguments.get("rule"),
                                          "request_sha256": digest(arguments), "detail": str(error),
                                          "enforcement": "host-consent-broker"})
            raise

    async def _authorize(self, name, arguments):
        if name != "browser_request":
            raise LabError("Unknown browser callback")
        rule = next((rule for rule in self.policy.rules if rule.id == arguments.get("rule")), None)
        if not rule or rule.effect != "write":
            raise LabError("Unknown write classification")
        decision = self.decisions.get(digest(arguments))
        if not decision:
            raise LabError("No exact host decision for this browser request")
        if decision.get("consumed"):
            raise LabError("One-use browser request decision was already consumed")
        envelope = {"run_id": self.path.name, "task_id": self.policy.task_id,
                    "principal": self.policy.principal, "target": rule.origin, "destination": arguments["url"],
                    "operation": arguments["method"], "resource_ids": [rule.path], "effect": "commit",
                    "state_version": decision["state_version"], "amount_minor": decision["amount_minor"],
                    "currency": decision["currency"], "credential_ref": self.policy.principal}
        now = datetime.now(UTC).isoformat()
        grant = self.broker.issue(envelope, REQUEST_SCHEMA, arguments, decision=decision["decision"],
                                  now=now, expires_at=decision["expires_at"])
        self.broker.consume(grant["id"], envelope, REQUEST_SCHEMA, arguments, now=now)
        decision["consumed"] = True
        self.record("network_authorized", {"binding": grant["binding"]})
        return {"authorized": True}

    def record(self, kind, value):
        from .sealed_events import record
        self.sequence += 1
        event = record(self.path, self.redactor, self.sequence, kind, value,
            scenario_id=self.policy.task_id, profile=self.evidence_profile, clock_id=self.clock_id,
            driver=getattr(self, "driver", "scripted"), run_id=self.evidence_run_id)
        self.events.append(event)
        with (self.path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    def model_view(self, value):
        return mask(value, self.secure)

    async def open(self):
        diagnostics_directory = self.diagnostics.prepare()
        await self.host.start()
        private = {}
        for name, value in self.policy.uploads.items():
            file = inside(self.root, value)
            if file.suffix not in self.policy.allowed_upload_extensions \
                    or file.stat().st_size > self.policy.max_file_bytes:
                raise LabError("Upload fixture type or size denied")
            private[name] = str(file)
        directory = self.path / "downloads"
        directory.mkdir(exist_ok=True)
        result = await self.host.request("browse_open", {
            **self.policy.model_dump(), "private_uploads": private,
            "download_directory": str(directory), "diagnostics_directory": diagnostics_directory,
            **self.host_options()})
        self.browser_version = result.get("browser_version")
        self.browser_backend = result.get("backend")
        return await self.accept(result)

    def host_options(self):
        """Trusted host extensions; not supplied in a model action or policy file."""
        return {}

    async def action(self, action):
        from .measurements import measure
        # Reading more of the current snapshot never touches the page, so it is not charged;
        # model turns still bound it.
        if not (action.get("kind") == "snapshot" and action.get("offset")):
            self.count += 1  # A WebMCP page-tool call too: it acts on the page like a click.
            if self.count > self.policy.max_actions:
                raise LimitExhausted("Browser action budget exhausted")
        if values := entered(action):
            # Evidence only, through the minimum length and longest-first ordering.
            self.redactor.add(values)
        if action.get("kind") == "webmcp":
            self.page_tool_calls += 1
        self.record("browser_action", {"action": action})
        evidence = getattr(self, "measurement_evidence", None)
        with measure(evidence, "browser") if evidence else nullcontext():
            result = await self.host.request("browse_action", action)
        observation = await self.accept(result)
        if observation.get("status") in REFUSED:
            raise BrowserActionRefused(observation)
        return observation

    async def accept(self, result):
        """Record the redacted observation; return the model's view of the same page."""
        recorded = self.redactor.clean(result)
        self.record("snapshot", recorded)
        status = result.get("status")
        if status in REFUSED:
            self.refusals[status] = self.refusals.get(status, 0) + 1
            self.record("browser_action_refused", {"status": status, "message": result.get("message"),
                                                   "generation": result.get("generation")})
        if "truncated" in result and not result["truncated"].get("offset"):
            self.truncated += 1
        # Diagnostics pair media with a new page snapshot, not with a continued slice,
        # a refused action or a suspended dialog.
        if isinstance(result.get("text"), str) and result.get("generation") != self.captured:
            self.captured = result.get("generation")
            await self.capture_diagnostics(recorded)
        return self.model_view(result)

    async def capture_diagnostics(self, snapshot):
        if not self.policy.diagnostics.enabled or "generation" not in snapshot:
            return
        try:
            captured = await self.host.request("browse_diagnostics_capture",
                                               {"generation": snapshot["generation"]})
            self.record("diagnostic_capture", self.diagnostics.accept(captured, snapshot))
        except Exception as error:  # noqa: BLE001 — diagnostics must preserve the interaction outcome
            self.diagnostics.fail("screenshot", error)
            self.record("diagnostic_capture", {"generation": snapshot["generation"],
                                               "status": "failed", "audience": "developer-only"})

    async def stop_diagnostics(self):
        """Host lifecycle operation, absent from the agent's action schema."""
        if self.diagnostics_stopped or not self.policy.diagnostics.enabled:
            return
        self.diagnostics_stopped = True
        try:
            self.diagnostics.accept_trace(await self.host.request("browse_diagnostics_stop", {}))
        except Exception as error:  # noqa: BLE001 — finalize browser cleanup despite capture failure
            self.diagnostics.fail("trace", error)

    async def close(self):
        await self.stop_diagnostics()
        await self.host.close()

    def assert_backend_count(self, identifier, actual, expected, *, source):
        """Runner-only assertion over an independent oracle; never a browser/agent action."""
        if type(expected) is not int or expected < 0 or (
                actual is not None and (type(actual) is not int or actual < 0)):
            raise LabError("Backend counts must be nonnegative integers or an unavailable observation")
        return self.assert_backend_value(identifier, actual, expected, source=source)

    def assert_backend_value(self, identifier, actual, expected, *, source):
        """Record a typed count or boolean observed through the independent oracle."""
        if type(expected) not in (int, bool) or type(expected) is int and expected < 0 or (
                actual is not None and (type(actual) is not type(expected)
                                       or type(actual) is int and actual < 0)):
            raise LabError("Backend observations must match the expected boolean or nonnegative integer type")
        status = "observation-gap" if actual is None else "passed" if actual == expected else "failed"
        return self._record_backend_check(identifier, status, actual, expected, source)

    def assert_backend_gap(self, identifier, *, source, detail):
        """Preserve unavailable verification even when no individual count exists."""
        return self._record_backend_check(identifier, "observation-gap", None, None, source, detail)

    def _record_backend_check(self, identifier, status, actual, expected, source, detail=None):
        if not identifier or not source or any(row["id"] == identifier for row in self.backend_checks):
            raise LabError("Backend assertions need a unique identity and explicit oracle provenance")
        check = {"id": identifier, "layer": "backend", "status": status, "actual": actual,
                 "expected": expected, "source": source, "basis": "independent fixture oracle"}
        if detail is not None:
            check["detail"] = detail
        self.backend_checks.append(check)
        self.record("assertion", check)
        return status

    def finish(self, status, detail=None):
        from .sealed_events import metadata
        from .sealed_report import finalize
        execution_status = status
        if any(row["status"] == "failed" for row in self.backend_checks):
            status = "failed"
        elif status == "completed" and any(row["status"] == "observation-gap" for row in self.backend_checks):
            status = "blocked"
        verified = {row["id"]: row["actual"] for row in self.backend_checks
                    if row["status"] == "passed"}
        webmcp = getattr(self.policy, "webmcp", False) is True
        try:
            diagnostics = self.diagnostics.finish()
        except Exception:  # noqa: BLE001 — retain execution evidence when diagnostic reporting fails
            diagnostics = {"status": "failed", "audience": "developer-only", "export": "excluded"}
        reporting_status = "failed" if diagnostics["status"] in ("incomplete", "failed") else "completed"
        result = {"status": status, "execution_status": execution_status,
                  **metadata(self.path, scenario_id=self.policy.task_id, profile=self.evidence_profile,
                      driver=getattr(self, "driver", "scripted"), run_id=self.evidence_run_id, locations={
                          "inference": "selected adapter; see model lock" if self.model_lock else "unknown",
                          "browser": "local isolated worker" if self.policy.worker else "local native",
                          "target": self.policy.origins, "telemetry": "disabled",
                          "egress": EGRESS["worker" if self.policy.worker else "native"]}),
                  "reporting_status": reporting_status, "diagnostics": diagnostics,
                  "detail": detail, "driver": getattr(self, "driver", "scripted"),
                  "surface": "restricted snapshot browser surrogate", "compatibility_pass": False,
                  "run_lock": {"policy": digest(self.policy.model_dump()),
                               "harness": self.harness_lock,
                               "model": self.model_lock, "browser": self.browser_version,
                               "browser_backend": self.browser_backend,
                               **({"browser_worker": self.worker_runtime} if self.worker_runtime else {}),
                               "driver": getattr(self, "driver", "scripted"),
                               "broker": file_digest(Path(__file__)),
                               "facade": file_digest(Path(__file__).parent / "data/surface_host/browser.mjs")},
                  "observations": {"snapshot_max_chars": self.policy.snapshot_max_chars,
                                   "truncated_snapshots": self.truncated,
                                   "action_refusals": dict(sorted(self.refusals.items())),
                                   "dialogs": self.policy.dialogs,
                                   **({"webmcp_page_tool_calls": self.page_tool_calls} if webmcp else {})},
                  "checks": self.backend_checks, "verified_service_outcome": verified or None,
                  "agent_evidence": getattr(self, "agent_evidence", None),
                  **({"agent_runs": self.agent_runs} if self.agent_runs else {}),
                  **({"model_preflight_runs": self.preflight_runs} if self.preflight_runs else {}),
                  # This result's evidence-limits list (report.html "Limits").
                  "limits": ["Browser observations alone do not verify backend outcomes.",
                             "OS isolation and actual Muse calibration are not established.",
                             *([WEBMCP_LIMIT] if webmcp else [])]}
        return finalize(self.path, self.redactor.clean_evidence(result))

    async def return_control(self):
        """Host-only handback. Not in the agent's browser action dispatcher."""
        await self.host.request("browse_return_control", {})
        return await self.action({"kind": "snapshot"})

    async def refresh(self, reason):
        """A snapshot the host takes for itself (a steered task resuming): not charged to the agent."""
        self.record("host_snapshot", {"reason": reason})
        return await self.accept(await self.host.request("browse_action", {"kind": "snapshot"}))

    async def fill_secret(self, ref, generation, value, *, label):
        """Host-only credential fill, never in the agent's action schema: the facade types a declared
        secret into the referenced field while the agent's own call waits. The value joins the masked
        values of every later model view and the redacted values of evidence; the agent only names the
        credential (`label`), and evidence records the fill without the value."""
        secret = admissible_secret(value)
        if not isinstance(value, str) or secret is None:
            raise LabError(f"Credential {label} is too short or too ordinary to keep out of model views")
        forms = shown_forms(secret)
        self.redactor.add([value, *forms])
        # Longest first, so no shorter value leaves part of a longer one.
        self.secure = sorted({*self.secure, *forms}, key=len, reverse=True)
        self.count += 1
        if self.count > self.policy.max_actions:
            raise LimitExhausted("Browser action budget exhausted")
        self.record("credential_fill", {"credential_name": label, "ref": ref, "generation": generation})
        result = await self.host.request("browse_action", {"kind": "type", "ref": ref, "generation": generation,
                                                           "text": value})
        observation = await self.accept(result)
        if observation.get("status") in REFUSED:
            raise BrowserActionRefused(observation)
        return observation


async def model_preflight(broker, profile):
    """Probe native tool calling before a browser opens, so an incapable provider is
    reported as unsupported coverage rather than as an agent failure mid-task."""
    from .errors import normalize_error
    from .evidence import Evidence
    from .providers import preflight
    gate = Evidence(broker.path / "agent", lane="browser behavioral", label="model preflight",
        profile={"name": "muse-browser-public-constraint-surrogate", "surface": "browser",
                 "label": "provider native tool-call preflight"},
        lock={"harness": broker.harness_lock, "model": broker.model_lock},
        locations={"inference": "selected model adapter; see provider record", "browser": "none",
                   "target": "none", "telemetry": "disabled", "egress": "model provider only"})
    broker.agent_runs.append(str(gate.path.relative_to(broker.path)))
    broker.preflight_runs.append(broker.agent_runs[-1])
    try:
        checks = await preflight(profile, gate)
    except Exception as error:
        exc = normalize_error(error)
        status = "blocked" if exc.code == 3 else "failed"
        gate.finish(status, [{"id": "model-preflight", "status": status, "class": exc.category,
                              "detail": str(exc)}])
        broker.record("model_preflight", {"status": status, "class": exc.category, "run": gate.run_id})
        if exc is error:
            raise
        raise exc from error
    # The same rule as the scenario lane: a failed check fails, and a required check that
    # is blocked or an observation gap blocks. Either way the browser never opens.
    from .cli import problems, statuses
    code, status = statuses(checks)
    gate.finish(status, checks)
    broker.record("model_preflight", {"status": "passed" if status == "completed" else status,
                                      "run": gate.run_id})
    if status == "completed":
        return
    found = problems(checks)
    detail = "Model preflight did not pass: " + "; ".join(
        f"{row.get('id', 'check')} {row['status']}" + (f" ({row['detail']})" if row.get("detail") else "")
        for row in found)
    if status == "blocked":
        raise Unsupported(detail)
    raise LabError(detail, code=code, category=next(
        (row["class"] for row in found if row.get("class")), "setup/infrastructure"))


def report_browse(result, category, broker):
    """The run command's outcome line on stderr: status, failure class and a one-line reason."""
    from .cli import report_outcome
    reporting = result["reporting_status"] != "completed" and result["execution_status"] == "completed"
    row = {"id": "reporting" if reporting else "execution",
           "status": "blocked" if result["status"] in ("blocked", "paused") else "failed",
           "class": "reporting" if reporting else category or "unclassified",
           "detail": "Browser evidence retained; report generation is incomplete" if reporting
           else result["detail"]}
    report_outcome("Browse", result["status"], [*result["checks"], row], broker.path, broker.redactor)


async def browse_cli(args):
    policy = BrowserPolicy.model_validate(load(inside(Path.cwd(), args.policy)))
    from .release_scope import require_supported
    development = getattr(args, "development_extensions", False)
    require_supported(experimental=development, worker=policy.worker)
    broker = BrowserBroker(Path.cwd(), policy, decisions=load(
        inside(Path.cwd(), args.decisions)) if args.decisions else None)
    broker.driver = args.driver
    status, detail, category = "completed", None, None
    try:
        model_profile = None
        if args.driver == "agent":
            from .config import read_config
            config = read_config(inside(Path.cwd(), args.config))
            profile = args.model_profile or config.defaults.model_profile
            if profile not in config.model_profiles or not args.goal:
                raise LabError("Browser agent runs require a configured model and an explicit --goal")
            model_profile = config.model_profiles[profile]
            require_supported(experimental=development, config=config, model=model_profile)
            broker.model_lock = model_profile.model_dump(mode="json")
            await model_preflight(broker, model_profile)
        snapshot = await broker.open()
        if model_profile:
            from .browser_agent import agent_evidence
            from .providers import resolve_model
            evidence = agent_evidence(broker)
            try:
                model = resolve_model(model_profile, evidence)
            except BaseException as error:
                evidence.finish("failed", [{"id": "model-setup", "status": "failed", "detail": str(error)}])
                raise
            snapshot = await run_browser_agent(model, broker, args.goal, snapshot, evidence=evidence)
        elif args.actions:
            for action in load(inside(Path.cwd(), args.actions)):
                snapshot = await broker.action(action)
                if snapshot.get("status") == "human-takeover":
                    break
        if snapshot.get("status") == "human-takeover":
            status, category = "paused", "waiting"
            detail = "Human takeover requires host-controlled handback"
        print(json.dumps(broker.redactor.clean(snapshot), indent=2))
        print(f"Browser evidence: {broker.path}")
    except Paused as error:
        # A resumable stop such as an operator limit: neither an agent outcome nor a failure.
        status, detail, category = "paused", str(error), error.category
    except BaseException as error:
        failure = normalize_error(error) if isinstance(error, Exception) else None
        status = "blocked" if failure and failure.code == 3 else "failed"
        detail, category = str(error), failure.category if failure else "interrupted"
        raise
    finally:
        try:
            await broker.close()
        except BaseException as error:
            cleanup = f"Host cleanup failed: {type(error).__name__}: {error}"
            broker.record("cleanup_failure", {"detail": cleanup})
            # As in surface runs, an earlier failure keeps its own detail; the cleanup failure is
            # recorded beside it. A run that otherwise completed or paused fails, since its
            # browser may still run.
            if status in ("completed", "paused"):
                status, category = "failed", "setup/infrastructure"
                detail = cleanup if detail is None else f"{detail}; {cleanup}"
                raise
        finally:
            result = broker.finish(status, detail)
            report_browse(result, category, broker)
    if result["reporting_status"] != "completed":
        return 2
    return {"paused": 3, "blocked": 3, "failed": 1, "completed": 0}[result["status"]]
