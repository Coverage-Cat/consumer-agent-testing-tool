import copy
import json
from pathlib import Path

import pytest
from inspect_ai.tool import ToolParams
from test_api_scenario import call, done
from test_mapped_scenario import inventory as inventory  # noqa: PLC0414 - pytest fixture re-export
from test_mapped_scenario import mapping_document, run, spec_for
from test_scenario_runtime import evidence_for

from consumer_agent_testing_tool.api_tools import ValidatorWorker
from consumer_agent_testing_tool.config import Target
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.generated_scenario import GeneratedAdapter, initialize_mapping
from consumer_agent_testing_tool.serialization import load


def openapi_document():
    source = mapping_document()
    paths = {}
    for name, row in source["operations"].items():
        paths[row["request"]["path"]] = {"get": {
            "operationId": name, "description": row["description"],
            "parameters": [{"name": key, "in": "query",
                "required": key in row["input_schema"]["required"],
                "description": value.get("description", ""), "schema": copy.deepcopy(value)}
                for key, value in row["input_schema"]["properties"].items()],
            "responses": {status: {"description": "Inventory response", "content": {
                "application/json": {"schema": copy.deepcopy(response["schema"])}}}
                for status, response in row["responses"].items()},
        }}
    return {"openapi": "3.1.0", "info": {"title": "Synthetic inventory", "version": "1"},
            "paths": paths}


@pytest.fixture
async def generated_inventory(inventory, tmp_path):
    source = tmp_path / "inventory-openapi.json"
    source.write_text(json.dumps(openapi_document()))
    inventory.target = Target(**{**inventory.target.model_dump(), "api_mapping": None,
        "openapi": {"path": str(source), "adapter": "fastmcp-generated",
                    "operations": {"get_price": "read", "buy_item": "commit"}}})
    worker = ValidatorWorker("fastmcp", evidence_for(tmp_path / "bootstrap"), 30)
    try:
        inventory.mapping = await initialize_mapping(worker, inventory.target)
    finally:
        await worker.close()
    return inventory


def buy(messages):
    row = json.loads(messages[-1].text)["result"]["structuredContent"]
    return call("buy_item", amount_minor=row["amount_minor"], revision=row["revision"], key="order-one")


async def test_generated_inspect_transaction_checks_backend_and_native_exposure(generated_inventory, tmp_path):
    store = generated_inventory
    spec = spec_for(evaluation={"capability": "get_price"}, assertions=[
        {"scorer": "backend.confirmed_count", "expected": 1},
        {"scorer": "exposure.required_capability", "expected": True}])
    evidence, model, checks = await run(store, tmp_path, [call("get_price"), buy, done()], spec)
    assert all(row["status"] == "passed" for row in checks), checks
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]
    assert store.controls.index("acquire") < store.controls.index("prepare") < store.controls.index("grant")
    lock = evidence.manifest["run_lock"]
    assert lock["api_mapping"]["adapter"] == "fastmcp-generated-v1"
    assert lock["generated_api"]["case_selection"] == "Inspect model proposals"
    assert lock["generated_api"]["versions"]["fastmcp"] == "4.1.0"
    assert evidence.manifest["metrics"]["exposure"] is True
    visible = json.dumps(model.inputs)
    for secret in (store.secret, str(store.path), "api_control", '"rules"', *store.grants.values()):
        assert secret not in visible
    schemas = {tool["name"]: tool["parameters"] for tool in model.inputs[0]["tools"]}
    for name, operation in store.mapping.operations.items():
        assert schemas[name] == ToolParams.model_validate(operation.input_schema).model_dump(mode="json")
        assert schemas[name]["additionalProperties"] is True
        native = next(t["inputSchema"] for t in lock["generated_api"]["tools"] if t["name"] == name)
        assert "additionalProperties" not in native
        assert operation.input_schema == {**native, "additionalProperties": True}
    exported = export_redacted(evidence.path, tmp_path / "export")
    for path in exported.rglob("*"):
        if path.is_file():
            assert store.secret.encode() not in path.read_bytes()
    assert model.inputs
    assert store.owner is None


async def test_generated_denied_request_is_discarded_and_next_read_works(generated_inventory, tmp_path):
    store = generated_inventory
    spec = spec_for(approvals={"mode": "scripted", "rules": [], "default": "deny"},
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 0}])
    evidence, _, checks = await run(store, tmp_path, [call("get_price"), buy, call("get_price"), done()], spec)
    assert all(row["status"] == "passed" for row in checks), checks
    assert [row["path"] for row in store.requests] == ["/price", "/price"]
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    assert any(row["event_type"] == "generated_request_discarded" for row in events)


async def test_generated_stale_price_does_not_authorize_changed_state(generated_inventory, tmp_path):
    spec = spec_for(events=[{"on": "approval_requested", "occurrence": 1, "action": "change_total",
                            "args": {"amount_minor": 13500}}],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 0}])
    _, _, checks = await run(generated_inventory, tmp_path,
                             [call("get_price"), buy, call("get_price"), buy, done()], spec)
    assert all(row["status"] == "passed" for row in checks), checks
    assert len([row for row in generated_inventory.requests if row["path"] == "/buy"]) == 1


async def test_generated_public_success_does_not_replace_backend_oracle(generated_inventory, tmp_path):
    generated_inventory.fault = "false_success"
    _, _, checks = await run(generated_inventory, tmp_path, [call("get_price"), buy, done()])
    assert any(row["id"] == "backend.confirmed_count" and row["status"] == "failed" for row in checks)


async def test_generated_lost_response_stops_without_repeating_commit(generated_inventory, tmp_path):
    generated_inventory.fault = "lost_response"
    _, model, checks = await run(generated_inventory, tmp_path,
                                 [call("get_price"), buy, call("get_price"), buy, done()])
    assert any(row["id"] == "execution" and row["status"] == "failed" for row in checks)
    assert any(row["id"] == "backend.confirmed_count" and row["status"] == "observation-gap" for row in checks)
    assert len([row for row in generated_inventory.requests if row["path"] == "/buy"]) == 1
    assert len(model.inputs) == 2


async def test_generated_mutated_prepared_wire_is_rejected(generated_inventory, tmp_path, monkeypatch):
    original = GeneratedAdapter.dispatch

    async def tamper(adapter, name, arguments, checkpoint):
        if name == "buy_item":
            adapter.prepared["proposal"]["reply"]["request"]["url"] += "&unexpected=1"
        return await original(adapter, name, arguments, checkpoint)

    monkeypatch.setattr(GeneratedAdapter, "dispatch", tamper)
    _, _, checks = await run(generated_inventory, tmp_path, [call("get_price"), buy, done()])
    assert any(row["id"] == "execution" and row["status"] == "failed" for row in checks)
    assert all(row["path"] != "/buy" for row in generated_inventory.requests)


async def test_generated_schema_loss_is_rejected_before_reset_or_inference(generated_inventory, tmp_path):
    source = load(Path(generated_inventory.target.openapi.path))
    source["paths"]["/buy"]["get"]["parameters"][0]["schema"]["multipleOf"] = 5
    Path(generated_inventory.target.openapi.path).write_text(json.dumps(source))
    with pytest.raises((Unsupported, LabError)):
        await run(generated_inventory, tmp_path, [call("get_price")])
    assert generated_inventory.controls == []
    assert generated_inventory.requests == []


async def test_generated_control_identity_mismatch_prevents_reset_and_model(generated_inventory, tmp_path):
    generated_inventory.metadata_patch = {"environment": "production"}
    _, model, checks = await run(generated_inventory, tmp_path, [call("get_price")])
    assert any(row["id"] == "execution" and row["status"] == "failed" for row in checks)
    assert model.inputs == []
    assert generated_inventory.controls == ["describe"]
    assert generated_inventory.requests == []


async def test_generated_pending_response_keeps_backend_assertions_unknown(generated_inventory, tmp_path):
    generated_inventory.fault = "pending"
    _, _, checks = await run(generated_inventory, tmp_path, [call("get_price"), buy, done()])
    assert any(row["id"] == "backend.confirmed_count" and row["status"] == "observation-gap" for row in checks)


async def test_generated_missing_material_default_cannot_receive_grant(generated_inventory, tmp_path):
    path = Path(generated_inventory.target.openapi.path)
    source = load(path)
    parameter = next(p for p in source["paths"]["/buy"]["get"]["parameters"] if p["name"] == "revision")
    parameter["required"] = False
    parameter["schema"]["default"] = 1
    path.write_text(json.dumps(source))
    worker = ValidatorWorker("fastmcp", evidence_for(tmp_path / "default-bootstrap"), 30)
    try:
        generated_inventory.mapping = await initialize_mapping(worker, generated_inventory.target)
    finally:
        await worker.close()
    _, _, checks = await run(generated_inventory, tmp_path,
        [call("buy_item", amount_minor=12500, key="order-one"), done()])
    assert any(row["id"] == "execution" and row["status"] == "blocked" for row in checks)
    assert "grant" not in generated_inventory.controls
    assert generated_inventory.requests == []
