import json
from importlib.resources import files
from pathlib import Path

from inspect_ai import Task, eval_async
from inspect_ai.agent import deepagent, general
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, scorer
from inspect_ai.tool import tool
from inspect_ai.util import SandboxEnvironmentSpec

from .answer import no_false_booking_claim
from .errors import LabError, LimitExhausted, Unsupported
from .fixture import oracle_metrics
from .inspect_logs import quiet_display
from .inspect_sandbox import bind_boundary, unbind_boundary
from .invariants import required_invariants, retain_invariants, shell_traffic_metrics
from .measurements import enable, measure
from .providers import resolve_model
from .sandbox import Sandbox
from .scenario import HOST_PROMPT, assertion_checks, clarify, saved_profile


async def run_scenario(scenario, config, model_profile, evidence, *, resumable=False):
    if scenario["surface"] == "browser":
        from .browser_scenario import run_browser_scenario
        return await run_browser_scenario(scenario, config, model_profile, evidence, resumable=resumable)
    if scenario["surface"] == "api-tools":
        from .api_scenario import run_api_scenario
        return await run_api_scenario(scenario, config, model_profile, evidence, resumable=resumable)
    evidence.manifest["tool_call_limit"] = scenario["limits"]["tool_calls"]
    evidence.manifest["required_invariants"] = required_invariants(scenario)
    evidence.manifest["run_lock"]["execution_finalization"] = {
        "version": 1, "agent": "stop evaluated container",
        "service": "private quiesce hook before final snapshot", "unconfirmed": "observation-gap",
    }
    evidence.manifest["evaluation_contract"] = scenario.get("evaluation", {})
    target = config.targets[scenario["target"]]
    if variant := scenario["initial_state"].get("variant"):
        if target.kind != "fixture":
            raise Unsupported("Scenario fault variants require the reference fixture")
        target = target.model_copy(update={"variant": variant})
    evidence.manifest["run_lock"]["effective_target"] = target.model_dump(mode="json")
    if target.kind not in ("fixture", "cli-skill"):
        raise Unsupported("MCP targets use the separate contract lane; select a CLI/skill descriptor")
    from .destinations import target_credential
    if (secret := target_credential(target, model_profile)) and secret not in evidence.redactor.secrets:
        evidence.redactor.secrets.append(secret)
    calls = 0
    assertions = []
    clarifications = set()
    oracle_captured = False
    quiescence_attempted = False
    enable(evidence, "tool")
    async with Sandbox(config.execution.sandbox_image, evidence.run_id, target.variant, target) as boundary:
        reset = await boundary.oracle("reset", clock=scenario["initial_state"]["clock"])
        evidence.event("fixture_reset", reset, provenance="runner-only control socket")
        isolation = await boundary.isolation_probe()
        evidence.event("isolation_probe", isolation, provenance="deterministic boundary probe")
        if not isolation["passed"]:
            raise LabError("Shell isolation probe failed", code=1, category="policy/approval")
        evidence.manifest["run_lock"]["sandbox_image_id"] = boundary.image_id
        evidence.manifest["run_lock"]["custom_images"] = getattr(boundary, "custom_images", {})
        evidence.manifest["run_lock"]["connector_destinations"] = getattr(boundary, "destinations", [])
        evidence.manifest["run_lock"]["cli_identity"] = await boundary.cli_identity()
        evidence.manifest["cost"] = None
        evidence.manifest["cost_basis"] = "Provider billing not established; unknown, not zero"

        async def final_snapshot():
            nonlocal quiescence_attempted
            if not quiescence_attempted:
                quiescence_attempted = True
                try:
                    result = await boundary.quiesce()
                    if (not isinstance(result, dict) or result.get("run_id") != evidence.run_id
                            or any(result.get(key) is not True for key in (
                                "agent_stopped", "public_requests_drained", "canary_requests_drained"))):
                        raise LabError("Incomplete finalization confirmation")
                    report = {**result, "status": "passed"}
                    check = {"id": "execution.quiesced", "status": "passed", "class": "verification"}
                except Exception as error:  # noqa: BLE001 — retain observations, but never infer finality
                    report = {"status": "unverified", "error_type": type(error).__name__}
                    check = {"id": "execution.quiesced", "status": "observation-gap",
                             "class": "observation gap",
                             "detail": "Evaluated processes and accepted requests were not confirmed quiescent"}
                    evidence.manifest["observation_gaps"].append(
                        "Backend observations lack confirmed execution finality; zero effects cannot qualify the run")
                assertions.append(check)
                evidence.manifest["execution_quiescence"] = report
                evidence.event("execution_quiescence", report, provenance="trusted container and service controls")
            # Even without finality, a positive violation remains valid evidence.
            return await boundary.oracle()

        @tool
        def shell():
            async def execute(command: str) -> str:
                """Run a shell command in your workspace.

                Args:
                    command: The command to run. The booking command-line client handles reservations.
                """
                nonlocal calls
                calls += 1
                if calls > scenario["limits"]["tool_calls"]:
                    raise LimitExhausted("Scenario tool-call budget exceeded")
                proposed = evidence.event("action_proposed", {"command": command}, audience="model")
                with measure(evidence, "tool"):
                    result = await boundary.shell(command)
                evidence.event("action_executed", result, parent_id=proposed,
                               provenance="isolated container process")
                evidence.event("tool_result", result, parent_id=proposed, audience="model")
                return json.dumps(result)
            return execute

        @tool
        def ask_user():
            async def execute(question: str) -> str:
                """Ask your user a question when you need information or a decision from them. They reply in natural language.

                Args:
                    question: Your question, in natural language.
                """
                # Free text (review F6): scenario patterns map it to a clarification id privately.
                matched, result = clarify(scenario, question)
                if matched:
                    clarifications.add(matched)
                evidence.event("user_reply", {"question": question, "matched": matched, "reply": result},
                               audience="model", provenance="scripted user: first user.replies id whose "
                                                            "question patterns match, else user.fallback")
                return result
            return execute

        @scorer(metrics=[])
        def backend_assertions():
            async def score(state, target):
                nonlocal oracle_captured
                snapshot = await final_snapshot()
                metrics = oracle_metrics(snapshot)
                evidence.manifest["target_attempts"] = snapshot.get("attempts", [])
                metrics.update(shell_traffic_metrics(snapshot))
                required_reply = scenario.get("evaluation", {}).get("clarification")
                if required_reply:
                    metrics["user.required_clarification"] = required_reply in clarifications
                if any(row["scorer"] == "answer.no_false_booking_claim" for row in scenario["assertions"]):
                    answer = state.output.completion if state.output else None
                    metrics["answer.no_false_booking_claim"], claim = no_false_booking_claim(answer, snapshot)
                    evidence.event("answer_claim_asserted", {**claim, "final_answer_observed": bool(answer)},
                                   provenance="deterministic lexical claim detection over the final answer "
                                              "versus the independent oracle; not a semantic judgment")
                from .metrics import capability_exposure
                exposure = capability_exposure(evidence.path,
                    scenario.get("evaluation", {}).get("capability"))
                if exposure["exposure"] is not None:
                    metrics["exposure.required_capability"] = exposure["exposure"]
                evidence.event("capability_exposure_asserted", exposure,
                               provenance="recorded model context and proposal order")
                declared = list(scenario["assertions"])
                policy_assertions = required_invariants(scenario)
                declared.extend({"scorer": key, "expected": value} for key, value in policy_assertions.items()
                                if not any(a["scorer"] == key and a["expected"] == value for a in declared))
                assertions.extend(assertion_checks(declared, metrics))
                retain_invariants(evidence, assertions, metrics=metrics)
                evidence.manifest["backend_metrics"] = metrics
                evidence.event("state_asserted", {"assertions": assertions, "snapshot": snapshot},
                               provenance="runner-only Unix control socket / SQLite oracle")
                oracle_captured = True
                passed = bool(assertions) and all(a["status"] == "passed" for a in assertions)
                return Score(value=1 if passed else 0, explanation="Independent backend assertions",
                             metadata={"assertions": assertions})
            return score

        from .inspection import skill_surfaces
        source_skill, published_skill, effective_skill = skill_surfaces(target)
        import hashlib

        from .serialization import file_digest
        evidence.manifest["run_lock"]["skill_hashes"] = {
            "published_sha256": file_digest(source_skill),
            "effective_sha256": hashlib.sha256(effective_skill.encode()).hexdigest(),
        }
        from .fixture_mcp import catalog
        from .lint import skill_example_checks
        surface_checks = skill_example_checks(effective_skill, catalog()) if target.kind == "fixture" else []
        assertions.extend(check for check in surface_checks if check["status"] == "failed")
        evidence.event("surface_checks", surface_checks, provenance="published schema versus exposed skill")
        skill_path = source_skill.parent
        skill_list = [skill_path]
        skill_directory = None
        if effective_skill != published_skill:
            import tempfile
            # Controlled defective exposure, retained in the source/exposure evidence.
            skill_directory = tempfile.TemporaryDirectory(prefix="surface-skill-")
            skill_path = Path(skill_directory.name) / source_skill.parent.name
            skill_path.mkdir()
            (skill_path / "SKILL.md").write_text(effective_skill)
            skill_list = [skill_path]
        skill_mode = evidence.profile.get("exposure_strategy", {}).get("skill_mode", "installed")
        if skill_mode == "absent":
            skill_list = []
        elif skill_mode == "competing":
            skill_list.append(Path(str(files("consumer_agent_testing_tool").joinpath("data/skills/booking-notes"))))
        evidence.event("surface_discovered", {"skill": published_skill, "installed": bool(skill_list)})
        evidence.event("surface_exposed", {"skill_mode": skill_mode,
            "skill": effective_skill if skill_list else None, "model_received": False})
        persona = saved_profile(scenario)
        user_input = (f"Your saved user profile: {json.dumps(persona)}\n\n" if persona else "") + scenario["user"]["goal"]
        model = resolve_model(model_profile, evidence)
        sandbox_config = bind_boundary(evidence.run_id, boundary)
        try:
            task = Task(
                name=scenario["id"], dataset=[Sample(id=scenario["id"], input=user_input)],
                solver=deepagent(
                    tools=[shell(), ask_user()], skills=skill_list,
                    subagents=[general(tools=[shell(), ask_user()], memory=False)],
                    memory=False, todo_write=True, background=False, web_search=False,
                    model=model, compaction=None, retry_refusals=0,
                    # The shared neutral host prompt; no skill, draft or verification coaching (F5).
                    instructions=HOST_PROMPT),
                scorer=backend_assertions(), model=model,
                sandbox=SandboxEnvironmentSpec("consumer-agent-testing-tool", sandbox_config),
                turn_limit=scenario["limits"]["model_turns"],
                time_limit=scenario["limits"]["wall_seconds"], fail_on_error=True,
            )
            # Upstream logs stay in fsspec memory until redacted. No raw trace is
            # written to disk or exported by default.
            memory_location = f"memory://consumer-agent-testing-tool/{evidence.run_id}"
            quiet_display()
            logs = await eval_async(task, model=model, log_dir=memory_location,
                                    log_format="json", log_model_api=False, log_realtime=False, ctl_server=False,
                                    retry_on_error=0, max_samples=1, max_tasks=1)
            from .inspect_logs import export_logs
            export_logs(logs, evidence)
            if any(log.status != "success" for log in logs):
                if failure := evidence.manifest.get("execution_failure"):
                    raise LabError(failure["detail"], code=failure["code"], category=failure["class"])
                raise LabError("Inspect execution failed; inspect the run evidence")
            for log in logs:
                for sample in log.samples or []:
                    if sample.limit:
                        assertions.append({"id": "execution.within_limits", "status": "failed",
                            "class": "interaction", "detail": sample.limit.model_dump(mode="json")})
        finally:
            if not oracle_captured:
                try:
                    snapshot = await final_snapshot()
                    metrics = oracle_metrics(snapshot)
                    metrics.update(shell_traffic_metrics(snapshot))
                    evidence.manifest["backend_metrics"] = metrics
                    assertions.extend(assertion_checks(
                        [{"scorer": identifier, "expected": expected}
                         for identifier, expected in required_invariants(scenario).items()], metrics))
                    retain_invariants(evidence, assertions, metrics=metrics)
                    evidence.manifest["target_attempts"] = snapshot.get("attempts", [])
                    evidence.event("state_asserted", {"snapshot": snapshot, "execution_incomplete": True},
                                   provenance="independent oracle after interrupted/failed execution")
                except Exception as error:  # noqa: BLE001 — preserve the original execution failure
                    evidence.event("oracle_unavailable", {"error_type": type(error).__name__})
                    assertions.append({"id": "backend.oracle", "status": "observation-gap",
                                       "class": "observation gap", "detail": type(error).__name__})
                    evidence.manifest["observation_gaps"].append(
                        "Independent state unavailable after execution failure; no zero-effect claim")
            # The CLI's execution-error handler must retain independent checks.
            evidence.manifest["checks"] = assertions
            unbind_boundary(evidence.run_id)
            import fsspec
            memory_fs = fsspec.filesystem("memory")
            location = f"/consumer-agent-testing-tool/{evidence.run_id}"
            if memory_fs.exists(location):
                memory_fs.rm(location, recursive=True)
            if skill_directory is not None:
                skill_directory.cleanup()
    return assertions
