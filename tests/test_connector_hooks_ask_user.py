"""The ask_user extension hook: scripted replies first, then on_ask_user in name order, then user.fallback."""
import json
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from test_connector_support import (
    API_KEY,
    Hotel,
    agent,
    answer,
    call,
    events,
    result_of,
    run_connector,
    scenario,
    services,
)

from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    BaseExtension,
    ScriptedDriver,
    run_trials,
)
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError

EMAIL = "taylor.private@example.com"
QUIET = "Do you want a quiet room or one with a view?"


class AnswerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reply: str | None = None
    label: str = "simulated user"
    answer: dict[str, Any] | None = None  # returned as is: a malformed answer
    fail: bool = False


class Asker(BaseExtension):
    """Answers ask_user questions no scripted reply matched (or declines to), and records what it was asked."""
    config_model = AnswerConfig
    asked: ClassVar[list] = []

    async def on_ask_user(self, run, question):
        Asker.asked.append((self.name, question))
        if self.config.fail:
            raise LabError("the simulated user is unavailable")
        if self.config.answer is not None:
            return self.config.answer
        return None if self.config.reply is None else {"reply": self.config.reply, "label": self.config.label}


def asking(monkeypatch, **configs):
    Asker.asked = []
    for name in configs:
        monkeypatch.setitem(EXTENSIONS, name, Asker)
    return scenario(checks=[], answer=None, extensions={"clock": {"now": "2026-11-30T09:00:00-08:00"}, **configs})


def tool_texts(api):
    return [message["content"] for message in api.inputs[-1]["messages"] if message["role"] == "tool"]


async def test_an_unmatched_question_goes_to_the_extensions_in_name_order_and_is_labeled(tmp_path, monkeypatch):
    document = asking(monkeypatch, **{"probe-ask-a": {}, "probe-ask-b": {"reply": "A quiet room, please."},
                                      "probe-ask-c": {"reply": "never asked"}})
    driver, api = agent([call("ask_user", question=QUIET), answer("Noted: a quiet room.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    assert tool_texts(api) == ["A quiet room, please."]
    assert Asker.asked == [("probe-ask-a", QUIET), ("probe-ask-b", QUIET)]  # In name order until one answers.
    asked = events(run, "ask_user")[0]
    assert (asked["matched"], asked["answered_by"], asked["label"]) == (None, "probe-ask-b", "simulated user")
    assert asked["visibility"] == "model" and "probe-ask-b" in asked["provenance"]
    result = result_of(run)
    assert result["metrics"]["clarifications"] == {"asked": 1, "matched": [], "fallback": 0, "simulated": 1}
    assert result["evidence_limits"][-1].startswith("1 of 1 ask_user replies came from an extension")
    assert "simulated user (probe-ask-b)" in result["evidence_limits"][-1]
    assert "answered_by" in result["run_lock"]["bases"]["scripted-user"]["detail"]
    # A question the hooks see is masked (a replay can name a private value; a model's payload never may).
    Asker.asked = []
    script = ScriptedDriver([{"tool": "ask_user", "arguments": {"question": f"Send it to {EMAIL}?"}},
                             {"answer": "Ok."}])
    run, code, _ = await run_connector(tmp_path, document, driver=script)
    assert code == 0 and Asker.asked[0] == ("probe-ask-a", "Send it to [REDACTED]?")


async def test_a_scripted_reply_wins_and_the_fallback_answers_when_no_extension_does(tmp_path, monkeypatch):
    document = asking(monkeypatch, **{"probe-ask-a": {}})
    driver, api = agent([call("ask_user", question="Whose name should the booking be under?"),
                         call("ask_user", question="Any dietary needs?"), answer("Done.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    assert tool_texts(api) == ["Taylor Example.", "I don't know; use your best judgement."]
    assert Asker.asked == [("probe-ask-a", "Any dietary needs?")]  # Never for the matched question.
    result = result_of(run)
    # No extension answered: the clarifications read exactly as before.
    assert result["metrics"]["clarifications"] == {"asked": 2, "matched": ["name"], "fallback": 1}
    assert not any("came from an extension" in line for line in result["evidence_limits"])
    assert [row.get("answered_by") for row in events(run, "ask_user")] == [None, None]


async def test_extension_replies_are_masked_and_bounded_again(tmp_path, monkeypatch):
    reply = f"Use {EMAIL}; my key is {API_KEY}. " + "x" * 9000
    document = asking(monkeypatch, **{"probe-ask-a": {"reply": reply}})
    driver, api = agent([call("ask_user", question="Which address?"), answer("Done.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    [shown] = tool_texts(api)
    assert shown.startswith("Use [REDACTED]; my key is [REDACTED]. xxx") and len(shown) <= 8000
    assert shown.endswith("more characters]") and EMAIL not in json.dumps(api.inputs)
    assert API_KEY not in json.dumps(api.inputs)


async def test_a_failing_or_malformed_ask_user_hook_stops_the_run(tmp_path, monkeypatch):
    for config, detail in (({"fail": True}, "the simulated user is unavailable"),
                           ({"answer": {"reply": "Quiet."}}, "answered ask_user without reply and label text")):
        driver, api = agent([call("ask_user", question="Quiet or view?"), answer("unused")])
        run, code, hotel = await run_connector(tmp_path, asking(monkeypatch, **{"probe-ask-a": config}), driver=driver)
        assert code == 2 and detail in result_of(run)["detail"]
        assert len(api.inputs) == 1 and hotel.calls == []  # Stopped at once: the model is not asked again.


async def test_trial_summaries_count_trials_an_extension_answered(tmp_path, monkeypatch):
    steps = [{"tool": "ask_user", "arguments": {"question": "Quiet or view?"}}, {"answer": "Done."}]
    for configs, expected in (({"probe-ask-a": {"reply": "Quiet."}}, 2), ({"probe-ask-a": {}}, None)):
        loaded = parse_scenario(asking(monkeypatch, **configs), base=tmp_path)
        _, runs, summary = await run_trials(loaded, driver=ScriptedDriver(steps), trials=2, root=tmp_path,
                                            output=tmp_path / "runs", services=services(Hotel()))
        assert summary.get("simulated_user_trials") == expected
        assert [run.result["metrics"]["clarifications"].get("simulated") for run in runs] == [expected and 1] * 2


async def test_deterministic_extension_answers_are_counted_by_label_not_as_a_simulated_user(tmp_path, monkeypatch):
    # The away user's fixed notice is no language model: it is counted per label, and the evidence limit does not
    # call it one. A label other than "simulated user" counts as simulated only with its own run-lock entry.
    steps = [{"tool": "ask_user", "arguments": {"question": "Quiet or view?"}}, {"answer": "Done."}]
    loaded = parse_scenario(asking(monkeypatch, **{"probe-ask-a": {"reply": "Not seen yet.", "label": "away user"}}),
                            base=tmp_path)
    _, runs, summary = await run_trials(loaded, driver=ScriptedDriver(steps), trials=2, root=tmp_path,
                                        output=tmp_path / "runs", services=services(Hotel()))
    result = runs[0].result
    assert result["metrics"]["clarifications"] == {"asked": 1, "matched": [], "fallback": 0,
                                                   "extension_answered": {"away user": 1}}
    [limit] = [line for line in result["evidence_limits"] if "came from an extension" in line]
    assert "away user (probe-ask-a)" in limit and "language model" not in limit
    assert "simulated_user_trials" not in summary and summary["extension_answered_trials"] == 2

    class Locked(Asker):
        def tools(self, run):
            run.record_lock(self.name, {"label": "neighbor", "model_profile": "persona-model"})
            return []
    document = asking(monkeypatch, **{"probe-ask-a": {}})
    document["extensions"]["probe-ask-b"] = {"reply": "A quiet room.", "label": "neighbor"}
    monkeypatch.setitem(EXTENSIONS, "probe-ask-b", Locked)
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps))
    assert code == 0, run.summary()
    assert result_of(run)["metrics"]["clarifications"]["simulated"] == 1
    assert "extension_answered" not in result_of(run)["metrics"]["clarifications"]
    assert any("language model" in line for line in result_of(run)["evidence_limits"])


class HoldingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hold: bool = True


class Holding(BaseExtension):
    """Takes matched questions too (keyword scripted), as the scheduler's away user holds background questions."""
    config_model = HoldingConfig
    asked: ClassVar[list] = []

    async def on_ask_user(self, run, question, scripted=None):
        Holding.asked.append((question, scripted))
        if not self.config.hold:
            return None
        return {"reply": f"Not seen yet (would have said: {scripted})", "label": "away user"}


async def test_a_hook_that_takes_the_scripted_reply_is_asked_first_about_matched_questions(tmp_path, monkeypatch):
    Holding.asked = []
    monkeypatch.setitem(EXTENSIONS, "probe-hold", Holding)
    document = asking(monkeypatch, **{"probe-ask-a": {"reply": "never asked"}})
    document["extensions"]["probe-hold"] = {}
    driver, api = agent([call("ask_user", question="Whose name should the booking be under?"), answer("Done.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    # Only the hook that takes `scripted` saw the matched question, with the reply that matched it.
    assert Holding.asked == [("Whose name should the booking be under?", "Taylor Example.")] and Asker.asked == []
    assert tool_texts(api) == ["Not seen yet (would have said: Taylor Example.)"]
    [asked] = events(run, "ask_user")
    assert (asked["matched"], asked["answered_by"], asked["label"]) == ("name", "probe-hold", "away user")
    assert "scripted reply name matched" in asked["provenance"]
    assert result_of(run)["metrics"]["clarifications"] == {"asked": 1, "matched": ["name"], "fallback": 0,
                                                           "extension_answered": {"away user": 1}}
    # Declining (None) leaves the matched question to its scripted reply; an unmatched one reaches it, in name
    # order, with scripted=None.
    Holding.asked, Asker.asked = [], []
    document["extensions"].update({"probe-ask-a": {}, "probe-hold": {"hold": False}})
    driver, api = agent([call("ask_user", question="Whose name should the booking be under?"),
                         call("ask_user", question="Any dietary needs?"), answer("Done.")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 0, run.summary()
    assert tool_texts(api) == ["Taylor Example.", "I don't know; use your best judgement."]
    assert Holding.asked == [("Whose name should the booking be under?", "Taylor Example."),
                             ("Any dietary needs?", None)]
    assert Asker.asked == [("probe-ask-a", "Any dietary needs?")]
    assert result_of(run)["metrics"]["clarifications"] == {"asked": 2, "matched": ["name"], "fallback": 1}
