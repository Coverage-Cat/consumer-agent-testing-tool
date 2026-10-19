"""Scheduler extension runs: background runs on the fixture clock, the service clock, approvals the away user
answers late or never, cancellation and the checks it adds. Scripted Inspect model, in-memory hotel."""
import json
from typing import Any

from inspect_ai.tool import ToolDef, ToolParams
from test_connector_e2e_support import FRAMING  # Test framing must never reach the model.
from test_connector_multi_support import NoConfig
from test_connector_support import (
    agent,
    answer,
    call,
    events,
    public_text,
    result_of,
    run_connector,
    structured,
)
from test_ext_scheduler_support import (
    STAY,
    ClockedHotel,
    clocked_services,
    scheduled,
    user_messages,
)

from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension, ScriptedDriver

EMAIL = "taylor.private@example.com"


def book(input):
    return call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example")


async def run(tmp_path, document, responses, hotel=None):
    hotel = hotel or ClockedHotel()
    driver, api = agent(responses)
    found, code, _ = await run_connector(tmp_path, document, driver=driver, hotel=hotel,
                                         services_=clocked_services(hotel))
    return found, code, api, hotel


def scheduler_metrics(found):
    return result_of(found)["metrics"]["extensions"]["scheduler"]


def model_visible(api):
    """The text the model was shown: message contents, tool names, descriptions and schemas."""
    return json.dumps([[message["content"] for message in request["messages"]] for request in api.inputs]
                      + [request["tools"] for request in api.inputs])


def tool_message(api, tool):
    """The last result the model received for `tool` (as JSON)."""
    for message in reversed(api.inputs[-1]["messages"]):
        if message["role"] == "tool" and message.get("function") == tool:
            return json.loads(message["content"])
    raise AssertionError(f"No {tool} result")


async def test_jobs_resume_the_stopped_agent_on_the_fixture_clock_and_move_the_service_clock(tmp_path):
    document = scheduled([{"id": "price-watch", "every": "1h", "max_runs": 2, "message": "Check the price."},
                          {"id": "digest", "every": "90m", "first": "2026-11-30T09:30:00-08:00", "max_runs": 1,
                           "message": "Summarize the day's changes."}], checks=[], answer=None)
    found, code, api, hotel = await run(tmp_path, document, [
        answer("I'll keep watching."), answer("Summary: nothing new."), answer("Price unchanged."),
        answer("Still unchanged.")])
    assert code == 0, found.summary()
    system = api.inputs[0]["messages"][0]["content"]
    # Upcoming in the standing context, after the clock's line, in the user's time zone.
    assert "Scheduled tasks (Upcoming); each runs in the background at its time:" in system
    assert "- price-watch: every 1 h, next Monday, 2026-11-30 10:00 (America/Los_Angeles, UTC-08:00), 2 runs. Check " \
           "the price." in system and system.index("Current date and time") < system.index("Scheduled tasks")
    # Earliest due first: the digest at 09:30, then the hourly runs at 10:00 and 11:00.
    away = "Your user is not in the conversation right now."
    zone = "(America/Los_Angeles, UTC-08:00)"
    assert user_messages(api)[1:] == [
        f"Scheduled task digest (run 1), Monday, 2026-11-30 09:30 {zone}. {away}\n\nSummarize the day's changes.",
        f"Scheduled task price-watch (run 1), Monday, 2026-11-30 10:00 {zone}. {away}\n\nCheck the price.",
        f"Scheduled task price-watch (run 2), Monday, 2026-11-30 11:00 {zone}. {away}\n\nCheck the price."]
    assert not FRAMING.findall(model_visible(api))
    # The service's clock follows the fixture clock to every run.
    assert [post["set"] for post in hotel.clock_posts] == [
        "2026-11-30T09:30:00-08:00", "2026-11-30T10:00:00-08:00", "2026-11-30T11:00:00-08:00"]
    result = result_of(found)
    assert result["answer"]["final_answer"] == "Still unchanged."
    assert result["metrics"]["extensions"]["clock"]["advanced_seconds"] == 7200
    metrics = scheduler_metrics(found)
    assert metrics["background_runs"] == 3 and metrics["jobs"]["price-watch"]["times"] == [
        "2026-11-30T10:00:00-08:00", "2026-11-30T11:00:00-08:00"]
    assert metrics["clock"] == {"start": "2026-11-30T09:00:00-08:00", "end": "2026-11-30T11:00:00-08:00",
                                "advanced_seconds": 7200.0, "service_clocks": 1, "service_clock_sets": 3}
    assert metrics["push"] == "unknown"
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["scheduler.background_runs"]["status"] == "passed"
    assert checks["scheduler.background_runs"]["actual"] == {"runs": 3, "missed": 0, "not_reached": 0}
    plan = events(found, "scheduler.plan")[0]
    assert plan["jobs"][0]["slots"] == ["2026-11-30T10:00:00-08:00", "2026-11-30T11:00:00-08:00"]
    assert plan["basis"]["third-party-push"]["basis"] == "unknown"
    assert [row["job"] for row in events(found, "scheduler.job_run")] == ["digest", "price-watch", "price-watch"]
    assert [row["extension"] for row in events(found, "extension_resumed")] == ["scheduler"] * 3
    assert [item["name"] for item in result["run_lock"]["extensions"]] == ["clock", "scheduler"]


async def test_a_background_card_waits_for_the_away_user_and_a_late_answer_sends_it_through_the_engine(tmp_path):
    hotel = ClockedHotel(quote_seconds=3600)
    document = scheduled(approvals={"answer": "late", "after": "30m"})
    found, code, api, hotel = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("I'll check every hour."),
        call("get_quote", room_id="garden-2", **STAY), book, answer("I asked you to approve the booking."),
        answer("Booked: confirmation BK-1000, $420.00."), answer("Your booking BK-1000 is confirmed; $420.00.")],
        hotel)
    assert code == 0, found.summary()
    # In the background the sensitive write waits: nothing reached the service until the user answered.
    held = tool_message(api, "create_booking")
    assert held == {"source": "app", "tool": "create_booking", "message": (
        "Your user has been asked to approve this and has not answered yet, so nothing was sent. If they approve "
        "it, the app sends it and tells you the result.")}
    assert [name for name, _ in hotel.calls] == ["get_quote", "get_quote", "create_booking"]
    relay = user_messages(api)[2]
    assert relay.startswith("Approval update, Monday, 2026-11-30 10:30 (America/Los_Angeles, UTC-08:00), scheduled "
                            "task price-watch: your user approved the create_booking request you made at 2026-11-30 "
                            "10:00, and the app sent it. Result: ")
    shown = json.loads(relay.split("Result: ", 1)[1])
    assert shown["source"] == "cedar-stays" and shown["result"]["structuredContent"]["booking_id"] == "BK-1000"
    assert shown["result"]["structuredContent"]["guest_email"] == "[REDACTED]" and EMAIL not in json.dumps(api.inputs)
    # The late answer went through the same path: a card for the scripted user, the dispatch, the evidence.
    card = [row for row in events(found, "approval_card") if row["tool"] == "create_booking"]
    assert len(card) == 1 and card[0]["disclosure"]["total"] == 42000
    request = next(row for row in events(found, "service_request") if row["tool"] == "create_booking")
    assert request["origin"] == "extension:scheduler:price-watch" and request["arguments"]["guest_email"] != EMAIL
    pending = events(found, "scheduler.approval_pending")[0]
    assert pending["requested_at"] == "2026-11-30T10:00:00-08:00" and pending["due"] == "2026-11-30T10:30:00-08:00"
    assert pending["arguments"] == {"quote_id": "q-2-garden-2", "guest_name": "Taylor Example"}  # No host-owned value.
    answered = events(found, "scheduler.approval_answered")[0]
    assert answered["outcome"] == "result" and answered["delay_seconds"] == 1800
    assert hotel.clock_posts[-2]["set"] == "2026-11-30T10:30:00-08:00"  # The service saw the answer's time.
    result = result_of(found)
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["one-confirmed-booking"]["status"] == "passed" and checks["approval.dispatches_match"]["status"] == \
        "passed"
    assert result["metrics"]["selection"]["called"] == ["create_booking", "get_quote"]  # The model's proposals only.
    assert scheduler_metrics(found)["approvals"] == {
        "answer": "late", "pending": 1, "answered_late": 1, "unanswered": 0, "repeated_requests": 0,
        "outcomes": {"result": 1}, "late_seconds": [1800]}
    assert EMAIL not in public_text(found)


async def test_never_answered_cards_stay_pending_and_a_repeat_is_not_asked_again(tmp_path):
    no_booking = {"id": "no-booking", "source": "oracle", "expect": {"op": "eq", "value": 0},
                  "oracle": {"url": "http://127.0.0.1:8765/__oracle/state", "pointer": "/confirmed_count"}}
    document = scheduled(approvals={"answer": "never"}, checks=[no_booking],
                         answer={"claims": [], "relay": [{"id": "awaiting-approval", "patterns": ["approv"]}]})
    found, code, api, hotel = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("I'll check every hour."),
        call("get_quote", room_id="garden-2", **STAY), book, answer("Waiting for your approval."),
        lambda input: call("create_booking", quote_id="q-2-garden-2", guest_name="Taylor Example"),
        answer("The booking still waits for your approval.")])
    assert code == 0, found.summary()
    told = tool_message(api, "create_booking")
    assert told["message"] == ("Your user has not answered the earlier approval request for this same action yet, so "
                               "nothing was sent.")
    assert "create_booking" not in [name for name, _ in hotel.calls]
    metrics = scheduler_metrics(found)
    assert metrics["approvals"]["pending"] == 1 and metrics["approvals"]["unanswered"] == 1
    assert metrics["approvals"]["repeated_requests"] == 1
    unanswered = events(found, "scheduler.approval_unanswered")[0]
    assert unanswered["pending"] == "pending-1" and unanswered["repeats"] == 1 and unanswered["due"] is None
    # No engine card was shown for the held call: the away user never saw it answered.
    assert [row["tool"] for row in events(found, "approval_card")] == ["get_quote"]
    assert events(found, "dispatch_vetoed")[0]["error"].startswith("Your user has been asked")


async def test_calls_a_grant_covers_go_through_in_the_background_and_first_uses_wait(tmp_path):
    document = scheduled(approvals={"answer": "never"}, checks=[], answer=None)
    found, code, _, hotel = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."),
        call("get_quote", room_id="garden-2", **STAY),
        call("search_rooms", check_in="2026-12-04", check_out="2026-12-06", adults=2), answer("Still watching."),
        answer("Nothing new.")])
    assert code == 0, found.summary()
    # get_quote has the foreground's Allow for this task; search_rooms was never used, so its card waits.
    assert [name for name, _ in hotel.calls] == ["get_quote", "get_quote"]
    annotated = events(found, "dispatch_annotated")
    assert annotated[0]["tool"] == "get_quote" and annotated[0]["annotations"] == {"scheduled_task": "price-watch",
                                                                                   "run": 1}
    assert events(found, "scheduler.approval_pending")[0]["tool"] == "search_rooms"
    assert events(found, "scheduler.approval_pending")[0]["tool_class"] == "read"


async def test_the_scheduler_needs_the_fixture_clock_and_service_clocks_that_move(tmp_path):
    document = scheduled()
    document["extensions"]["clock"] = {}
    found, code, _, _ = await run(tmp_path, document, [answer("unused")])
    assert code == 2 and "set extensions.clock.now" in result_of(found)["detail"]
    assert result_of(found)["failure_stage"] == "project"

    class Stuck(ClockedHotel):
        """A service whose clock control answers but never moves."""
    stuck = Stuck()
    services = clocked_services(stuck)
    original = services.http

    def frozen():
        client = original()
        post = client.post

        async def refuse(url, **kwargs):
            response = await post(url, **kwargs)
            stuck.now = stuck.now.replace(year=2020)
            return type(response)(200, json={"clock": "2020-01-01T00:00:00Z"})
        client.post = refuse
        return client
    services.http = frozen
    driver, _ = agent([call("get_quote", room_id="garden-2", **STAY), answer("unused")])
    found, code, _ = await run_connector(tmp_path, scheduled(), driver=driver, hotel=stuck, services_=services)
    assert code == 2 and "did not follow the fixture clock to 2026-11-30T09:00:00-08:00" in result_of(found)["detail"]
    assert stuck.calls == []  # The clock is set before the first call; a stale service clock sends nothing.


async def test_scripted_runs_never_stop_so_no_scheduled_task_runs(tmp_path):
    hotel = ClockedHotel()
    # A script without {"idle": true} steps never stops between steps (tests/test_ext_scheduler_e2e.py has one).
    steps = [{"tool": "get_quote", "arguments": {"room_id": "garden-2", **STAY}},
             {"answer": "Quoted $420.00; I will keep watching."}]
    document = scheduled(checks=[], answer=None)
    found, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver(steps), hotel=hotel,
                                         services_=clocked_services(hotel))
    assert code == 3
    check = next(check for check in result_of(found)["checks"] if check["id"] == "scheduler.background_runs")
    assert check["status"] == "unsupported" and 'a scripted replay stops only at {"idle": true} steps' in \
        check["detail"]


async def test_cancel_scheduled_task_stops_later_runs_and_unknown_tasks_are_model_errors(tmp_path):
    document = scheduled([{"id": "price-watch", "every": "1h", "max_runs": 5, "message": "Check the price."}],
                         checks=[], answer=None)
    found, code, api, _ = await run(tmp_path, document, [
        answer("Watching."), call("cancel_scheduled_task", task_id="price-wach"),
        call("cancel_scheduled_task", task_id="price-watch"), answer("Booked elsewhere; I stopped watching.")])
    assert code == 0, found.summary()
    assert "cancel_scheduled_task" in [tool["name"] for tool in api.inputs[0]["tools"]]
    messages = [message for message in api.inputs[-1]["messages"] if message["role"] == "tool"]
    assert messages[0]["error"]["message"] == "No scheduled task named 'price-wach'; scheduled tasks: price-watch"
    assert messages[1]["content"] == "The scheduled task price-watch is cancelled; it will not run again."
    metrics = scheduler_metrics(found)
    assert metrics["jobs"]["price-watch"]["runs"] == 1 and metrics["jobs"]["price-watch"]["cancelled"] == \
        "2026-11-30T10:00:00-08:00"
    check = next(check for check in result_of(found)["checks"] if check["id"] == "scheduler.background_runs")
    assert check["status"] == "passed" and check["expected"] == {"planned_slots": 1}
    quiet = scheduled(cancel_tool=False, standing_context=False, checks=[], answer=None)
    found, code, api, _ = await run(tmp_path, quiet, [answer("Watching."), answer("Same."), answer("Same.")])
    assert code == 0 and "cancel_scheduled_task" not in json.dumps(api.inputs[0]["tools"])
    assert "Scheduled tasks" not in api.inputs[0]["messages"][0]["content"]


class LongTask(BaseExtension):
    """A probe tool that takes three hours of fixture time, as a long host task would."""
    config_model = NoConfig

    def tools(self, run):
        async def wait_for_delivery(**kwargs: Any):
            run.fixture_clock.advance(3 * 3600, reason="delivery")
            return "Delivered."
        return [ToolDef(wait_for_delivery, name="wait_for_delivery", description="Wait for the delivery.",
                        parameters=ToolParams(), parallel=False, max_output=0)]


async def test_slots_that_pass_while_the_agent_works_run_once_late_and_count_as_missed(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-long-task", LongTask)
    document = scheduled([{"id": "price-watch", "every": "1h", "max_runs": 5, "message": "Check the price."}],
                         checks=[], answer=None)
    document["extensions"]["probe-long-task"] = {}
    found, code, api, hotel = await run(tmp_path, document, [
        answer("Watching."), call("wait_for_delivery"), answer("Delivered; still watching."), answer("Same."),
        answer("Same.")])
    assert code == 0, found.summary()
    runs = events(found, "scheduler.job_run")
    # The 10:00 run took until 13:00: the 11:00, 12:00 and 13:00 slots run once, at 13:00; then 14:00.
    assert [(row["scheduled"], row["at"], row["missed"]) for row in runs] == [
        ("2026-11-30T10:00:00-08:00", "2026-11-30T10:00:00-08:00", 0),
        ("2026-11-30T11:00:00-08:00", "2026-11-30T13:00:00-08:00", 2),
        ("2026-11-30T14:00:00-08:00", "2026-11-30T14:00:00-08:00", 0)]
    assert "(run 2), Monday, 2026-11-30 13:00" in user_messages(api)[2]
    assert [post["set"] for post in hotel.clock_posts] == [
        "2026-11-30T10:00:00-08:00", "2026-11-30T13:00:00-08:00", "2026-11-30T14:00:00-08:00"]
    assert scheduler_metrics(found)["jobs"]["price-watch"]["missed"] == 2
    check = next(check for check in result_of(found)["checks"] if check["id"] == "scheduler.background_runs")
    assert check["status"] == "passed" and check["actual"] == {"runs": 3, "missed": 2, "not_reached": 0}
    assert check["detail"] == "3 background run(s) over 5 slot(s), 2 missed while the agent was busy"


async def test_without_a_service_clock_the_moved_fixture_clock_is_an_observation_gap(tmp_path):
    document = scheduled(advance=[], checks=[], answer=None)
    found, code, _, hotel = await run(tmp_path, document, [answer("Watching."), answer("Same."), answer("Same.")])
    assert code == 0 and hotel.clock_posts == []
    check = next(check for check in result_of(found)["checks"] if check["id"] == "scheduler.service_clock")
    assert check["status"] == "observation-gap" and check["required"] is False
    assert "moved 2 h" in check["detail"]


async def test_a_forward_only_service_clock_moves_by_the_seconds_since_its_last_move(tmp_path):
    hotel = ClockedHotel(now="2026-11-20T17:00:00+00:00")  # Ten days behind the fixture clock: an offset, kept.
    document = scheduled(advance=[{"url": "http://127.0.0.1:8765/__oracle/advance", "send": "seconds"}], checks=[],
                         answer=None)
    found, code, _, hotel = await run(tmp_path, document, [answer("Watching."), answer("Same."), answer("Same.")],
                                      hotel)
    assert code == 0, found.summary()
    # The first POST reads the clock (0 s); each later one moves it by the fixture clock's own step.
    assert hotel.clock_posts == [{"seconds": 0}, {"seconds": 3600}]
    rows = events(found, "scheduler.clock_set")
    assert [row["offset_seconds"] for row in rows] == [-10 * 86400 - 3600, -10 * 86400 - 3600]
    assert rows[0]["sent"] == {"seconds": 0} and rows[1]["reported"] == "2026-11-20T18:00:00+00:00"

    class Skewed(ClockedHotel):
        """A clock control that moves twice as far as asked."""
    skewed = Skewed()
    services = clocked_services(skewed)
    original = services.http

    def doubling():
        client = original()
        post = client.post

        async def twice(url, **kwargs):
            body = kwargs["json"]
            if body.get("advance_seconds"):
                kwargs["json"] = {"advance_seconds": body["advance_seconds"] * 2}
            return await post(url, **kwargs)
        client.post = twice
        return client
    services.http = doubling
    driver, _ = agent([answer("Watching."), answer("unused")])
    found, code, _ = await run_connector(tmp_path, scheduled(
        advance=[{"url": "http://127.0.0.1:8765/__oracle/clock", "send": "advance_seconds"}], checks=[], answer=None),
        driver=driver, hotel=skewed, services_=services)
    assert code == 2 and "did not follow the fixture clock to 2026-11-30T11:00:00-08:00" in result_of(found)["detail"]
