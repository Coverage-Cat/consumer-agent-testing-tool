import json

import pytest
from test_connector_support import scenario
from test_ext_user_simulator_support import (
    EMAIL,
    PERSONA,
    PROFILES,
    USER_KEY,
    Mock,
    asks,
    booking,
    document,
    events,
    make_run,
    prepared,
    result_of,
    run_agent,
    said,
    simulate,
    simulator,
    write_config,
)

from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    ConnectorRun,
    ScriptedDriver,
    discover,
    extension_module,
)
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.ext_user_simulator import (
    FRAME,
    LABEL,
    UserSimulator,
    UserSimulatorConfig,
    refuse_simulated,
    simulated,
)

QUIET = "Do you prefer a quiet room or one with a view?"


@pytest.mark.parametrize("change, message", [
    ({"persona": "   "}, "persona describes the simulated user"),
    ({"seed": True}, "seed: Input should be a valid integer"),
    ({"seed": -1}, "seed: Input should be greater than or equal to 0"),
    ({"max_turns": 0}, "max_turns: Input should be greater than or equal to 1"),
    ({"max_turns": 51}, "max_turns: Input should be less than or equal to 50"),
    ({"knows": ["goal", "goal"]}, "knows lists goal, replies or identity at most once each"),
    ({"knows": ["memory"]}, "knows.0: Input should be 'goal', 'replies' or 'identity'"),
    ({"temperature": 0.2}, "temperature: Extra inputs are not permitted"),
    ({"model_profile": "two words"}, "model_profile: String should match pattern"),
    ({"persona": None}, "persona: Input should be a valid string"),
])
def test_the_config_is_strict_and_checked_when_the_scenario_loads(change, message):
    config = {**simulator(), **change}
    with pytest.raises(LabError, match="Invalid extensions.user_simulator") as raised:
        parse_scenario(scenario(extensions={"user_simulator": config}))
    assert message in str(raised.value)


def test_the_extension_is_discovered_by_name_with_its_defaults():
    loaded = parse_scenario(scenario(extensions={"user_simulator": {"model_profile": "user-sim", "persona": PERSONA}}))
    assert extension_module("user_simulator") == "consumer_agent_testing_tool.ext_user_simulator"
    assert discover("user_simulator") is UserSimulator and EXTENSIONS["user_simulator"] is UserSimulator
    config = UserSimulatorConfig.model_validate(loaded.scenario.extensions["user_simulator"])
    assert (config.seed, config.max_turns, config.knows) == (None, 10, ["goal", "replies"])


def test_knows_selects_what_the_simulated_user_is_told():
    loaded = parse_scenario(scenario()).scenario

    def prompt(**config):
        return UserSimulator(UserSimulatorConfig.model_validate(simulator(**config))).system_prompt(loaded)
    full = prompt()
    assert full.startswith(FRAME[0]) and f"Who you are:\n{PERSONA}" in full
    assert f"What you asked your assistant to do:\n{loaded.goal}" in full
    assert "Answers you have already decided on (when a question asks for one of them, give it):\n- Taylor Example." \
        in full and "About you:" not in full
    assert prompt(knows=[]) == "\n".join([*FRAME, "", "Who you are:", PERSONA])
    assert "About you:\n- name: Taylor Example\n- timezone: America/Los_Angeles" in prompt(knows=["identity"])
    # Test framing never reaches the simulated user, who might repeat it to the agent.
    for word in ("test", "evaluat", "scor", "oracle", "fixture", "scenario", "grade", "simulat"):
        assert word not in "\n".join(FRAME).lower()


def refused(run, code, failure_class, message, *models, hotel=None):
    """A setup refusal: sealed with its reason, before the lock entry, any model request or service call."""
    result = result_of(run)
    assert run.outcome["exit_code"] == code, run.summary()
    assert result["failure_class"] == failure_class and message in result["detail"], result["detail"]
    assert result["failure_stage"] == "project" and "user_simulator" not in result["run_lock"]
    assert all(model.requests == [] for model in models) and (hotel is None or hotel.calls == [])


async def test_scripted_replays_and_decision_contracts_refuse_a_simulated_user(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    user = simulate(monkeypatch)[0]
    run = make_run(tmp_path, document(), ScriptedDriver([{"tool": "ask_user", "arguments": {"question": QUIET}},
                                                         {"answer": "Done."}]))
    assert await run.run() == 3
    refused(run, 3, "unsupported", "a scripted replay is deterministic connector evidence", user)
    agent = Mock(asks(QUIET), *booking())
    run, code, hotel = await run_agent(tmp_path, document(), agent, before=lambda run: run.run_lock.update(
        decision_contract={"sha256": "0" * 64}))
    assert code == 3
    refused(run, 3, "unsupported", "a decision contract counts deterministic trials only", user, agent, hotel=hotel)


@pytest.mark.parametrize("config, environment, code, failure_class, message", [
    ({"model_profile": "nope"}, {}, 2, "setup/infrastructure",
     ("'nope' is not a model profile of consumer-agent-testing-tool.yaml; configured: glimmer, meta-responses, "
      "template, user-sim")),
    ({"model_profile": "meta-responses"}, {}, 2, "setup/infrastructure",
     ("model profile 'meta-responses' with seed 7: The Responses API exposes no seed parameter; it cannot be "
      "silently dropped")),
    ({"model_profile": "glimmer"}, {}, 3, "unsupported", "Local Glimmer inference is outside local-consumer-v1"),
    ({"persona": f"You are Taylor; your email is {EMAIL}."}, {}, 2, "setup/infrastructure",
     "persona contains private.email"),
    ({"persona": f"You are Taylor. Your key is {USER_KEY}."}, {}, 2, "setup/infrastructure",
     "persona contains a registered credential"),
    ({}, {"USER_SIM_KEY": None}, 2, "authentication", "Missing credential: set USER_SIM_KEY"),
    ({}, {"CEDAR_STAYS_KEY": USER_KEY}, 2, "authentication", "must not reuse the cedar-stays connector's credential"),
], ids=["unknown-profile", "seed-refused", "glimmer", "private-persona", "credential-persona", "missing-key",
        "reused-key"])
async def test_setup_refuses_profiles_personas_and_keys_before_anything_runs(
        tmp_path, monkeypatch, config, environment, code, failure_class, message):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    for name, value in environment.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    user, agent = simulate(monkeypatch)[0], Mock(asks(QUIET), *booking())
    run, result_code, hotel = await run_agent(tmp_path, document(**config), agent)
    assert result_code == code
    refused(run, code, failure_class, message, user, agent, hotel=hotel)


async def test_an_oauth_connector_s_client_secret_is_never_the_simulated_user_s_key(tmp_path, monkeypatch):
    write_config(tmp_path)
    for name in ("USER_SIM_KEY", "CEDAR_CLIENT_SECRET"):
        monkeypatch.setenv(name, USER_KEY)
    monkeypatch.setenv("CEDAR_CLIENT_ID", "cedar-client-4417")
    custom = document()
    custom["connectors"][0]["auth"] = {"mode": "oauth", "registration": "preregistered",
                                       "client_id_env": "CEDAR_CLIENT_ID", "client_secret_env": "CEDAR_CLIENT_SECRET"}
    user, agent = simulate(monkeypatch)[0], Mock(asks(QUIET), *booking())
    run, code, hotel = await run_agent(tmp_path, custom, agent)
    assert code == 2
    refused(run, 2, "authentication", "must not reuse the cedar-stays connector's credential", user, agent,
            hotel=hotel)


async def test_a_missing_configuration_or_an_adapter_without_a_live_gate_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    agent = Mock(asks(QUIET), *booking())
    run, code, hotel = await run_agent(tmp_path, document(), agent)
    assert code == 2
    refused(run, 2, "setup/infrastructure", "there is no coordinator configuration at", agent, hotel=hotel)
    # The real providers.resolve_model (not replaced here) refuses a template adapter at setup.
    write_config(tmp_path)
    run, code, hotel = await run_agent(tmp_path, document(model_profile="template"), agent)
    assert code == 3
    refused(run, 3, "unsupported", "openai is specified/unverified", agent, hotel=hotel)


async def test_a_profile_without_a_seed_records_none(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    simulate(monkeypatch)
    run, extension = await prepared(tmp_path, document(model_profile="meta-responses", seed=None))
    assert run.run_lock["user_simulator"]["seed"] is None and extension.lock["model"]["provider"] == "meta"
    assert extension.metrics(run)["seed"] is None and extension.metrics(run)["model"] == "meta/muse-spark-1.3"


async def test_the_runner_s_config_path_names_the_coordinator_configuration(tmp_path, monkeypatch):
    other = {"user-sim": {**PROFILES["user-sim"], "model_id": "example/other-sim"}}
    path = write_config(tmp_path, "elsewhere.yaml", **other)
    write_config(tmp_path)  # The workspace default names another model: the run's own file wins.
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    simulate(monkeypatch)
    run = make_run(tmp_path, document(), Mock().driver(), config_path=path)  # As connector-run --config sets it.
    assert run.config_path == path
    await run.setup()
    assert run.run_lock["user_simulator"]["model"]["model_id"] == "example/other-sim"


async def test_the_lock_entry_is_recorded_through_the_runner_s_record_lock(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    simulate(monkeypatch)
    recorded, original = [], ConnectorRun.record_lock

    def record_lock(self, name, entry):
        recorded.append(name)
        return original(self, name, entry)
    monkeypatch.setattr(ConnectorRun, "record_lock", record_lock)
    run, extension = await prepared(tmp_path, document())
    # The documented API (runner keys refused, JSON only), during setup: the agent sub-run's lock copies it.
    assert recorded == ["user_simulator"] and run.run_lock["user_simulator"] == extension.lock


async def test_the_hook_prepares_a_run_whose_setup_never_asked_for_tools(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    simulate(monkeypatch, said("Quiet, please."))
    run = make_run(tmp_path, document(), Mock().driver())
    extension = next(item for item in run.extensions if item.name == "user_simulator")
    assert await extension.on_ask_user(run, QUIET) == {"reply": "Quiet, please.", "label": LABEL}
    ready = events(run, "user_simulator.ready")
    assert len(ready) == 1 and ready[0]["lock"] == run.run_lock["user_simulator"] and USER_KEY in run.redactor.secrets
    assert ready[0]["prompt"].startswith(FRAME[0]) and ready[0]["visibility"] == "developer"


async def test_release_and_decision_code_can_refuse_finished_simulated_runs(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    simulate(monkeypatch, said("A quiet room, please."))
    run, code, _ = await run_agent(tmp_path, document(), Mock(asks(QUIET), *booking()))
    assert code == 0, run.summary()
    result = result_of(run)
    agent_manifest = json.loads((run.path / result["agent_runs"][0] / "manifest.json").read_text())
    assert simulated(result["run_lock"])["label"] == LABEL and simulated(agent_manifest["run_lock"])
    for documents in ([result], [agent_manifest]):
        with pytest.raises(Unsupported, match=f"A decision contract cannot count trials with a {LABEL}"):
            refuse_simulated([{"run_id": "r-plain", "run_lock": {}}, *documents])
    with pytest.raises(Unsupported, match=r"A release suite cannot count .*\(r-1, r-2, r-3, r-4, r-5, \.\.\.\)"):
        refuse_simulated([{"run_id": f"r-{n}", "run_lock": {"user_simulator": {}}} for n in range(1, 7)],
                         suite="A release suite")
    refuse_simulated([{"run_id": "r-plain", "run_lock": {"extensions": []}}, "not a run", {"run_lock": None}])
    assert simulated(None) is None and simulated({"user_simulator": "forged"}) is None
