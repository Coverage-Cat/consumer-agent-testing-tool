"""The small shipped examples for the presentation extension's voice channel and the user simulator (agent only),
against the loopback connector fixture: the voice example passes a scripted trial, and the user-simulator
example answers an unscripted question with a (mockllm) simulated user and refuses a scripted replay."""
import json

from test_connector_e2e_support import (
    EXAMPLES,
    STAY,
    checks_of,
    fixture,
    oracle,
    result_of,
    run_scenario,
)
from test_ext_user_simulator_support import Mock, call, said, simulate

from consumer_agent_testing_tool.serialization import load

SCRIPT = load(EXAMPLES / "fixture-booking.calls.json")
SIM_KEY = "sk-or-example-user-sim-7c41"


async def test_the_voice_example_passes_a_scripted_trial_with_its_speakable_answer(tmp_path):
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, load(EXAMPLES / "presentation-voice.yaml"), script=SCRIPT)
    assert code == 0, run.summary()
    checks = checks_of(run)
    assert (checks["presentation.length"]["status"], checks["presentation.voice_speakable"]["status"]) == (
        "passed", "passed")
    assert checks["presentation.voice_speakable"]["channel"] == "voice"
    # The booking ID is speakable by declaration; without it, reading it out is a finding.
    document = load(EXAMPLES / "presentation-voice.yaml")
    document["extensions"]["presentation"]["speakable_ids"] = []
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, document, script=SCRIPT)
    assert code == 1
    assert "identifiers from results (booking_id)" in checks_of(run)["presentation.voice_speakable"]["detail"]


async def test_the_user_simulator_example_is_agent_only(tmp_path):
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, load(EXAMPLES / "user-simulator-booking.yaml"),
                                       script=SCRIPT)
    assert code == 3
    assert "a scripted replay is deterministic connector evidence" in checks_of(run)["execution"]["detail"]


async def test_the_user_simulator_example_answers_an_unscripted_question(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_SIM_KEY", SIM_KEY)
    config = tmp_path / "consumer-agent-testing-tool.yaml"
    config.write_text(json.dumps({"schema_version": 1, "model_profiles": {"user-sim": {
        "provider": "openrouter", "api": "chat-completions", "model_id": "example/user-sim-1",
        "auth": {"mode": "api_key", "env": "USER_SIM_KEY"}}}}))
    user, _ = simulate(monkeypatch, said("A ground-floor room, please, if one is free."))
    agent = Mock(call("ask_user", question="Do you have a preference for the room?"),
                 call("search_rooms", **STAY),
                 call("get_quote", room_id="rm-garden-queen", **STAY),
                 call("create_booking", quote_id="q-0001", guest_name="Taylor Example"),
                 said("Booked the ground-floor Garden Queen, December 4 to 6, 2026, confirmation b-0001, "
                      "$448.36 USD in total."))
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, load(EXAMPLES / "user-simulator-booking.yaml"),
                                       driver=agent.driver(), config_path=config)
        state = oracle(running)
    assert code == 0, run.summary()
    assert state["confirmed_count"] == 1
    result = result_of(run)
    assert result["metrics"]["clarifications"]["simulated"] == 1
    assert result["run_lock"]["user_simulator"]["model_profile"] == "user-sim"
    assert checks_of(run)["user_simulator.simulated_replies"]["required"] is False
    # The agent got the simulated user's words; the simulated user never saw the persona's private data.
    assert "ground-floor room, please" in agent.visible() and "taylor@example.com" not in user.visible()
