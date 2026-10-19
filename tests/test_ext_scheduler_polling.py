"""What background work costs and how it reacts: polls, conditional reads (an ETag analog), wasted polls,
Retry-After on rate-limited reads and expired quotes after a late approval. Scripted Inspect model,
in-memory clocked hotel (test_ext_scheduler_support)."""
import json

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
    STAY,
    ClockedHotel,
    clocked_services,
    scheduled,
    user_messages,
)

CONFIRM = [{"id": "confirm-watch", "every": "1h", "max_runs": 3,
            "message": "Check whether the booking is confirmed yet and tell your user once it is."}]
CONDITIONAL = {"conditional": {"argument": "/if_none_match", "etag": "/structuredContent/etag",
                               "not_modified": "/structuredContent/not_modified"},
               "volatile": ["/structuredContent/checked_at"]}
RATE_LIMIT = {"retry_after": "/content/0/text/retry_after_seconds"}
EXPIRY = [{"tool": "create_booking", "error": QUOTE_EXPIRED, "reference": "/quote_id", "renew": ["get_quote"]}]


def booked_first():
    """The foreground: quote, book (the user is present and approves), read the status once."""
    return [call("get_quote", room_id="garden-2", **STAY),
            lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
            call("get_booking", booking_id="BK-1000"), answer("Booked BK-1000; it is pending confirmation.")]


async def run(tmp_path, document, responses, hotel):
    driver, api = agent(responses)
    found, code, _ = await run_connector(tmp_path, document, driver=driver, hotel=hotel,
                                         services_=clocked_services(hotel))
    return found, code, api


def polls(found):
    return result_of(found)["metrics"]["extensions"]["scheduler"]["polls"]


async def test_conditional_polls_count_not_modified_answers_and_tokens_the_agent_held_but_did_not_send(tmp_path):
    hotel = ClockedHotel(etag=True, confirm_after=5400)  # Pending until 10:30.
    document = scheduled(CONFIRM, polling=CONDITIONAL)
    found, code, api = await run(tmp_path, document, [
        *booked_first(),
        call("get_booking", booking_id="BK-1000", if_none_match="W/BK-1000-pending"), answer("Still pending."),
        call("get_booking", booking_id="BK-1000"), answer("BK-1000 is confirmed, $420.00."),
        call("get_booking", booking_id="BK-1000"), answer("BK-1000 is still confirmed, $420.00.")], hotel)
    assert code == 0, found.summary()
    # The first poll sent the token the foreground read returned: not modified. The next two did not send the token
    # they held: one found the confirmation, the other re-read unchanged data (checked_at is volatile).
    assert polls(found) == {"total": 3, "by_tool": {"get_booking": 3}, "first": 0, "changed": 1, "wasted": 1,
                            "not_modified": 1, "errors": 0, "conditional": 1, "missed_conditional": 2,
                            "wasted_rate": 0.3333, "conditional_rate": 0.3333, "conditional_configured": True}
    results = [json.loads(message["content"]) for message in api.inputs[-1]["messages"] if message["role"] == "tool"
               and message.get("function") == "get_booking"]
    assert results[1]["result"]["structuredContent"] == {"not_modified": True, "etag": "W/BK-1000-pending"}
    assert results[2]["result"]["structuredContent"]["status"] == "confirmed"


async def test_unchanged_full_rereads_are_wasted_polls_and_volatile_fields_do_not_hide_them(tmp_path):
    hotel = ClockedHotel()
    found, code, _ = await run(tmp_path, scheduled(CONFIRM, polling={"volatile": ["/structuredContent/checked_at"]}), [
        *booked_first(), call("get_booking", booking_id="BK-1000"), answer("Confirmed."),
        call("get_booking", booking_id="BK-1000"), call("get_booking", booking_id="BK-1000"), answer("Confirmed."),
        answer("Nothing new; still $420.00.")], hotel)
    assert code == 0, found.summary()
    found_polls = polls(found)
    # Each read returns a new checked_at; ignoring it, all three polls repeat what the foreground read showed.
    assert {key: found_polls[key] for key in ("total", "first", "changed", "wasted", "conditional_configured")} == {
        "total": 3, "first": 0, "changed": 0, "wasted": 3, "conditional_configured": False}
    assert found_polls["wasted_rate"] == 1.0 and found_polls["missed_conditional"] == 0
    noisy, code, _ = await run(tmp_path, scheduled(CONFIRM), [
        *booked_first(), call("get_booking", booking_id="BK-1000"), answer("Confirmed."), answer("Same."),
        answer("Same: $420.00.")], ClockedHotel())
    assert code == 0 and polls(noisy)["changed"] == 1 and polls(noisy)["wasted"] == 0  # checked_at counted as change.


async def test_a_call_before_retry_after_passes_fails_the_trial_and_waiting_for_the_next_run_honors_it(tmp_path):
    document = scheduled(CONFIRM, rate_limit=RATE_LIMIT)
    hotel = ClockedHotel(rate_limited=1, limit_after=1, retry_after=120)  # The first background read is limited.
    found, code, api = await run(tmp_path, document, [
        *booked_first(), call("get_booking", booking_id="BK-1000"), call("get_booking", booking_id="BK-1000"),
        answer("Confirmed, $420.00."), answer("Confirmed, $420.00."), answer("Confirmed, $420.00.")], hotel)
    assert code == 1
    limited = [json.loads(message["content"]) for message in api.inputs[-1]["messages"] if message["role"] == "tool"
               and message.get("function") == "get_booking"][1]
    assert json.loads(limited["result"]["content"][0])["retry_after_seconds"] == 120
    check = next(check for check in result_of(found)["checks"] if check["id"] == "scheduler.retry_after_respected")
    assert check["status"] == "failed" and check["class"] == "interaction" and check["extension"] == "scheduler"
    assert check["actual"] == {"limited": 1, "honored": 0, "violations": 1}
    assert result_of(found)["detail"].startswith("scheduler.retry_after_respected: 1 call(s) came before Retry-After")
    rows = events(found, "scheduler.retry_after")
    assert rows[0]["honored"] is False and rows[0]["until"] == "2026-11-30T10:02:00-08:00"
    patient, code, _ = await run(tmp_path, document, [
        *booked_first(), call("get_booking", booking_id="BK-1000"),
        answer("The service asked me to wait; I'll check at the next run."),
        call("get_booking", booking_id="BK-1000"), answer("Confirmed, $420.00."), answer("Confirmed, $420.00.")],
        ClockedHotel(rate_limited=1, limit_after=1, retry_after=120))
    assert code == 0, patient.summary()
    check = next(check for check in result_of(patient)["checks"] if check["id"] == "scheduler.retry_after_respected")
    assert check["status"] == "passed" and check["actual"] == {"limited": 1, "honored": 1, "violations": 0}
    assert polls(patient)["errors"] == 1


async def test_an_approval_that_comes_after_the_quote_expired_is_refused_and_renewing_counts(tmp_path):
    hotel = ClockedHotel(quote_seconds=900)
    document = scheduled(approvals={"answer": "late", "after": ["40m", "5m"]}, expiry=EXPIRY)
    found, code, api = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("I'll check every hour."),
        call("get_quote", room_id="garden-2", **STAY),
        lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
        answer("I asked you to approve the booking."),
        call("get_quote", room_id="garden-2", **STAY),
        lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
        answer("That quote expired before you approved; I asked again with a new quote."),
        answer("Booked: confirmation BK-1000, $420.00 total."), answer("Your booking BK-1000 is confirmed, $420.00.")],
        hotel)
    assert code == 0, found.summary()
    updates = [message for message in user_messages(api) if message.startswith("Approval update")]
    # 10:40: approved, sent, refused (the 10:00 quote expired at 10:15). 10:45: the renewed request, booked.
    assert '\\"error\\": \\"quote_expired\\"' in updates[0] and "2026-11-30 10:40" in updates[0]
    assert '"booking_id": "BK-1000"' in updates[1] and "2026-11-30 10:45" in updates[1]
    metrics = result_of(found)["metrics"]["extensions"]["scheduler"]
    assert metrics["expiry"] == {"expired": 1, "renewed": 1, "stale_retries": 0}
    assert metrics["approvals"]["outcomes"] == {"result": 2} and metrics["approvals"]["late_seconds"] == [2400, 300]
    expired = events(found, "scheduler.expired")[0]
    assert expired["reference"] == "q-2-garden-2" and expired["host_dispatch"] is True
    assert [name for name, _ in hotel.calls].count("create_booking") == 2 and hotel.state()["confirmed_count"] == 1
    stale, code, _ = await run(tmp_path, document, [
        call("get_quote", room_id="garden-2", **STAY), answer("Watching."),
        call("get_quote", room_id="garden-2", **STAY),
        lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
        answer("Asked you to approve."),
        call("create_booking", quote_id="q-2-garden-2", guest_name="Taylor Example"), answer("Asked again."),
        answer("Booked."), answer("Booked.")], ClockedHotel(quote_seconds=900))
    assert result_of(stale)["metrics"]["extensions"]["scheduler"]["expiry"] == {
        "expired": 2, "renewed": 0, "stale_retries": 1}
    assert code == 1  # Nothing was booked: the stale quote was refused twice.


async def test_since_cursor_reads_with_no_changes_count_as_not_modified(tmp_path):
    hotel = ClockedHotel()
    feed = next(tool for tool in hotel.catalog if tool["name"] == "get_booking")
    feed["inputSchema"]["properties"]["since"] = {"type": "string", "description": "Cursor of an earlier read."}
    original = hotel.call

    def changes(name, arguments):
        if name != "get_booking" or "since" not in arguments:
            return original(name, arguments)
        hotel.calls.append((name, dict(arguments)))
        cursor = int(arguments["since"].removeprefix("c")) + 1
        # The first delta read finds the confirmation; later ones list nothing new.
        return hotel.reply({"changes": [{"status": "confirmed"}] if cursor == 2 else [], "cursor": f"c{cursor}"})
    hotel.call = changes
    since = {"conditional": {"argument": "/since", "etag": "/structuredContent/cursor",
                             "changes": "/structuredContent/changes"}}
    found, code, _ = await run(tmp_path, scheduled(CONFIRM, polling=since), [
        *booked_first(), call("get_booking", booking_id="BK-1000", since="c1"), answer("Confirmed, $420.00."),
        call("get_booking", booking_id="BK-1000", since="c2"), answer("Nothing new."),
        call("get_booking", booking_id="BK-1000"), answer("Still confirmed, $420.00.")], hotel)
    assert code == 0, found.summary()
    found_polls = polls(found)
    assert {key: found_polls[key] for key in ("total", "conditional", "not_modified", "missed_conditional")} == {
        "total": 3, "conditional": 2, "not_modified": 1, "missed_conditional": 1}


async def test_a_per_call_retry_after_limits_only_the_same_call(tmp_path):
    hotel = ClockedHotel(rate_limited=1, limit_after=1, retry_after=120)
    original = hotel.call

    def two_bookings(name, arguments):
        if name == "get_booking" and arguments.get("booking_id") == "BK-2000":
            hotel.calls.append((name, dict(arguments)))
            return hotel.reply({"booking_id": "BK-2000", "status": "confirmed"})
        return original(name, arguments)
    hotel.call = two_bookings
    document = scheduled(CONFIRM, rate_limit={**RATE_LIMIT, "scope": "call"})
    found, code, _ = await run(tmp_path, document, [
        *booked_first(), call("get_booking", booking_id="BK-1000"), call("get_booking", booking_id="BK-2000"),
        answer("BK-1000 is busy; BK-2000 is confirmed. $420.00."), call("get_booking", booking_id="BK-1000"),
        answer("Confirmed, $420.00."), answer("Confirmed, $420.00.")], hotel)
    assert code == 0, found.summary()
    # Another booking's status is another call: reading it at once breaks no Retry-After.
    check = next(item for item in result_of(found)["checks"] if item["id"] == "scheduler.retry_after_respected")
    assert check["status"] == "passed" and check["actual"] == {"limited": 1, "honored": 1, "violations": 0}


async def test_a_retry_after_beyond_the_bound_is_a_malformed_reply_not_a_host_failure(tmp_path):
    document = scheduled(CONFIRM, rate_limit=RATE_LIMIT)
    for delay in (8e13, 1e14, 30 * 86400 + 1):  # Past the calendar, past timedelta, past the emulation's bound.
        hotel = ClockedHotel(rate_limited=1, limit_after=1, retry_after=delay)
        found, code, _ = await run(tmp_path, document, [
            *booked_first(), call("get_booking", booking_id="BK-1000"), answer("I'll check again later."),
            answer("Confirmed, $420.00."), answer("Confirmed, $420.00.")], hotel)
        assert code == 0, (delay, found.summary())
        [malformed] = events(found, "scheduler.rate_limit_malformed")
        assert malformed["retry_after_seconds"] == delay and "30 days" in malformed["detail"]
        limits = result_of(found)["metrics"]["extensions"]["scheduler"]["rate_limits"]
        assert limits == {"limited": 0, "honored": 0, "violations": 0, "malformed": 1}
        assert "scheduler.retry_after_respected" not in {check["id"] for check in result_of(found)["checks"]}
