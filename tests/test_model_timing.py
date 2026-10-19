"""Model latency requires complete, uniquely paired evidence from known clocks."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from consumer_agent_testing_tool.config import interface_profile
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.measurements import summarize, summarize_trials
from consumer_agent_testing_tool.metrics import recorded_events


def pair(identifier="request", *, clock="first", start=1_000_000_000, end=3_000_000_000,
         terminal="model_response"):
    return [
        ({"event_type": "model_request", "event_id": identifier, "parent_id": None,
          "clock_id": clock, "monotonic_ns": start}, {}),
        ({"event_type": terminal, "event_id": identifier + "-end", "parent_id": identifier,
          "clock_id": clock, "monotonic_ns": end}, {}),
    ]


def evidence_for(path):
    return Evidence(path, lane="contract", label="protocol-tested",
        profile=interface_profile("api-tools-surrogate-v1"), lock={},
        scenario_id="synthetic-model-timing")


@pytest.mark.parametrize("fault", [
    "missing-response", "legacy-clock", "cross-clock", "empty-clock", "backwards",
    "boolean-tick", "negative-tick", "string-tick", "duplicate-request",
    "duplicate-response", "orphan-response", "reordered", "missing-request-id",
    "missing-parent-id",
])
def test_incomplete_model_timing_never_becomes_a_complete_total(fault):
    rows = pair()
    if fault == "missing-response":
        rows.pop()
    elif fault == "legacy-clock":
        for event, _ in rows:
            event.pop("clock_id")
    elif fault == "cross-clock":
        rows[-1][0]["clock_id"] = "new-process"
    elif fault == "empty-clock":
        for event, _ in rows:
            event["clock_id"] = ""
    elif fault == "backwards":
        rows[-1][0]["monotonic_ns"] = 0
    elif fault == "boolean-tick":
        rows[0][0]["monotonic_ns"] = True
    elif fault == "negative-tick":
        rows[0][0]["monotonic_ns"] = -1
    elif fault == "string-tick":
        rows[-1][0]["monotonic_ns"] = "3000000000"
    elif fault == "duplicate-request":
        rows.insert(1, rows[0])
    elif fault == "duplicate-response":
        rows.append(rows[-1])
    elif fault == "orphan-response":
        rows.pop(0)
    elif fault == "reordered":
        rows.reverse()
    elif fault == "missing-request-id":
        rows[0][0].pop("event_id")
    elif fault == "missing-parent-id":
        rows[-1][0].pop("parent_id")
    result = summarize([*rows, *pair("complete")], {})
    assert result["model_seconds"] is None
    detail = result["timing_coverage"]["model"]
    assert detail["observed_seconds"] == 2
    assert detail["completed"] == 1
    assert detail["observation_gaps"] > 0


def test_model_timing_distinguishes_missing_evidence_from_measured_zero():
    result = summarize([], {})
    assert result["model_seconds"] is None
    assert "model" not in result["timing_coverage"]
    result = summarize(pair(end=1_000_000_000), {})
    assert result["model_seconds"] == 0
    assert result["timing_coverage"]["model"]["completed"] == 1
    result = summarize(pair()[:1], {})
    assert result["model_seconds"] is None
    assert result["timing_coverage"]["model"]["observed_seconds"] is None


def test_model_timing_sums_complete_epochs_without_joining_them():
    events = [
        *pair("before", clock="old-process", start=8_000_000_000, end=10_000_000_000),
        *pair("after", clock="new-process", terminal="provider_error"),
    ]
    result = summarize(events, {})
    assert result["model_seconds"] == 4
    detail = result["timing_coverage"]["model"]
    assert detail["started"] == 2 and detail["completed"] == detail["failed"] == 1
    assert detail["observation_gaps"] == 0


def test_model_timing_allows_concurrency_but_requires_each_request_identity():
    first, second = pair("first", end=4_000_000_000), pair("second")
    result = summarize([first[0], second[0], second[1], first[1]], {})
    assert result["model_seconds"] == 5
    assert result["timing_coverage"]["model"]["completed"] == 2


def test_model_timing_coverage_survives_finish_export_and_summary(tmp_path):
    evidence = evidence_for(tmp_path)
    with patch("consumer_agent_testing_tool.evidence.time.monotonic_ns", return_value=1_000_000_000):
        request = evidence.event("model_request", {})
    with patch("consumer_agent_testing_tool.evidence.time.monotonic_ns", return_value=3_000_000_000):
        evidence.event("model_response", {}, parent_id=request)
    evidence.event("model_request", {})
    evidence.finish("blocked", [{"id": "interrupted", "status": "blocked"}])
    metrics = evidence.manifest["metrics"]
    assert metrics["model_seconds"] is None
    assert metrics["timing_coverage"]["model"]["observed_seconds"] == 2
    events = list(recorded_events(evidence.path))
    assert all(isinstance(event.get("clock_id"), str) and event["clock_id"] for event, _ in events)
    exported = export_redacted(evidence.path, tmp_path / "public")
    manifest = json.loads((exported / "manifest.json").read_text())
    assert manifest["metrics"] == metrics
    summary = summarize_trials([manifest])
    assert summary["latency_seconds"]["model_seconds"] is None
    assert summary["latency_coverage"]["model_seconds"]["unmeasured_trials"] == 1


async def test_cancelled_native_provider_call_retains_unknown_model_latency(tmp_path):
    from inspect_ai.model import ChatMessageUser, GenerateConfig

    from consumer_agent_testing_tool.providers import CapturingAPI

    entered = asyncio.Event()

    async def generate(*args):
        entered.set()
        await asyncio.Event().wait()

    evidence = evidence_for(tmp_path)
    capture = CapturingAPI(SimpleNamespace(model_name="synthetic", base_url=None,
                                          generate=generate), evidence)
    task = asyncio.create_task(capture.generate(
        [ChatMessageUser(content="Synthetic cancellation proof")], [], "auto", GenerateConfig()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    evidence.finish("blocked", [{"id": "cancelled", "status": "blocked"}])
    assert evidence.manifest["metrics"]["model_seconds"] is None
    detail = evidence.manifest["metrics"]["timing_coverage"]["model"]
    assert detail["started"] == detail["observation_gaps"] == 1
    assert detail["completed"] == detail["failed"] == 0
