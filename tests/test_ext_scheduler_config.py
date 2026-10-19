"""The scheduler extension's scenario config and schedule arithmetic (no run needed)."""
from datetime import datetime, timedelta

import pytest
from test_ext_scheduler_support import scheduled

from consumer_agent_testing_tool.connector_run import EXTENSIONS, discover, make_extensions
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_scheduler import (
    BASIS,
    Schedule,
    SchedulerConfig,
    SchedulerExtension,
    call_result,
    seconds,
    span,
    without,
)

WATCH = {"id": "price-watch", "every": "1h", "max_runs": 2, "message": "Check the price."}
START = datetime.fromisoformat("2026-11-30T09:00:00-08:00")


def test_the_scheduler_registers_from_its_own_module_and_records_its_bases():
    assert discover("scheduler") is SchedulerExtension and EXTENSIONS["scheduler"] is SchedulerExtension
    loaded = parse_scenario(scheduled())
    names = [extension.name for extension in make_extensions(loaded.scenario)]
    assert names == ["clock", "scheduler"]
    # Every Muse-specific behavior carries a basis; third-party push is labeled unknown, never assumed away.
    assert {item["basis"] for item in BASIS.values()} <= {"documented", "assumed", "unknown"}
    assert BASIS["third-party-push"]["basis"] == "unknown" and "calendar Connectors" in BASIS["third-party-push"]["quote"]
    assert BASIS["scheduled-work"]["quote"].startswith("Once you’ve set a goal or a task, Muse continues to work on a "
                                                       "schedule")
    assert all(item.get("source") or item.get("motivation") or item["basis"] == "assumed" for item in BASIS.values())


def test_durations_accept_seconds_and_compact_units():
    assert [seconds(value, "x") for value in (0, 90, "90s", "15m", "1h30m", "2d", " 1d2h ")] == [
        0, 90, 90, 900, 5400, 172800, 93600]
    for bad in ("", "1w", "h", "-5m", True, 1.5, "30d1s", 30 * 86400 + 1):
        with pytest.raises(ValueError):
            seconds(bad, "x")
    assert [span(value) for value in (90, 3600, 5400, 0)] == ["1 min 30 s", "1 h", "1 h 30 min", "0 s"]


@pytest.mark.parametrize("change, message", [
    ({"jitter": "1h"}, "jitter must be shorter than every"),
    ({"every": 0}, "every is at least 1 second"),
    ({"every": "1w"}, "duration such as 90s"),
    ({"max_runs": None}, "needs until or max_runs"),
    ({"max_runs": 21}, "less than or equal to 20"),
    ({"until": "2026-11-30T21:00:00"}, "needs a UTC offset"),
    ({"first": "2026-11-30T10:00:00.5-08:00"}, "whole seconds"),
    ({"id": "Price Watch"}, "should match pattern"),
    ({"message": "Check\x07 the price."}, "without control characters"),
    ({"every": 3600.0}, "valid integer"),
    ({"surprise": 1}, "Extra inputs are not permitted"),
])
def test_job_configs_fail_at_load_with_the_job_named(change, message):
    job = {**WATCH, **change}
    job = {key: value for key, value in job.items() if value is not None}
    with pytest.raises(LabError, match=message):
        parse_scenario(scheduled([job]))


@pytest.mark.parametrize("config, message", [
    ({"approvals": {"answer": "late"}}, "needs after"),
    ({"approvals": {"answer": "never", "after": "1h"}}, "applies to answer: late only"),
    ({"approvals": {"answer": "late", "after": ["10m", 0]}}, "at least 1 second"),
    ({"approvals": {"answer": "maybe"}}, "late"),
    ({"advance": [{"url": "http://example.com/__oracle/clock"}]}, "plain http is allowed only for loopback"),
    ({"advance": [{"url": "http://127.0.0.1:8765/__oracle/clock?x=1"}]}, "query or fragment"),
    ({"polling": {"conditional": {"argument": "if_none_match", "etag": "/structuredContent/etag"}}}, "JSON pointer"),
    ({"rate_limit": {"retry_after": "", "scope": "connector"}}, "JSON pointer"),
    ({"rate_limit": {"retry_after": "/content/0/text/retry_after_seconds", "scope": "site"}}, "connector"),
    ({"expiry": [{"tool": "create_booking", "error": {"pointer": "/content/0/text/error", "value": "quote_expired"},
                  "reference": "/quote_id", "renew": []}]}, "at least 1 item"),
    ({"seed": -1}, "greater than or equal to 0"),
])
def test_approval_clock_and_signal_configs_are_strict(config, message):
    with pytest.raises(LabError, match=message):
        parse_scenario(scheduled(**config))


def test_jobs_and_clock_urls_are_unique():
    with pytest.raises(LabError, match="jobs ids must be unique"):
        parse_scenario(scheduled([WATCH, WATCH]))
    url = {"url": "http://127.0.0.1:8765/__oracle/clock"}
    with pytest.raises(LabError, match="each clock URL once"):
        parse_scenario(scheduled(advance=[url, url]))


def test_clock_urls_take_the_scenario_port_variable():
    document = scheduled(advance=[{"url": "http://127.0.0.1:${PORT:-8765}/__oracle/clock"}])
    loaded = parse_scenario(document, variables={"PORT": "9123"})
    assert loaded.scenario.extensions["scheduler"]["advance"] == [{"url": "http://127.0.0.1:9123/__oracle/clock"}]


def schedule(**job):
    config = SchedulerConfig.model_validate({"jobs": [{**WATCH, **job}]})
    item = config.jobs[0]
    every = seconds(item.every, "every")
    until = START + timedelta(seconds=seconds(item.until, "until")) if item.until is not None else None
    return Schedule(item, every, seconds(item.jitter, "jitter"), START + timedelta(seconds=every), until, config.seed)


def test_slots_are_jittered_deterministically_and_keep_their_order():
    jittered = schedule(jitter="10m", max_runs=12)
    slots = [jittered.slot(index) for index in range(12)]
    assert slots == [schedule(jitter="10m", max_runs=12).slot(index) for index in range(12)]  # Same seed, same plan.
    assert all(START + timedelta(hours=index + 1) <= moment <= START + timedelta(hours=index + 1, minutes=10)
               for index, moment in enumerate(slots))
    assert slots == sorted(slots) and len(set(slots)) == 12 and jittered.slot(12) is None
    assert any(moment.minute for moment in slots)  # Jitter actually moved some runs.
    other = Schedule(jittered.job, jittered.every, jittered.jitter, jittered.first, None, 7)
    assert [other.slot(index) for index in range(12)] != slots  # Another seed, another jitter.


def test_until_bounds_the_plan_and_slots_coalesce_after_a_long_wait():
    bounded = schedule(max_runs=None, until="5h30m")
    assert bounded.planned == 5 and bounded.slot(4) == START + timedelta(hours=5) and bounded.slot(5) is None
    assert bounded.upto(START + timedelta(hours=3, minutes=10)) == 3 and bounded.upto(START) == 0
    assert bounded.upto(START + timedelta(days=20)) == 5
    capped = schedule(every="1m", max_runs=None, until="30d")
    assert capped.planned == 1000  # Counted up to a cap: the runner resumes at most 20 times anyway.
    assert schedule(max_runs=3).planned == 3


def test_payload_helpers_read_both_projections_and_drop_volatile_fields():
    native = {"source": "cedar-stays", "tool": "get_booking", "untrusted": True,
              "result": {"isError": False, "content": ['{"status": "pending", "checked_at": "10:00"}'],
                         "structuredContent": {"status": "pending", "checked_at": "10:00"}}}
    found = call_result(native, "cedar-stays")
    assert found["content"] == [{"type": "text", "text": '{"status": "pending", "checked_at": "10:00"}'}]
    assert call_result({"source": "app", "tool": "x", "message": "declined"}, "cedar-stays") is None
    cli = {"exit_status": 0, "stdout": {"content": [], "isError": False}, "stderr": "", "source": "cedar-stays",
           "untrusted": True}
    assert call_result(cli, "cedar-stays") == {"content": [], "isError": False}
    assert without(found, "/structuredContent/checked_at")["structuredContent"] == {"status": "pending"}
    assert without(found, "/content/0/text/checked_at")["content"][0]["text"] == '{"status": "pending"}'
    assert without({"a": [1, 2, 3]}, "/a/1") == {"a": [1, 3]} and without({"a": 1}, "/b/c") == {"a": 1}
