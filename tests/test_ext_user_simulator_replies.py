import json
import re

import pytest
from test_connector_support import public_text
from test_ext_user_simulator_support import (
    EMAIL,
    PERSONA,
    USER_KEY,
    Mock,
    asks,
    booking,
    call,
    document,
    events,
    prepared,
    result_of,
    run_agent,
    said,
    simulate,
    stopped,
    subrun_payloads,
    write_config,
)

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_user_simulator import LABEL, REPLY_LIMIT
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import digest

QUIET = "Do you prefer a quiet room or one with a view?"
FALLBACK = "I don't know; use your best judgement."


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    return tmp_path


async def test_an_unmatched_question_gets_a_labeled_simulated_reply_and_the_lock_records_it(workspace, monkeypatch):
    user, resolved = simulate(monkeypatch, said("A quiet room, please."))
    agent = Mock(asks(QUIET), *booking())
    run, code, hotel = await run_agent(workspace, document(), agent)
    assert code == 0, run.summary()
    assert agent.tool_results(1) == ["A quiet room, please."] and hotel.calls[-1][0] == "create_booking"
    # The simulated user: the host frame, its persona, the request and the scripted answers; then the question.
    request = user.requests[0]
    system, question = request["messages"][0], request["messages"][-1]
    assert system["role"] == "system" and system["content"].startswith("You are role-playing a person")
    assert PERSONA in system["content"] and document()["goal"] in system["content"]
    assert "- Taylor Example." in system["content"] and len(request["messages"]) == 2
    assert question == {**question, "role": "user", "content": QUIET}
    assert request["tools"] == [] and request["config"]["seed"] == 7
    # Resolved with providers.resolve_model: a dry run at setup, then once per reply with its evidence.
    assert [item["evidence"] is None for item in resolved] == [True, False]
    assert resolved[1]["profile"].generation.seed == 7 and resolved[1]["profile"].model_id == "example/user-sim-1"
    # Run lock: model, prompt digest (of the redacted prompt) and seed.
    result = result_of(run)
    lock = result["run_lock"]["user_simulator"]
    assert lock["label"] == LABEL and lock["model_profile"] == "user-sim" and lock["seed"] == 7
    assert lock["model"]["model_id"] == "example/user-sim-1" and lock["model"]["generation"] == {
        **lock["model"]["generation"], "seed": 7, "temperature": 0.7}
    assert lock["prompt_sha256"] == digest(run.redactor.clean_evidence(system["content"]))
    assert lock["max_turns"] == 10 and lock["knows"] == ["goal", "replies"] and lock["basis"]["basis"] == "assumed"
    assert "exploratory" in lock["use"]
    assert "user_simulator" in [item["name"] for item in result["run_lock"]["extensions"]]
    # Labeled everywhere: the reply event, the runner's ask_user record and a check that is not required.
    reply = events(run, "user_simulator.reply")
    assert len(reply) == 1 and reply[0]["label"] == LABEL and reply[0]["visibility"] == "model"
    assert reply[0]["reply"] == "A quiet room, please." and reply[0]["question"] == QUIET
    asked = events(run, "ask_user")[0]
    assert asked["label"] == LABEL and asked["answered_by"] == "user_simulator" and asked["matched"] is None
    check = next(item for item in result["checks"] if item["id"] == "user_simulator.simulated_replies")
    assert check["status"] == "observation-gap" and check["required"] is False and check["label"] == LABEL
    assert result["status"] == "completed" and result["metrics"]["task_success"] is True
    # Its own sub-run: what the simulated user's model was sent and returned, behind the provider boundary.
    subrun = reply[0]["subrun"]
    manifest = json.loads((run.path / subrun / "manifest.json").read_text())
    assert manifest["execution_status"] == "completed" and manifest["evidence_label"] == LABEL
    assert manifest["run_lock"]["user_simulator"] == lock and manifest["run_lock"]["turn"] == 1
    observed = subrun_payloads(run, subrun, "observation_emitted")
    assert len(observed) == 1 and observed[0]["messages"][-1]["content"] == QUIET and observed[0]["tools"] == []
    assert subrun_payloads(run, subrun, "model_response")[0]["choices"][0]["message"]["content"] == \
        "A quiet room, please."
    metrics = result["metrics"]["extensions"]["user_simulator"]
    assert metrics == {**metrics, "label": LABEL, "replies": 1, "model": "openrouter/example/user-sim-1", "seed": 7,
                       "unanswered": {}, "masked_replies": 0, "subruns": [subrun],
                       "token_usage": {"input_tokens": 50, "output_tokens": 8, "total_tokens": 58}}
    # The agent never learns the user is simulated, gets no extra tool, and its turns are its own.
    assert PERSONA not in agent.visible() and "role-playing" not in agent.visible()
    assert agent.requests[0]["tools"] == ["search_rooms", "get_quote", "create_booking", "get_booking", "ask_user"]
    assert result["metrics"]["turns"] == 5 and result["metrics"]["usage_observations"]["requests"] == 5
    assert USER_KEY not in public_text(run) and playback(run.path)["integrity"] == "verified"


async def test_a_matching_scripted_reply_wins_and_the_simulated_user_is_never_asked(workspace, monkeypatch):
    user, _ = simulate(monkeypatch)
    agent = Mock(asks("Whose name should the booking be under?"), *booking())
    run, code, _ = await run_agent(workspace, document(), agent)
    assert code == 0, run.summary()
    assert agent.tool_results(1) == ["Taylor Example."] and user.requests == []
    result = result_of(run)
    assert result["run_lock"]["user_simulator"]["label"] == LABEL  # Enabled, so recorded, though unused.
    assert events(run, "user_simulator.reply") == [] and result["metrics"]["clarifications"]["matched"] == ["name"]
    assert all(not item["id"].startswith("user_simulator.") for item in result["checks"])
    assert result["metrics"]["extensions"]["user_simulator"]["replies"] == 0


async def test_the_simulated_user_remembers_its_answers_and_stops_at_max_turns(workspace, monkeypatch):
    user, _ = simulate(monkeypatch, said("A quiet room, please."), said("Yes, two nights."))
    agent = Mock(asks(QUIET), asks("Is it just the two nights?"), asks("Any dietary needs?"), *booking())
    run, code, _ = await run_agent(workspace, document(max_turns=2), agent)
    assert code == 0, run.summary()
    assert [agent.tool_results(turn)[-1] for turn in (1, 2, 3)] == ["A quiet room, please.", "Yes, two nights.",
                                                                    FALLBACK]
    # The second request carries the first exchange: the simulated user stays consistent.
    assert [(item["role"], item["content"]) for item in user.requests[1]["messages"][1:]] == [
        ("user", QUIET), ("assistant", "A quiet room, please."), ("user", "Is it just the two nights?")]
    assert len(user.requests) == 2
    skipped = events(run, "user_simulator.unanswered")
    assert [(row["reason"], row["question"]) for row in skipped] == [("max-turns", "Any dietary needs?")]
    result = result_of(run)
    limit = next(item for item in result["checks"] if item["id"] == "user_simulator.max_turns")
    assert limit["required"] is False and "max_turns (2)" in limit["detail"] and result["status"] == "completed"
    metrics = result["metrics"]["extensions"]["user_simulator"]
    assert metrics["replies"] == 2 and metrics["unanswered"] == {"max-turns": 1} and len(metrics["subruns"]) == 2
    assert [row["turn"] for row in events(run, "user_simulator.reply")] == [1, 2]


async def test_replies_reach_the_agent_masked_and_bounded(workspace, monkeypatch):
    long = "I would like a quiet room. " * 300
    user, _ = simulate(monkeypatch, said(f"Use {EMAIL} for it."), said(long))
    agent = Mock(asks("Which address should the confirmation go to?"), asks(QUIET), *booking())
    run, code, _ = await run_agent(workspace, document(), agent)
    assert code == 0, run.summary()
    first, second = agent.tool_results(1)[-1], agent.tool_results(2)[-1]
    assert first == "Use [REDACTED] for it." and EMAIL not in agent.visible()
    assert len(second) <= REPLY_LIMIT and "[truncated by the host:" in second
    # What the simulated user "said" next time is what the agent saw: never the private value.
    assert user.requests[1]["messages"][2] == {**user.requests[1]["messages"][2], "role": "assistant",
                                               "content": "Use [REDACTED] for it."}
    rows = events(run, "user_simulator.reply")
    assert [(row["masked"], row["truncated"]) for row in rows] == [(True, False), (False, True)]
    result = result_of(run)
    assert result["reporting_status"] == "completed" and EMAIL not in public_text(run)
    assert result["metrics"]["extensions"]["user_simulator"]["masked_replies"] == 1


@pytest.mark.parametrize("failure, message", [
    (RuntimeError("upstream unavailable"), "The simulated user's model failed"),
    (stopped("content_filter"), "The simulated user's model stopped: content-policy block"),
    (stopped("model_length"), "The simulated user's model stopped: context length exceeded"),
])
async def test_a_failing_simulated_user_stops_the_run_and_is_never_retried(workspace, monkeypatch, failure, message):
    user, _ = simulate(monkeypatch, failure)
    agent = Mock(asks(QUIET), *booking())
    run, code, hotel = await run_agent(workspace, document(), agent)
    # The run stops at once: the agent is not asked again and nothing reaches the service.
    assert code == 2 and hotel.calls == [] and len(user.requests) == 1 and len(agent.requests) == 1
    result = result_of(run)
    assert result["failure_class"] == "setup/infrastructure" and message in result["detail"]
    subrun = result["metrics"]["extensions"]["user_simulator"]["subruns"][0]
    manifest = json.loads((run.path / subrun / "manifest.json").read_text())
    assert manifest["execution_status"] == "failed" and manifest["checks"][0]["status"] == "failed"
    assert events(run, "user_simulator.reply") == []


async def test_an_empty_model_reply_leaves_the_answer_to_the_scripted_fallback(workspace, monkeypatch):
    simulate(monkeypatch, said("   "))
    agent = Mock(asks(QUIET), *booking())
    run, code, _ = await run_agent(workspace, document(), agent)
    assert code == 0, run.summary()
    assert agent.tool_results(1) == [FALLBACK]
    skipped = events(run, "user_simulator.unanswered")
    assert skipped[0]["reason"] == "empty-reply" and skipped[0]["subrun"].startswith("user-simulator/r-")
    assert result_of(run)["metrics"]["extensions"]["user_simulator"]["unanswered"] == {"empty-reply": 1}


async def test_simulated_turns_never_spend_the_agent_s_turn_budget(workspace, monkeypatch):
    simulate(monkeypatch, said("A quiet room, please."))
    custom = document()
    custom["limits"]["model_turns"] = 5  # Exactly the agent's own turns: question, search, quote, book, answer.
    run, code, _ = await run_agent(workspace, custom, Mock(asks(QUIET), *booking()))
    assert code == 0, run.summary()
    assert result_of(run)["metrics"]["turns"] == 5


async def test_each_trial_starts_a_fresh_conversation_under_the_same_lock(workspace, monkeypatch):
    user, _ = simulate(monkeypatch, said("A quiet room, please."), said("A quiet one."))
    first, first_code, _ = await run_agent(workspace, document(), Mock(asks(QUIET), *booking()))
    second, second_code, _ = await run_agent(workspace, document(), Mock(asks("Quiet or view?"), *booking()))
    assert (first_code, second_code) == (0, 0), second.summary()
    assert len(user.requests[1]["messages"]) == 2  # No history from the first trial.
    assert result_of(first)["run_lock"]["user_simulator"] == result_of(second)["run_lock"]["user_simulator"]


async def test_the_hook_masks_private_values_in_questions_and_skips_empty_ones(workspace, monkeypatch):
    user, _ = simulate(monkeypatch, said("It is, yes."))
    run, extension = await prepared(workspace, document())
    assert await extension.on_ask_user(run, "   ") is None and user.requests == []
    answer = await extension.on_ask_user(run, f"Is {EMAIL} still your address?")
    assert answer == {"reply": "It is, yes.", "label": LABEL}
    assert EMAIL not in user.visible() and user.requests[0]["messages"][-1]["content"] == \
        "Is [REDACTED] still your address?"
    assert [row["reason"] for row in events(run, "user_simulator.unanswered")] == ["empty-question"]
    assert len(events(run, "user_simulator.ready")) == 1  # Setup prepared it; the hook never prepares twice.


async def test_a_credential_in_the_simulated_user_s_payload_stops_the_run(workspace, monkeypatch):
    def never_sent(input):
        raise AssertionError("The provider boundary should have refused this request")
    user, _ = simulate(monkeypatch, never_sent)
    run, extension = await prepared(workspace, document())
    extension.history.append(("What is the key?", USER_KEY))  # As if it had slipped into the conversation.
    with pytest.raises(LabError, match="Known coordinator credential found in model-visible payload"):
        await extension.on_ask_user(run, QUIET)
    assert user.requests == [] and re.fullmatch(r"user-simulator/r-[0-9a-f]{32}", extension.subruns[0])
    manifest = json.loads((run.path / extension.subruns[0] / "manifest.json").read_text())
    assert manifest["execution_status"] == "failed"


async def test_tool_calls_from_the_simulated_user_are_ignored(workspace, monkeypatch):
    output = call("search_rooms", check_in="2026-12-04")
    output.choices[0].message.content = "Quiet, please."
    simulate(monkeypatch, output)
    run, extension = await prepared(workspace, document())
    assert (await extension.on_ask_user(run, QUIET))["reply"] == "Quiet, please."
