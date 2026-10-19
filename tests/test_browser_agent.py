import asyncio
import json

import fsspec
import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
from inspect_ai.tool import ToolCall
from test_browser_portals import portal, reference  # noqa: F401 — shared local fixture

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy, run_browser_agent
from consumer_agent_testing_tool.browser_agent import agent_evidence
from consumer_agent_testing_tool.errors import LabError, LimitExhausted
from consumer_agent_testing_tool.providers import CapturingAPI


class BrowserDouble:
    def __init__(self):
        self.calls = []

    async def request(self, method, arguments):
        assert method == "browse_action"
        self.calls.append(arguments)
        if arguments["kind"] == "takeover":
            return {"status": "human-takeover"}
        return {"generation": len(self.calls) + 1, "text": "Current synthetic snapshot"}


def make_broker(tmp_path, **policy):
    return BrowserBroker(tmp_path, BrowserPolicy(
        url="http://127.0.0.1:8765", origins=["http://127.0.0.1:8765"], rules=[], **policy),
        host=BrowserDouble())


class Scripted(ModelAPI):
    def __init__(self, responses):
        super().__init__("test/browser")
        self.responses, self.inputs = iter(responses), []

    async def generate(self, input, tools, tool_choice, config):
        self.inputs.append({"messages": [message.model_dump(mode="json") for message in input],
                            "tools": [tool.model_dump(mode="json") for tool in tools]})
        return next(self.responses)


def call(kind="snapshot", identifier="step-1", **arguments):
    return ModelOutput.for_tool_call("test/browser", "browser_action",
                                    {"kind": kind, **arguments}, tool_call_id=identifier)


def model_for(broker, responses):
    evidence = agent_evidence(broker)
    api = Scripted(responses)
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    return model, api, evidence


def manifest(broker):
    return json.loads((broker.path / broker.agent_evidence / "manifest.json").read_text())


async def test_real_inspect_react_browser_tools_continuation_and_logs(tmp_path):
    broker = make_broker(tmp_path)
    model, api, evidence = model_for(broker, [
        call(), ModelOutput.from_content("test/browser", "I observed the page; outcome is unverified.")])
    result = await run_browser_agent(model, broker, "Inspect this synthetic page.",
                                    {"generation": 1, "text": "Initial snapshot"}, evidence=evidence)
    assert result["verified_outcome"] is None
    assert broker.host.calls == [{"kind": "snapshot"}]
    assert len(api.inputs) == 2
    assert [tool["name"] for tool in api.inputs[0]["tools"]] == ["browser_action"]
    assert api.inputs[0]["tools"][0]["parameters"]["additionalProperties"] is False
    assert any(message["role"] == "tool" and "Current synthetic snapshot" in message["content"]
               for message in api.inputs[1]["messages"])
    record = manifest(broker)
    assert record["execution_status"] == "completed"
    assert record["run_lock"]["agent"] == "inspect_ai.agent.react"
    path = evidence.path / record["inspect_logs"][0]
    log = read_eval_log(str(path))
    assert log.status == "success"
    assert any(event.event == "approval" for event in log.samples[0].events)
    assert not fsspec.filesystem("memory").exists(f"/consumer-agent-testing-tool/browser/{evidence.run_id}")


async def test_takeover_stops_inspect_before_another_generation(tmp_path):
    broker = make_broker(tmp_path)
    model, api, evidence = model_for(broker, [call("takeover")])
    result = await run_browser_agent(model, broker, "Request human authentication.",
                                    {"generation": 1, "text": "Login required"}, evidence=evidence)
    assert result["status"] == "human-takeover"
    assert len(api.inputs) == 1 and len(broker.host.calls) == 1
    assert manifest(broker)["execution_status"] == "paused"

async def test_stale_reference_requires_new_snapshot_without_automatic_action_replay(tmp_path):
    broker = make_broker(tmp_path)
    original = broker.host.request
    dispatched = []

    async def request(method, arguments):
        dispatched.append(arguments)
        if len(dispatched) == 1:
            raise LabError("Reference changed before dispatch", category="stale-reference")
        return await original(method, arguments)

    broker.host.request = request
    model, api, evidence = model_for(broker, [
        call("click", ref="e1", generation=1), call(),
        ModelOutput.from_content("test/browser", "The reference changed; I reread the page.")])
    await run_browser_agent(model, broker, "Inspect the page.",
                            {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    assert [a["kind"] for a in dispatched] == ["click", "snapshot"]
    assert json.loads(api.inputs[1]["messages"][-1]["content"])["status"] == "stale-reference"
    assert manifest(broker)["execution_status"] == "completed"


async def test_batch_is_denied_by_inspect_approval_before_any_action(tmp_path):
    broker = make_broker(tmp_path)
    output = call("takeover")
    output.message.tool_calls.append(ToolCall(
        id="second", function="browser_action", arguments={"kind": "snapshot"}))
    # The batch is rejected before dispatch and returned to the model as tool errors;
    # an agent that never proposes a valid call afterwards fails the trial.
    model, api, evidence = model_for(broker, [output, ModelOutput.from_content("test/browser", "Stopped.")])
    with pytest.raises(LabError, match="exactly one") as failed:
        await run_browser_agent(model, broker, "Inspect the page.",
                                {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    assert failed.value.code == 1 and failed.value.category == "interpretation"
    assert broker.host.calls == []
    assert len(api.inputs) == 2
    errors = [message for message in api.inputs[1]["messages"] if message["role"] == "tool"]
    assert len(errors) == 2 and all(message["error"]["type"] == "approval" for message in errors)
    assert manifest(broker)["execution_status"] == "failed"


@pytest.mark.parametrize("budget", ["max_actions", "max_model_turns"])
async def test_inspect_browser_limits_do_not_claim_completion(tmp_path, budget):
    broker = make_broker(tmp_path, **{budget: 1})
    model, api, evidence = model_for(broker, [call(identifier=f"step-{i}") for i in range(4)])
    with pytest.raises(LabError, match="budget|limit"):
        await run_browser_agent(model, broker, "Keep looking.",
                                {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    assert len(broker.host.calls) <= 1
    if budget == "max_model_turns":
        assert len(api.inputs) == 1
    assert manifest(broker)["execution_status"] == "failed"
    assert manifest(broker)["inspect_logs"]


async def test_forbidden_parameters_never_reach_browser_dispatch(tmp_path):
    broker = make_broker(tmp_path, max_model_turns=1)
    model, api, evidence = model_for(broker, [call(script="fetch('/canary')")])
    with pytest.raises(LimitExhausted, match="turn limit"):
        await run_browser_agent(model, broker, "Try an invalid browser action.",
                                {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    assert broker.host.calls == []
    assert len(api.inputs) == 1
    assert any(event.get("decision") == "reject" for event in broker.events)
    record = manifest(broker)
    assert record["execution_status"] == "failed"
    assert any(check["id"] == "agent.tool_proposals" and "facade schema" in check["detail"]
               for check in record["checks"])


async def test_inspect_logs_redact_known_private_values(tmp_path):
    broker = make_broker(tmp_path)
    broker.redactor.secrets.append("synthetic-private-value")
    model, _, evidence = model_for(broker, [
        ModelOutput.from_content("test/browser", "A response containing synthetic-private-value.")])
    await run_browser_agent(model, broker, "Inspect the page.",
                            {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    for path in evidence.path.rglob("*.json"):
        assert "synthetic-private-value" not in path.read_text()
    record = manifest(broker)
    log = read_eval_log(str(evidence.path / record["inspect_logs"][0]))
    assert "synthetic-private-value" not in log.model_dump_json()


async def test_entered_text_is_redacted_before_trace_artifact_writes(tmp_path):
    broker = make_broker(tmp_path)
    model, api, evidence = model_for(broker, [
        call("type", ref="e1", generation=1, text="synthetic-entered-value"),
        ModelOutput.from_content("test/browser", "Entered synthetic-entered-value.")])
    await run_browser_agent(model, broker, "Fill the synthetic field.",
                            {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    assert len(api.inputs) == 2  # Remembered trace text must not trip the credential boundary.
    for path in evidence.path.rglob("*.json"):
        assert "synthetic-entered-value" not in path.read_text()
    log = read_eval_log(str(evidence.path / manifest(broker)["inspect_logs"][0]))
    assert "synthetic-entered-value" not in log.model_dump_json()


async def test_inspect_browser_wall_limit_cancels_inference(tmp_path):
    broker = make_broker(tmp_path, wall_seconds=1)

    class Slow(ModelAPI):
        cancelled = False

        async def generate(self, input, tools, tool_choice, config):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("The wall deadline should interrupt inference")

    api = Slow("test/slow")
    evidence = agent_evidence(broker)
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    with pytest.raises(LabError, match="limit"):
        await run_browser_agent(model, broker, "Inspect the page.",
                                {"generation": 1, "text": "Snapshot"}, evidence=evidence)
    assert api.cancelled and broker.host.calls == []
    assert manifest(broker)["execution_status"] == "failed"


@pytest.mark.parametrize("diagnostics", [False, True])
async def test_inspect_react_real_browser_and_independent_effect_oracle(portal, diagnostics, tmp_path):  # noqa: F811
    snapshot = await portal.open("duplicate", writes=["/commit"], diagnostics={
        "enabled": diagnostics, "retain_raw_trace": diagnostics,
        "mask_selectors": ["#synthetic-private-mask"] if diagnostics else []})
    portal.approve("/commit")

    class PortalModel(ModelAPI):
        calls = 0

        async def generate(self, input, tools, tool_choice, config):
            self.calls += 1
            assert [tool.name for tool in tools] == ["browser_action"]
            visible = json.dumps([message.model_dump(mode="json") for message in input])
            assert "diagnostics.private" not in visible and "synthetic-private-mask" not in visible
            assert "data:image" not in visible
            observation = json.loads(input[-1].text)
            if self.calls == 1:
                return call("click", ref=reference(observation, "Confirm reservation"),
                            generation=observation["generation"])
            if "Confirmed" not in observation["text"]:
                return call(identifier=f"poll-{self.calls}")
            return ModelOutput.from_content("test/portal", "The page reports confirmation.")

    evidence = agent_evidence(portal.broker)
    api = PortalModel("test/portal")
    model = Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))
    result = await run_browser_agent(model, portal.broker, "Confirm the synthetic reservation once.",
                                    snapshot, evidence=evidence)
    assert result["verified_outcome"] is None  # The model is not the oracle.
    assert portal.broker.assert_backend_count(
        "confirmations", len((await portal.oracle())["effects"]), 1,
        source="runner-only Unix fixture oracle") == "passed"
    assert manifest(portal.broker)["execution_status"] == "completed"
    assert api.calls >= 2
    if diagnostics:
        from consumer_agent_testing_tool.browser_diagnostics import verify_diagnostics
        from consumer_agent_testing_tool.replay import export_public_run, playback
        await portal.broker.close()
        portal.broker.finish("completed")
        assert verify_diagnostics(portal.broker.path)[1]["trace"]["status"] == "captured"
        destination = export_public_run(portal.broker.path, tmp_path / "export")
        assert playback(destination)["result"]["verified_service_outcome"] == {"confirmations": 1}
        assert list(destination.glob("agent/*/inspect/*.eval"))
        assert not list(destination.rglob("*.png")) and not list(destination.rglob("diagnostics.private"))
        assert "synthetic-private-mask" not in next(destination.glob("agent/*/manifest.json")).read_text()
