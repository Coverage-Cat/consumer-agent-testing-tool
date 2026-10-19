"""Actual FastMCP continuation against retained local inventory state."""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import pytest
from test_api_lease import retained  # noqa: F401
from test_api_scenario import call, done
from test_generated_scenario import buy, openapi_document
from test_mapped_resume import resume
from test_mapped_scenario import inventory, run, spec_for  # noqa: F401
from test_scenario_resume import interrupt
from test_scenario_runtime import evidence_for

from consumer_agent_testing_tool.api_tools import ValidatorWorker
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, Target, interface_profile
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.generated_scenario import initialize_mapping
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import load
from consumer_agent_testing_tool.surface import atomic_json


async def configure(store, tmp_path, *, recovery=True, poll=None):
    document = openapi_document()
    operations = {"get_price": "read", "buy_item": "commit"}
    if recovery:
        receipt = {"type": "object", "properties": {
            **{key: {"type": "string"} for key in (
                "run_id", "task_id", "principal", "operation", "arguments_sha256")},
            "response": {"type": "object", "properties": {
                "status": {"type": "integer"}, "body_base64": {"type": "string"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}}},
                "required": ["status", "body_base64", "headers"]}},
            "required": ["run_id", "task_id", "principal", "operation", "arguments_sha256", "response"]}
        document["paths"]["/receipt"] = {"get": {
            "operationId": "lookup_receipt", "description": "Read the original response for an order key.",
            "parameters": [{"in": "query", "name": "key", "required": True,
                            "description": "Original order idempotency key.", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Public receipt", "content": {
                "application/json": {"schema": receipt}}}},
        }}
        if poll:
            # FastMCP exposes one native output schema across response statuses.
            # The developer's source schema must represent the pending value.
            receipt["properties"]["response"]["type"] = ["object", "null"]
            document["paths"]["/receipt"]["get"]["responses"]["202"] = {
                "description": "Original response is pending",
                "content": {"application/json": {"schema": {"type": "object"}}}}
        operations["lookup_receipt"] = "read"
    path = tmp_path / "generated.json"
    path.write_text(json.dumps(document))
    store.target = Target(**{**store.target.model_dump(), "api_mapping": None, "openapi": {
        "path": str(path), "adapter": "fastmcp-generated", "operations": operations,
        "recovery": {"buy_item": {"lookup_tool": "lookup_receipt", "arguments": {"key": "key"},
                                **({"poll": poll} if poll else {})}}
                    if recovery else {}}})
    worker = ValidatorWorker("fastmcp", evidence_for(tmp_path / "bootstrap"), 30)
    try:
        store.mapping = await initialize_mapping(worker, store.target)
    finally:
        await worker.close()
    store.generated_receipts = True
    return store


@pytest.fixture
async def generated_retained(retained, tmp_path):
    return await configure(retained, tmp_path)


async def test_generated_resume_delivers_retained_native_result_without_dispatch(generated_retained, tmp_path):
    store = generated_retained
    spec = spec_for(events=[interrupt("tool_completed", 2)],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, checks = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert statuses(checks) == (3, "blocked"), checks
    restored, api, checks = await resume(store, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]
    assert store.controls.count("acquire_retained") == 1
    result = json.loads(api.inputs[0]["messages"][-1]["content"])
    assert result["result"]["structuredContent"] == {"status": "completed"}
    assert len(restored.manifest["inspect_logs"]) == 2
    exported = export_redacted(restored.path, tmp_path / "export")
    assert not (exported / "private").exists()


async def test_generated_resume_reconstructs_after_native_finish_loss(generated_retained, tmp_path, monkeypatch):
    store, original_request = generated_retained, ValidatorWorker.request
    dropped = False

    async def fail_finish(worker, command, **arguments):
        nonlocal dropped
        if command == "finish" and not dropped and b'"completed"' in base64.b64decode(
                arguments["response"]["body"]):
            dropped = True
            await worker.close()
            raise LabError("Synthetic native result conversion loss")
        return await original_request(worker, command, **arguments)

    monkeypatch.setattr(ValidatorWorker, "request", fail_finish)
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, checks = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert dropped and statuses(checks)[0] != 0
    restored, api, checks = await resume(store, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]
    assert json.loads(api.inputs[0]["messages"][-1]["content"])["result"]["structuredContent"] == {
        "status": "completed"}
    events = [json.loads(line) for line in (restored.path / "events.jsonl").read_text().splitlines()]
    assert any(row["event_type"] == "generated_result_reconstructed" for row in events)


async def test_generated_resume_lost_response_uses_native_public_receipt(generated_retained, tmp_path):
    store = generated_retained
    store.fault = "lost_response"
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    restored, api, checks = await resume(store, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert [row["path"] for row in store.requests] == ["/price", "/buy", "/receipt"]
    assert json.loads(api.inputs[0]["messages"][-1]["content"])["reconciled"] is True
    assert restored.manifest["backend_metrics"]["backend.confirmed_count"] == 1


async def test_generated_resume_abandons_prepared_request_and_requires_fresh_approval(generated_retained, tmp_path):
    store = generated_retained
    spec = spec_for(events=[interrupt("approval_requested")],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    assert [r["path"] for r in store.requests] == ["/price"]
    _, _, checks = await resume(store, original, spec, [call("get_price"), buy, done()])
    assert statuses(checks) == (0, "completed"), checks
    assert store.controls.count("grant") == 1
    assert [r["path"] for r in store.requests] == ["/price", "/price", "/buy"]


@pytest.mark.parametrize("patch", [
    {"principal": "wrong-account"}, {"run_id": "wrong-run"}, {"arguments_sha256": "wrong"},
    {"response": {"status": 200, "body_base64": "!!!", "headers": {}}},
    {"response": {"status": 200, "body_base64": "e30=", "headers": {"set-cookie": "synthetic-private"}}},
])
async def test_generated_resume_rejects_wrong_or_private_receipts(generated_retained, tmp_path, patch):
    store = generated_retained
    store.fault, store.receipt_patch = "lost_response", patch
    spec = spec_for()
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    _, api, checks = await resume(store, original, spec, [])
    assert statuses(checks)[0] != 0, checks
    assert api.inputs == []
    assert [row["path"] for row in store.requests] == ["/price", "/buy", "/receipt"]


async def test_generated_resume_missing_lookup_blocks_before_inference(retained, tmp_path):
    store = await configure(retained, tmp_path, recovery=False)
    store.fault = "lost_response"
    spec = spec_for()
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    _, api, checks = await resume(store, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == []
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]


async def test_generated_resume_source_change_rejected_before_worker_or_target(generated_retained, tmp_path):
    store = generated_retained
    spec = spec_for(events=[interrupt("tool_completed")])
    original, _, _ = await run(store, tmp_path, [call("get_price")], spec, resumable=True)
    path = Path(store.target.openapi.path)
    document = load(path)
    document["info"]["version"] = "changed"
    path.write_text(json.dumps(document))
    before = list(store.controls)
    with pytest.raises(LabError, match="changed"):
        await resume(store, original, spec, [])
    assert store.controls == before


@pytest.mark.parametrize("after_record", [False, True])
async def test_generated_resume_after_process_exit_around_response_checkpoint(generated_retained, tmp_path, after_record):
    from consumer_agent_testing_tool.protocol import child_environment
    store = generated_retained
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "config.json").write_text(Config(targets={"inventory": store.target}).model_dump_json())
    code = """
import asyncio, os, sys
from pathlib import Path
from inspect_ai.model import Model, GenerateConfig
from test_api_scenario import Scripted, call
from test_generated_scenario import buy
from test_scenario_runtime import evidence_for
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.generated_scenario import GeneratedAdapter
from consumer_agent_testing_tool.serialization import load
root = Path(sys.argv[1])
spec = load(root / 'spec.json')
evidence = evidence_for(root / 'runs')
evidence.scenario_id = evidence.manifest['scenario_id'] = spec['id']
(root / 'run.txt').write_text(str(evidence.path))
record = GeneratedAdapter.retain_response
def crash(self, name, arguments, checkpoint, response):
    if name != 'buy_item' or sys.argv[2] == 'True':
        record(self, name, arguments, checkpoint, response)
    if name == 'buy_item':
        os._exit(73)
GeneratedAdapter.retain_response = crash
asyncio.run(run_api_scenario(spec, Config.model_validate(load(root / 'config.json')), None, evidence,
    model=Model(Scripted([call('get_price'), buy]), GenerateConfig(max_retries=0)), resumable=True))
"""
    env = {**child_environment(), "PYTHONPATH": os.pathsep.join(["src", "tests"]),
           "INVENTORY_TOKEN": store.secret}
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(tmp_path), str(after_record),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
    assert process.returncode == 73, (stdout, stderr)
    original = reopen_evidence((tmp_path / "run.txt").read_text(), interface_profile("api-tools-surrogate-v1"))
    _, api, checks = await resume(store, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    expected = ["/price", "/buy"] + ([] if after_record else ["/receipt"])
    assert [row["path"] for row in store.requests] == expected
    assert len(api.inputs) == 1


async def test_generated_resume_rejects_native_catalog_drift_before_target(generated_retained, tmp_path, monkeypatch):
    store = generated_retained
    spec = spec_for(events=[interrupt("tool_completed")])
    original, _, _ = await run(store, tmp_path, [call("get_price")], spec, resumable=True)
    old_request = ValidatorWorker.request

    async def drift(worker, command, **arguments):
        result = await old_request(worker, command, **arguments)
        if command == "init":
            result["tools"][0]["description"] += " changed generator behavior"
        return result

    monkeypatch.setattr(ValidatorWorker, "request", drift)
    before = list(store.controls)
    with pytest.raises(LabError, match="changed"):
        await resume(store, original, spec, [])
    assert store.controls == before


@pytest.mark.parametrize("token", ["synthetic-private-token", "123"])
async def test_generated_receipt_encoded_body_is_absent_from_export(generated_retained, tmp_path, token):
    store = generated_retained
    store.fault = "lost_response"
    encoded = base64.b64encode(json.dumps({"status": "completed",
        "email": "synthetic-private@example.com", "token": token}).encode()).decode()
    store.receipt_patch = {"response": {"status": 200, "body_base64": encoded,
                                      "headers": {"content-type": "application/json"}}}
    spec = spec_for()
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    restored, _, _ = await resume(store, original, spec, [done()])
    exported = export_redacted(restored.path, tmp_path / "encoded-export")
    for path in exported.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert encoded.encode() not in data
            assert b"synthetic-private@example.com" not in data
            assert f"'token': '{token}'".encode() not in data
            assert f'"token": "{token}"'.encode() not in data
            if len(token) >= 4:
                assert token.encode() not in data


async def test_generated_cli_resume_preflights_before_resolved_worker_execution(generated_retained, tmp_path, monkeypatch):
    from inspect_ai.model import GenerateConfig, Model
    from test_api_scenario import Scripted

    from consumer_agent_testing_tool.cli import execute, parser
    from consumer_agent_testing_tool.providers import CapturingAPI
    store = generated_retained
    config = Config(targets={"inventory": store.target})
    config.model_profiles["scripted"] = ModelProfile(provider="local-glimmer", api="chat-completions",
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
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]


async def test_generated_reconstruction_rejects_changed_serialized_request(generated_retained, tmp_path, monkeypatch):
    store = generated_retained
    store.fault = "lost_response"
    spec = spec_for()
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    native = ValidatorWorker.request

    async def change(worker, command, **arguments):
        result = await native(worker, command, **arguments)
        if command == "prepare" and arguments["tool"] == "buy_item":
            result["request"]["url"] += "&unapproved=1"
        return result

    monkeypatch.setattr(ValidatorWorker, "request", change)
    _, api, checks = await resume(store, original, spec, [])
    assert statuses(checks)[0] != 0
    assert api.inputs == []
    assert "serialization changed" in json.dumps(checks)
    assert [row["path"] for row in store.requests] == ["/price", "/buy", "/receipt"]


async def test_generated_recovery_rejects_write_lookup_before_control(generated_retained, tmp_path):
    store = generated_retained
    store.target.openapi.recovery["buy_item"].lookup_tool = "buy_item"
    with pytest.raises(Unsupported, match="read-only"):
        await run(store, tmp_path, [], resumable=True)
    assert store.requests == store.controls == []


async def test_generated_resume_rejects_changed_private_wire_before_lookup(generated_retained, tmp_path):
    store = generated_retained
    store.fault = "lost_response"
    spec = spec_for()
    original, _, _ = await run(store, tmp_path, [call("get_price"), buy], spec, resumable=True)
    path = original.path / "private/generated-2.private.json"
    saved = load(path)
    saved["record"]["wire"]["url"] += "&unapproved=1"
    atomic_json(path, saved)
    _, api, checks = await resume(store, original, spec, [])
    assert statuses(checks)[0] != 0
    assert api.inputs == []
    assert "checkpoint identity or digest differs" in json.dumps(checks)
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]
