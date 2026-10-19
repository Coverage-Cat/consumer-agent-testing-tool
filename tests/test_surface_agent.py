import asyncio
import json

import fsspec
import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
from inspect_ai.tool import ToolCall
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.surface import (
    STEP_MENU,
    STEP_MENU_GAP,
    SurfaceRun,
    SurfaceScenario,
)
from consumer_agent_testing_tool.surface_agent import InspectSurfaceDriver


class WorkflowAPI(ModelAPI):
    def __init__(self, responses=None):
        super().__init__("test/workflow")
        self.responses = iter(responses) if responses is not None else None
        self.inputs = []

    async def generate(self, input, tools, tool_choice, config):
        self.inputs.append({"messages": [message.model_dump(mode="json") for message in input],
                            "tools": [tool.model_dump(mode="json") for tool in tools]})
        if self.responses is not None:
            return next(self.responses)
        observation = json.loads(input[-1].content)
        selected = observation["available_actions"][0]["id"]
        return action(selected, str(len(self.inputs)))


def action(identifier, call_id="action"):
    return ModelOutput.for_tool_call("test/workflow", "perform_action",
                                    {"id": identifier}, tool_call_id=call_id)


def driver(api=None):
    api = api or WorkflowAPI()
    return InspectSurfaceDriver(model=Model(api, GenerateConfig(max_retries=0))), api


def agent_manifest(run):
    path = run.path / run.state["agent_runs"][-1]
    return path, json.loads((path / "manifest.json").read_text())


async def test_inspect_workflow_updates_available_tools_and_keeps_private_data_out(tmp_path):
    planner, api = driver()
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(), host,
                     secrets={"access": "original-secret-canary"}, planner=planner)
    assert await run.run() == 0
    assert [row["tools"][0]["parameters"]["properties"]["id"]["enum"]
            for row in api.inputs] == [["status"], ["prepare"], ["form"], ["verify"]]
    submitted = json.dumps(api.inputs)
    assert all(secret not in submitted for secret in
               ("original-secret-canary", "rotated-secret-canary", "Test Shopper"))
    path, manifest = agent_manifest(run)
    assert manifest["execution_status"] == "completed"
    assert manifest["run_lock"]["agent"] == "inspect_ai.agent.react"
    # A one-choice menu is labeled as such everywhere; it is not exercised agent behavior.
    assert manifest["evidence_label"] == STEP_MENU and STEP_MENU_GAP in manifest["observation_gaps"]
    assert manifest["scenario_id"] == "preferences"
    assert manifest["metrics"]["agent_interface"] == STEP_MENU
    assert manifest["metrics"]["tool_use_quality"] == "not measured"
    result = json.loads((run.path / "result.json").read_text())
    assert result["evidence_label"] == STEP_MENU and result["lane"] == "action-menu"
    assert result["evidence_label"] != "surrogate behavior"
    assert result["layers"]["agent"]["exercised"] is False
    assert result["layers"]["agent"]["interface"] == STEP_MENU
    assert STEP_MENU_GAP in result["observation_gaps"]
    assert not result["compatibility_pass"]
    assert [check["status"] for check in result["checks"]] == ["passed"]  # Service outcomes are kept.
    log = read_eval_log(str(path / manifest["inspect_logs"][0]))
    assert log.status == "success"
    assert any(event.event == "approval" for event in log.samples[0].events)
    assert "secret-canary" not in log.model_dump_json()
    assert not fsspec.filesystem("memory").exists(f"/consumer-agent-testing-tool/workflow/{path.name}")
    assert json.loads((run.path / "result.json").read_text())["agent_runs"]
    before = len(api.inputs)
    resumed = SurfaceRun(tmp_path, scenario(), host, planner=planner, resume=run.path)
    assert await resumed.run() == 0
    assert len(api.inputs) == before
    assert playback(run.path)["integrity"] == "verified"
    sealed = json.loads((run.path / "evidence.sha256.json").read_text())
    assert str((path / manifest["inspect_logs"][0]).relative_to(run.path)) in sealed
    assert not any("private" in name for name in sealed)
    (path / manifest["inspect_logs"][0]).write_bytes(b"altered evidence")
    with pytest.raises(LabError, match="integrity"):
        playback(run.path)


async def test_inspect_workflow_pauses_for_private_reply_without_extra_generation(tmp_path):
    data = scenario().model_dump()
    data["steps"].insert(2, {"id": "reply", "kind": "user", "reply_key": "answer",
        "after": ["prepare"], "description": "Supply the recorded user answer"})
    data["steps"][3]["after"] = ["prepare", "reply"]
    contract = SurfaceScenario.model_validate(data)
    planner, api = driver()
    host = FakeHost()
    run = SurfaceRun(tmp_path, contract, host, secrets={"access": "original-secret-canary"},
                     planner=planner)
    assert await run.run() == 3
    assert len(api.inputs) == 3
    assert agent_manifest(run)[1]["execution_status"] == "blocked"
    resumed = SurfaceRun(tmp_path, contract, host, planner=planner, resume=run.path,
                         replies={"answer": "private-user-answer"})
    assert await resumed.run() == 0
    assert "private-user-answer" not in json.dumps(api.inputs)
    assert [name for name, _ in host.calls].count("prepare") == 1
    assert len(resumed.state["agent_runs"]) == 2


async def test_inspect_workflow_reconciles_lost_callback_without_duplicate_write(tmp_path):
    host = FakeHost(fail_after_save=True)
    planner, api = driver()
    first = SurfaceRun(tmp_path, scenario(), host, planner=planner,
                       secrets={"access": "original-secret-canary"})
    assert await first.run() == 2  # Lost response: integration failure, resumable.
    assert len(api.inputs) == 3
    assert agent_manifest(first)[1]["execution_status"] == "failed"
    resumed = SurfaceRun(tmp_path, scenario(), host, planner=planner, resume=first.path)
    assert await resumed.run() == 0
    assert host.rendered == 1
    assert [name for name, _ in host.calls].count("save") == 1
    assert [name for name, _ in host.calls].count("prepare") == 1


@pytest.mark.parametrize("violation", ["batch", "unknown", "private-argument", "early-stop"])
async def test_inspect_workflow_rejects_invalid_proposals_before_dispatch(tmp_path, violation):
    output = action("status")
    if violation == "batch":
        output.message.tool_calls.append(ToolCall(
            id="second", function="perform_action", arguments={"id": "status"}))
    elif violation == "unknown":
        output = action("create_production_order")
    elif violation == "private-argument":
        output.message.tool_calls[0].arguments["token"] = "invented"
    else:
        output = ModelOutput.from_content("test/workflow", "Everything succeeded.")
    planner, api = driver(WorkflowAPI([output]))
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(), host, planner=planner)
    assert await run.run() == 1
    assert host.calls == []
    assert len(api.inputs) == 1
    assert agent_manifest(run)[1]["execution_status"] == "failed"
    assert not json.loads((run.path / "result.json").read_text())["compatibility_pass"]


async def test_inspect_workflow_turn_limit_preserves_checkpoint(tmp_path):
    planner, api = driver()
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(max_model_turns=1), host, planner=planner)
    assert await run.run() == 3
    assert len(api.inputs) == 1
    assert [name for name, _ in host.calls] == ["status"]
    assert run.state["steps"]["status"]["status"] == "completed"
    assert agent_manifest(run)[1]["execution_status"] == "blocked"


async def test_inspect_workflow_wall_limit_cancels_generation(tmp_path):
    class Stalled(WorkflowAPI):
        cancelled = False

        async def generate(self, *args, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    api = Stalled()
    planner, _ = driver(api)
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(wall_seconds=1), host, planner=planner)
    assert await run.run() == 3
    assert api.cancelled and host.calls == []
    assert agent_manifest(run)[1]["execution_status"] == "blocked"


async def test_inspect_workflow_wall_limit_during_write_reconciles_on_resume(tmp_path):
    class Pending(FakeHost):
        cancelled = False

        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            if name == "save":
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return result

    planner, _ = driver()
    host = Pending()
    contract = scenario(wall_seconds=1)
    first = SurfaceRun(tmp_path, contract, host, planner=planner,
                       secrets={"access": "original-secret-canary"})
    assert await first.run() == 3
    assert host.cancelled and host.saved
    assert first.state["operations"]["ui:form:save"]["status"] == "pending"
    resumed = SurfaceRun(tmp_path, contract, host, planner=planner, resume=first.path)
    assert await resumed.run() == 0
    assert host.rendered == 1
    assert [name for name, _ in host.calls].count("save") == 1
