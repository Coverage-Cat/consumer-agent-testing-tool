"""The scheduler on the runner's extension API: late results shown through run.model_view (so other extensions'
result hooks, content trust's included, see them), holds decided by run.approval_needed, questions the away user
answers late or never through the ask_user hook (after a simulated user's foreground replies), what the agent said
when each background resume ended, and its basis labels in the run lock (Factory.bases). Scripted Inspect model,
in-memory clocked hotel."""
import json

import pytest
from test_connector_support import agent, answer, call, events, result_of, run_connector, structured
from test_ext_scheduler_support import (
    STAY,
    ClockedHotel,
    clocked_services,
    scheduled,
    user_messages,
)
from test_ext_user_simulator_support import (
    USER_KEY,
    Mock,
    asks,
    said,
    simulate,
    simulator,
    write_config,
)

from consumer_agent_testing_tool.connector_run import ConnectorRun
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_scheduler import (
    BASIS,
    CONNECTORS_ARTICLE,
    UNSEEN,
    UNSEEN_AGAIN,
    SchedulerConfig,
    SchedulerExtension,
)

CODE = "482913"
FALLBACK = "I don't know; use your best judgement."
ZONE = "(America/Los_Angeles, UTC-08:00)"


def book(input):
    return call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example")


def ask(question):
    return call("ask_user", question=question)


async def run(tmp_path, document, responses, hotel=None):
    hotel = hotel or ClockedHotel(quote_seconds=3600)
    driver, api = agent(responses)
    found, code, _ = await run_connector(tmp_path, document, driver=driver, hotel=hotel,
                                         services_=clocked_services(hotel))
    return found, code, api, hotel


def scheduler_metrics(found):
    return result_of(found)["metrics"]["extensions"]["scheduler"]


def tool_texts(api, tool):
    return [message["content"] for message in api.inputs[-1]["messages"]
            if message["role"] == "tool" and message.get("function") == tool]


async def test_a_late_result_passes_the_other_extensions_result_hooks_before_the_model_sees_it(tmp_path):
    hotel = ClockedHotel(quote_seconds=3600)
    original = hotel.call

    def with_code(name, arguments):
        result = original(name, arguments)
        if name == "create_booking" and result and not result.get("isError"):
            result["structuredContent"]["login_code"] = CODE  # A one-time code content trust withholds by name.
            result["content"] = [{"type": "text", "text": json.dumps(result["structuredContent"])}]
        return result
    hotel.call = with_code
    document = scheduled(approvals={"answer": "late", "after": "30m"})
    document["extensions"]["content_trust"] = {}
    # A test code the scenario knows: private from the start, so the raw service_response hides it too and the run
    # stays shareable (a code the service sent unannounced makes the run not shareable once it is withheld).
    document["private"]["login_code"] = CODE
    found, code, api, hotel = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("I'll check every hour."),
        call("get_quote", room_id="garden-2", **STAY), book, answer("I asked you to approve the booking."),
        answer("Booked: confirmation BK-1000, $420.00."), answer("Your booking BK-1000 is confirmed; $420.00.")],
        hotel)
    assert code == 0, found.summary()
    relay = next(text for text in user_messages(api) if text.startswith("Approval update"))
    shown = json.loads(relay.split("Result: ", 1)[1])
    # Content trust labeled the relayed result as it labels a model call's, and withheld the code.
    assert shown["provenance"] == {**shown["provenance"], "connector": "cedar-stays", "tool": "create_booking",
                                   "untrusted": True}
    assert shown["result"]["structuredContent"]["login_code"].startswith("[one-time code withheld by the app")
    assert CODE not in json.dumps(api.inputs) and shown["result"]["structuredContent"]["booking_id"] == "BK-1000"
    assert [name for name, _ in hotel.calls].count("create_booking") == 1
    assert scheduler_metrics(found)["approvals"]["outcomes"] == {"result": 1}


async def test_background_holds_follow_run_approval_needed(tmp_path, monkeypatch):
    asked = []
    original = ConnectorRun.approval_needed

    def approval_needed(self, connector, tool):
        asked.append((connector, tool))
        return False if tool == "create_booking" else original(self, connector, tool)
    monkeypatch.setattr(ConnectorRun, "approval_needed", approval_needed)
    found, code, _, hotel = await run(tmp_path, scheduled(), [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."), call("get_quote", room_id="garden-2", **STAY),
        book, answer("Booked BK-1000 for $420.00."), answer("Your booking BK-1000 is confirmed; $420.00.")])
    assert code == 0, found.summary()
    # The runner says no card would show, so the background booking is not held: it takes the normal path.
    assert ("cedar-stays", "create_booking") in asked and events(found, "scheduler.approval_pending") == []
    assert [name for name, _ in hotel.calls] == ["get_quote", "get_quote", "create_booking"]


async def test_an_away_user_answers_a_background_question_late_with_the_scripted_fallback(tmp_path):
    document = scheduled(questions={"answer": "late", "after": "20m"}, checks=[], answer=None)
    found, code, api, hotel = await run(tmp_path, document, [
        answer("I'll check every hour."), ask("Should I book it now at $420.00?"), answer("I asked you first."),
        answer("Understood; I'll keep watching."), answer("Price unchanged.")])
    assert code == 0, found.summary()
    assert tool_texts(api, "ask_user") == [UNSEEN]  # Asked at 10:00, while the user was away.
    task = ("Your user is not in the conversation right now.\n\nCheck the Garden double's price for December 4 to 6 "
            "and book it if the total is at most $500.")
    late = (f"Question answered, Monday, 2026-11-30 10:20 {ZONE}, scheduled task price-watch: your user answered the "
            f"question you asked at 2026-11-30 10:00 (\"Should I book it now at $420.00?\"): {FALLBACK}")
    assert user_messages(api)[1:] == [f"Scheduled task price-watch (run 1), Monday, 2026-11-30 10:00 {ZONE}. {task}",
                                      late, f"Scheduled task price-watch (run 2), Monday, 2026-11-30 11:00 {ZONE}. {task}"]
    # The runner labels the hook's reply; the service clock followed the late answer's time.
    [asked] = events(found, "ask_user")
    assert (asked["answered_by"], asked["label"], asked["matched"]) == ("scheduler", "away user", None)
    assert any("away user (scheduler)" in line for line in result_of(found)["evidence_limits"])
    assert "2026-11-30T10:20:00-08:00" in [post["set"] for post in hotel.clock_posts]
    pending = events(found, "scheduler.question_pending")[0]
    assert pending["due"] == "2026-11-30T10:20:00-08:00" and pending["question"] == "Should I book it now at $420.00?"
    assert events(found, "scheduler.question_answered")[0]["delay_seconds"] == 1200
    metrics = scheduler_metrics(found)
    assert metrics["questions"] == {"answer": "late", "asked": 1, "answered_late": 1, "unanswered": 0,
                                    "repeated_questions": 0, "late_seconds": [1200]}
    # What the agent said as each background resume ended (its notification to the user).
    assert [(row["kind"], row["run"], row["characters"]) for row in metrics["notifications"]] == [
        ("run", 1, len("I asked you first.")), ("question", 1, len("Understood; I'll keep watching.")),
        ("run", 2, len("Price unchanged."))]
    assert metrics["silent_resumes"] == 0


async def test_never_answered_questions_stay_pending_and_a_repeat_is_not_asked_again(tmp_path):
    found, code, api, _ = await run(tmp_path, scheduled(checks=[], answer=None), [
        ask("Is a quiet room important to you?"), answer("Watching the price."), ask("Should I book it now?"),
        ask("should I  book it NOW?"), answer("Waiting for your answer."), answer("Still waiting for your answer.")])
    assert code == 0, found.summary()
    # In the foreground the user is present: the scripted fallback answers. Then the away user never does.
    assert tool_texts(api, "ask_user") == [FALLBACK, UNSEEN, UNSEEN_AGAIN]
    assert [row.get("answered_by") for row in events(found, "ask_user")] == [None, "scheduler", "scheduler"]
    unanswered = events(found, "scheduler.question_unanswered")
    assert [(row["pending"], row["repeats"], row["due"]) for row in unanswered] == [("question-1", 1, None)]
    assert scheduler_metrics(found)["questions"] == {"answer": "never", "asked": 1, "answered_late": 0,
                                                     "unanswered": 1, "repeated_questions": 1, "late_seconds": []}
    assert not any(text.startswith("Question answered") for text in user_messages(api))


async def test_a_simulated_user_answers_in_the_foreground_and_the_away_user_in_the_background(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setenv("USER_SIM_KEY", USER_KEY)
    user, _ = simulate(monkeypatch, said("A quiet room, please."))
    document = scheduled(checks=[], answer=None)
    document["extensions"]["user_simulator"] = simulator()
    model = Mock(asks("Do you prefer a quiet room or one with a view?"), said("Noted; I'll check every hour."),
                 asks("Should I book the quiet room now?"), said("Waiting for your answer."), said("Still waiting."))
    hotel = ClockedHotel()
    found = ConnectorRun(parse_scenario(document, base=tmp_path), driver=model.driver(), root=tmp_path,
                         output=tmp_path / "runs", services=clocked_services(hotel))
    assert await found.run() == 0, found.summary()
    # Hooks run in name order: the scheduler's away user takes background questions before the simulated user.
    assert model.tool_results(1) == ["A quiet room, please."] and model.tool_results(3)[-1] == UNSEEN
    assert len(user.requests) == 1  # The simulated user never saw the background question.
    rows = events(found, "ask_user")
    assert [(row["answered_by"], row["label"]) for row in rows] == [("user_simulator", "simulated user"),
                                                                    ("scheduler", "away user")]
    limit = next(line for line in result_of(found)["evidence_limits"] if "came from an extension" in line)
    assert limit.startswith("2 of 2 ask_user replies") and "away user (scheduler)" in limit
    assert "simulated user (user_simulator)" in limit


async def test_the_run_lock_records_the_scheduler_s_basis_labels(tmp_path):
    found, code, _, _ = await run(tmp_path, scheduled(checks=[], answer=None), [
        answer("Watching."), answer("Same."), answer("Same.")])
    assert code == 0, found.summary()
    entry = next(item for item in result_of(found)["run_lock"]["extensions"] if item["name"] == "scheduler")
    assert entry["bases"] == SchedulerExtension.bases and set(entry["bases"]) == set(BASIS)
    assert entry["bases"]["third-party-push"] == {"basis": "unknown", "source": CONNECTORS_ARTICLE}
    assert entry["bases"]["background-questions"] == {"basis": "assumed"}
    assert {item["basis"] for item in entry["bases"].values()} == {"documented", "assumed", "unknown"}


@pytest.mark.parametrize("questions, message", [
    ({"answer": "late"}, "questions.answer late needs after"),
    ({"answer": "never", "after": "1h"}, "questions.after applies to answer: late only"),
    ({"answer": "late", "after": ["10m", 0]}, "questions.after delays are at least 1 second"),
    ({"answer": "soon"}, "Input should be 'late' or 'never'"),
    ({"answer": "never", "reply": "yes"}, "Extra inputs are not permitted"),
])
def test_question_configs_are_strict(questions, message):
    with pytest.raises(LabError, match=message):
        parse_scenario(scheduled(questions=questions))
    config = SchedulerConfig.model_validate({"jobs": [{"id": "w", "every": "1h", "max_runs": 1, "message": "Check."}]})
    assert config.questions.answer == "never" and config.approvals.answer == "never"


async def test_a_background_question_a_scripted_reply_matches_also_waits_for_the_away_user(tmp_path):
    # The scenario's user.replies answers "who" questions: at once in the foreground, late in the background.
    document = scheduled(questions={"answer": "late", "after": "20m"}, checks=[], answer=None)
    found, code, api, _ = await run(tmp_path, document, [
        ask("Who should the booking be under?"), answer("I'll check every hour."),
        ask("Who is the guest for this booking?"), answer("I asked you first."),
        answer("Understood; I'll keep watching."), answer("Price unchanged.")])
    assert code == 0, found.summary()
    assert tool_texts(api, "ask_user") == ["Taylor Example.", UNSEEN]
    rows = events(found, "ask_user")
    assert [(row["matched"], row.get("answered_by")) for row in rows] == [("name", None), ("name", "scheduler")]
    [pending] = events(found, "scheduler.question_pending")
    assert pending["scripted_reply"] is True
    # The late answer is what the scripted user says to that question: the matched reply, not the fallback.
    late = next(text for text in user_messages(api) if text.startswith("Question answered"))
    assert late.endswith('("Who is the guest for this booking?"): Taylor Example.') and FALLBACK not in late


def test_cards_other_extensions_show_themselves_are_not_held_and_the_trial_says_so():
    from types import SimpleNamespace
    extension = SchedulerExtension(SchedulerConfig.model_validate(scheduled()["extensions"]["scheduler"]))
    extension.resumes = [{"kind": "run", "job": "price-watch", "run": 1, "turn": 2}]
    run_ = SimpleNamespace(scenario=SimpleNamespace(extensions={"scheduler": {}, "custom_connector": {}, "memory": {}}),
                           fixture_clock=None)
    import asyncio
    checks = {check["id"]: check for check in asyncio.run(extension.on_final(run_, "done"))}
    scope = checks["scheduler.away_user_scope"]
    assert scope["status"] == "observation-gap" and scope["required"] is False
    assert "cards custom_connector showed on its own permission engine" in scope["detail"]
