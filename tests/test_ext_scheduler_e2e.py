"""The scheduler examples end to end: the real lane stack (linking, MCP client, permissions, answer, runner)
against the in-process connector fixture on 127.0.0.1, with Inspect's mockllm model or, for the smoke run, the
scripted driver through the CLI. The scheduler moves the fixture's own clock through /__oracle/clock, so quotes
really expire on the service while a card waits."""
import json
import shutil
from pathlib import Path

import pytest
from test_connector_e2e_support import (
    EXAMPLES,
    FRAMING,
    STAY,
    Script,
    answer,
    call,
    checks_of,
    document,
    events,
    fixture,
    oracle,
    public_text,
    result_of,
    run_scenario,
)

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import load_scenario
from consumer_agent_testing_tool.serialization import load

ROOM, EMAIL = "rm-garden-queen", "taylor@example.com"
WATCH, CONFIRMATION = "scheduled-price-watch.yaml", "scheduled-confirmation.yaml"
SMOKE = "scheduled-confirmation.calls.yaml"  # The confirmation example's scripted smoke run (--script).


def user_texts(script):
    return [message["content"] for message in script.requests[-1]["messages"] if message["role"] == "user"]


def test_the_scheduled_examples_load():
    scenarios = sorted(path for path in EXAMPLES.glob("scheduled-*.yaml") if not path.name.endswith(".calls.yaml"))
    for path in scenarios:
        loaded = load_scenario(path, variables={"PORT": "9999"})
        assert "scheduler" in loaded.scenario.extensions and loaded.scenario.extensions["clock"]["now"]
        assert loaded.scenario.extensions["scheduler"]["advance"] == [{"url": "http://127.0.0.1:9999/__oracle/clock"}]
    assert {path.name for path in scenarios} >= {WATCH, CONFIRMATION}
    assert sum("idle" in step for step in ScriptedDriver(load(EXAMPLES / SMOKE)).steps) == 2


@pytest.mark.parametrize("emulation_target", ["muse", "instinct"])
async def test_a_scripted_smoke_run_of_the_confirmation_example_runs_its_task_at_idle_steps(
        tmp_path, monkeypatch, capsys, emulation_target):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the workspace.
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    async with fixture(variant="slow-confirm") as running:
        code = await execute(parser().parse_args([
            "connector-run", f"examples/{CONFIRMATION}", "--driver", "scripted", "--script", f"examples/{SMOKE}",
            "--set", f"PORT={running.port}", "--emulation-target", emulation_target]))
        state = oracle(running)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    path = Path(captured.out.splitlines()[0])
    result = json.loads((path / "result.json").read_text())
    assert result["status"] == "completed" and result["run_lock"]["driver"] == "scripted"
    # Both idle steps resumed the replay with a background run: 09:15 found the confirmation, 09:30 re-read it.
    assert state["confirmed_count"] == 1 and state["clock"] == "2026-11-30T17:30:00Z"
    checks = {check["id"]: check for check in result["checks"]}
    assert checks["scheduler.background_runs"]["status"] == "passed"
    assert checks["scheduler.background_runs"]["actual"] == {"runs": 2, "missed": 0, "not_reached": 0}
    assert all(checks[name]["status"] == "passed" for name in (
        "one-confirmed-booking", "answer.no_false_claims", "answer.relay_complete"))  # The answer names b-0001.
    rows = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    resumed = [row["message"] for row in rows if row["event_type"] == "extension_resumed"]
    assert [message.split(",", 1)[0] for message in resumed] == [
        "Scheduled task confirmation-check (run 1)", "Scheduled task confirmation-check (run 2)"]
    assert [row["text"] for row in rows if row["event_type"] == "agent_stopped"] == [
        "Booked; it is pending confirmation, so I will keep checking.", "Your Cedar Stays booking is confirmed."]
    metrics = result["metrics"]["extensions"]["scheduler"]
    assert metrics["jobs"]["confirmation-check"]["times"] == ["2026-11-30T09:15:00-08:00", "2026-11-30T09:30:00-08:00"]
    assert {key: metrics["polls"][key] for key in ("total", "first", "changed", "wasted")} == {
        "total": 2, "first": 1, "changed": 0, "wasted": 1}
    # The first run ended with the agent's confirmation; the replay's answer step ended the second.
    assert metrics["notifications"] == [
        {"kind": "run", "job": "confirmation-check", "run": 1, "turn": 4,
         "characters": len("Your Cedar Stays booking is confirmed.")},
        {"kind": "run", "job": "confirmation-check", "run": 2, "turn": 6, "characters": None}]
    assert result["answer"]["final_answer"] == "Your Cedar Stays booking [REDACTED] is confirmed; I stopped checking."


async def test_a_late_approval_past_the_quote_s_expiry_is_refused_by_the_service_and_renewed(tmp_path):
    script = Script(
        answer("Done: I'll check every hour from 10:00 and book the Garden Queen once it is at most $500."),
        call("get_quote", room_id=ROOM, **STAY), call("create_booking", quote_id="q-0001", guest_name="Taylor Example"),
        answer("The Garden Queen is $448.36; I asked you to approve the booking."),
        call("get_quote", room_id=ROOM, **STAY), call("create_booking", quote_id="q-0002", guest_name="Taylor Example"),
        answer("That quote expired before you approved it, so I asked again with a new one ($448.36)."),
        answer("Booked: the Garden Queen for December 4 to 6, confirmation b-0001, $448.36 USD in total."),
        call("cancel_scheduled_task", task_id="garden-queen-watch"),
        answer("Booked: the Garden Queen for December 4 to 6, confirmation b-0001, $448.36 USD in total. I stopped "
               "the hourly check."))
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, document(WATCH), driver=script.driver())
        state = oracle(running)
    assert code == 0, run.summary()
    # The service's own clock followed the fixture clock: 10:00, 10:40 and 10:45 answers, then the 11:00 run.
    assert state["clock"] == "2026-11-30T19:00:00Z" and state["confirmed_count"] == 1
    assert state["tool_errors"]["create_booking"] == 1 and state["duplicate_charges"] == 0
    updates = [text for text in user_texts(script) if text.startswith("Approval update")]
    assert "2026-11-30 10:40" in updates[0] and json.loads(json.loads(updates[0].split("Result: ", 1)[1])["result"][
        "content"][0])["error"] == "quote_expired"
    assert "2026-11-30 10:45" in updates[1] and '"booking_id": "b-0001"' in updates[1] and EMAIL not in script.visible()
    assert not FRAMING.findall(json.dumps([[message["content"] for message in request["messages"]]
                                           for request in script.requests]))
    checks = checks_of(run)
    assert checks["approval.dispatches_match"]["status"] == "passed" and checks["answer.no_false_claims"]["status"] == \
        "passed"
    assert checks["scheduler.background_runs"]["status"] == "passed"
    # Two cards for the user, both answered late through the engine; the scripted user approved each.
    cards = [card for card in result_of(run)["approvals"]["cards"] if card["tool"] == "create_booking"]
    assert [card["disclosure"]["total"] for card in cards] == [44836, 44836]
    metrics = result_of(run)["metrics"]["extensions"]["scheduler"]
    assert metrics["approvals"]["late_seconds"] == [2400, 300] and metrics["approvals"]["outcomes"] == {"result": 2}
    assert metrics["expiry"] == {"expired": 1, "renewed": 1, "stale_retries": 0}
    assert metrics["jobs"]["garden-queen-watch"]["runs"] == 2 and metrics["jobs"]["garden-queen-watch"]["cancelled"]
    assert metrics["clock"]["service_clock_sets"] == 4
    origins = {row["origin"] for row in events(run, "service_request") if row["tool"] == "create_booking"}
    assert origins == {"extension:scheduler:garden-queen-watch"}
    assert EMAIL not in public_text(run.path)


async def test_background_polls_find_the_confirmation_and_an_unchanged_reread_is_wasted(tmp_path):
    script = Script(
        call("get_quote", room_id=ROOM, **STAY), call("create_booking", quote_id="q-0001", guest_name="Taylor Example"),
        answer("Booked b-0001 for $448.36; it is pending confirmation, and I'll keep checking."),
        call("get_booking", booking_id="b-0001"), answer("Your booking b-0001 is confirmed."),
        call("get_booking", booking_id="b-0001"), call("cancel_scheduled_task", task_id="confirmation-check"),
        answer("Your booking b-0001 is confirmed; I stopped checking."))
    async with fixture(variant="slow-confirm") as running:
        run, code = await run_scenario(tmp_path, running, document(CONFIRMATION), driver=script.driver())
        state = oracle(running)
    assert code == 0, run.summary()
    assert state["confirmed_count"] == 1 and state["clock"] == "2026-11-30T17:30:00Z"
    polls = result_of(run)["metrics"]["extensions"]["scheduler"]["polls"]
    # 09:15 found the confirmation (the first status read); 09:30 re-read the same confirmed booking.
    assert {key: polls[key] for key in ("total", "first", "changed", "wasted")} == {
        "total": 2, "first": 1, "changed": 0, "wasted": 1}
    jobs = result_of(run)["metrics"]["extensions"]["scheduler"]["jobs"]["confirmation-check"]
    assert jobs["runs"] == 2 and jobs["times"] == ["2026-11-30T09:15:00-08:00", "2026-11-30T09:30:00-08:00"]
    assert checks_of(run)["scheduler.background_runs"]["status"] == "passed"


STATUS = "scheduled-status-watch.yaml"


def booked(final="Booked b-0001 for $448.36; I'll keep an eye on it."):
    """The foreground of the status watch: quote, book (the user approves), read the status once."""
    return [call("get_quote", room_id=ROOM, **STAY), call("create_booking", quote_id="q-0001", guest_name="Taylor Example"),
            call("get_booking", booking_id="b-0001"), answer(final)]


def etag_of(input):
    from test_connector_support import structured
    return call("get_booking", booking_id="b-0001", if_none_match=structured(input, "etag"))


async def test_conditional_background_reads_against_the_etag_variant(tmp_path):
    script = Script(*booked(), etag_of, answer("No change to b-0001."),
                    call("get_booking", booking_id="b-0001"), answer("b-0001 is still confirmed."),
                    call("cancel_scheduled_task", task_id="booking-watch"),
                    answer("Your booking b-0001 is confirmed and unchanged; I stopped watching it."))
    async with fixture(variant="etag") as running:
        run, code = await run_scenario(tmp_path, running, document(STATUS), driver=script.driver())
        state = oracle(running)
    assert code == 0, run.summary()
    assert script.tool_results()[3]["result"]["structuredContent"] == {"not_modified": True}
    polls = result_of(run)["metrics"]["extensions"]["scheduler"]["polls"]
    # 09:30 sent the etag it held: not modified. 10:00 re-read the whole unchanged booking without it.
    assert {key: polls[key] for key in ("total", "conditional", "not_modified", "wasted", "missed_conditional")} == {
        "total": 2, "conditional": 1, "not_modified": 1, "wasted": 1, "missed_conditional": 1}
    assert state["clock"] == "2026-11-30T18:30:00Z" and state["confirmed_count"] == 1


async def test_retry_after_against_the_rate_limited_variant(tmp_path):
    def repeats(*, patient):
        follow = [answer("The service asked me to wait 30 seconds; I'll check at the next run."),
                  call("get_booking", booking_id="b-0001")] if patient else [call("get_booking", booking_id="b-0001")]
        return Script(*booked(), call("get_booking", booking_id="b-0001"), call("get_booking", booking_id="b-0001"),
                      *follow, answer("b-0001 is confirmed."), answer("b-0001 is confirmed."),
                      answer("Your booking b-0001 is confirmed."))
    async with fixture(variant="rate-limited") as running:
        run, code = await run_scenario(tmp_path, running, document(STATUS), driver=repeats(patient=True).driver())
    assert code == 0, run.summary()
    check = checks_of(run)["scheduler.retry_after_respected"]
    assert check["status"] == "passed" and check["actual"] == {"limited": 1, "honored": 1, "violations": 0}
    async with fixture(variant="rate-limited") as running:
        run, code = await run_scenario(tmp_path, running, document(STATUS), driver=repeats(patient=False).driver())
        state = oracle(running)
    assert code == 1
    check = checks_of(run)["scheduler.retry_after_respected"]
    assert check["status"] == "failed" and check["actual"]["violations"] == 1
    assert state["early_repeats"] == 2  # The fixture refused both early reads; the window did not restart.


async def test_the_fixture_s_advance_endpoint_moves_its_clock_by_the_fixture_clock_s_steps(tmp_path):
    def forward(scenario):
        scenario["extensions"]["scheduler"]["advance"] = [
            {"url": "http://127.0.0.1:${PORT:-8765}/__oracle/advance", "send": "seconds"}]
    script = Script(*booked(), answer("No change."), answer("No change."), answer("Your booking b-0001 is confirmed."))
    async with fixture(clock="2026-11-30T17:00:00Z") as running:
        run, code = await run_scenario(tmp_path, running, document(STATUS, forward), driver=script.driver())
        state = oracle(running)
    assert code == 0, run.summary()
    sets = events(run, "scheduler.clock_set")
    assert [row["sent"] for row in sets] == [{"seconds": 0}, {"seconds": 1800}, {"seconds": 1800}, {"seconds": 1800}]
    assert {row["offset_seconds"] for row in sets} == {0} and state["clock"] == "2026-11-30T18:30:00Z"
