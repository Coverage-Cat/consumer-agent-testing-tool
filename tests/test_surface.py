import asyncio
import json

import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.surface import SurfaceRun, SurfaceScenario, inside


def scenario(**updates):
    return SurfaceScenario.model_validate({
        "id": "preferences", "goal": "Save the synthetic preference through the embedded app",
        "endpoint": "http://127.0.0.1:8765/mcp", "allowed_origins": ["http://127.0.0.1:8765"],
        "fixture": {"name": "Test Shopper"},
        "tools": {
            "status": {"effect": "read"},
            "prepare": {"effect": "write", "host_arguments": {
                "token": {"$ref": "/secrets/access"}},
                "capture": {"access": "/structuredContent/token"}},
            "save": {"effect": "write", "arguments_schema": {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False},
                "host_arguments": {"token": {"$ref": "/secrets/access"}},
                "reconcile": {"tool": "status", "path": "/structuredContent/status", "expected": "saved"}},
        },
        "steps": [
            {"id": "status", "description": "Read current state", "kind": "tool", "tool": "status"},
            {"id": "prepare", "description": "Prepare form", "kind": "tool", "tool": "prepare",
             "after": ["status"]},
            {"id": "form", "description": "Use the embedded form", "kind": "ui", "after": ["prepare"],
             "ui": {"source": "prepare", "resource_uri": "ui://test/preferences",
                    "ready_selector": "#name", "callback_tools": ["save"], "callback_required": True,
                    "actions": [{"kind": "expect_value", "selector": "#name",
                                 "value": {"$ref": "/fixture/name"}},
                                {"kind": "click", "selector": "#save"}]}},
            {"id": "verify", "description": "Verify service state", "kind": "tool", "tool": "status",
             "after": ["form"], "checks": [{"id": "saved", "layer": "service",
                "path": "/verify/result/structuredContent/status", "expected": "saved"}]},
        ], **updates})


class FakeHost:
    def __init__(self, *, fail_after_save=False):
        self.saved = False
        self.calls = []
        self.fail_after_save = fail_after_save
        self.rendered = 0

    async def connect(self, scenario):
        return {"discovery": "passed", "negotiation": "passed"}

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "save":
            self.saved = True
            if self.fail_after_save:
                self.fail_after_save = False
                raise RuntimeError("Response lost after service applied the change")
        return {"structuredContent": {"status": "saved" if self.saved else "form_needed",
                                      "token": "rotated-secret-canary"}}

    async def render(self, ui, source, path):
        self.rendered += 1
        self.observe({"rendering": "passed", "interaction": "passed"})
        result = await self.callback("save", {"value": "Test Shopper"})
        self.observe({"completion": "passed"})
        return {"callback": result}

    async def close(self):
        pass


async def test_full_host_workflow_and_private_boundary(tmp_path):
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    assert host.calls[1][1]["token"] == "original-secret-canary"
    assert host.calls[2][1]["token"] == "rotated-secret-canary"
    report = json.loads((run.path / "result.json").read_text())
    assert report["compatibility_pass"] and report["capabilities"]["completion"] == "passed"
    assert report["checks"][0]["layer"] == "service"
    public = (run.path / "events.jsonl").read_text() + (run.path / "report.html").read_text()
    assert "original-secret-canary" not in public and "rotated-secret-canary" not in public
    assert (run.path / "checkpoint.private.json").stat().st_mode & 0o777 == 0o600


async def test_interrupted_callback_reconciles_without_replaying_form(tmp_path):
    host = FakeHost(fail_after_save=True)
    first = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await first.run() == 2  # A lost response is an integration failure; state stays resumable.
    resumed = SurfaceRun(tmp_path, scenario(), host, resume=first.path)
    assert await resumed.run() == 0
    assert host.rendered == 1
    assert [name for name, _ in host.calls].count("save") == 1
    assert [name for name, _ in host.calls].count("prepare") == 1


async def test_interrupted_ui_without_callback_is_not_replayed(tmp_path):
    class Interrupted(FakeHost):
        async def render(self, *args):
            self.rendered += 1
            raise RuntimeError("Browser stopped after a possible click")
    host = Interrupted()
    first = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await first.run() == 2
    assert await SurfaceRun(tmp_path, scenario(), host, resume=first.path).run() == 3
    assert host.rendered == 1


async def test_reconciliation_does_not_turn_negative_status_into_success(tmp_path):
    host = FakeHost(fail_after_save=True)
    first = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    await first.run()
    host.saved = False
    assert await SurfaceRun(tmp_path, scenario(), host, resume=first.path).run() == 3
    assert host.rendered == 1


async def test_user_reply_pause_and_resume(tmp_path):
    data = scenario().model_dump()
    data["steps"].insert(0, {"id": "reply", "kind": "user", "reply_key": "preference",
                            "description": "Provide a test preference"})
    data["steps"][1]["after"] = ["reply"]
    contract = SurfaceScenario.model_validate(data)
    host = FakeHost()
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "secret"})
    assert await first.run() == 3
    assert not host.calls
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path, replies={"preference": "blue"})
    assert await resumed.run() == 0
    assert resumed.state["steps"]["reply"]["result"] == {"reply": "blue"}


async def test_agent_observes_available_actions_not_private_payloads(tmp_path):
    from test_surface_agent import driver
    planner, api = driver()
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret-canary"},
                     planner=planner)
    assert await run.run() == 0
    serialized = json.dumps(api.inputs)
    assert "secret-canary" not in serialized and "Test Shopper" not in serialized
    assert json.loads((run.path / "result.json").read_text())["driver"] == "agent"


async def test_agent_cannot_invoke_undeclared_action(tmp_path):
    from test_surface_agent import WorkflowAPI, action, driver
    planner, _ = driver(WorkflowAPI([action("create_production_order")]))
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(), host, planner=planner)
    assert await run.run() == 1
    assert host.calls == []


async def test_assertion_failure_is_not_a_pass_or_reexecuted_write(tmp_path):
    contract = scenario()
    contract.steps[-1].checks[0].expected = "wrong"
    host = FakeHost()
    run = SurfaceRun(tmp_path, contract, host, secrets={"access": "secret"})
    assert await run.run() == 1  # Spec exit code 1: a test assertion failed.
    report = json.loads((run.path / "result.json").read_text())
    assert report["status"] == "failed" and report["failure_class"] == "assertion"
    assert report["failure_stage"] == "verify" and not report["resumable"]
    before = len(host.calls)
    assert await SurfaceRun(tmp_path, contract, host, resume=run.path).run() == 1
    assert len(host.calls) == before
    assert run.state["checks"][0]["status"] == "failed"


async def test_service_envelope_error_stops_before_ui(tmp_path):
    class ErrorHost(FakeHost):
        async def call(self, name, arguments):
            return {"isError": True, "structuredContent": {"error": "not_found"}}
    host = ErrorHost()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 1
    assert host.rendered == 0


async def test_changed_duplicate_callback_arguments_are_not_silently_accepted(tmp_path):
    class Changed(FakeHost):
        async def render(self, ui, source, path):
            await self.callback("save", {"value": "first"})
            return await self.callback("save", {"value": "different"})
    host = Changed()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret"})
    assert await run.run() == 3
    assert [name for name, _ in host.calls].count("save") == 1


@pytest.mark.parametrize("treatment", ["observation", "workaround"])
async def test_nonbaseline_success_is_not_a_compatibility_pass(tmp_path, treatment):
    run = SurfaceRun(tmp_path, scenario(treatment=treatment), FakeHost(), secrets={"access": "secret"})
    assert await run.run() == 0
    assert not json.loads((run.path / "result.json").read_text())["compatibility_pass"]


def test_schema_and_workspace_boundaries(tmp_path):
    with pytest.raises(ValidationError, match="allowlist"):
        scenario(endpoint="https://production.invalid/mcp")
    data = scenario().model_dump()
    data["steps"][2]["ui"]["resource_file"] = "patch.html"
    with pytest.raises(ValidationError, match="workaround"):
        SurfaceScenario.model_validate(data)
    with pytest.raises(Exception, match="escapes"):
        inside(tmp_path, "../outside")
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(Exception, match="escapes"):
        inside(tmp_path, "link/something")
    data = scenario().model_dump()
    data["tools"]["status"]["arguments_schema"] = {"$ref": "https://remote.invalid/schema"}
    with pytest.raises(ValidationError, match="local references"):
        SurfaceScenario.model_validate(data)


async def test_changed_scenario_cannot_resume_previous_authority(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    await run.run()
    with pytest.raises(Exception, match="Scenario changed"):
        SurfaceRun(tmp_path, scenario(goal="Different goal"), FakeHost(), resume=run.path)


async def test_cancellation_cannot_report_success(tmp_path):
    class Cancelled(FakeHost):
        async def call(self, name, arguments):
            raise asyncio.CancelledError()
    run = SurfaceRun(tmp_path, scenario(), Cancelled())
    assert await run.run() == 3
    report = json.loads((run.path / "result.json").read_text())
    assert report["status"] == "blocked" and not report["compatibility_pass"]


async def test_public_observations_are_explicit_and_redacted(tmp_path):
    contract = scenario()
    contract.steps[0].expose = {"status": "/structuredContent/status"}
    run = SurfaceRun(tmp_path, contract, FakeHost(), secrets={"access": "secret"})
    await run.run()
    observation = run.observation([])
    assert observation["public_results"]["status"] == {"status": "form_needed"}
    assert "token" not in json.dumps(observation)
    data = contract.model_dump()
    data["steps"][0]["expose"] = {"unsafe": "/structuredContent/token"}
    with pytest.raises(ValidationError, match="Private metadata"):
        SurfaceScenario.model_validate(data)


def test_example_and_cli_schema():
    from consumer_agent_testing_tool.cli import parser
    from consumer_agent_testing_tool.surface import read_surface
    contract = read_surface("examples/surface-preferences.yaml")
    assert contract.id == "preferences"
    args = parser().parse_args(["surface-run", "examples/surface-preferences.yaml"])
    assert args.driver == "scripted"


async def test_cli_runs_and_resumes_with_workspace_inputs(tmp_path, monkeypatch, capsys):
    from consumer_agent_testing_tool import surface_host
    from consumer_agent_testing_tool.cli import parser
    host = FakeHost()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(surface_host, "BrowserHost", lambda *args: host)
    (tmp_path / "scenario.json").write_text(scenario().model_dump_json())
    (tmp_path / "secrets.json").write_text(json.dumps({"access": "secret"}))
    args = parser().parse_args(["surface-run", "scenario.json", "--secrets-file", "secrets.json"])
    assert await surface_host.run_from_cli(args) == 0
    run = next((tmp_path / "runs").iterdir())
    before = len(host.calls)
    args.resume = run
    assert await surface_host.run_from_cli(args) == 0
    assert len(host.calls) == before
    assert not (run / ".active").exists()
    sequences = [json.loads(line)["sequence"] for line in (run / "events.jsonl").read_text().splitlines()]
    assert sequences == sorted(set(sequences))
    assert "Surface report:" in capsys.readouterr().out
