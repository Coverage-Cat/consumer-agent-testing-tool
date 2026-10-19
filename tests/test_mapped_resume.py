"""Retained developer-API trials through actual Inspect and public HTTP."""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from inspect_ai.model import GenerateConfig, Model
from test_api_lease import retained  # noqa: F401
from test_api_scenario import Scripted, call, done
from test_mapped_scenario import buy, inventory, mapping_document, run, spec_for  # noqa: F401
from test_scenario_resume import interrupt

from consumer_agent_testing_tool.api_mapping import ApiMapping
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import file_digest, load


async def resume(store, evidence, spec, responses):
    restored = reopen_evidence(evidence.path, evidence.profile)
    api = Scripted(responses)
    checks = await run_api_scenario(spec, Config(targets={"inventory": store.target}), None,
        restored, model=Model(api, GenerateConfig(max_retries=0)), resume=True)
    restored.finish(statuses(checks)[1], checks)
    return restored, api, checks


def recovery_mapping(store):
    doc = mapping_document()
    doc["operations"]["lookup_receipt"] = {
        "operation": "orders.receipt", "description": "Read the recorded response for this order key.",
        "effect": "read", "input_schema": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Original order idempotency key."}},
            "required": ["key"], "additionalProperties": False},
        "request": {"method": "GET", "path": "/receipt", "query_parameters": {"key": "key"}},
        "responses": {"200": {"schema": {"type": "object"}}},
    }
    doc["recovery"] = {"buy_item": {"lookup_tool": "lookup_receipt", "arguments": {"key": "key"}}}
    store.mapping = ApiMapping.model_validate(doc)
    Path(store.target.api_mapping).write_text(store.mapping.model_dump_json(by_alias=True))


async def test_mapped_resume_preserves_history_state_and_original_budget(retained, tmp_path):
    spec = spec_for(events=[interrupt("tool_completed", 2)],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, checks = await run(retained, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert statuses(checks) == (3, "blocked"), checks
    log = original.path / original.manifest["inspect_logs"][0]
    checksum = file_digest(log)
    restored, api, checks = await resume(retained, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert restored.run_id == original.run_id
    assert file_digest(log) == checksum
    assert len(restored.manifest["inspect_logs"]) == 2
    assert sum(r["path"] == "/buy" for r in retained.requests) == 1
    assert retained.controls.count("acquire_retained") == 1
    assert retained.controls.count("resume") == 1
    assert retained.lease is None
    state = load(restored.path / "private/trial.private.json")
    assert state["turns"] == 3 and state["calls"] == 2 and state["attempt"] == 2
    assert state["pending"] is None and state["status"] == "completed"
    assert "completed" in api.inputs[0]["messages"][-1]["content"]
    assert "api_control" not in json.dumps(api.inputs)
    exported = export_redacted(restored.path, tmp_path / "export")
    assert not (exported / "private").exists()


async def test_mapped_resume_retires_interrupted_approval_and_requires_fresh_proposal(retained, tmp_path):
    spec = spec_for(events=[interrupt("approval_requested")],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, _ = await run(retained, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert not any(r["path"] == "/buy" for r in retained.requests)
    _, api, checks = await resume(retained, original, spec, [call("get_price"), buy, done()])
    assert statuses(checks) == (0, "completed"), checks
    assert retained.controls.count("grant") == 1
    assert sum(r["path"] == "/buy" for r in retained.requests) == 1
    assert "propose again" in api.inputs[0]["messages"][-1]["content"]


async def test_mapped_resume_reconciles_exact_public_receipt_without_repeating_commit(retained, tmp_path):
    recovery_mapping(retained)
    retained.fault = "lost_response"
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, checks = await run(retained, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert statuses(checks)[0] != 0
    assert next(c for c in checks if c["id"] == "backend.confirmed_count")["status"] == "observation-gap"
    restored, api, checks = await resume(retained, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]
    assert '"reconciled": true' in api.inputs[0]["messages"][-1]["content"]
    with sqlite3.connect(retained.path) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
    assert load(restored.path / "private/request-2.private.json")["status"] == "reconciled"


@pytest.mark.parametrize("patch", [
    {"run_id": "another-trial"}, {"principal": "another-account"}, {"task_id": "another-task"},
    {"arguments_sha256": "wrong"}, {"operation": "another-operation"},
    {"response": {"status": 503, "body": {"status": "completed"}}},
])
async def test_mapped_resume_mismatched_receipt_blocks_inference_and_new_writes(retained, tmp_path, patch):
    recovery_mapping(retained)
    retained.fault, retained.receipt_patch = "lost_response", patch
    spec = spec_for()
    original, _, _ = await run(retained, tmp_path, [call("get_price"), buy], spec, resumable=True)
    _, api, checks = await resume(retained, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == []
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]
    assert retained.controls.count("acquire_retained") == 1


async def test_mapped_resume_missing_recovery_stays_blocked_without_inference(retained, tmp_path):
    retained.fault = "lost_response"
    spec = spec_for()
    original, _, _ = await run(retained, tmp_path, [call("get_price"), buy], spec, resumable=True)
    _, api, checks = await resume(retained, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == []
    assert [r["path"] for r in retained.requests] == ["/price", "/buy"]


async def test_mapped_cli_resume_preflights_once_per_attempt(retained, tmp_path, monkeypatch):
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config(targets={"inventory": retained.target})
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path, spec_path = tmp_path / "config.json", tmp_path / "scenario.json"
    config_path.write_text(config.model_dump_json())
    spec = spec_for(events=[interrupt("tool_completed", 2)],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    spec_path.write_text(json.dumps(spec))
    preflights, api = [], Scripted([call("get_price"), buy, done()])

    async def preflight(*args):
        preflights.append(True)
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.mapped_scenario.resolve_model", lambda profile, evidence:
                        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    command = ["run", str(spec_path), "--surface", "api-tools", "--interface-profile",
        "api-tools-surrogate-v1", "--config", str(config_path), "--model-profile", "scripted",
        "--trials", "1", "--output", str(tmp_path / "runs"), "--resumable"]
    assert await execute(parser().parse_args(command)) == 3
    manifest = next(p for p in (tmp_path / "runs").glob("*/manifest.json") if load(p).get("backend_metrics"))
    assert await execute(parser().parse_args([*command, "--resume", str(manifest.parent)])) == 0
    assert len(preflights) == 2 and len(api.inputs) == 3
    assert retained.controls.count("acquire_retained") == 1
    spec["limits"]["tool_calls"] += 1
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(LabError, match="changed"):
        await execute(parser().parse_args([*command, "--resume", str(manifest.parent)]))
    assert len(preflights) == 2


async def test_mapped_resume_after_process_exit_following_commit(retained, tmp_path):
    from consumer_agent_testing_tool.protocol import child_environment
    recovery_mapping(retained)
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "config.json").write_text(Config(targets={"inventory": retained.target}).model_dump_json())
    code = """
import asyncio, os, sys
from pathlib import Path
from inspect_ai.model import Model, GenerateConfig
from test_api_scenario import Scripted, call
from test_mapped_scenario import buy
from test_scenario_runtime import evidence_for
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.mapped_scenario import MappedAdapter
from consumer_agent_testing_tool.serialization import load
root = Path(sys.argv[1])
spec = load(root / 'spec.json')
evidence = evidence_for(root / 'runs')
evidence.scenario_id = evidence.manifest['scenario_id'] = spec['id']
(root / 'run.txt').write_text(str(evidence.path))
dispatch = MappedAdapter.dispatch
async def crash(self, name, arguments, checkpoint):
    result = await dispatch(self, name, arguments, checkpoint)
    if name == 'buy_item':
        os._exit(73)
    return result
MappedAdapter.dispatch = crash
asyncio.run(run_api_scenario(spec, Config.model_validate(load(root / 'config.json')), None, evidence,
    model=Model(Scripted([call('get_price'), buy]), GenerateConfig(max_retries=0)), resumable=True))
"""
    env = {**child_environment(), "PYTHONPATH": os.pathsep.join(["src", "tests"]),
           "INVENTORY_TOKEN": retained.secret}
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(tmp_path),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 73, (stdout, stderr)
    original = reopen_evidence((tmp_path / "run.txt").read_text(), interface_profile("api-tools-surrogate-v1"))
    _, api, checks = await resume(retained, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]
    assert len(api.inputs) == 1


@pytest.mark.parametrize("limit", ["model_turns", "tool_calls"])
async def test_mapped_resume_cannot_reset_cumulative_budget(retained, tmp_path, limit):
    spec = spec_for(events=[interrupt("tool_completed")])
    spec["limits"][limit] = 1
    original, _, _ = await run(retained, tmp_path, [call("get_price")], spec, resumable=True)
    _, api, checks = await resume(retained, original, spec, [call("get_price")])
    # The exhausted budget stays exhausted: a counted limit failure, not a new wait.
    assert statuses(checks) == (1, "failed"), checks
    assert next(c for c in checks if c["id"] == "execution")["class"] == "interaction"
    # The over-budget call is refused at admission; a used turn budget leaves no generation.
    assert len(api.inputs) == (0 if limit == "model_turns" else 1)
    assert [row["path"] for row in retained.requests] == ["/price"]
    assert retained.controls.count("acquire_retained") == 1


@pytest.mark.parametrize("change", ["write_lookup", "missing_tool", "missing_input", "unknown_input"])
def test_recovery_mapping_rejects_effectful_or_incomplete_lookup(change):
    doc = mapping_document()
    doc["recovery"] = {"buy_item": {"lookup_tool": "get_price", "arguments": {}}}
    if change == "write_lookup":
        doc["operations"]["get_price"]["effect"] = "draft"
    elif change == "missing_tool":
        doc["recovery"]["buy_item"]["lookup_tool"] = "missing"
    elif change == "missing_input":
        doc["recovery"]["buy_item"] = {"lookup_tool": "buy_item", "arguments": {}}
    else:
        doc["recovery"]["buy_item"]["arguments"] = {"unknown": "key"}
    with pytest.raises(ValueError, match="Recovery"):
        ApiMapping.model_validate(doc)


async def test_mapped_resume_reconciles_lost_private_event_once(retained, tmp_path, monkeypatch):
    original_control = retained.control
    lost = False

    def drop(connection, request):
        nonlocal lost
        result = original_control(connection, request)
        if request["operation"] == "event" and not lost:
            lost = True
            connection.close()
        return result

    monkeypatch.setattr(retained, "control", drop)
    spec = spec_for(events=[{"on": "approval_requested", "occurrence": 1,
                            "action": "change_total", "args": {"amount_minor": 13500}}])
    original, _, _ = await run(retained, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert retained.revision == 2 and lost
    _, _, checks = await resume(retained, original, spec, [call("get_price"), buy, done()])
    assert statuses(checks) == (0, "completed"), checks
    assert retained.revision == 2
    assert retained.controls.count("event") == 2
    assert not any(row["path"] == "/buy" for row in retained.requests)
