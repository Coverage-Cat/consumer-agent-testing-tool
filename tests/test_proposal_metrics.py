import copy
import json

import pytest
from inspect_ai.model import ChatMessageUser, GenerateConfig, Model, ModelAPI, ModelOutput
from inspect_ai.tool import ToolCall, ToolInfo, ToolParams
from test_scenario_runtime import evidence_for, scenario

from consumer_agent_testing_tool.metrics import recorded_events, summarize_run, summarize_trials
from consumer_agent_testing_tool.proposal_metrics import summarize, validate
from consumer_agent_testing_tool.providers import CapturingAPI

SCHEMA = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}},
          "required": ["count"], "additionalProperties": False}


def catalog(schema=SCHEMA):
    return [{"name": "lookup", "parameters": schema}]


@pytest.mark.parametrize("arguments,valid", [
    ({"count": 1}, True), ({"count": "1"}, False), ({}, False),
    ({"count": 0}, False), ({"count": True}, False), ({"count": 1, "grant": "invented"}, False),
    ({"count": float("nan")}, False), ({1: "key"}, False), ({"count": "\ud800"}, False),
])
def test_native_proposal_schema_validation_is_strict(arguments, valid):
    call = ToolCall(id="one", function="lookup", arguments={})
    call.arguments = arguments
    result = validate(call, catalog())
    assert result["valid"] is valid


def test_native_proposal_unknown_tool_and_parse_error_are_invalid():
    assert validate(ToolCall(id="one", function="hidden", arguments={}), catalog()) == {
        "valid": False, "reason": "tool_not_exposed"}
    result = validate(ToolCall(id="one", function="lookup", arguments={"count": 1},
                               parse_error="DO NOT RECORD THIS INPUT"), catalog())
    assert result == {"valid": False, "reason": "native_argument_parse_error"}
    assert validate(ToolCall(id="one", function="lookup", arguments={"count": 1}), catalog() * 2)["valid"] is None


@pytest.mark.parametrize("schema", [
    {"$ref": "http://127.0.0.1:1/private-schema"},
    {"$ref": "#/$defs/missing"},
    {"type": "object", "properties": {"count": {"type": "string", "format": "unregistered"}}},
    {"$schema": "https://unknown.invalid/dialect", "type": "object"},
    {"type": "not-a-json-schema-type"},
    {"$ref": "#"},
])
def test_unsupported_proposal_schemas_remain_unknown_without_retrieval(schema, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Proposal measurement attempted network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert validate(ToolCall(id="one", function="lookup", arguments={"count": 1}), catalog(schema))["valid"] is None


def test_proposal_validation_preserves_union_references_and_missing_defaults():
    schema = copy.deepcopy(SCHEMA)
    schema["$defs"] = {"value": {"anyOf": [{"type": "integer", "minimum": 1}, {"const": "all"}]}}
    schema["properties"]["count"] = {"$ref": "#/$defs/value", "default": "all"}
    assert validate(ToolCall(id="one", function="lookup", arguments={"count": "all"}), catalog(schema))["valid"] is True
    assert validate(ToolCall(id="one", function="lookup", arguments={}), catalog(schema))["valid"] is False
    assert schema["properties"]["count"]["default"] == "all"


def test_schema_traversal_distinguishes_keywords_from_instance_data():
    literal = {"format": "not-a-schema-format", "$ref": "https://not-a-reference.invalid"}
    schema = {"type": "object", "properties": {
        "format": {"type": "string"}, "$ref": {"type": "string"},
        "payload": {"enum": [literal], "default": literal}},
        "required": ["format", "$ref", "payload"], "additionalProperties": False}
    call = ToolCall(id="one", function="lookup", arguments={
        "format": "custom", "$ref": "instance", "payload": literal})
    assert validate(call, catalog(schema))["valid"] is True
    call.arguments["payload"] = {}
    assert validate(call, catalog(schema))["valid"] is False
    schema["properties"]["payload"] = {"$ref": "https://real-schema-reference.invalid"}
    assert validate(call, catalog(schema))["valid"] is None


class Scripted(ModelAPI):
    def __init__(self, outputs):
        super().__init__("test/proposal-measurement")
        self.outputs = iter(outputs)

    async def generate(self, *args):
        return next(self.outputs)


async def capture(evidence, api, schema=SCHEMA):
    return await api.generate([ChatMessageUser(content="Use the exposed tool.")],
        [ToolInfo(name="lookup", description="Synthetic lookup", parameters=ToolParams.model_validate(schema))],
        "auto", GenerateConfig())


def measured(evidence):
    evidence.finish("completed", [])
    return summarize_run(evidence.path, evidence.manifest)


async def test_dynamic_catalog_and_reused_call_ids_use_each_request_schema(tmp_path):
    evidence = evidence_for(tmp_path)
    outputs = [ModelOutput.for_tool_call("test", "lookup", {"count": 1}, tool_call_id="same")] * 2
    api = CapturingAPI(Scripted(outputs), evidence)
    await capture(evidence, api)
    changed = copy.deepcopy(SCHEMA)
    changed["properties"]["count"]["minimum"] = 2
    await capture(evidence, api, changed)
    result = measured(evidence)
    assert result["argument_validity"]["numerator"] == 1
    assert result["argument_validity"]["denominator"] == 2
    assert result["argument_observations"]["invalid"] == 1
    assert result["target_argument_validity"] is None


async def test_proposal_measurement_precedes_budget_denial(tmp_path):
    from consumer_agent_testing_tool.errors import LabError

    evidence = evidence_for(tmp_path)
    evidence.manifest["tool_call_limit"] = 1
    output = ModelOutput.for_tool_call("test", "lookup", {"count": 1})
    output.message.tool_calls.append(ToolCall(id="second", function="lookup", arguments={"count": "bad"}))
    with pytest.raises(LabError, match="budget"):
        await capture(evidence, CapturingAPI(Scripted([output]), evidence))
    result = measured(evidence)
    assert result["argument_validity"]["numerator"] == 1
    assert result["argument_validity"]["denominator"] == 2
    assert not any(e["event_type"] == "action_executed" for e, _ in recorded_events(evidence.path))


async def test_proposal_validation_survives_redaction_without_revalidating_redacted_values(tmp_path):
    from consumer_agent_testing_tool.evidence import export_redacted

    evidence = evidence_for(tmp_path)
    evidence.scenario_id = evidence.manifest["scenario_id"] = "redaction"
    evidence.redactor.secrets.append("secret-123")
    schema = {"type": "object", "properties": {"label": {"type": "string", "pattern": "^secret-[0-9]+$"}},
              "required": ["label"], "additionalProperties": False}
    output = ModelOutput.for_tool_call("test", "lookup", {"label": "secret-123"})
    await capture(evidence, CapturingAPI(Scripted([output]), evidence), schema)
    evidence.finish("completed", [])
    exported = export_redacted(evidence.path, tmp_path / "public")
    public = json.loads((exported / "manifest.json").read_text())
    assert public["metrics"]["argument_validity"]["rate"] == 1
    assert public["metrics"]["argument_validity"] == evidence.manifest["metrics"]["argument_validity"]
    for path in exported.rglob("*.json"):
        assert "secret-123" not in path.read_text()


@pytest.mark.parametrize("fault", ["missing", "duplicate", "id", "index", "observation", "version", "orphan"])
async def test_proposal_evidence_gaps_cannot_report_complete_validity(tmp_path, fault):
    evidence = evidence_for(tmp_path)
    output = ModelOutput.for_tool_call("test", "lookup", {"count": 1})
    await capture(evidence, CapturingAPI(Scripted([output]), evidence))
    events = list(recorded_events(evidence.path))
    row = next(row for row in events if row[0]["event_type"] == "proposal_validation")
    if fault == "missing":
        # A retained older response has no validation, alongside a new complete record.
        response = next(row for row in events if row[0]["event_type"] == "model_response")
        events.append(({**response[0], "event_id": "legacy"}, response[1]))
    elif fault == "duplicate":
        events.append(row)
    elif fault == "id":
        row[1]["proposals"][0]["call_id"] = "other"
    elif fault == "index":
        row[1]["proposals"][0]["index"] = True
    elif fault == "observation":
        row[1]["observation_event"] = "wrong"
    elif fault == "version":
        row[1]["version"] = True
    else:
        row[0]["parent_id"] = "missing-response"
    result = summarize(events)
    assert result["unknown"] or result["observation_gaps"]


async def test_invalid_api_proposal_remains_in_denominator_before_target_dispatch(tmp_path):
    from test_api_scenario import Scripted as ApiScripted
    from test_api_scenario import draft_call

    from consumer_agent_testing_tool.api_scenario import run_api_scenario
    from consumer_agent_testing_tool.cli import statuses
    from consumer_agent_testing_tool.config import Config
    evidence = evidence_for(tmp_path)
    evidence.scenario_id = evidence.manifest["scenario_id"] = "invalid-input"
    output = draft_call()
    output.message.tool_calls[0].arguments["grant_id"] = "invented-authority"
    checks = await run_api_scenario(scenario(), Config(), None, evidence,
        model=Model(ApiScripted([output]), GenerateConfig(max_retries=0)))
    assert statuses(checks) == (1, "failed")
    evidence.finish("failed", checks)
    assert evidence.manifest["target_attempts"] == []
    metrics = evidence.manifest["metrics"]
    assert metrics["argument_validity"]["numerator"] == 0
    assert metrics["argument_validity"]["denominator"] == 1
    assert metrics["target_argument_validity"]["denominator"] == 0


async def test_invalid_browser_proposal_is_measured_before_facade_dispatch(tmp_path):
    from consumer_agent_testing_tool.browser_scenario import PROFILE, run_browser_scenario
    from consumer_agent_testing_tool.cli import statuses
    from consumer_agent_testing_tool.config import Config, interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    spec = scenario(surface="browser", interface_profile=PROFILE)
    evidence = Evidence(tmp_path / "runs", lane="browser", label="synthetic model",
        profile=interface_profile(PROFILE), lock={}, scenario_id=spec["id"])
    output = ModelOutput.for_tool_call("test", "browser_action", {"kind": "evaluate", "code": "1 + 1"})
    checks = await run_browser_scenario(spec, Config(), None, evidence,
        model=Model(Scripted([output]), GenerateConfig(max_retries=0)))
    assert statuses(checks) == (1, "failed")
    evidence.finish("failed", checks)
    metrics = evidence.manifest["metrics"]
    assert metrics["argument_validity"]["numerator"] == 0
    assert metrics["argument_validity"]["denominator"] == 1
    assert metrics["timing_coverage"]["browser"]["started"] == 0
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == 0


def test_argument_summary_retains_failed_trials_and_unknown_coverage():
    def counts(valid, invalid):
        return {"version": 1, "valid": valid, "invalid": invalid, "unknown": 0,
                "proposals": valid + invalid, "responses": 1, "observed_responses": 1, "observation_gaps": 0}
    good = {"execution_status": "completed", "checks": [],
            "metrics": {"argument_observations": counts(2, 0)}}
    failed = {"execution_status": "failed", "checks": [{"status": "failed", "class": "setup/infrastructure"}],
              "metrics": {"argument_observations": counts(0, 1)}}
    result = summarize_trials([good, failed])
    assert result["argument_validity"]["numerator"] == 2
    assert result["argument_validity"]["denominator"] == 3
    result = summarize_trials([good, failed, {"execution_status": "blocked", "checks": []}])
    assert result["argument_validity"] is None
    assert result["observed_argument_validity"]["denominator"] == 3
    assert result["argument_trial_coverage"] == {"observed": 2, "complete": 2, "unmeasured": 1}
    partial = copy.deepcopy(failed)
    partial["metrics"]["argument_observations"].update(unknown=1, proposals=2)
    result = summarize_trials([good, partial])
    assert result["argument_validity"] is None
    assert result["observed_argument_validity"]["denominator"] == 3
    assert result["argument_observations"]["invalid"] == result["argument_observations"]["unknown"] == 1
    legacy = {"execution_status": "completed", "checks": [],
              "metrics": {"argument_validity": {"numerator": 10, "denominator": 10}}}
    assert summarize_trials([legacy])["argument_validity"] is None


@pytest.mark.parametrize("key,value", [
    ("version", True), ("valid", True), ("invalid", -1),
    ("unknown", "0"), ("proposals", 100), ("observed_responses", 2),
])
def test_invalid_proposal_counts_cannot_establish_summary_coverage(key, value):
    counts = {"version": 1, "valid": 1, "invalid": 0, "unknown": 0,
              "proposals": 1, "responses": 1, "observed_responses": 1, "observation_gaps": 0}
    counts[key] = value
    result = summarize_trials([{"execution_status": "completed", "checks": [],
                                "metrics": {"argument_observations": counts}}])
    assert result["argument_validity"] is None
    assert result["argument_trial_coverage"]["unmeasured"] == 1
