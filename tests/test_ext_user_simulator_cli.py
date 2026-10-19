"""A real connector-run CLI trial with a simulated user: the command line and the coordinator configuration it
names (--config), the agent driver with its model preflight, the loopback connector fixture and the real lane
stack. Inspect's mockllm plays both the agent and the simulated user (no model provider is called). Every place
that labels a simulated reply is checked: run lock, ask_user event, clarification metrics, evidence limits,
checks, report.html, the trial summary and the decision-contract refusal."""
import json
from pathlib import Path

import pytest
from inspect_ai.model import Model
from test_connector_e2e_support import NATIVE, document, fixture, happy_path, oracle, public_text
from test_ext_user_simulator_support import (
    PERSONA,
    PROFILES,
    USER_KEY,
    Mock,
    asks,
    said,
    simulate,
    write_config,
)

from consumer_agent_testing_tool import decision, providers
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import Unsupported
from consumer_agent_testing_tool.ext_user_simulator import LABEL, simulated
from consumer_agent_testing_tool.providers import CapturingAPI
from consumer_agent_testing_tool.replay import playback

AGENT_KEY = "sk-or-agent-model-51c0e7d2"
QUIET = "Do you prefer a quiet room or one with a view?"
AGENT = {"provider": "openrouter", "api": "chat-completions", "model_id": "example/agent-1",
         "auth": {"mode": "api_key", "env": "AGENT_KEY"}}
REPLIES = ("A quiet room, please.", "Quiet, thanks.")


def rows(path, kind):
    return [row for row in map(json.loads, (path / "events.jsonl").read_text().splitlines())
            if row["event_type"] == kind]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """The fixture-booking example with a simulated user, and two coordinator configurations: the workspace
    default and the one --config names, whose user-sim profile is the one that must be used."""
    monkeypatch.chdir(tmp_path)
    scenario = document(NATIVE)
    scenario["extensions"]["user_simulator"] = {"model_profile": "user-sim", "persona": PERSONA, "seed": 7}
    (tmp_path / "simulated-booking.yaml").write_text(json.dumps(scenario))  # JSON is YAML.
    write_config(tmp_path)
    (tmp_path / "lab").mkdir()
    write_config(tmp_path / "lab", "coordinator.yaml", agent=AGENT,
                 **{"user-sim": {**PROFILES["user-sim"], "model_id": "example/cli-sim"}})
    monkeypatch.setenv("AGENT_KEY", AGENT_KEY)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    return tmp_path


def agent_model(monkeypatch, agent):
    """The agent's profile resolves to mockllm (behind CapturingAPI, as the real adapter is) after a passing
    preflight; both are the CLI's own calls into providers."""
    async def preflight(profile, evidence):
        return [{"id": "native-tool-continuation", "status": "passed"}]

    def resolve(profile, evidence=None):
        assert profile.model_id == AGENT["model_id"]
        model = agent.model()
        return Model(CapturingAPI(model.api, evidence), model.config) if evidence is not None else model
    monkeypatch.setattr(providers, "preflight", preflight)
    monkeypatch.setattr(providers, "resolve_model", resolve)


async def test_a_connector_run_cli_trial_labels_simulated_replies_end_to_end(workspace, monkeypatch, capsys):
    agent = Mock(asks(QUIET), *happy_path(), asks(QUIET), *happy_path())
    agent_model(monkeypatch, agent)
    user, _ = simulate(monkeypatch, *map(said, REPLIES))
    async with fixture() as running:
        code = await execute(parser().parse_args([
            "connector-run", "simulated-booking.yaml", "--config", "lab/coordinator.yaml", "--model-profile", "agent",
            "--set", f"PORT={running.port}", "--trials", "2"]))
        state = oracle(running)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "Connector trials: 2 of 2 succeeded" in captured.err and state["confirmed_count"] == 1
    printed = [Path(line) for line in captured.out.splitlines() if line and not line.startswith("Connector report:")]
    trials = [path for path in printed if path.name.startswith("connector-")]
    assert len(trials) == 2
    # The agent heard each trial's simulated reply; the simulated user heard only the question, after its persona.
    assert agent.tool_results(1) == [REPLIES[0]] and agent.tool_results(6) == [REPLIES[1]]
    assert len(user.requests) == 2 and user.requests[0]["messages"][-1]["content"] == QUIET
    assert PERSONA in user.requests[0]["messages"][0]["content"] and PERSONA not in agent.visible()
    for path, reply in zip(trials, REPLIES, strict=True):
        result = json.loads((path / "result.json").read_text())
        assert result["status"] == "completed" and result["metrics"]["task_success"] is True
        lock = result["run_lock"]
        assert lock["driver"] == "agent" and lock["model"]["model_id"] == "example/agent-1"
        # The profile came from the file --config named, not the workspace default.
        entry = simulated(lock)
        assert entry["label"] == LABEL and entry["model_profile"] == "user-sim"
        assert entry["model"]["model_id"] == "example/cli-sim" and entry["seed"] == 7
        agent_lock = json.loads((path / result["agent_runs"][0] / "manifest.json").read_text())["run_lock"]
        assert simulated(agent_lock) == entry
        # Labeled where the runner records the reply, counts it and states the evidence limit.
        [asked] = rows(path, "ask_user")
        assert (asked["question"], asked["reply"], asked["matched"]) == (QUIET, reply, None)
        assert (asked["answered_by"], asked["label"]) == ("user_simulator", LABEL)
        assert result["metrics"]["clarifications"] == {"asked": 1, "matched": [], "fallback": 0, "simulated": 1}
        [limit] = [line for line in result["evidence_limits"] if "came from an extension" in line]
        assert limit.startswith("1 of 1 ask_user replies") and f"{LABEL} (user_simulator)" in limit
        check = next(item for item in result["checks"] if item["id"] == "user_simulator.simulated_replies")
        assert check["status"] == "observation-gap" and check["required"] is False and check["label"] == LABEL
        metrics = result["metrics"]["extensions"]["user_simulator"]
        assert metrics["replies"] == 1 and metrics["label"] == LABEL and metrics["model"] == "openrouter/example/cli-sim"
        subrun = json.loads((path / metrics["subruns"][0] / "manifest.json").read_text())
        assert subrun["evidence_label"] == LABEL and subrun["execution_status"] == "completed"
        [shown] = rows(path, "user_simulator.reply")
        assert shown["label"] == LABEL and shown["reply"] == reply
        report = (path / "report.html").read_text()
        assert f"{LABEL} (user_simulator)" in report and "user_simulator.simulated_replies" in report
        # Never deterministic evidence: a decision contract refuses the trial.
        with pytest.raises(Unsupported, match="cannot count trials with a simulated user"):
            decision.refuse_simulated([result])
        assert playback(path)["integrity"] == "verified"
        assert USER_KEY not in public_text(path) and AGENT_KEY not in public_text(path)
    suite = json.loads((printed[-1] / "manifest.json").read_text())
    assert suite["metrics"]["simulated_user_trials"] == 2 and len(suite["child_runs"]) == 2
