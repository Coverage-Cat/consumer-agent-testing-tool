from pathlib import Path

import pytest
from inspect_ai.model import Model, ModelAPI, ModelOutput

from consumer_agent_testing_tool.config import Config, ModelProfile, interface_profile
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.providers import CapturingAPI, generation
from consumer_agent_testing_tool.runner import run_scenario
from consumer_agent_testing_tool.scenario import read_scenario


async def test_real_inspect_deepagent_execution_and_oracle(tmp_path, monkeypatch):
    """In-memory model and sandbox doubles test composition, never isolation or live inference."""
    from consumer_agent_testing_tool.fixture import Fixture
    fixture = Fixture(tmp_path / "fixture.sqlite", "test")
    fixture.reset()

    class BoundaryDouble:
        image_id = "test-double"

        def __init__(self, image, run_id, *args):
            self.run_id = run_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def isolation_probe(self):
            return {"passed": True, "test_double": True}

        async def cli_identity(self):
            return {"test_double": True}

        async def quiesce(self):
            return {"run_id": self.run_id, "agent_stopped": True, "public_requests_drained": True,
                    "canary_requests_drained": True, "test_double": True}

        async def oracle(self, operation="snapshot", **parameters):
            if operation == "reset":
                return fixture.reset(**parameters)
            # Explicit synthetic observation; this test does not establish isolation.
            return {**fixture.snapshot(), "canary_connections": []}

        async def shell(self, command):
            raise AssertionError("This model should only submit an answer")

    class Scripted(ModelAPI):
        async def generate(self, input, tools, tool_choice, config):
            return ModelOutput.for_tool_call("test/scripted", "submit",
                {"answer": "The current afternoon price is $125."}, tool_call_id="submit-answer")

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="test",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    model = Model(CapturingAPI(Scripted("test/scripted"), evidence), generation(profile))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.Sandbox", BoundaryDouble)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    scenario = read_scenario(Path("src/consumer_agent_testing_tool/data/scenarios/compare_slots.yaml"))
    # This double tests completion/oracle wiring, not successful service discovery.
    scenario["assertions"] = [a for a in scenario["assertions"] if a["scorer"] != "backend.read_performed"]
    scenario["assertions"].append({"scorer": "exposure.required_capability", "expected": False})
    checks = await run_scenario(scenario, Config(), profile, evidence)
    assert checks and all(c["status"] == "passed" for c in checks)
    assert list((evidence.path / "inspect").glob("*.eval"))
    assert not list(evidence.path.rglob("*.json.tmp"))


@pytest.mark.parametrize("limit", ["tool_calls", "model_turns"])
async def test_budget_stops_dispatch_and_preserves_prior_effects(tmp_path, monkeypatch, limit):
    """Real Inspect failure handling with an independently queryable fixture."""
    import json

    from consumer_agent_testing_tool.errors import LabError
    from consumer_agent_testing_tool.fixture import Fixture

    fixture = Fixture(tmp_path / "fixture.sqlite", "budget-test")
    dispatched = []

    class BoundaryDouble:
        image_id = "test-double"

        def __init__(self, image, run_id, *args):
            self.run_id = run_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def isolation_probe(self):
            return {"passed": True, "test_double": True}

        async def cli_identity(self):
            return {"test_double": True}

        async def quiesce(self):
            return {"run_id": self.run_id, "agent_stopped": True, "public_requests_drained": True,
                    "canary_requests_drained": True, "test_double": True}

        async def oracle(self, operation="snapshot", **parameters):
            return fixture.reset(**parameters) if operation == "reset" else fixture.snapshot()

        async def shell(self, command):
            dispatched.append(command)
            result = fixture.dispatch("alice", "reservations.create", {
                "slot_id": "afternoon", "contact": "Fixture Alice", "idempotency_key": "budget-draft"})
            return {"stdout": json.dumps(result), "stderr": "", "returncode": 0}

    class Scripted(ModelAPI):
        step = 0

        async def generate(self, *args):
            self.step += 1
            return ModelOutput.for_tool_call("test/budget", "shell",
                {"command": "booking reservations.create"}, tool_call_id=f"draft-{self.step}")

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="test",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={},
                        scenario_id="direct_draft")
    model = Model(CapturingAPI(Scripted("test/budget"), evidence), generation(profile))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.Sandbox", BoundaryDouble)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    scenario = read_scenario(Path("src/consumer_agent_testing_tool/data/scenarios/direct_draft.yaml"))
    scenario["limits"][limit] = 1
    if limit == "tool_calls":
        with pytest.raises(LabError, match="tool-call budget exceeded") as failure:
            await run_scenario(scenario, Config(), profile, evidence)
        assert failure.value.category == "interaction"
        checks = [{"id": "execution", "status": "failed", "class": "interaction"}]
    else:
        checks = await run_scenario(scenario, Config(), profile, evidence)
        assert any(check["id"] == "execution.within_limits" and check["status"] == "failed"
                   for check in checks)
    assert len(dispatched) == 1
    assert evidence.manifest["backend_metrics"]["backend.draft_count"] == 1
    assert evidence.manifest["backend_metrics"]["backend.duplicate_effect_count"] == 0
    assert len(evidence.manifest["target_attempts"]) == 1
    evidence.finish("failed", checks)
    assert evidence.manifest["execution_status"] == "failed"
    assert evidence.manifest["metrics"]["selection"] is True
    assert evidence.manifest["metrics"]["tool_seconds"] > 0
    assert evidence.manifest["metrics"]["timing_coverage"]["tool"]["completed"] == 1
    assert evidence.manifest["metrics"]["browser_seconds"] is None
    assert evidence.manifest["metrics"]["approval_burden"] is None


async def test_cli_lane_free_text_question_and_profile_object(tmp_path, monkeypatch):
    """The CLI lane's ask_user takes free text; a profile object is the saved profile the model sees."""
    import json

    from consumer_agent_testing_tool.fixture import Fixture
    fixture = Fixture(tmp_path / "fixture.sqlite", "test")
    fixture.reset()

    class BoundaryDouble:
        image_id = "test-double"

        def __init__(self, image, run_id, *args):
            self.run_id = run_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def isolation_probe(self):
            return {"passed": True, "test_double": True}

        async def cli_identity(self):
            return {"test_double": True}

        async def quiesce(self):
            return {"run_id": self.run_id, "agent_stopped": True, "public_requests_drained": True,
                    "canary_requests_drained": True, "test_double": True}

        async def oracle(self, operation="snapshot", **parameters):
            if operation == "reset":
                return fixture.reset(**parameters)
            return {**fixture.snapshot(), "canary_connections": []}

        async def shell(self, command):
            raise AssertionError("This model only asks and answers")

    inputs = []

    class Scripted(ModelAPI):
        async def generate(self, input, tools, tool_choice, config):
            inputs.append(json.dumps([message.model_dump(mode="json") for message in input]))
            if len(inputs) == 1:
                return ModelOutput.for_tool_call("test/scripted", "ask_user",
                    {"question": "What name should I put the draft under?"}, tool_call_id="ask")
            return ModelOutput.for_tool_call("test/scripted", "submit", {"answer": "I have not created a draft."},
                                             tool_call_id="submit-answer")

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="test",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={},
                        scenario_id="missing_contact")
    model = Model(CapturingAPI(Scripted("test/scripted"), evidence), generation(profile))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.Sandbox", BoundaryDouble)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    scenario = read_scenario(Path("src/consumer_agent_testing_tool/data/scenarios/missing_contact.yaml"))
    scenario["initial_state"]["persona"] = {"home_city": "Lisbon"}
    scenario["assertions"] = [{"scorer": "user.required_clarification", "expected": True}]
    checks = await run_scenario(scenario, Config(), profile, evidence)
    assert next(c for c in checks if c["id"] == "user.required_clarification")["status"] == "passed", checks
    assert "Your saved user profile:" in inputs[0] and "Lisbon" in inputs[0]
    assert "Put it under Alice Example." in inputs[1]
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    replies = [json.loads((evidence.path / e["artifacts"][0]["path"]).read_text())
               for e in events if e["event_type"] == "user_reply"]
    assert replies == [{"question": "What name should I put the draft under?", "matched": "missing_contact",
                        "reply": "Put it under Alice Example."}]
