import asyncio
import json
import os
import sys

import pytest
from inspect_ai.model import GenerateConfig, Model, ModelOutput
from test_browser_scenario import CONFIRM, DRAFT, PortalAgent
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.browser_scenario import PROFILE, run_browser_scenario
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, interface_profile
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import file_digest, load


class PauseAgent(PortalAgent):
    async def generate(self, input, tools, tool_choice, config):
        if self.index == len(self.steps):
            self.inputs.append({"messages": [m.model_dump(mode="json") for m in input],
                                "tools": [t.model_dump(mode="json") for t in tools]})
            return ModelOutput.for_tool_call(self.model_name, "browser_action", {"kind": "takeover"})
        return await super().generate(input, tools, tool_choice, config)


def spec_for(**updates):
    spec = scenario(surface="browser", interface_profile=PROFILE, **updates)
    spec["limits"] = {"wall_seconds": 180, "model_turns": 40, "tool_calls": 60}
    return spec


async def trial(tmp_path, spec, steps, *, original=None, pause=False):
    evidence = reopen_evidence(original.path, original.profile) if original else Evidence(
        tmp_path / "runs", lane="browser", label="synthetic model", profile=interface_profile(PROFILE), lock={})
    api = (PauseAgent if pause else PortalAgent)(steps)
    checks = await run_browser_scenario(spec, Config(), None, evidence,
        model=Model(api, GenerateConfig(max_retries=0)), resumable=True, resume=bool(original))
    evidence.finish(statuses(checks)[1], checks)
    return evidence, api, checks


async def test_browser_resume_restores_storage_retires_approval_and_preserves_segments(tmp_path):
    spec = spec_for(events=[{"on": "approval_requested", "occurrence": 1, "action": "interrupt", "args": {}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                    {"scorer": "backend.duplicate_effect_count", "expected": 0}])
    original, _, initial = await trial(tmp_path, spec, DRAFT + CONFIRM)
    assert any(row["status"] == "blocked" for row in initial), initial
    old_seal = file_digest(original.path / "browser/evidence.sha256.json")
    storage = load(original.path / "private/trial.private.json")["browser_storage"]
    assert storage["cookies"] == []
    assert "Synthetic shopper" in json.dumps(storage)
    steps = [("click", "Reconcile last operation"), ("see", "outcome unknown"),
             ("click", "Refresh reservation"), *CONFIRM, ("see", ": confirmed")]
    resumed, api, checks = await trial(tmp_path, spec, steps, original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert resumed.run_id == original.run_id
    assert file_digest(original.path / "browser/evidence.sha256.json") == old_seal
    assert [r["path"] for r in resumed.manifest["browser_segments"]] == ["browser", "browser-attempt-2"]
    assert len(resumed.manifest["inspect_logs"]) == 2
    assert sum(a["operation"] == "reservations.confirm" for a in resumed.manifest["target_attempts"]) == 1
    latest = json.loads(api.inputs[0]["messages"][-1]["content"])
    assert latest["generation"] >= 1000000
    assert "Restored browser context" in latest["text"]
    assert "Synthetic shopper" not in latest["text"]
    runtime = load(resumed.path / "private/scenario.private.json")
    assert next(iter(runtime["approvals"].values()))["abandoned_on_resume"]
    exported = export_redacted(resumed.path, tmp_path / "export")
    for reference in load(exported / "manifest.json")["browser_segments"]:
        assert playback(exported / reference["path"])["integrity"] == "verified"
        assert f'{reference["path"]}/report.html' in (exported / "report.html").read_text()
    assert not list(exported.rglob("*.private.json")) and not list(exported.rglob("*.sqlite"))
    assert "Synthetic shopper" not in (exported / "browser-attempt-2/events.jsonl").read_text()


async def test_browser_resume_lost_confirmation_uses_visible_reconciliation_without_repeating_write(tmp_path):
    spec = spec_for(events=[{"on": "task_started", "occurrence": 1, "action": "lose_response",
        "args": {"operation": "reservations.confirm", "key": "confirm-draft-1-1"}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                    {"scorer": "backend.duplicate_effect_count", "expected": 0}])
    original, _, initial = await trial(tmp_path, spec,
        DRAFT + CONFIRM + [("see", "Failed to fetch")], pause=True)
    assert statuses(initial) == (3, "blocked"), initial
    resumed, api, checks = await trial(tmp_path, spec,
        [("click", "Reconcile last operation"), ("see", ": confirmed")], original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert sum(a["operation"] == "reservations.confirm" for a in resumed.manifest["target_attempts"]) == 1
    records = [load(path) for path in (resumed.path / "private").glob("request-*.private.json")]
    assert [r["status"] for r in records if r["operation"] == "reservations.confirm"] == ["reconciled"]
    visible = json.dumps(api.inputs)
    assert "scenario_browser_request" not in visible and "arguments_digest" not in visible


async def test_browser_resume_server_error_after_commit_remains_uncertain_until_public_lookup(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from consumer_agent_testing_tool.browser_scenario import serve

    @contextmanager
    def failing_response(*args, **kwargs):
        with serve(*args, **kwargs) as server:
            handler = server.http.RequestHandlerClass
            original = handler.respond

            def respond(request, status, result, **options):
                if request.command == "POST" and request.path.endswith("/confirm") and status == 200:
                    status, result = 500, {"error": "Synthetic server error after commit"}
                return original(request, status, result, **options)

            handler.respond = respond
            yield server

    monkeypatch.setattr("consumer_agent_testing_tool.browser_scenario.serve", failing_response)
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, initial = await trial(tmp_path, spec,
        DRAFT + CONFIRM + [("see", "Synthetic server error after commit")], pause=True)
    assert statuses(initial) == (3, "blocked"), initial
    writes = [load(p) for p in (original.path / "private").glob("request-*.private.json")
              if load(p)["operation"] == "reservations.confirm"]
    assert len(writes) == 1 and writes[0]["status"] == "pending"
    assert writes[0]["response"]["status"] == 500
    resumed, _, checks = await trial(tmp_path, spec,
        [("click", "Reconcile last operation"), ("see", ": confirmed")], original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert sum(a["operation"] == "reservations.confirm" for a in resumed.manifest["target_attempts"]) == 1


@pytest.mark.parametrize("receipt_available", [True, False])
async def test_browser_resume_lost_stop_response_reconciles_command_receipt(tmp_path, monkeypatch, receipt_available):
    from contextlib import contextmanager

    from consumer_agent_testing_tool.browser_scenario import serve

    @contextmanager
    def failing_response(*args, **kwargs):
        with serve(*args, **kwargs) as server:
            handler = server.http.RequestHandlerClass
            original = handler.respond

            def respond(request, status, result, **options):
                if request.command == "POST" and request.path.startswith("/operations/") and status == 200:
                    status, result = 500, {"error": "Synthetic stop response lost"}
                if not receipt_available and request.command == "GET" and isinstance(result, list):
                    result = [row for row in result if row.get("operation") != "operations.cancel"]
                return original(request, status, result, **options)

            handler.respond = respond
            yield server

    monkeypatch.setattr("consumer_agent_testing_tool.browser_scenario.serve", failing_response)
    spec = spec_for(events=[{"on": "task_started", "occurrence": 1, "action": "defer_operation", "args": {
        "operation": "reservations.confirm", "key": "confirm-draft-1-1",
        "ready_at": "2026-10-01T17:01:00Z", "principal": "alice"}}])
    original, _, initial = await trial(tmp_path, spec,
        DRAFT + CONFIRM + [("see", ": pending"), ("click", "Stop pending operation"),
                           ("see", "Synthetic stop response lost")], pause=True)
    assert statuses(initial) == (3, "blocked"), initial
    resumed, api, checks = await trial(tmp_path, spec,
        [("click", "Reconcile last operation"), ("see", ": cancelled")], original=original)
    assert statuses(checks) == ((0, "completed") if receipt_available else (3, "blocked")), checks
    assert sum(a["operation"] == "operations.cancel" for a in resumed.manifest["target_attempts"]) == 1
    writes = [load(p) for p in (resumed.path / "private").glob("request-*.private.json")
              if load(p)["operation"] == "operations.cancel"]
    assert len(writes) == 1
    assert writes[0]["status"] == ("reconciled" if receipt_available else "pending")
    assert "arguments_digest" not in json.dumps(api.inputs)
    assert resumed.manifest["backend_metrics"]["backend.confirmed_count"] == 0


async def test_browser_resume_stale_page_cannot_authorize_a_second_uncertain_write(tmp_path, monkeypatch):
    from pathlib import Path
    original_read = Path.read_text
    broken_controls = False

    def read(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if broken_controls and path.name == "reservations.html":
            text = text.replace(
                "element('confirm').disabled = restored || !reservation || reservation.state !== 'draft' || busy;",
                "element('confirm').disabled = false;")
        return text

    monkeypatch.setattr(Path, "read_text", read)
    spec = spec_for(events=[{"on": "task_started", "occurrence": 1, "action": "lose_response",
        "args": {"operation": "reservations.confirm", "key": "confirm-draft-1-1"}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                    {"scorer": "backend.duplicate_effect_count", "expected": 0}])
    original, _, _ = await trial(tmp_path, spec, DRAFT + CONFIRM + [("see", "Failed to fetch")], pause=True)
    broken_controls = True
    resumed, _, checks = await trial(tmp_path, spec, [
        *CONFIRM, ("see", "Reconcile the outstanding operation before another mutation"),
        ("click", "Reconcile last operation"), ("see", ": confirmed")], original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert sum(a["operation"] == "reservations.confirm" for a in resumed.manifest["target_attempts"]) == 1
    writes = [load(p) for p in (resumed.path / "private").glob("request-*.private.json")
              if load(p)["operation"] == "reservations.confirm"]
    assert sorted(row["status"] for row in writes) == ["denied", "reconciled"]


async def test_browser_resume_invalidates_prior_generation_references(tmp_path):
    spec = spec_for(events=[{"on": "tool_completed", "occurrence": 1, "action": "interrupt", "args": {}}])
    original, first, _ = await trial(tmp_path, spec, [("click", "Load afternoon availability")])
    messages = load(original.path / "private/trial.private.json")["messages"]
    stale = next(m["tool_calls"][0]["arguments"] for m in reversed(messages) if m.get("tool_calls"))

    class StaleAgent(PortalAgent):
        sent = False

        async def generate(self, input, tools, tool_choice, config):
            if not self.sent:
                self.sent = True
                return ModelOutput.for_tool_call(self.model_name, "browser_action", stale)
            return await super().generate(input, tools, tool_choice, config)

    evidence = reopen_evidence(original.path, original.profile)
    api = StaleAgent([("snapshot",)])
    checks = await run_browser_scenario(spec, Config(), None, evidence,
        model=Model(api, GenerateConfig(max_retries=0)), resume=True)
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks) == (0, "completed"), checks
    assert "stale-reference" in json.dumps(api.inputs[0]["messages"])
    assert sum(a["operation"] == "slots.get" for a in evidence.manifest["target_attempts"]) == 1
    assert len(first.inputs) == 1


@pytest.mark.parametrize("budget", ["model_turns", "tool_calls"])
async def test_browser_resume_keeps_cumulative_execution_budgets(tmp_path, budget):
    spec = spec_for(events=[{"on": "tool_completed", "occurrence": 1, "action": "interrupt", "args": {}}])
    spec["limits"][budget] = 1
    original, _, _ = await trial(tmp_path, spec, [("click", "Load afternoon availability")])
    # Spent tool calls still leave the agent its final answer, so it proposes once more; a spent
    # turn budget leaves no generation at all.
    steps = [("snapshot",)] if budget == "tool_calls" else []
    resumed, api, checks = await trial(tmp_path, spec, steps, original=original)
    # The exhausted budget stays exhausted: a counted limit failure, not a new wait.
    assert statuses(checks) == (1, "failed"), checks
    assert next(c for c in checks if c["id"] == "execution")["class"] == "interaction"
    assert len(api.inputs) == (1 if budget == "tool_calls" else 0)
    assert load(resumed.path / "private/trial.private.json")["calls"] == 1


async def test_browser_resume_rejects_changed_browser_build_before_inference(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.browser_scenario import ScenarioBrowser
    spec = spec_for(events=[{"on": "tool_completed", "occurrence": 1, "action": "interrupt", "args": {}}])
    original, _, _ = await trial(tmp_path, spec, [("click", "Load afternoon availability")])
    original_open = ScenarioBrowser.open

    async def changed(broker):
        snapshot = await original_open(broker)
        broker.browser_version = "unexpected-browser-version"
        return snapshot

    monkeypatch.setattr(ScenarioBrowser, "open", changed)
    _, api, checks = await trial(tmp_path, spec, [], original=original)
    assert statuses(checks) == (3, "blocked")
    assert not api.inputs
    assert any("Browser build" in c.get("detail", "") for c in checks)


async def test_browser_original_deadline_includes_browser_startup(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.browser_scenario import ScenarioBrowser
    original_open = ScenarioBrowser.open

    async def slow(broker):
        await asyncio.sleep(2)
        return await original_open(broker)

    monkeypatch.setattr(ScenarioBrowser, "open", slow)
    spec = spec_for()
    spec["limits"]["wall_seconds"] = 1
    evidence, api, checks = await trial(tmp_path, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert not api.inputs
    assert not [a for a in evidence.manifest["target_attempts"] if a["operation"] == "reservations.create"]


async def test_browser_cli_resume_preflights_and_continues_the_same_trial(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.cli import execute, parser
    from consumer_agent_testing_tool.config import ModelProfile
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config()
    config.model_profiles["test"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path, spec_path = tmp_path / "config.json", tmp_path / "spec.json"
    config_path.write_text(config.model_dump_json())
    spec = spec_for(events=[{"on": "task_started", "occurrence": 1, "action": "interrupt", "args": {}}],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    spec_path.write_text(json.dumps(spec))
    api = PortalAgent(DRAFT + CONFIRM + [("see", ": confirmed")])
    preflights = []

    async def preflight(*args):
        preflights.append(True)
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.browser_scenario.resolve_model", lambda _, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    command = ["run", str(spec_path), "--surface", "browser", "--interface-profile", PROFILE,
        "--config", str(config_path), "--model-profile", "test", "--trials", "1",
        "--resumable", "--output", str(tmp_path / "runs")]
    assert await execute(parser().parse_args(command)) != 0
    path = next(path for path in (tmp_path / "runs").glob("r-*") if (path / "private/trial.private.json").exists())
    assert await execute(parser().parse_args([*command, "--resume", str(path)])) == 0
    assert len(preflights) == 2
    assert len(load(path / "manifest.json")["browser_segments"]) == 2
    assert len(list((tmp_path / "runs").glob("*/private/trial.private.json"))) == 1


async def test_browser_resume_after_process_exit_following_confirmation_commit(tmp_path):
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                               {"scorer": "backend.duplicate_effect_count", "expected": 0}])
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    script = """
import asyncio, os, sys
from pathlib import Path
from test_browser_resume import trial
from test_browser_scenario import DRAFT, CONFIRM
from consumer_agent_testing_tool.reservation_fixture import ReservationFixture
from consumer_agent_testing_tool.surface_host import BrowserHost
from consumer_agent_testing_tool.serialization import load
root = Path(sys.argv[1])
dispatch = ReservationFixture.dispatch
def crash(self, principal, operation, args, **kwargs):
    result = dispatch(self, principal, operation, args, **kwargs)
    if operation == 'reservations.confirm':
        os._exit(23)
    return result
ReservationFixture.dispatch = crash
start = BrowserHost.start
async def started(self):
    await start(self)
    (root / 'host.txt').write_text(str(self.process.pid))
BrowserHost.start = started
asyncio.run(trial(root, load(root / 'spec.json'), DRAFT + CONFIRM))
"""
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(tmp_path),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(["src", "tests"])},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 23, (stdout, stderr)
    host_pid = int((tmp_path / "host.txt").read_text())
    for _ in range(100):
        try:
            os.kill(host_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(.1)
    else:
        os.kill(host_pid, 15)
        pytest.fail("Browser host survived its coordinator")
    path = next((tmp_path / "runs").glob("r-*"))
    original = reopen_evidence(path, interface_profile(PROFILE))
    assert not (path / "browser/evidence.sha256.json").exists()
    resumed, _, checks = await trial(tmp_path, spec,
        [("click", "Reconcile last operation"), ("see", ": confirmed")], original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert sum(a["operation"] == "reservations.confirm" for a in resumed.manifest["target_attempts"]) == 1
    assert playback(path / "browser")["result"]["execution_status"] == "interrupted"
    assert playback(path / "browser-attempt-2")["result"]["verified_service_outcome"]["backend.confirmed_count"] == 1


async def test_browser_agent_that_used_exactly_its_tool_calls_still_answers(tmp_path):
    spec = spec_for()
    spec["limits"]["tool_calls"] = 1
    evidence, api, checks = await trial(tmp_path, spec, [("click", "Load afternoon availability")])
    # Only admitting a call spends the budget, as in the API, mapped and CLI lanes.
    assert next(c for c in checks if c["id"] == "execution.within_limits")["status"] == "passed", checks
    assert not any(c["id"] == "execution" for c in checks)
    assert len(api.inputs) == 2  # The final answer came after the last admitted call.
    assert load(evidence.path / "private/trial.private.json")["calls"] == 1


class MistakenAgent(PortalAgent):
    """Opens with a proposal the facade schema rejects, then follows its steps."""
    async def generate(self, input, tools, tool_choice, config):
        if not self.inputs:
            self.inputs.append({})
            return ModelOutput.for_tool_call(self.model_name, "browser_action", {"kind": "snapshot", "offset": -1})
        return await super().generate(input, tools, tool_choice, config)


async def test_browser_rejected_proposal_spends_none_of_the_tool_budget(tmp_path):
    spec = spec_for()
    spec["limits"]["tool_calls"] = 1
    evidence = Evidence(tmp_path / "runs", lane="browser", label="synthetic model",
                        profile=interface_profile(PROFILE), lock={})
    api = MistakenAgent([("click", "Load afternoon availability")])
    checks = await run_browser_scenario(spec, Config(), None, evidence,
        model=Model(api, GenerateConfig(max_retries=0)), resumable=True)
    # Neither admission nor the provider boundary charged the rejected proposal.
    assert statuses(checks) == (0, "completed"), checks
    assert [row["reason"] for row in evidence.manifest["proposal_rejections"]] == ["invalid_arguments"]
    assert len(api.inputs) == 3 and load(evidence.path / "private/trial.private.json")["calls"] == 1


def limited_inspect(monkeypatch, kind, *, before_generation=False):
    from inspect_ai.log import EvalSampleLimit
    from inspect_ai.solver import solver

    import consumer_agent_testing_tool.browser_agent as lane
    original = lane.eval_async

    @solver
    def idle():
        async def solve(state, generate):
            return state  # Inspect's clock ran out before the agent's first generation.
        return solve

    async def limited(*args, **kwargs):
        logs = await original(*args, **({**kwargs, "solver": idle()} if before_generation else kwargs))
        logs[0].samples[0].limit = EvalSampleLimit(type=kind, limit=1)
        return logs

    monkeypatch.setattr(lane, "eval_async", limited)


@pytest.mark.parametrize("kind,expected", [
    ("operator", (3, "blocked")), ("custom", (3, "blocked")),  # A human interrupt is not the agent's outcome.
    ("turn", (1, "failed")), ("time", (1, "failed")),
])
async def test_browser_scenario_only_agent_spent_inspect_limits_are_limit_failures(tmp_path, monkeypatch,
                                                                                   kind, expected):
    from consumer_agent_testing_tool.metrics import summarize_trials
    limited_inspect(monkeypatch, kind)
    evidence, _, checks = await trial(tmp_path, spec_for(), [("click", "Load afternoon availability")])
    assert statuses(checks) == expected, checks
    execution = next(c for c in checks if c["id"] == "execution")
    assert ("class" in execution) is (expected[0] == 1)  # Paused carries no failure class.
    if expected[0] == 3:
        assert execution["detail"] == f"Inspect browser execution stopped by its {kind} limit; not an agent outcome"
    assert summarize_trials([evidence.manifest])["blocked" if expected[0] == 3 else "failed"] == 1


async def test_browser_clock_limit_before_the_first_generation_stays_resumable(tmp_path, monkeypatch):
    limited_inspect(monkeypatch, "time", before_generation=True)
    original, first, checks = await trial(tmp_path, spec_for(), [("click", "Load afternoon availability")])
    assert statuses(checks) == (3, "blocked"), checks
    assert not first.inputs and "class" not in next(c for c in checks if c["id"] == "execution")
    state = load(original.path / "private/trial.private.json")
    assert state["turns"] == 0 and not state.get("resume_blocked_reason")
    monkeypatch.undo()
    resumed, api, checks = await trial(tmp_path, spec_for(), [("click", "Load afternoon availability")],
                                       original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert resumed.run_id == original.run_id and len(api.inputs) == 2
