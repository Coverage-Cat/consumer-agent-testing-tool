"""Inspect browser scenarios with private fixture controls and target-enforced consent."""
import asyncio
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from inspect_ai.model import Model

from .api_scenario import assertions_for, private_control
from .browser import BrowserBroker, BrowserPolicy
from .browser_agent import BrowserTraceRedactor, run_browser_agent
from .browser_execution import execution_contract, validate_worker
from .browser_segments import publish, recover_interrupted
from .consent import effective_arguments
from .errors import LabError, LimitExhausted, Unsupported
from .fixture import fingerprint
from .fixture_mcp import public_operations
from .providers import CapturingAPI, resolve_model
from .reservation_fixture import TRANSACTIONS, ReservationFixture
from .reservation_http import route, serve
from .scenario import SCORERS, clarify, grant_seconds, require_scorers, saved_profile, validate_user
from .scenario_runtime import ScenarioRuntime
from .scenario_session import ScenarioSession
from .serialization import digest, loads
from .surface import Paused, atomic_json

PROFILE = "muse-browser-public-2026-09"
VARIANTS = {"good", "false_success", "duplicate_draft", "transient_error",
            "false_confirm", "stale_grant", "duplicate_confirm", "false_operation_cancel", "hostile_portal"}
READS = {"portal", "slots.list", "slots.get", "reservations.get", "operations.get"}
SCHEMAS = {operation: {"type": "object", "properties": properties,
                      "required": required, "additionalProperties": False}
           for operation, properties, required, _ in public_operations(True).values()}
# The fixture oracle, session ledgers and final answer; the canary only with
# hostile_portal. No lane-wide forbidden-traffic oracle exists here.
BROWSER_SCORERS = {key for key in SCORERS if not key.startswith("isolation.")}
CANARY_SCORERS = {"isolation.browser_canary_count", "isolation.browser_attempt_observed"}
# Budget messages raised before browser_agent raised LimitExhausted itself.
BUDGET_MESSAGES = {
    "Inspect browser execution reached its configured model turn limit",
    "Inspect browser execution reached its configured limit", "Cumulative browser action budget reached",
    "Cumulative browser execution budget reached", "Browser action budget exhausted",
}
# Agent proposal violations that older browser_agent versions ended a trial with.
PROPOSAL_MESSAGES = {"Browser agent must request exactly one supported action per turn",
                     "Browser proposal violates the facade schema"}


def counted(error):
    """The agent's own budget and proposal failures are counted failures, not waits or infrastructure."""
    message = str(error)
    if isinstance(error, (Paused, LabError)) and not isinstance(error, LimitExhausted) \
            and message in BUDGET_MESSAGES:
        return LimitExhausted(message)
    if isinstance(error, LabError) and error.category == "setup/infrastructure" and message in PROPOSAL_MESSAGES:
        return LabError(message, code=1, category="interpretation")
    return error


def validate_browser_scenario(scenario, config, *, retain=False):
    target = config.targets[scenario["target"]]
    if target.kind != "fixture" or scenario["surface"] != "browser" \
            or scenario["interface_profile"] != PROFILE:
        raise Unsupported("Browser scenarios require the built-in portal and muse-browser-public-2026-09")
    if scenario.get("exposure"):
        raise Unsupported("Skill exposure modes do not apply to a browser scenario")
    validate_user(scenario)
    saved_profile(scenario)
    variant = scenario["initial_state"].get("variant", target.variant)
    if variant not in VARIANTS:
        raise Unsupported("Variant has no browser scenario mapping")
    if variant == "hostile_portal" and not config.execution.browser_worker:
        raise Unsupported("Hostile browser scenarios require the explicitly selected isolated worker")
    require_scorers(scenario, BROWSER_SCORERS | (CANARY_SCORERS if variant == "hostile_portal" else set()),
                    "The browser lane" + ("" if variant == "hostile_portal" else " without hostile_portal"))
    grant_seconds(scenario)
    # Check the browser's limits before model preflight or starting a fixture.
    BrowserPolicy(url="http://127.0.0.1:1/portal", origins=["http://127.0.0.1:1"], rules=[],
        max_actions=scenario["limits"]["tool_calls"], max_model_turns=scenario["limits"]["model_turns"],
        wall_seconds=scenario["limits"]["wall_seconds"])
    validate_worker(config.execution.browser_worker)
    return target, variant


class ScenarioBrowser(BrowserBroker):
    def __init__(self, root, policy, runtime, fixture, evidence, session):
        attempt = session.state["attempt"]
        segment = "browser" if attempt == 1 else f"browser-attempt-{attempt}"
        super().__init__(root, policy, path=evidence.path / segment,
                         secrets=[*evidence.redactor.secrets,
                                  *getattr(evidence.redactor, "entered_values", ())])
        self.runtime, self.fixture, self.evidence, self.session = runtime, fixture, evidence, session
        self.requests, self.serial = {}, 0
        for path in session.private.glob("request-*.private.json"):
            record = loads(path.read_text(), json_only=True)
            self.requests[record["request_id"]] = record
            self.serial = max(self.serial, record["number"])
        self.active_action, self.terminal_error = None, None
        self.active_action_kind = None
        self.canary = None
        self.clarifications = set(session.state["clarifications"])
        self.count = session.state["calls"]
        self.admitting = True
        session.state["browser_attempt"] = {"path": segment, "status": "started",
            "run_lock": {"harness": self.harness_lock, "policy": digest(policy.model_dump()),
                         "model": evidence.manifest["run_lock"].get("model")}}
        session.persist()

    def observe(self, value):
        if value.get("kind") == "network_denied":
            value = {**value, "agent_action": self.active_action,
                     "agent_action_kind": self.active_action_kind}
            if self.canary:
                self.canary.observe(value)
        super().observe(value)

    def host_options(self):
        options = {"scenario_request_ledger": True,
                   "scenario_generation_floor": (self.session.state["attempt"] - 1) * 1000000}
        if self.session.retain:
            options["scenario_private_storage"] = True
            if storage := self.session.state.get("browser_storage"):
                options["scenario_storage_state"] = {"cookies": [], "origins": [
                    {**row, "origin": self.policy.origins[0]} for row in storage["origins"]]}
        return options

    def remember_storage(self, storage):
        if not self.session.retain or set(storage) != {"cookies", "origins"} or storage["cookies"] \
                or not isinstance(storage["origins"], list) or len(storage["origins"]) > 1 \
                or len(json.dumps(storage).encode()) > 1024 * 1024:
            raise LabError("Invalid private fixture browser storage")
        for row in storage["origins"]:
            if set(row) != {"origin", "localStorage"} or row["origin"] != self.policy.origins[0] \
                    or not isinstance(row["localStorage"], list) \
                    or any(set(item) != {"name", "value"} or not all(isinstance(value, str)
                        for value in item.values()) for item in row["localStorage"]):
                raise LabError("Browser storage belongs to a different fixture origin")
        self.session.state["browser_storage"] = storage
        self.session.persist()

    async def capture_storage(self):
        if self.session.retain:
            self.remember_storage(await self.host.request("browse_storage_state", {}))

    async def recover(self):
        """Retire undispatched requests; uncertain requests stay gated until a visible read."""
        self.runtime.resume()
        for record in self.requests.values():
            if record["status"] not in ("admitting", "admission_failed"):
                continue
            approval = self.runtime.state["approvals"].get(record["checkpoint_id"])
            if approval:
                frozen = approval["payload"]
                await self.runtime.approve(record["checkpoint_id"], frozen["envelope"], frozen["arguments"])
                if approval.get("grant_id"):
                    await self.runtime.control("revoke", grant_id=approval["grant_id"])
                approval.update(status="completed", decision="deny", abandoned_on_resume=True)
                self.runtime.persist()
            record["status"] = "denied"
            self.persist_request(record)
        pending = self.session.state["pending"]
        if pending and pending.get("rejected"):
            self.session.deliver_rejected()  # Rejected before admission: nothing ran.
            pending = None
        if pending:
            if len(pending["calls"]) != 1:
                raise LabError("Invalid saved browser proposal")
            if pending.get("hook"):
                await self.runtime.hook("tool_completed", "completed:" + pending["browser_action"])
            if pending["phase"] != "responded":
                self.session.result({"status": "interrupted", "instruction":
                    "The old browser action will not be replayed. Use the fresh snapshot; reconcile any outstanding operation."})
            self.session.deliver()
        for identifier, checkpoint in list(self.runtime.state["checkpoints"].items()):
            if not checkpoint["done"]:
                if checkpoint["hook"] == "approval_requested":
                    raise LabError("Unassociated browser approval cannot be resumed")
                await self.runtime.hook(checkpoint["hook"], identifier)

    def unresolved_writes(self):
        return [record for record in self.requests.values()
                if record["status"] == "pending" and record["operation"] not in READS]

    def persist_request(self, record):
        atomic_json(self.runtime.path.parent / f"request-{record['number']}.private.json", record)

    async def authorize(self, name, arguments):
        try:
            if name == "scenario_browser_request":
                return await self.admit(arguments)
            if name == "scenario_browser_response":
                return self.responded(arguments)
            raise LabError("Unsupported scenario callback")
        except Exception as error:
            self.terminal_error = error
            raise

    async def admit(self, request):
        if not self.admitting or self.terminal_error:
            raise Paused("Scenario is stopped")
        request = dict(request)
        if self.session.retain:
            self.remember_storage(request.pop("storage_state", {}))
        if set(request) != {"request_id", "url", "method", "body"} \
                or request["request_id"] in self.requests:
            raise LabError("Invalid or reused browser request identity")
        url = urlsplit(request["url"])
        origin = f"{url.scheme}://{url.netloc}"
        if origin != self.policy.origins[0] or url.query or url.fragment or url.username or url.password:
            raise LabError("Browser request is outside this fixture")
        if len(request["body"].encode()) > 65536:
            raise LabError("Browser request is too large")
        body = loads(request["body"], json_only=True) if request["body"] else {}
        if not isinstance(body, dict) or (request["method"] == "GET" and body):
            raise LabError("Browser fixture requests require an object body; GET has no arguments in its body")
        if request["method"] == "GET" and url.path == "/portal" and not body:
            operation, arguments = "portal", {}
        else:
            operation, arguments = route(request["method"], url.path, body)
            if operation not in SCHEMAS:
                raise LabError("No declared browser operation")
            arguments = effective_arguments(SCHEMAS[operation], arguments)
            self.fixture._validate(operation, arguments)
        if operation not in READS and self.active_action is None:
            raise LabError("A page mutation has no active agent action")
        blocked = operation not in READS and bool(self.unresolved_writes())
        self.serial += 1
        record = {"number": self.serial, "request_id": request["request_id"],
            "operation": operation, "arguments": arguments, "request": request,
            "checkpoint_id": "network:" + request["request_id"], "status": "admitting",
            "browser_action": self.active_action}
        self.requests[request["request_id"]] = record
        self.persist_request(record)
        try:
            allowed = not blocked
            if allowed and operation in TRANSACTIONS:
                envelope = self.fixture.envelope("alice", operation, arguments)
                allowed = await self.runtime.approve(record["checkpoint_id"], envelope, arguments) == "approve_once"
            # This profile explicitly permits reads, nonbinding drafts/revisions,
            # and stopping uncommitted jobs. Commit/cancel require domain grants.
            record["status"] = "pending" if allowed else "denied"
            self.persist_request(record)
            self.evidence.event("browser_request_decision", {
                "operation": operation, "arguments": arguments, "allowed": allowed,
                "request_sha256": digest(request), "checkpoint_id": record["checkpoint_id"]},
                provenance="private browser classifier and scenario approval policy")
            return {"allowed": allowed, "effect": "read" if operation in READS else "write",
                    **({"reason": "Reconcile the outstanding operation before another mutation"} if blocked else {})}
        except BaseException:
            record["status"] = "admission_failed"
            self.persist_request(record)
            raise

    def responded(self, response):
        record = self.requests.get(response["request_id"])
        if not record:
            # Rejected before admission: no request was allowed to reach the target.
            return {}
        if record["status"] in ("denied", "admission_failed"):
            return {}
        if record["status"] != "pending":
            raise LabError("Duplicate browser response checkpoint")
        status, result = response["status"], response["body"]
        record["response"] = response
        # A server error or missing JSON receipt can follow a committed write.
        # Only documented fixture responses settle mutations without a lookup.
        if (record["operation"] in READS and status is not None) or (
                status in (200, 202, 400, 401, 403, 404, 409, 415, 429) and isinstance(result, dict)):
            record["status"] = "responded"
        self.persist_request(record)
        self.evidence.event("browser_service_response", {
            "checkpoint_id": record["checkpoint_id"], "operation": record["operation"],
            "status": status, "result": result},
            provenance="browser transport response; not an agent tool result")
        if record["operation"] == "operations.get" and status == 200 and isinstance(result, list):
            key = record["arguments"]["idempotency_key"]
            for prior in self.requests.values():
                if prior["status"] == "pending" and prior["arguments"].get("idempotency_key") == key \
                        and sum(row.get("operation") == prior["operation"]
                            and row.get("arguments_digest") == fingerprint(prior["arguments"])
                            for row in result if isinstance(row, dict)) == 1:
                    prior["status"] = "reconciled"
                    prior["reconciled_by"] = record["checkpoint_id"]
                    self.persist_request(prior)
        return {}

    async def action(self, action):
        if self.terminal_error:
            raise self.terminal_error
        self.session.remaining_seconds()
        self.count = self.session.state["calls"] - 1
        self.active_action = f"browser:{self.session.state['calls']}"
        self.active_action_kind = action.get("kind")
        self.session.state["pending"]["browser_action"] = self.active_action
        self.session.persist()
        try:
            snapshot = await super().action(action)
            if self.terminal_error:
                raise self.terminal_error
            await self.capture_storage()
            self.session.result(snapshot, hook=snapshot.get("status") != "human-takeover")
            if snapshot.get("status") != "human-takeover":
                await self.runtime.hook("tool_completed", "completed:" + self.active_action)
            return snapshot
        except Exception:
            if self.terminal_error:
                raise self.terminal_error
            raise
        finally:
            self.active_action = None
            self.active_action_kind = None

    async def ask_user(self, question):
        if self.terminal_error:
            raise self.terminal_error
        self.count = self.session.state["calls"]
        if self.count > self.policy.max_actions:
            raise LimitExhausted("Scenario tool-call budget reached")
        # Free text, as in the other lanes (review F6): reply ids stay private.
        matched, reply = clarify(self.runtime.scenario, question)
        if matched:
            self.clarifications.add(matched)
            self.session.state["clarifications"] = sorted(self.clarifications)
        self.evidence.event("user_reply", {"question": question, "matched": matched, "reply": reply},
                            audience="model", provenance="scripted user: first user.replies id whose question "
                                                         "patterns match, else user.fallback")
        self.session.result(reply)
        return reply


def continuation_contract(target, model_profile, evidence, model=None, *, worker=None):
    from .scenario_session import contract_target
    return {"target": contract_target(target, evidence), "mapping": SCHEMAS, "profile": evidence.profile,
            "browser_execution": execution_contract(worker),
            "model": model_profile.model_dump(mode="json") if model_profile else {
                "injected": model.api.model_name, "config": model.config.model_dump(mode="json")}}


async def run_browser_scenario(scenario, config, model_profile, evidence, *, model=None,
                               resume=False, resumable=False):
    target, variant = validate_browser_scenario(scenario, config, retain=resume or resumable)
    if (model is None) == (model_profile is None):
        raise LabError("Select one model profile or injected model")
    worker = config.execution.browser_worker
    contract = continuation_contract(target, model_profile, evidence, model, worker=worker)
    with ScenarioSession(evidence, scenario, contract, resume=resume, retain=resumable) as session:
        return await _run_browser_scenario(scenario, model_profile, evidence, target, variant, session, model,
                                          worker=worker)


async def _run_browser_scenario(scenario, model_profile, evidence, target, variant, session, model, *, worker=None):
    session.remaining_seconds()
    evidence.redactor = BrowserTraceRedactor(evidence.redactor.secrets)
    evidence.redactor.remember(session.state["messages"])
    if session.resuming:
        recover_interrupted(session)
    evidence.manifest["run_lock"].update({
        "agent": "inspect_ai.agent.react", "browser_profile": PROFILE,
        "browser_mapping": SCHEMAS, "principal": "alice", "grant_seconds": grant_seconds(scenario),
        "browser_execution": execution_contract(worker),
        "effective_target": target.model_copy(update={"variant": variant}).model_dump(mode="json"),
        "effective_policy": {"read_draft_and_stop_pending": "allowed on the local fixture",
            "commit_and_cancel": "private scripted grants; target enforced",
            "tool_completed": "each completed browser action; excludes initial navigation and user questions",
            "fallback": "disabled", "os_isolation": "qualification-pending" if worker else "unverified"},
    })
    evidence.manifest["tool_call_limit"] = scenario["limits"]["tool_calls"]
    evidence.manifest["evaluation_contract"] = scenario.get("evaluation", {})
    evidence.manifest["observation_gaps"].append(
        "Container browser isolation requires platform and bypass qualification; real-Muse fidelity is unknown"
        if worker else "Browser request guards do not establish OS isolation or real-Muse fidelity")
    private = evidence.path / "private"
    fixture = ReservationFixture(private / "fixture.sqlite", evidence.run_id, variant, task_id=scenario["id"])
    socket = Path(".cache") / f"b-{uuid.uuid4().hex[:8]}.sock"
    checks, broker, status = [], None, "failed"
    canary_options = {"path": private / "browser-canary.sqlite", "identity": session.state["identity"],
                      "attempt": session.state["attempt"], "resume": session.resuming}
    with serve(fixture, socket, canary_options=canary_options) as server:
        evidence.redactor.secrets.extend([server.cookie, *server.tokens])

        async def control(operation, **args):
            return await private_control(server, operation, **args)

        if not session.state["initialized"]:
            evidence.event("fixture_reset", await control("reset", clock=scenario["initial_state"]["clock"]),
                           provenance="runner-only Unix control socket")
            session.state["initialized"] = True
            session.persist()
        with ScenarioRuntime(private / "scenario.private.json", scenario, control, evidence,
                             grant_seconds=grant_seconds(scenario)) as runtime:
            policy = BrowserPolicy(url=server.origin + "/portal", origins=[server.origin], rules=[],
                principal="alice", task_id=scenario["id"], max_actions=scenario["limits"]["tool_calls"],
                max_model_turns=scenario["limits"]["model_turns"], wall_seconds=scenario["limits"]["wall_seconds"],
                worker={**worker.model_dump(), "destinations": {server.origin: "127.0.0.1"}} if worker else None)
            broker = ScenarioBrowser(Path.cwd(), policy, runtime, fixture, evidence, session)
            broker.canary = server.browser_canary
            broker.driver = "agent"
            broker.model_lock = evidence.manifest["run_lock"].get("model")
            try:
                try:
                    async with asyncio.timeout(session.remaining_seconds()):
                        if session.resuming:
                            await broker.recover()
                        if server.browser_canary:
                            evidence.redactor.add([server.browser_canary.control_token])
                            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                                response = await client.get(server.browser_canary.origin + "/positive-control",
                                    headers={"X-Canary-Control": server.browser_canary.control_token})
                            if response.status_code != 200 or (
                                    await control("browser_canary_snapshot"))["positive_control_count"] != 1:
                                raise LabError("Browser canary positive control failed")
                        await runtime.hook("task_started", "task:start")
                        snapshot = await broker.open()
                        await broker.capture_storage()
                except TimeoutError as error:
                    raise Paused("Original browser scenario deadline has expired") from error
                build = {"browser": broker.browser_version, "backend": broker.browser_backend}
                if broker.worker_runtime:
                    build["worker"] = broker.worker_runtime
                if session.state.get("browser_build") and session.state["browser_build"] != build:
                    raise Paused("Browser build or MCP backend changed; continuation refused")
                session.state["browser_build"] = build
                session.persist()
                evidence.manifest["run_lock"].update(browser=broker.browser_version,
                    browser_backend=broker.browser_backend,
                    **({"browser_worker": broker.worker_runtime} if broker.worker_runtime else {}))
                active_model = resolve_model(model_profile, evidence) if model_profile else Model(
                    CapturingAPI(model.api, evidence), model.config)
                if hasattr(active_model.api, "proposed_tool_count"):
                    active_model.api.proposed_tool_count = session.state["calls"]
                saved = saved_profile(scenario)
                goal = (f"Your saved user profile: {json.dumps(saved)}\n\n" if saved else "") \
                    + scenario["user"]["goal"]
                outcome = await run_browser_agent(active_model, broker, goal, snapshot, evidence=evidence,
                    ask_user=broker.ask_user, finalize_evidence=False, session=session)
                status = "paused" if outcome.get("status") == "human-takeover" else "completed"
                if status == "completed":
                    session.state["status"] = "completed"
                    session.persist()
                checks.append({"id": "execution.within_limits",
                               "status": "blocked" if status == "paused" else "passed"})
            except (Paused, LabError) as raised:
                error = counted(raised)
                if isinstance(error, Paused):
                    status = "paused"
                    checks.append({"id": "execution", "status": "blocked", "detail": str(error)})
                else:
                    status = "paused" if error.code == 3 else "failed"
                    checks.append({"id": "execution", "status": "blocked" if error.code == 3 else "failed",
                                   "class": error.category, "detail": evidence.redactor.clean(str(error)),
                                   **session.failure_timing(error.category)})
            except Exception as error:  # noqa: BLE001 — preserve the independent oracle after browser failure
                checks.append({"id": "execution", "status": "failed", "detail": evidence.redactor.clean(str(error))})
            finally:
                if status != "paused":
                    checks.extend(session.rejection_checks())
                broker.admitting = False
                try:
                    await broker.close()
                except Exception as error:  # noqa: BLE001 — cleanup does not replace the execution result
                    checks.append({"id": "browser.cleanup", "status": "failed", "detail": type(error).__name__})
                backend, metrics = [], None
                try:
                    if server.browser_canary:
                        server.browser_canary.stop()
                    snapshot = await control("snapshot")
                    boundary = None
                    if server.browser_canary:
                        boundary = {
                            "destination": await control("browser_canary_snapshot"),
                            "denials": server.browser_canary.ledger.denials(),
                            "counter_scope": "cumulative trial; positive control is current attempt",
                            "positive_control_scope": "trusted host HTTP, not a browser/kernel bypass proof",
                            "os_isolation": "qualification-pending",
                        }
                    backend = assertions_for(scenario, snapshot, broker.clarifications, evidence, runtime,
                        catalog=evidence.manifest["run_lock"].get("browser_tools", {}), browser_boundary=boundary,
                        answer=session.final_answer())
                    metrics = evidence.manifest["backend_metrics"]
                except Exception as error:  # noqa: BLE001 — missing oracle is never a zero-effect pass
                    backend.append({"id": "backend.oracle", "status": "observation-gap",
                                    "detail": type(error).__name__})
                session.retain_invariants(backend, metrics=metrics)
                for row in backend:
                    if not row["id"].startswith(("backend.", "isolation.browser_")):
                        continue
                    source = ("retained independent oracle observation " + row["observation_event"]
                              if "observation_event" in row else "runner-only Unix fixture oracle")
                    if row["id"].startswith("isolation.browser_"):
                        source = "private canary counters and attributed browser boundary events"
                    if type(row.get("expected")) in (int, bool):
                        broker.assert_backend_value(row["id"],
                            row["actual"] if row["status"] != "observation-gap" else None,
                            row["expected"], source=source)
                    elif row["status"] == "observation-gap":
                        broker.assert_backend_gap(row["id"], source=source, detail=row.get("detail"))
                checks.extend(backend)
                report = broker.finish(status)
                if report["reporting_status"] != "completed":
                    checks.append({"id": "browser.reporting", "status": "failed", "class": "reporting",
                                   "detail": "Browser evidence retained; report generation is incomplete"})
                publish(evidence, broker.path)
                session.state["browser_attempt"]["status"] = "sealed"
                session.persist()
    return checks
