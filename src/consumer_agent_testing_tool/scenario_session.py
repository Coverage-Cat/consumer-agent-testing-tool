"""Private continuation state for Inspect API/browser trials and their dispatch boundary.

Inspect still owns generation and tool scheduling. This journal retains its
messages and the unsatisfied tool call across process exits; it never executes a
saved mutation. Public reports are not a source of resumable credentials/history.
"""
import asyncio
import copy
import fcntl
import json
import os
import time
from pathlib import Path

from inspect_ai.model import ChatMessage, ChatMessageTool, ModelAPI, modelapi
from inspect_ai.tool import ToolCallError
from pydantic import TypeAdapter

from .errors import LabError, LimitExhausted
from .invariants import required_invariants
from .serialization import digest, file_digest, implementation_lock, load, loads, recordable
from .surface import Paused, atomic_json


def continuation_identity(evidence, scenario, contract):
    """Bind the trajectory to its original experiment, including execution placement."""
    return digest({"run_id": evidence.run_id, "scenario": scenario, "contract": contract,
                   "execution": evidence.manifest.get("run_lock", {}).get("execution"),
                   "implementation": implementation_lock(Path.cwd())})


def verify_execution(evidence, selected):
    """Refuse an execution switch before taking ownership or spending on preflight."""
    recorded = evidence.manifest.get("run_lock", {}).get("execution")
    if not isinstance(recorded, dict) or not isinstance(selected, dict):
        raise LabError("Continuation requires the original recorded execution configuration")
    if digest(recorded) != digest(selected):
        raise LabError("Execution configuration changed; cannot resume this trial")


def contract_target(target, evidence):
    """Name bundled worker inputs by their pinned logical paths, not a temporary job path."""
    result = target.model_dump(mode="json")
    execution = evidence.manifest.get("run_lock", {}).get("execution", {})
    if execution.get("backend") != "worker":
        return result

    def bundled(value):
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(Path.cwd())
            except ValueError as error:
                raise LabError("Worker continuation input is outside its job workspace") from error
        name = path.as_posix()
        if name not in execution.get("input_files", {}):
            raise LabError("Worker continuation input lacks its pinned bundle identity")
        return name

    for name in ("skill", "api_mapping"):
        if result.get(name):
            result[name] = bundled(result[name])
    if result.get("openapi"):
        result["openapi"]["path"] = bundled(result["openapi"]["path"])
    return result


class ScenarioSession:
    def __init__(self, evidence, scenario, contract, *, resume=False, retain=False, storage="fixture"):
        self.evidence, self.scenario = evidence, scenario
        evidence.manifest["required_invariants"] = required_invariants(scenario)
        self.failure = None
        # Set by this attempt's first generation: from then on the agent spends the budget.
        self.agent_active = False
        self.retain = retain or resume
        self.private = evidence.path / "private"
        # Reject links before opening SQLite, lock files, or checkpoints.
        for path in [evidence.path, self.private]:
            if path.is_symlink():
                raise LabError("Scenario continuation cannot use linked paths")
        self.private.mkdir(mode=0o700, exist_ok=True)
        if any(path.is_symlink() for path in self.private.rglob("*")):
            raise LabError("Scenario continuation cannot use linked private files")
        self.path = self.private / "trial.private.json"
        self.owner = os.fdopen(os.open(self.private / "trial.lock", os.O_CREAT | os.O_RDWR, 0o600), "w")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.owner.close()
            raise LabError("Scenario trial already has an active owner") from error
        identity = continuation_identity(evidence, scenario, contract)
        try:
            if resume:
                backing = "api-lease.private.json" if storage == "api" else "fixture.sqlite"
                if not self.path.is_file() or not (self.private / backing).is_file():
                    raise LabError("Scenario continuation requires its original private trial and fixture")
                self.state = load(self.path)
                if self.state.get("identity") != identity:
                    raise LabError("Scenario, model, target, execution, or implementation changed; "
                                   "cannot resume this trial")
                if self.state["status"] == "completed":
                    raise LabError("Completed trials cannot be resumed")
                if self.state.get("resume_blocked_reason"):
                    raise LabError(self.state["resume_blocked_reason"], code=1, category="interaction")
            else:
                if self.path.exists() or (self.private / "fixture.sqlite").exists():
                    raise LabError("Existing scenario state requires explicit resume")
                self.state = {"schema_version": 1, "identity": identity,
                    "contract_sha256": digest(contract), "scenario_sha256": digest(scenario),
                    "run_id": evidence.run_id, "status": "running", "attempt": 0,
                    "deadline": time.time() + scenario["limits"]["wall_seconds"],
                    "turns": 0, "calls": 0, "messages": [], "pending": None,
                    "clarifications": [], "initialized": False}
            self.resuming = resume
            self.remaining_seconds()  # A restart is not a fresh wall-time budget.
            self.state["attempt"] += 1
            if self.state["status"] != "agent_finished":
                self.state["status"] = "running"
            self.persist()
            evidence.manifest.pop("execution_failure", None)
            if resume:
                evidence.event("previous_attempt_outcome", {
                    "attempt": self.state["attempt"] - 1,
                    "execution_status": evidence.manifest["execution_status"],
                    "reporting_status": evidence.manifest["reporting_status"],
                    "checks": evidence.manifest["checks"],
                    "observation_gaps": evidence.manifest["observation_gaps"],
                    "backend_metrics": evidence.manifest.get("backend_metrics")},
                    provenance="retained prior attempt; continuation is the same trial")
                evidence.manifest["observation_gaps"] = []
            evidence.manifest.update(execution_status="running", reporting_status="pending",
                                     continuation_attempt=self.state["attempt"], resumable=self.retain)
            evidence.event("trial_continued" if resume else "trial_started", {
                "attempt": self.state["attempt"], "model_turns_used": self.state["turns"],
                "tool_calls_used": self.state["calls"]})
            evidence.save()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.owner.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def persist(self):
        if self.retain:
            atomic_json(self.path, self.state)

    def retain_invariants(self, checks, *, metrics=None):
        """Keep proven forbidden effects across every attempt, including interrupted ones."""
        from .invariants import retain_invariants

        failures = self.state.setdefault("observed_invariant_failures", {})
        retain_invariants(self.evidence, checks, metrics=metrics, failures=failures,
                          attempt=self.state["attempt"])
        self.persist()

    def verify_contract(self, contract):
        identity = continuation_identity(self.evidence, self.scenario, contract)
        if self.state["identity"] != identity:
            raise LabError("Resolved adapter, model or implementation changed after continuation admission")

    def disallow_resume(self):
        self.state["resume_blocked_reason"] = "Invalid agent proposal cannot be erased by continuing this trial"
        self.persist()

    def remaining_seconds(self):
        remaining = self.state["deadline"] - time.time()
        if remaining <= 0:
            # The agent's own turns and tools spend this budget: a counted failure.
            # Before this attempt's first generation (setup, recovery, service
            # reconciliation) the trial waits externally and stays resumable.
            raise (LimitExhausted if self.agent_active else Paused)("Original scenario deadline has expired")
        return remaining

    def begin_generation(self, messages):
        self.agent_active = True
        self.remaining_seconds()
        if self.state["pending"]:
            self.settle_rejected(messages)
        if self.state["turns"] >= self.scenario["limits"]["model_turns"]:
            raise LimitExhausted("Cumulative scenario model-turn limit reached")
        self.state["turns"] += 1  # Charge before network dispatch, including lost responses.
        self.state["messages"] = [message.model_dump(mode="json") for message in messages]
        self.persist()

    def settle_rejected(self, messages):
        """Clear a proposal the host or Inspect rejected before admission.

        Unknown tools, unparsable or schema-invalid arguments and multi-call turns
        never reach the approver's admission step: nothing was approved, charged
        or executed, and the model already holds the ordinary tool error.
        """
        pending = self.state["pending"]
        errors = {message.tool_call_id: message.error for message in messages
                  if isinstance(message, ChatMessageTool) and message.error is not None}
        if pending["phase"] != "proposed" or not all(call["id"] in errors for call in pending["calls"]):
            raise LabError("Unresolved tool proposal must be reconciled before inference")
        if "rejected" not in pending:
            error = errors[pending["calls"][0]["id"]]
            self.reject("rejected_by_inspect_" + error.type, error.message)
        self.state["pending"] = None

    def reject(self, reason, detail=None):
        """Record a proposal refused before admission; the model receives it as a tool error."""
        pending = self.state["pending"]
        if not pending or pending["phase"] != "proposed":
            raise LabError("Rejected proposal lacks its persisted Inspect message")
        if "rejected" in pending:
            return  # One record per turn, however many calls it proposed.
        pending["rejected"] = reason
        row = {"turn": self.state["turns"], "reason": reason, "calls": len(pending["calls"]),
               "functions": [call["function"] for call in pending["calls"]]}
        self.state.setdefault("proposal_rejections", []).append(row)
        self.state["unrecovered_rejection"] = row
        self.evidence.manifest["proposal_rejections"] = copy.deepcopy(self.state["proposal_rejections"])
        self.persist()
        self.evidence.event("proposal_rejected", {**row, **({"detail": str(detail)[:500]} if detail else {})},
                            provenance="host rejection before admission; returned to the model as a tool error")

    def deliver_rejected(self):
        """On resume, answer every call of a rejected turn with its rejection; nothing ran."""
        pending = self.state["pending"]
        for call in pending["calls"]:
            self.state["messages"].append(ChatMessageTool(
                tool_call_id=call["id"], function=call["function"], content="",
                error=ToolCallError("approval", f"Rejected before dispatch ({pending['rejected']}); "
                                                "nothing was executed.")).model_dump(mode="json"))
        self.state["pending"] = None
        self.persist()

    def rejection_checks(self):
        """An agent that never recovers from a rejected proposal fails as interpretation."""
        row = self.state.get("unrecovered_rejection")
        if not row:
            return []
        return [{"id": "agent.tool_proposals", "status": "failed", "class": "interpretation",
                 "detail": f"The agent's last proposal was rejected before dispatch ({row['reason']}) "
                           "and it never proposed a valid call afterwards"}]

    def failure_timing(self, category):
        """Whether a harness-side failure preceded every model request of this trial."""
        from .metrics import HARNESS_CLASSES
        return {"before_first_model_request": self.state["turns"] == 0} if category in HARNESS_CLASSES else {}

    def final_answer(self):
        """Text of the agent's last assistant message with text, if it produced one."""
        for message in reversed(self.state["messages"]):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            # Mirrors Inspect's ChatMessage.text: text parts only, never reasoning.
            text = content if isinstance(content, str) else "\n".join(
                item["text"] for item in (content if isinstance(content, list) else [])
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
            if text.strip():
                return text
        return None

    def generated(self, output):
        self.state["messages"].append(output.message.model_dump(mode="json"))
        if output.message.tool_calls:
            # A record only (admission keeps its own copy): NaN or Infinity proposals must not make
            # the journal unloadable for a later continuation.
            self.state["pending"] = {"phase": "proposed", "calls": [
                {"id": call.id, "function": call.function, "arguments": recordable(call.arguments)}
                for call in output.message.tool_calls]}
        else:
            self.state["status"] = "agent_finished"
            self.state["final_stop_reason"] = output.stop_reason
        self.persist()

    def proposed(self, name, arguments):
        self.remaining_seconds()
        if self.state["calls"] >= self.scenario["limits"]["tool_calls"]:
            raise LimitExhausted("Cumulative scenario tool-call budget reached")
        pending = self.state["pending"]
        if not pending or len(pending["calls"]) != 1 or pending["calls"][0]["function"] != name \
                or "rejected" in pending:
            raise LabError("Proposal lacks its persisted Inspect message")
        self.state["calls"] += 1
        self.state["unrecovered_rejection"] = None  # A valid admitted proposal recovers.
        pending.update(arguments=copy.deepcopy(arguments), checkpoint_id=f"proposal:{self.state['calls']}",
                       number=self.state["calls"], phase="admitting")
        self.persist()
        return pending["checkpoint_id"]

    def result(self, value, *, hook=False):
        pending = self.state["pending"]
        pending.update(phase="responded", result=value, hook=hook)
        self.persist()

    def deliver(self):
        """Attach only the response actually retained by the trusted dispatch boundary."""
        pending = self.state["pending"]
        if not pending or pending["phase"] != "responded":
            raise LabError("No retained result to deliver")
        call = pending["calls"][0]
        content = pending["result"]
        self.state["messages"].append(ChatMessageTool(
            tool_call_id=call["id"], function=call["function"],
            content=json.dumps(content)).model_dump(mode="json"))
        self.state["pending"] = None
        self.persist()

    def observe_history(self, messages):
        if self.state["pending"]:
            # A terminal error may have replaced the actual reply with "stopped"
            # inside Inspect. Retain the original result for explicit continuation.
            return
        self.state["messages"] = [message.model_dump(mode="json") for message in messages]
        self.persist()

    def input_messages(self):
        messages = TypeAdapter(list[ChatMessage]).validate_python(self.state["messages"])
        # ReAct reinstalls the locked system prompt. Never duplicate it on resume.
        return [message for message in messages if message.role != "system"]

    def wrap(self, model):
        session = self

        @modelapi(name="consumer-agent-testing-tool-continuation")
        class JournalAPI(ModelAPI):
            def __init__(self):
                super().__init__(model.api.model_name, base_url=model.api.base_url)
                self.evidence = session.evidence

            def connection_key(self):
                return model.api.connection_key()

            def max_tokens(self):
                return model.api.max_tokens()

            def should_retry(self, ex):
                # Resumable attempts are explicit; ordinary runs retain the
                # provider's existing bounded retry behavior.
                return not session.retain and model.api.should_retry(ex)

            async def generate(self, input, tools, tool_choice, config):
                try:
                    # Session failures (budgets, deadline, unreconciled proposals)
                    # are the trial's outcome, not an opaque Inspect sample error.
                    session.begin_generation(input)
                    timeout = session.remaining_seconds()
                except (LabError, Paused) as error:
                    session.failure = error
                    raise
                if hasattr(model.api, "proposed_tool_count"):
                    # Only admitted calls spend the tool-call budget: a proposal rejected before
                    # admission is not charged at the provider boundary either.
                    model.api.proposed_tool_count = session.state["calls"]
                deadline = asyncio.timeout(timeout)
                try:
                    async with deadline:
                        result = await model.api.generate(input, tools, tool_choice, config)
                    output = result[0] if isinstance(result, tuple) else result
                    if not isinstance(output, Exception):
                        session.generated(output)
                    return result
                except TimeoutError as error:
                    if not deadline.expired():
                        raise  # A provider timeout, not this trial's deadline.
                    session.failure = LimitExhausted("Original scenario deadline has expired")
                    raise session.failure from error
                except Paused as error:
                    session.failure = error
                    raise
                except LabError:
                    if session.evidence.manifest.get("execution_failure", {}).get("code") == 1:
                        session.disallow_resume()
                    raise

        return type(model)(JournalAPI(), model.config)


def reopen_evidence(path, profile):
    """Read-only restoration. The session obtains ownership before appending."""
    from .evidence import Evidence, Redactor
    path = Path(path)
    required = [path, path / "manifest.json", path / "events.jsonl", path / "artifacts",
                path / "private", path / "private/trial.private.json"]
    if any(item.is_symlink() or not item.exists() for item in required):
        raise LabError("Resume requires the original run directory and private checkpoints")
    manifest = load(path / "manifest.json")
    backing = "api-lease.private.json" if manifest.get("run_lock", {}).get(
        "effective_target", {}).get("kind") == "api" else "fixture.sqlite"
    if not (path / "private" / backing).is_file() or (path / "private" / backing).is_symlink():
        raise LabError("Resume requires the original private fixture or retained API lease")
    if manifest.get("run_id") != path.name:
        raise LabError("Resume run identity does not match its directory")
    sequence = 0
    for line in (path / "events.jsonl").read_text().splitlines():
        event = loads(line, json_only=True)
        sequence += 1
        if event.get("run_id") != path.name or event.get("event_id") != f"{path.name}:{sequence}":
            raise LabError("Resume event history is incomplete or belongs to another run")
        for reference in event["artifacts"]:
            artifact = path / "artifacts" / f"{reference['sha256']}.json"
            if reference["path"] != f"artifacts/{reference['sha256']}.json" \
                    or artifact.is_symlink() or file_digest(artifact) != reference["sha256"]:
                raise LabError("Resume evidence artifact is missing or corrupted")
    for index, reference in enumerate(manifest.get("inspect_log_artifacts", [])):
        relative = f"inspect/trial-{index}.eval"
        log = path / relative
        if reference.get("path") != relative or (path / "inspect").is_symlink() \
                or log.is_symlink() or file_digest(log) != reference["sha256"]:
            raise LabError("Resume Inspect log is missing or corrupted")
    from .browser_segments import references, verify
    for reference in references(manifest):
        verify(path, reference)
    evidence = Evidence.__new__(Evidence)
    evidence.path, evidence.run_id = path, path.name
    evidence.redactor, evidence.sequence = Redactor.environment(), sequence
    evidence.profile, evidence.scenario_id = profile, manifest.get("scenario_id")
    evidence.manifest = manifest
    return evidence
