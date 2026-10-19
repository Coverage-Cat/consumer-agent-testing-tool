"""Scheduler edges: declined and oversized late answers, the model-turn budget, tasks cancelled before they run,
expiries without a reference, the muse-cli projection and two connectors. Scripted Inspect model, in-memory hotels."""
import json

from test_connector_multi_support import STAY as MULTI_STAY
from test_connector_multi_support import multi_services, two_connectors
from test_connector_support import (
    agent,
    answer,
    call,
    events,
    result_of,
    run_connector,
    structured,
)
from test_ext_scheduler_support import (
    QUOTE_EXPIRED,
    START,
    STAY,
    ClockedHotel,
    clocked_services,
    scheduled,
    user_messages,
)

from consumer_agent_testing_tool.connector_run import RESUME_LIMIT


def book(input):
    return call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example")


async def run(tmp_path, document, responses, hotel=None):
    hotel = hotel or ClockedHotel(quote_seconds=4 * 3600)
    driver, api = agent(responses)
    found, code, _ = await run_connector(tmp_path, document, driver=driver, hotel=hotel,
                                         services_=clocked_services(hotel))
    return found, code, api, hotel


def check(found, identifier):
    return next(item for item in result_of(found)["checks"] if item["id"] == identifier)


async def test_a_late_answer_the_scripted_user_declines_sends_nothing_and_says_so(tmp_path):
    no_booking = {"id": "no-booking", "source": "oracle", "expect": {"op": "eq", "value": 0},
                  "oracle": {"url": "http://127.0.0.1:8765/__oracle/state", "pointer": "/confirmed_count"}}
    document = scheduled(approvals={"answer": "late", "after": "1h"}, checks=[no_booking], answer=None)
    hotel = ClockedHotel(quote_seconds=4 * 3600, total_minor=61000)  # Over the $500 the user approves.
    found, code, api, hotel = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."), call("get_quote", room_id="garden-2", **STAY),
        book, answer("Asked you to approve $610.00."), answer("You declined, so nothing was booked."),
        answer("Nothing booked.")], hotel)
    assert code == 0, found.summary()
    update = next(text for text in user_messages(api) if text.startswith("Approval update"))
    assert update == ("Approval update, Monday, 2026-11-30 11:00 (America/Los_Angeles, UTC-08:00), scheduled task "
                      "price-watch: about the create_booking request you made at 2026-11-30 10:00: The user declined "
                      "this action.")
    assert "create_booking" not in [name for name, _ in hotel.calls]
    decision = next(row for row in events(found, "approval_decision") if row["tool"] == "create_booking")
    assert decision["allowed"] is False and decision["decision"]["kind"] == "deny"
    assert result_of(found)["metrics"]["extensions"]["scheduler"]["approvals"]["outcomes"] == {"declined": 1}


async def test_a_large_late_result_is_bounded_into_one_resume_message_as_json(tmp_path):
    hotel = ClockedHotel(quote_seconds=4 * 3600)
    original = hotel.call

    def verbose(name, arguments):
        result = original(name, arguments)
        if name == "create_booking" and result and not result.get("isError"):
            result["structuredContent"]["terms"] = "Long terms. " * 2000
        return result
    hotel.call = verbose
    document = scheduled(approvals={"answer": "late", "after": "30m"})
    document["limits"]["result_characters"] = 65536
    found, code, api, _ = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."), call("get_quote", room_id="garden-2", **STAY),
        book, answer("Asked."), answer("Booked BK-1000 for $420.00."), answer("Booked BK-1000 for $420.00.")], hotel)
    assert code == 0, found.summary()
    update = next(text for text in user_messages(api) if text.startswith("Approval update"))
    assert len(update) <= RESUME_LIMIT
    shown = json.loads(update.split("Result: ", 1)[1])  # Still JSON: the relay is bounded, not cut.
    assert shown["truncated"]["field"] == "result" and shown["source"] == "cedar-stays"


async def test_no_late_answer_is_sent_when_the_agent_has_no_turn_left_to_hear_it(tmp_path):
    document = scheduled(approvals={"answer": "late", "after": "30m"})
    document["limits"]["model_turns"] = 5
    found, code, _, hotel = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."), call("get_quote", room_id="garden-2", **STAY),
        book, answer("Asked you to approve the booking at $420.00.")])
    assert "create_booking" not in [name for name, _ in hotel.calls]  # Never sent unheard.
    assert result_of(found)["answer"]["final_answer"] == "Asked you to approve the booking at $420.00."
    assert code == 1 and check(found, "one-confirmed-booking")["status"] == "failed"
    runs = check(found, "scheduler.background_runs")
    assert runs["status"] == "observation-gap" and runs["required"] is False
    assert result_of(found)["metrics"]["extensions"]["scheduler"]["approvals"]["unanswered"] == 1


async def test_tasks_cancelled_before_they_run_are_no_harness_gap(tmp_path):
    document = scheduled(checks=[], answer=None)
    found, code, _, _ = await run(tmp_path, document, [
        call("cancel_scheduled_task", task_id="price-watch"), answer("I won't watch it after all.")])
    assert code == 0, found.summary()
    runs = check(found, "scheduler.background_runs")
    assert runs["status"] == "passed" and runs["detail"] == "Every scheduled task was cancelled before it ran"
    assert events(found, "scheduler.job_cancelled")[0]["at"] == START


async def test_an_expiry_without_its_reference_never_makes_later_calls_stale(tmp_path):
    expiry = [{"tool": "create_booking", "error": QUOTE_EXPIRED, "reference": "/hold_id", "renew": ["get_quote"]}]
    document = scheduled(approvals={"answer": "late", "after": "40m"}, expiry=expiry)
    found, _, _, _ = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."), call("get_quote", room_id="garden-2", **STAY),
        book, answer("Asked."), call("create_booking", quote_id="q-9-garden-2", guest_name="Taylor Example"),
        answer("Asked again."), answer("Nothing yet."), answer("Nothing yet.")], ClockedHotel(quote_seconds=900))
    assert result_of(found)["metrics"]["extensions"]["scheduler"]["expiry"] == {
        "expired": 1, "renewed": 0, "stale_retries": 0}
    assert events(found, "scheduler.expired")[0]["reference"] is None


async def test_background_calls_through_the_muse_cli_projection_wait_and_poll_the_same_way(tmp_path):
    def exec_(tool, **arguments):
        return call("exec", command=f"cedar-stays call-tool --name {tool} --arguments-json '{json.dumps(arguments)}'")
    document = scheduled(approvals={"answer": "never"}, checks=[], answer=None)
    document["projection"] = "muse-cli"
    found, code, api, hotel = await run(tmp_path, document, [
        exec_("get_quote", room_id="garden-2", **STAY), answer("Watching."),
        exec_("get_quote", room_id="garden-2", **STAY),
        lambda input: exec_("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
        answer("Waiting for your approval."), exec_("get_quote", room_id="garden-2", **STAY), answer("Same price.")])
    assert code == 0, found.summary()
    outputs = [json.loads(message["content"]) for message in api.inputs[-1]["messages"] if message["role"] == "tool"]
    held = outputs[2]
    assert held["exit_status"] == 1 and held["stderr"].startswith("Your user has been asked to approve this")
    assert "create_booking" not in [name for name, _ in hotel.calls]
    polls = result_of(found)["metrics"]["extensions"]["scheduler"]["polls"]
    # Each quote is new (a new quote_id), so the background quotes changed what the agent knew.
    assert polls["total"] == 2 and polls["changed"] == 2 and polls["by_tool"] == {"get_quote": 2}


async def test_with_two_connectors_holds_and_polls_name_their_connector(tmp_path):
    document = two_connectors()
    document["checks"], document["answer"] = [], None
    document["extensions"] = {"clock": {"now": START}, "scheduler": {
        "jobs": [{"id": "watch", "every": "1h", "max_runs": 1, "message": "Compare both hotels and book the cheaper."}],
        "approvals": {"answer": "never"}}}
    driver, _ = agent([
        call("pine-lodge__get_quote", room_id="garden-2", **MULTI_STAY), answer("Watching both."),
        call("pine-lodge__get_quote", room_id="garden-2", **MULTI_STAY),
        lambda input: call("pine-lodge__create_booking", quote_id=structured(input, "quote_id"),
                           guest_name="Taylor Example"), answer("Asked you to approve Pine Lodge.")])
    found, code, _ = await run_connector(tmp_path, document, driver=driver, services_=multi_services())
    assert code == 0, found.summary()
    pending = events(found, "scheduler.approval_pending")[0]
    assert pending["connector"] == "pine-lodge" and pending["tool"] == "create_booking"
    polls = result_of(found)["metrics"]["extensions"]["scheduler"]["polls"]
    assert polls["by_tool"] == {"pine-lodge/get_quote": 1}
