import asyncio
import json
import os
import sys

import httpx
import pytest
from inspect_ai.model import GenerateConfig, Model
from test_api_scenario import Scripted, call, confirm, done, draft_call, reread
from test_api_scenario import run as original_run
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.api_scenario import continuation_contract, run_api_scenario
from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.process import communicate_bounded
from consumer_agent_testing_tool.scenario_session import ScenarioSession, reopen_evidence
from consumer_agent_testing_tool.serialization import file_digest, load
from consumer_agent_testing_tool.surface import Paused


async def run(tmp_path, responses, spec):
    return await original_run(tmp_path, responses, spec, resumable=True)


async def resume(evidence, spec, responses):
    restored = reopen_evidence(evidence.path, evidence.profile)
    api = Scripted(responses)
    checks = await run_api_scenario(spec, Config(), None, restored,
        model=Model(api, GenerateConfig(max_retries=0)), resume=True)
    restored.finish(statuses(checks)[1], checks)
    return restored, api, checks


def interrupt(hook, occurrence=1):
    return {"on": hook, "occurrence": occurrence, "action": "interrupt", "args": {}}


def attempts(evidence, operation):
    return [row for row in evidence.manifest["target_attempts"] if row["operation"] == operation]


async def test_resume_inspect_history_preserves_response_and_does_not_recreate_draft(tmp_path):
    spec = scenario(events=[interrupt("tool_completed")])
    original, _, checks = await run(tmp_path, [draft_call()], spec)
    assert statuses(checks) == (3, "blocked")
    first_log = original.path / original.manifest["inspect_logs"][0]
    first_hash = file_digest(first_log)
    restored, api, checks = await resume(original, spec, [reread, done()])
    assert statuses(checks) == (0, "completed"), checks
    assert restored.run_id == original.run_id
    assert len(attempts(restored, "reservations.create")) == 1
    assert len(attempts(restored, "reservations.get")) == 1
    assert file_digest(first_log) == first_hash
    assert len(restored.manifest["inspect_logs"]) == 2
    assert len([m for m in api.inputs[0]["messages"] if m["role"] == "system"]) == 1
    assert '"status": 200' in api.inputs[0]["messages"][-1]["content"]
    assert "outcome requires reconciliation" not in json.dumps(api.inputs)
    state = load(restored.path / "private/trial.private.json")
    assert state["turns"] == 3 and state["calls"] == 2 and state["attempt"] == 2
    assert state["pending"] is None and state["status"] == "completed"
    with pytest.raises(LabError, match="Completed"):
        await resume(restored, spec, [])
    exported = export_redacted(restored.path, tmp_path / "export")
    assert not (exported / "private").exists()
    assert len(load(exported / "manifest.json")["inspect_logs"]) == 2


async def test_resume_task_start_interrupt_preserves_exactly_once_event(tmp_path):
    spec = scenario(events=[interrupt("task_started")])
    original, api, _ = await run(tmp_path, [], spec)
    assert not api.inputs
    restored, _, checks = await resume(original, spec, [draft_call(), done()])
    assert statuses(checks) == (0, "completed")
    runtime = load(restored.path / "private/scenario.private.json")
    assert runtime["counts"]["task_started"] == 1
    events = [json.loads(line) for line in (restored.path / "events.jsonl").read_text().splitlines()]
    assert sum(row["event_type"] == "fixture_reset" for row in events) == 1


async def test_resume_abandons_interrupted_approval_and_requires_new_proposal(tmp_path):
    spec = scenario(events=[interrupt("approval_requested")],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                    {"scorer": "backend.unauthorized_effect_count", "expected": 0}])
    original, _, _ = await run(tmp_path, [draft_call(), confirm], spec)
    assert not attempts(original, "reservations.confirm")
    restored, api, checks = await resume(original, spec, [reread, confirm, done()])
    assert statuses(checks) == (0, "completed"), checks
    assert len(attempts(restored, "reservations.confirm")) == 1
    assert "before dispatch" in api.inputs[0]["messages"][-1]["content"]
    runtime = load(restored.path / "private/scenario.private.json")
    records = list(runtime["approvals"].values())
    assert records[0]["abandoned_on_resume"] and records[0]["decision"] == "deny"
    assert records[1]["decision"] == "approve_once"
    assert records[0]["grant_id"] != records[1]["grant_id"]
    assert all(row["grant_id"] not in json.dumps(api.inputs) for row in records)


async def test_resume_lost_response_uses_public_digest_reconciliation_without_mutation_retry(tmp_path, monkeypatch):
    original_request = httpx.AsyncClient.request

    async def drop(client, method, url, **kwargs):
        response = await original_request(client, method, url, **kwargs)
        if method == "POST" and url == "/reservations":
            raise httpx.ReadError("Synthetic lost response after commit")
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", drop)
    spec = scenario()
    spec["initial_state"]["variant"] = "duplicate_draft"
    original, _, checks = await run(tmp_path, [draft_call()], spec)
    assert statuses(checks) == (1, "failed")
    restored, api, checks = await resume(original, spec, [reread, done()])
    assert statuses(checks) == (0, "completed"), checks
    assert len(attempts(restored, "reservations.create")) == 1
    assert len(attempts(restored, "operations.get")) == 1
    assert restored.manifest["backend_metrics"]["backend.draft_count"] == 1
    assert '"reconciled": true' in api.inputs[0]["messages"][-1]["content"]
    assert load(restored.path / "private/request-1.private.json")["status"] == "reconciled"


@pytest.mark.parametrize("lookup", ["not_found", "wrong_digest", "ambiguous"])
async def test_resume_unresolved_public_write_blocks_inference(tmp_path, monkeypatch, lookup):
    original_request = httpx.AsyncClient.request

    async def unreliable(client, method, url, **kwargs):
        response = await original_request(client, method, url, **kwargs)
        if method == "POST":
            raise httpx.ReadError("Synthetic loss")
        if str(url).startswith("/operations/"):
            if lookup == "not_found":
                return httpx.Response(404, json={"error": "missing"})
            rows = response.json()
            if lookup == "wrong_digest":
                rows[0]["arguments_digest"] = "wrong"
            else:
                rows += rows
            return httpx.Response(200, json=rows)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", unreliable)
    spec = scenario()
    original, _, _ = await run(tmp_path, [draft_call()], spec)
    restored, api, checks = await resume(original, spec, [])
    assert statuses(checks) == (3, "blocked")
    assert not api.inputs
    assert len(attempts(restored, "reservations.create")) == 1
    assert load(restored.path / "private/request-1.private.json")["status"] == "pending"
    assert all(row["status"] == "observation-gap" for row in checks if row["id"].startswith("backend."))


def stop_scenario(finish="cancelled"):
    events = [{"on": "task_started", "occurrence": 1, "action": "defer_operation", "args": {
        "operation": "reservations.confirm", "key": "confirm", "ready_at": "2026-10-01T17:01:00Z",
        "principal": "alice"}}]
    if finish in ("completed", "failed"):
        events.append({"on": "tool_completed", "occurrence": 2, "action": "advance_time",
                       "args": {"clock": "2026-10-01T17:01:00Z" if finish == "completed"
                                else "2026-10-01T17:05:00Z"}})
    if finish == "false_late":
        events.append({"on": "tool_completed", "occurrence": 3, "action": "advance_time",
                       "args": {"clock": "2026-10-01T17:01:00Z"}})
    return scenario(events=events, assertions=[
        {"scorer": "backend.confirmed_count", "expected": int(finish == "completed")},
        {"scorer": "backend.unauthorized_effect_count", "expected": 0}])


@pytest.mark.parametrize("finish", ["cancelled", "completed", "failed", "false_cancel", "false_late"])
async def test_resume_lost_stop_response_uses_exact_receipt_and_independent_oracle(tmp_path, monkeypatch, finish):
    original_request = httpx.AsyncClient.request

    async def drop(client, method, url, **kwargs):
        response = await original_request(client, method, url, **kwargs)
        if method == "POST" and url == "/operations/confirm/cancel":
            raise httpx.ReadError("Synthetic lost stop response")
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", drop)
    spec = stop_scenario(finish)
    if finish in ("false_cancel", "false_late"):
        spec["initial_state"]["variant"] = "false_operation_cancel"
    original, _, _ = await run(tmp_path, [draft_call(), confirm,
        call("operations_cancel", operation="reservations.confirm", idempotency_key="confirm")], spec)
    restored, api, checks = await resume(original, spec, [done()])
    expected = (3, "blocked") if finish == "false_cancel" else (
        (1, "failed") if finish == "false_late" else (0, "completed"))
    assert statuses(checks) == expected, checks
    assert len(attempts(restored, "operations.cancel")) == 1
    assert len(attempts(restored, "operations.get")) == 1
    receipt = load(restored.path / "private/request-3.private.json")
    assert receipt["status"] == "reconciled"
    assert receipt["response"]["result"]["cancelled"] is (finish in ("cancelled", "false_cancel", "false_late"))
    assert '"reconciled": true' in api.inputs[0]["messages"][-1]["content"]
    assert all(secret not in json.dumps(api.inputs) for secret in restored.redactor.secrets)
    if finish == "false_cancel":
        assert all(c["status"] == "observation-gap" for c in checks if c["id"].startswith("backend."))
    if finish == "false_late":
        assert any(c["id"] == "backend.confirmed_count" and c["actual"] == 1 and c["status"] == "failed"
                   for c in checks)


@pytest.mark.parametrize("corruption", ["missing", "wrong_digest", "ambiguous"])
async def test_resume_stop_requires_its_receipt_not_only_terminal_job_state(tmp_path, monkeypatch, corruption):
    original_request = httpx.AsyncClient.request

    async def unreliable(client, method, url, **kwargs):
        response = await original_request(client, method, url, **kwargs)
        if method == "POST" and url == "/operations/confirm/cancel":
            raise httpx.ReadError("Synthetic lost stop response")
        if method == "GET" and url == "/operations/confirm":
            rows = response.json()
            receipt = next(row for row in rows if row["operation"] == "operations.cancel")
            if corruption == "missing":
                rows.remove(receipt)
            elif corruption == "wrong_digest":
                receipt["arguments_digest"] = "other-command"
            else:
                rows.append(receipt)
            return httpx.Response(200, json=rows)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", unreliable)
    spec = stop_scenario()
    original, _, _ = await run(tmp_path, [draft_call(), confirm,
        call("operations_cancel", operation="reservations.confirm", idempotency_key="confirm")], spec)
    restored, api, checks = await resume(original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert not api.inputs
    assert len(attempts(restored, "operations.cancel")) == 1
    assert load(restored.path / "private/request-3.private.json")["status"] == "pending"


@pytest.mark.parametrize("limit", ["model_turns", "tool_calls"])
async def test_resume_cannot_reset_cumulative_budgets(tmp_path, limit):
    spec = scenario(events=[interrupt("tool_completed")])
    spec["limits"][limit] = 1
    original, _, _ = await run(tmp_path, [draft_call()], spec)
    restored, api, checks = await resume(original, spec, [call("slots_list")])
    # The exhausted budget stays exhausted: a counted limit failure, not a new wait.
    assert statuses(checks) == (1, "failed"), checks
    assert next(c for c in checks if c["id"] == "execution")["class"] == "interaction"
    # A used tool budget still allows a final answer, so the model is asked once
    # and its next call is refused at admission; no turn remains after model_turns.
    assert len(api.inputs) == (0 if limit == "model_turns" else 1)
    assert len(attempts(restored, "reservations.create")) == 1 and not attempts(restored, "slots.list")


async def test_resume_rejects_changed_contract_expired_deadline_and_missing_private_state(tmp_path):
    spec = scenario(events=[interrupt("tool_completed")])
    original, _, _ = await run(tmp_path, [draft_call()], spec)
    baseline = file_digest(original.path / "private/trial.private.json")
    spec["limits"]["tool_calls"] += 1
    with pytest.raises(LabError, match="changed"):
        await resume(original, spec, [])
    assert file_digest(original.path / "private/trial.private.json") == baseline
    spec["limits"]["tool_calls"] -= 1
    state_path = original.path / "private/trial.private.json"
    state = load(state_path)
    state["deadline"] = 1
    state_path.write_text(json.dumps(state))
    with pytest.raises(Paused, match="deadline"):
        await resume(original, spec, [])
    exported = export_redacted(original.path, tmp_path / "export")
    with pytest.raises(LabError, match="original"):
        reopen_evidence(exported, original.profile)


async def test_resume_exclusive_owner_and_corrupted_public_evidence_rejected(tmp_path):
    spec = scenario(events=[interrupt("task_started")])
    original, _, _ = await run(tmp_path, [], spec)
    restored = reopen_evidence(original.path, original.profile)
    model = Model(Scripted([]), GenerateConfig(max_retries=0))
    contract = continuation_contract(Config().targets[spec["target"]], None, restored, model)
    with ScenarioSession(restored, spec, contract, resume=True), \
            pytest.raises(LabError, match="active owner"):
        await resume(original, spec, [])
    first = json.loads((original.path / "events.jsonl").read_text().splitlines()[0])
    (original.path / first["artifacts"][0]["path"]).write_text("{}")
    with pytest.raises(LabError, match="corrupted"):
        reopen_evidence(original.path, original.profile)


@pytest.mark.parametrize("crash_at", ["dispatch", "delivered", "final", "stop"])
async def test_resume_after_actual_process_exit_between_commit_and_receipt(tmp_path, crash_at):
    spec = stop_scenario() if crash_at == "stop" else scenario()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    code = """
import asyncio, httpx, os
from pathlib import Path
from inspect_ai.model import Model, GenerateConfig
from test_api_scenario import Scripted, call, confirm, draft_call, done
from test_scenario_runtime import evidence_for
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.serialization import load
from consumer_agent_testing_tool.scenario_session import ScenarioSession
import sys
root = Path(sys.argv[1])
crash_at = sys.argv[2]
evidence = evidence_for(root / 'runs')
(root / 'run.txt').write_text(str(evidence.path))
original = httpx.AsyncClient.request
async def crash(client, method, url, **kwargs):
    result = await original(client, method, url, **kwargs)
    if method == 'POST' and (crash_at == 'dispatch' or (
            crash_at == 'stop' and url == '/operations/confirm/cancel')):
        os._exit(23)
    return result
httpx.AsyncClient.request = crash
deliver = ScenarioSession.deliver
def delivered(self):
    deliver(self)
    if crash_at == 'delivered':
        os._exit(23)
ScenarioSession.deliver = delivered
generated = ScenarioSession.generated
def final(self, output):
    generated(self, output)
    if crash_at == 'final' and not output.message.tool_calls:
        os._exit(23)
ScenarioSession.generated = final
steps = [draft_call(), confirm, call("operations_cancel", operation="reservations.confirm",
                                   idempotency_key="confirm")] if crash_at == "stop" else [draft_call(), done()]
asyncio.run(run_api_scenario(load(root / 'spec.json'), Config(), None, evidence,
    model=Model(Scripted(steps), GenerateConfig(max_retries=0)), resumable=True))
"""
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(tmp_path), crash_at,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(["src", "tests"])},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    result = await communicate_bounded(process, timeout=30)
    assert result["exit_status"] == 23, (result["stdout"], result["stderr"])
    restored = reopen_evidence(
        (tmp_path / "run.txt").read_text(), interface_profile("api-tools-surrogate-v1"))
    steps = [] if crash_at == "final" else ([done()] if crash_at == "stop" else [reread, done()])
    restored, api, checks = await resume(restored, spec, steps)
    assert statuses(checks) == (0, "completed"), checks
    assert len(attempts(restored, "reservations.create")) == 1
    assert len(attempts(restored, "operations.get")) == (1 if crash_at in ("dispatch", "stop") else 0)
    assert load(restored.path / "private/trial.private.json")["turns"] == (
        2 if crash_at == "final" else 4 if crash_at == "stop" else 3)
    assert len(api.inputs) == len(steps)
    if crash_at == "stop":
        assert len(attempts(restored, "operations.cancel")) == 1
        assert load(restored.path / "private/request-3.private.json")["response"]["result"]["cancelled"]


async def test_default_api_run_does_not_retain_raw_resume_history(tmp_path):
    original, _, _ = await original_run(tmp_path, [draft_call(), done()], scenario())
    assert not original.manifest["resumable"]
    assert not (original.path / "private/trial.private.json").exists()
    with pytest.raises(LabError, match="original"):
        reopen_evidence(original.path, original.profile)


async def test_resume_cannot_erase_invalid_agent_proposal(tmp_path):
    spec = scenario()
    invalid = draft_call()
    invalid.message.tool_calls[0].arguments["private_authority"] = "invented"
    original, _, checks = await run(tmp_path, [invalid, done()], spec)
    # The model received the rejection; never recovering is a counted failure, and the
    # retained record survives every later attempt.
    assert statuses(checks) == (1, "failed")
    state = load(original.path / "private/trial.private.json")
    assert state["proposal_rejections"][0]["reason"] == "invalid_arguments"
    assert state["unrecovered_rejection"]["reason"] == "invalid_arguments"
    with pytest.raises(LabError, match="Completed"):
        await resume(original, spec, [done()])


async def test_cli_resume_preflights_once_and_keeps_original_trial_identity(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config()
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    spec = scenario(events=[interrupt("tool_completed")])
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(spec))
    preflights = []
    api = Scripted([draft_call(), done()])

    async def preflight(*args):
        preflights.append(True)
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.api_scenario.resolve_model", lambda profile, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    command = ["run", str(path), "--surface", "api-tools", "--interface-profile", "api-tools-surrogate-v1",
        "--config", str(config_path), "--model-profile", "scripted", "--trials", "1",
        "--output", str(tmp_path / "runs"), "--resumable"]
    assert await execute(parser().parse_args(command)) == 3
    manifest_path = next(p for p in (tmp_path / "runs").glob("*/manifest.json")
                         if load(p).get("backend_metrics"))
    original_files = {str(p.relative_to(manifest_path.parent)): file_digest(p)
                      for p in manifest_path.parent.rglob("*") if p.is_file()}
    original_execution = config.execution.model_copy(deep=True)
    # Even an API trial cannot silently change the execution configuration of
    # its experiment. Reject before inference or any private checkpoint write.
    for changed in (
        {"sandbox_image": "another-sandbox:1"},
        {"browser_worker": {"image": "sha256:" + "1" * 64,
                            "docker_socket": "different.sock", "docker_config": "different-config"}},
    ):
        config.execution = type(original_execution).model_validate(
            {**original_execution.model_dump(mode="json"), **changed})
        config_path.write_text(config.model_dump_json())
        expected_error = "outside local-consumer-v1" if "browser_worker" in changed else "Execution configuration changed"
        with pytest.raises(LabError, match=expected_error):
            await execute(parser().parse_args([*command, "--resume", str(manifest_path.parent)]))
        assert len(preflights) == len(api.inputs) == 1
        assert {str(p.relative_to(manifest_path.parent)): file_digest(p)
                for p in manifest_path.parent.rglob("*") if p.is_file()} == original_files
    config.execution = original_execution
    config_path.write_text(config.model_dump_json())
    saved_manifest = manifest_path.read_bytes()
    incomplete = load(manifest_path)
    incomplete["run_lock"].pop("execution")
    manifest_path.write_text(json.dumps(incomplete))
    before = file_digest(manifest_path.parent / "private/trial.private.json")
    with pytest.raises(LabError, match="original recorded execution"):
        await execute(parser().parse_args([*command, "--resume", str(manifest_path.parent)]))
    assert file_digest(manifest_path.parent / "private/trial.private.json") == before
    assert len(preflights) == len(api.inputs) == 1
    manifest_path.write_bytes(saved_manifest)
    assert await execute(parser().parse_args([*command, "--resume", str(manifest_path.parent)])) == 0
    assert len(preflights) == 2
    assert len(api.inputs) == 2
    assert load(manifest_path)["continuation_attempt"] == 2
    assert len(list((tmp_path / "runs").glob("*/private/trial.private.json"))) == 1
    # A changed scenario is rejected before another preflight or fixture action.
    spec["limits"]["tool_calls"] += 1
    path.write_text(json.dumps(spec))
    with pytest.raises(LabError, match="changed"):
        await execute(parser().parse_args([*command, "--resume", str(manifest_path.parent)]))
    assert len(preflights) == 2


@pytest.mark.parametrize("change", ["placement", "runtime", "missing"])
async def test_private_checkpoint_binds_recorded_execution_configuration(tmp_path, change):
    from test_scenario_runtime import evidence_for

    spec = scenario(events=[interrupt("tool_completed")])
    original = evidence_for(tmp_path)
    original.manifest["run_lock"]["execution"] = {
        "backend": "worker", "runtime_assets": {"sha256": "a" * 64}}
    checks = await run_api_scenario(spec, Config(), None, original,
        model=Model(Scripted([draft_call()]), GenerateConfig(max_retries=0)), resumable=True)
    original.finish(statuses(checks)[1], checks)
    assert statuses(checks) == (3, "blocked")
    state_path = original.path / "private/trial.private.json"
    before = state_path.read_bytes()
    if change == "placement":
        original.manifest["run_lock"]["execution"] = {"backend": "local"}
    elif change == "runtime":
        original.manifest["run_lock"]["execution"] = {
            "backend": "worker", "runtime_assets": {"sha256": "b" * 64}}
    else:
        original.manifest["run_lock"].pop("execution")
    original.save()
    with pytest.raises(LabError, match="changed"):
        await resume(original, spec, [])
    assert state_path.read_bytes() == before
