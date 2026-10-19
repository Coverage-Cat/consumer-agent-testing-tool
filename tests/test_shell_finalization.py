"""Final CLI state requires stopped processes and drained service requests."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from inspect_ai.model import Model, ModelAPI, ModelOutput
from test_oracle_reporting import cli_trial

from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, Target, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.providers import CapturingAPI, generation
from consumer_agent_testing_tool.runner import run_scenario
from consumer_agent_testing_tool.sandbox import Sandbox
from consumer_agent_testing_tool.scenario import read_scenario

LIVE = pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                         reason="Unverified: requires the actual local Docker boundary")


def developer_target(command=None):
    return Target(kind="cli-skill", source_revision="synthetic-quiescence-v1",
        container_image="consumer-agent-testing-tool-sandbox:0.1.0", service_image="consumer-agent-testing-tool-sandbox:0.1.0",
        command=command or ["python", "-m", "consumer_agent_testing_tool.fixture_service", "serve", "--no-canary"],
        cli_command=["booking"], readiness=["test", "-S", "/control/oracle.sock"],
        oracle_command=["python", "-m", "consumer_agent_testing_tool.fixture_service", "control-stdin"],
        skill=str(Path("src/consumer_agent_testing_tool/data/skills/booking/SKILL.md").resolve()))


def detached_command(child):
    return "python - <<'PY'\nimport subprocess\nsubprocess.Popen(['python','-c'," + repr(child) + """],
stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
print('detached')
PY"""


@pytest.mark.parametrize("finality", [
    None, {}, False, "error",
    {"run_id": "wrong-trial", "agent_stopped": True, "public_requests_drained": True,
     "canary_requests_drained": True},
    {"agent_stopped": 1, "public_requests_drained": True, "canary_requests_drained": True},
])
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("violation", [False, True])
async def test_cli_unknown_finality_never_qualifies_zero_or_erases_a_violation(
        tmp_path, monkeypatch, finality, interrupt, violation):
    evidence, checks, _ = await cli_trial(tmp_path, monkeypatch, finality=finality, interrupt=interrupt,
                                         effect="unauthorized" if violation else None)
    assert statuses(checks)[0] == (1 if violation else 3)
    assert evidence.manifest["execution_quiescence"]["status"] == "unverified"
    assert any(row["id"] == "execution.quiesced" and row["status"] == "observation-gap" for row in checks)
    public = export_redacted(evidence.path, tmp_path / "export")
    exported = json.loads((public / "manifest.json").read_text())
    assert exported["execution_status"] == ("failed" if violation else "blocked")
    assert exported["execution_quiescence"] == evidence.manifest["execution_quiescence"]


@LIVE
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_actual_cli_final_snapshot_stops_detached_children(tmp_path, monkeypatch, custom, interrupt):
    state = {"armed": False}
    child = """import pathlib,time,subprocess
while not pathlib.Path('/workspace/release').exists(): time.sleep(.01)
subprocess.run(['booking','reservations.create','--args',
'{"slot_id":"afternoon","contact":"Synthetic race","idempotency_key":"late"}'],check=True)
"""

    class Boundary(Sandbox):
        async def oracle(self, operation="snapshot", **parameters):
            snapshot = await super().oracle(operation, **parameters)
            if operation == "snapshot" and state["armed"]:
                state["armed"] = False
                state["closing"] = self.closing
                state["container_state"] = json.loads(await self.required(
                    "inspect", self.agent, "--format", "{{json .State}}"))
                # Release the preexisting child only after the snapshot. Before
                # the fix this succeeded and the child changed state after scoring.
                result = await self.docker("exec", self.agent, "touch", "/workspace/release")
                state["release_exit"] = result["exit_status"]
                state["after"] = await super().oracle()
            return snapshot

    class Agent(ModelAPI):
        step = 0

        async def generate(self, *args):
            self.step += 1
            if self.step == 1:
                return ModelOutput.for_tool_call(self.model_name, "shell", {"command": detached_command(child)})
            state["armed"] = True
            if interrupt:
                raise LabError("Synthetic interrupted model")
            return ModelOutput.for_tool_call(self.model_name, "submit", {"answer": "Finished"})

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="synthetic",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="synthetic finalization proof",
        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={}, scenario_id="compare_slots")
    model = Model(CapturingAPI(Agent("test/detached-child"), evidence), generation(profile))
    config = Config()
    if custom:
        config.targets["booking-service"] = developer_target()
    scenario = read_scenario(Path("src/consumer_agent_testing_tool/data/scenarios/compare_slots.yaml"))
    scenario["assertions"] = [{"scorer": "backend.draft_count", "expected": 0}]
    monkeypatch.setattr("consumer_agent_testing_tool.runner.Sandbox", Boundary)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    if interrupt:
        with pytest.raises(LabError):
            await run_scenario(scenario, config, profile, evidence)
        checks = evidence.manifest["checks"]
    else:
        checks = await run_scenario(scenario, config, profile, evidence)
    assert state["closing"] is True and state["container_state"]["Running"] is False
    assert state["release_exit"] != 0
    assert state["after"]["reservations"] == []
    assert evidence.manifest["backend_metrics"]["backend.draft_count"] == 0
    assert evidence.manifest["execution_quiescence"]["status"] == "passed"
    assert all(row["status"] == "passed" for row in checks)
    if interrupt:
        checks = [*checks, {"id": "execution", "status": "failed", "class": "setup/infrastructure"}]
    evidence.finish("failed" if interrupt else "completed", checks)
    public = export_redacted(evidence.path, tmp_path / "export")
    exported = json.loads((public / "manifest.json").read_text())
    assert exported["execution_quiescence"] == evidence.manifest["execution_quiescence"]


@LIVE
async def test_actual_quiescence_drains_accepted_work_after_stopping_client():
    slow = """import os,pathlib,time
from consumer_agent_testing_tool.fixture import Fixture
from consumer_agent_testing_tool.fixture_service import serve
class Slow(Fixture):
    def dispatch(self, principal, operation, arguments):
        if operation == 'reservations.create':
            pathlib.Path('/data/request-accepted').touch()
            deadline = time.monotonic() + 15
            while not pathlib.Path('/data/release').exists():
                if time.monotonic() > deadline: raise RuntimeError('test release missing')
                time.sleep(.01)
        return super().dispatch(principal, operation, arguments)
serve(Slow('/data/fixture.sqlite', os.environ['CONSUMER_AGENT_TESTING_TOOL_RUN_ID']), canary_enabled=False)
"""
    target = developer_target(["python", "-c", slow])
    async with Sandbox("consumer-agent-testing-tool-sandbox:0.1.0", "accepted-work-proof", descriptor=target) as sandbox:
        await sandbox.oracle("reset")
        request = """import subprocess
subprocess.run(['booking','reservations.create','--args',
'{"slot_id":"afternoon","contact":"Synthetic in-flight request","idempotency_key":"accepted"}'])
"""
        assert (await sandbox.shell(detached_command(request)))["exit_status"] == 0
        async with asyncio.timeout(10):
            while (await sandbox.docker("exec", sandbox.service, "test", "-f", "/data/request-accepted"))[
                    "exit_status"]:
                await asyncio.sleep(.05)
        closing = asyncio.create_task(sandbox.quiesce())
        concurrent = asyncio.create_task(sandbox.quiesce())
        try:
            async with asyncio.timeout(10):
                while json.loads(await sandbox.required(
                        "inspect", sandbox.agent, "--format", "{{json .State}}"))["Running"]:
                    await asyncio.sleep(.05)
            assert not closing.done()  # Accepted service work has not finished.
            await sandbox.required("exec", sandbox.service, "touch", "/data/release")
            result, repeated = await asyncio.wait_for(asyncio.gather(closing, concurrent), 10)
            assert repeated == result
            assert result["agent_stopped"] is True and result["public_requests_drained"] is True
            snapshot = await sandbox.oracle()
            assert len(snapshot["reservations"]) == len(snapshot["effects"]) == 1
            assert await sandbox.quiesce() == result
            with pytest.raises(LabError, match="further execution"):
                await sandbox.shell("booking slots.list")
            with pytest.raises(LabError):
                await sandbox.oracle("reset")
            # An independent bind proves that each public listener closed.
            bind = "import socket;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',int(__import__('sys').argv[1])))"
            await sandbox.required("exec", sandbox.service, "python", "-c", bind, "8765")
            await sandbox.required("exec", sandbox.target, "python", "-c", bind, "8766")
        finally:
            closing.cancel()
            concurrent.cancel()
            await asyncio.gather(closing, concurrent, return_exceptions=True)


@pytest.mark.parametrize("bad", ["running", "restarting", "paused", "wrong-trial", "numeric", "missing"])
async def test_quiescence_requires_typed_stop_and_matching_service_confirmation(bad):
    class ControlDouble(Sandbox):
        async def required(self, *args):
            return json.dumps({"Running": "false" if bad == "running" else False,
                               "Restarting": bad == "restarting",
                               "Paused": bad == "paused", "Status": "exited"})

        async def oracle(self, *args):
            return {"quiesced": 1 if bad == "numeric" else True,
                    "run_id": "wrong" if bad == "wrong-trial" else None if bad == "missing" else self.run_id}

    sandbox = ControlDouble("synthetic", "synthetic-trial")
    with pytest.raises(LabError):
        await sandbox.quiesce()
    assert sandbox.quiescence is None and sandbox.closing
    with pytest.raises(LabError, match="further execution"):
        await sandbox.exec_agent(["anything"])
