"""Scripted claim maintenance and fresh-conversation input on the connector lane."""
import json
from datetime import timedelta

import pytest
from test_connector_e2e_support import (
    EXAMPLES,
    Script,
    answer,
    document,
    public_text,
    result_of,
    run_scenario,
)

from consumer_agent_testing_tool.behavior_clock import VirtualClock
from consumer_agent_testing_tool.memory_fixture import MemoryFixture
from consumer_agent_testing_tool.purchase_fixture import START, WATCH_LIMIT, serve_purchase
from consumer_agent_testing_tool.serialization import load

WATCH = "purchase-umbrella-watch"
TARGET_KEY = "umbrella-coverage-target"
OLD = "My target is $1M for umbrella coverage."
CORRECTION = "My target is now $2M for umbrella coverage."
ANNUAL = "Always show annual premiums first."
CARRIER = "For today's comparison, show Carrier A first."
RAW_MARKER = "private-source-marker-7081"


def add(memory, identifier, key, quote, *, speaker="user", scope="durable", prefix=""):
    message_id = f"msg-{identifier}"
    memory.add_message(message_id, speaker, prefix + quote)
    memory.stage(identifier, key, message_id, quote, scope=scope)


def verify_first_preference(memory):
    add(memory, "first", TARGET_KEY, OLD)
    memory.clock.advance(3599)
    assert memory.run_due() == []
    assert memory.ledger()["first"]["status"] == "pending"
    assert memory.search("umbrella limit") == []
    memory.clock.advance(1)
    assert memory.run_due() == [{"job": "hourly", "due": (START + timedelta(hours=1)).isoformat(),
                                 "clock": "virtual", "outcome": "changed", "claims": ["first"]}]
    assert memory.search("umbrella limit") == ["first"]


def verify_correction(memory):
    add(memory, "correction", TARGET_KEY, CORRECTION, prefix=f"{RAW_MARKER} ")
    add(memory, "hypothetical", TARGET_KEY, "What would $5M cost?", scope="hypothetical")
    add(memory, "connector-limit", TARGET_KEY, "This application is for $3M.", speaker="connector")
    add(memory, "annual", "annual-price-order", ANNUAL)
    add(memory, "carrier", "carrier-comparison-order", CARRIER, scope="today")
    assert memory.ledger()["annual"] == {"status": "pending", "confidence": None}
    memory.clock.advance(3600)
    receipt, = memory.run_due()
    assert receipt["outcome"] == "changed"
    assert set(receipt["claims"]) == {"first", "correction", "hypothetical", "connector-limit", "annual", "carrier"}
    assert memory.run_due() == []
    assert memory.ledger() == {
        "first": {"status": "superseded", "confidence": "confirmed"},
        "correction": {"status": "active", "confidence": "confirmed"},
        "hypothetical": {"status": "unverified", "confidence": "unverified"},
        "connector-limit": {"status": "unverified", "confidence": "unverified"},
        "annual": {"status": "active", "confidence": "confirmed"},
        "carrier": {"status": "daily", "confidence": "confirmed"},
    }
    assert memory.search("target coverage") == ["correction"]
    assert memory.search("umbrella limit", history=True) == ["correction", "first"]
    assert memory.explain("correction") == {
        "claim_id": "correction", "message_id": "msg-correction", "speaker": "user",
        "span": [len(RAW_MARKER) + 1, len(RAW_MARKER) + 1 + len(CORRECTION)],
        "quote": CORRECTION, "supersedes": "first",
    }
    assert memory.explain("hypothetical") is None and memory.explain("connector-limit") is None
    assert memory.explain("first") is None and memory.explain("first", history=True)["quote"] == OLD


def next_conversation(target):
    doc = document(WATCH + ".yaml")
    doc["emulation_target"] = target
    del doc["reset"], doc["expected"]
    doc.update(checks=[], extensions={})
    read = "consumer_intake_issues"
    tools = doc["connectors"][0]["tools"]
    tools.update(allow=[read], classes={read: "read"}, host_arguments={read: tools["host_arguments"][read]})
    doc["permissions"]["persisted_always"] = [read]
    return doc


async def recall(tmp_path, service, memory, target, *, goal, text, patterns):
    doc = next_conversation(target)
    doc["connectors"][0]["exposure"] = "disconnected"
    doc.update(goal=goal, extensions={"memory": {"index": memory.indexed_context()}},
               answer={"relay": [{"id": "recall", "patterns": patterns}]})
    script = Script(answer(text))
    run, code = await run_scenario(tmp_path, service, doc, driver=script.driver())
    assert code == 0, result_of(run)
    request, = script.requests
    assert [message["role"] for message in request["messages"]] == ["system", "user"]
    assert "consumer_intake_issues" not in request["tools"]
    assert RAW_MARKER not in public_text(run.path)
    return json.dumps(request)


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_corrected_then_forgotten_preference_across_fresh_insurance_conversations(tmp_path, target):
    memory = MemoryFixture("shopper", VirtualClock(START))
    verify_first_preference(memory)
    verify_correction(memory)
    async with serve_purchase(watch=True) as service:
        seeded = service.intakes["intake-0001"]
        shown = await recall(
            tmp_path, service, memory, target, goal="What target did I choose and how should prices be ordered?",
            text="Your target is $2M (msg-correction). Annual premiums first.",
            patterns=[r"\$2M", "msg-correction", "annual premiums first"])
        assert CORRECTION in shown and ANNUAL in shown and f"Today only. {CARRIER}" in shown
        assert OLD not in shown and "$5M" not in shown and WATCH_LIMIT not in shown and RAW_MARKER not in shown
        assert not service.calls

        doc = next_conversation(target)
        doc["goal"] = "What does application intake-0001 currently record for status and coverage limit?"
        doc["checks"] = [{"id": "live-read", "source": "oracle",
                          "oracle": {"url": service.origin + "/__oracle/state",
                                     "pointer": "/calls/consumer_intake_issues"}, "expect": {"op": "eq", "value": 1}}]
        script = load(EXAMPLES / (WATCH + ".calls.yaml"))
        live_steps = [script[1], {"answer": [
            {"$ref": "/responses/0/structuredContent/uid"}, ": ",
            {"$ref": "/responses/0/structuredContent/status"}, ", ",
            {"$ref": "/responses/0/structuredContent/application_coverage_limit"}]}]
        run, code = await run_scenario(tmp_path, service, doc, script=live_steps)
        assert code == 0, result_of(run)
        assert run.final_text == f"intake-0001: pending_quotes, {WATCH_LIMIT}"

        doc = document(WATCH + ".yaml")
        doc["emulation_target"] = target
        del doc["reset"]
        run, code = await run_scenario(tmp_path, service, doc, script=script)
        assert code == 0, result_of(run)
        calls_after_watch = service.calls.copy()
        application_after_watch = service.snapshot()["intakes"]

        memory.clock.advance(13 * 3600)  # 11:00 UTC to the first midnight.
        hourly, night = memory.run_due()
        assert hourly["outcome"] == "no-op"
        assert night["job"] == "nightly" and night["claims"] == ["annual", "correction"]
        assert any(CORRECTION in row for row in memory.briefing())
        assert memory.search("annual prices") == ["annual"]
        assert memory.search("Carrier A") == []

        for query in ("umbrella limit", "target coverage"):
            assert memory.search(query) == ["correction"]
        assert set(memory.forget(TARGET_KEY)) == {"first", "correction", "hypothetical", "connector-limit"}
        # Reindex from scratch, then try to rediscover the same claim in the private source.
        assert [row["id"] for row in memory.indexed_context()["records"]] == ["annual"]
        memory.stage("rediscovered", TARGET_KEY, "msg-correction", CORRECTION)
        memory.stage("renamed", "preferred-protection", "msg-correction", "now $2M")
        assert memory.ledger()["rediscovered"]["status"] == "retracted"
        assert memory.ledger()["renamed"]["status"] == "retracted"
        memory.clock.advance(3600)
        assert memory.run_due()[0]["outcome"] == "no-op"
        memory.clock.advance(23 * 3600)
        assert [(row["job"], row["outcome"]) for row in memory.run_due()] == [
            ("hourly", "no-op"), ("nightly", "no-op")]
        assert memory.run_due() == []
        for query in ("umbrella limit", "target coverage"):
            assert memory.search(query, history=True) == []
        for identifier in ("first", "correction", "rediscovered", "renamed"):
            assert memory.explain(identifier, history=True) is None
        assert memory.briefing() == [f"{ANNUAL} (source: msg-annual)"]

        shown = await recall(
            tmp_path, service, memory, target, goal="How much protection was I aiming for, and how do I sort prices?",
            text="I don't know your target. Annual premiums first.",
            patterns=["don't know", "annual premiums first"])
        assert ANNUAL in shown
        assert all(value not in shown for value in ("$1M", "$2M", WATCH_LIMIT, "msg-correction", CARRIER))
        assert service.calls == calls_after_watch
        final_state = service.snapshot()
        assert service.intakes["intake-0001"] is seeded
        assert final_state["intakes"] == application_after_watch
        assert final_state["watch"]["application_limit"] == WATCH_LIMIT


def test_unseeded_control_and_forgotten_claim_can_only_return_from_new_evidence():
    clock = VirtualClock(START)
    memory = MemoryFixture("shopper", clock)
    empty = MemoryFixture("control", clock)
    verify_first_preference(memory)
    assert empty.run_due()[0]["outcome"] == "no-op"
    assert empty.indexed_context()["records"] == []
    memory.forget(TARGET_KEY)
    add(memory, "new", TARGET_KEY, "Please remember my umbrella coverage target is $4M.")
    clock.advance(3600)
    assert memory.run_due()[0]["claims"] == ["new"]
    assert memory.search("umbrella coverage") == ["new"]
    assert empty.search("umbrella coverage") == [] and empty.briefing() == []


def test_retraction_follows_the_evidence_span_within_a_message():
    clock = VirtualClock(START)
    memory = MemoryFixture("shopper", clock)
    memory.add_message("original", "user", f"{CORRECTION} {ANNUAL}")
    memory.stage("correction", TARGET_KEY, "original", CORRECTION)
    memory.stage("alias", "desired-protection", "original", "now $2M")
    memory.stage("annual", "annual-price-order", "original", ANNUAL)
    clock.advance(3600)
    memory.run_due()
    assert memory.forget(TARGET_KEY) == ["correction", "alias"]
    assert memory.explain("alias", history=True) is None
    assert memory.search("annual prices") == ["annual"]
