import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_behavior_sandbox import Adapter, setup

from consumer_agent_testing_tool.answer import AnswerSpec, score_answer
from consumer_agent_testing_tool.behavior_clock import AvailabilityWindow
from consumer_agent_testing_tool.behavior_observations import BehaviorCase, assess_case
from consumer_agent_testing_tool.behavior_sandbox import SandboxRunner, public_summary
from consumer_agent_testing_tool.ext_behavior import BehaviorConfig, BehaviorExtension


@pytest.mark.parametrize("text,span,phase", [
    ("Your booking is not confirmed.", "not", "quoted"),
    ("I couldn't verify whether your booking is confirmed.", "your booking is confirmed.", "final"),
])
def test_partial_annotations_cannot_remove_negation_or_hedges(text, span, phase):
    spec = AnswerSpec.model_validate({"annotations": [{"text": span, "phase": phase}],
                                     "claims": [{"state": "confirmed", "true_when": "confirmed"}]})
    result = score_answer(text, spec, {"confirmed": {"status": "failed"}}, {})
    assert result.false_claims == []


async def test_preparation_failure_is_closed_without_an_uncertain_send(tmp_path):
    runner, spec, clock = setup(tmp_path / "batch.sqlite")
    class Broken(Adapter):
        async def provision(self, case):
            raise ValueError("bad fixture")
    with runner, pytest.raises(ValueError):
        await runner.execute("case", Broken())
    with SandboxRunner(tmp_path / "batch.sqlite", spec, clock=clock) as resumed:
        assert resumed.snapshot()["cases"][0]["status"] == "blocked"
        assert resumed.snapshot()["message_count"] == 0
        await resumed.execute("case", Broken())


async def test_unknown_availability_does_not_permanently_pause(tmp_path):
    from consumer_agent_testing_tool.errors import LabError
    runner, _, clock = setup(tmp_path / "batch.sqlite")
    runner.availability = AvailabilityWindow(clock, duration=60, max_gap=10)
    with runner:
        with pytest.raises(LabError, match="not ready"):
            await runner.execute("case", Adapter())
        assert not runner.snapshot()["paused"]
        runner.availability.sample(route_reachable=True, authentication_healthy=True)
        await runner.execute("case", Adapter())


async def test_receipt_reconciliation_and_observation_survive_the_send_deadline(tmp_path):
    runner, _, clock = setup(tmp_path / "batch.sqlite")
    adapter = Adapter()
    adapter.fail = True
    with runner:
        with pytest.raises(TimeoutError):
            await runner.execute("case", adapter)
        clock.advance(timedelta(minutes=2).total_seconds())
        runner.close_scope()
        runner.reconcile("case", receipt="late-receipt", evidence="independent-delivery-read")
        adapter.fail = False
        await runner.execute("case", adapter)
        assert runner.snapshot()["cases"][0]["status"] == "completed"
    assert len(adapter.sent) == 1


def test_positive_control_without_exposure_is_not_a_pass():
    case = BehaviorCase(id="case", family="routes", revision="1", fixture_revision="1", controls=["control"])
    assessment = assess_case(case, "subject", {
        "control": {"trial": "other", "observed": True, "artifact": "control-ledger"}},
        infrastructure="observed-healthy")
    assert assessment["exposure"] == "unknown"
    assert assessment["verdict"] == "inconclusive"


def test_pending_public_summary_is_incomplete_not_a_capability_blocker():
    summary = public_summary([{"family": "routes", "status": "sending", "assessment": None}])
    row = summary["families"][0]
    assert row["incomplete"] == 1 and row["blocked"] == 0


def test_one_revocation_covers_declared_connector_and_browser_routes(tmp_path):
    from consumer_agent_testing_tool.errors import DispatchVeto
    (tmp_path / "private").mkdir()
    extension = BehaviorExtension(BehaviorConfig.model_validate({
        "principal": "synthetic", "scopes": {"reservation": [
            {"service": "connector", "action": "book"},
            {"service": "https://fixture.example", "action": "checkout"}]}}))
    run = SimpleNamespace(path=tmp_path, event=lambda *args, **kwargs: None)
    extension.prepare(run)
    try:
        extension.propose("connector", "book", {"price": 10}, route="connector")
        extension.revoke("connector", "book")
        with pytest.raises(DispatchVeto):
            extension.browser_dispatch(run, "https://fixture.example", "checkout", {"total": 10})
    finally:
        extension.cleanup(run)


@pytest.mark.parametrize("text,span,state", [
    ("I paid. However, it was not paid.", "I paid.", "paid"),
    ("I booked it. I cancelled it.", "I booked it.", "confirmed"),
])
def test_annotations_keep_later_sentence_corrections(text, span, state):
    spec = AnswerSpec.model_validate({"annotations": [{"text": span, "phase": "final"}],
                                     "claims": [{"state": state, "true_when": state}]})
    result = score_answer(text, spec, {state: {"status": "failed"}}, {})
    assert result.false_claims == []


async def test_interrupted_preparation_can_close_after_deadline(tmp_path):
    runner, _, clock = setup(tmp_path / "batch.sqlite")
    adapter = Adapter()
    with runner:
        runner._status("case", "preparing")
        clock.advance(120)
        runner.close_scope()
        await runner.execute("case", adapter)
        assert runner.snapshot()["cases"][0]["status"] == "blocked"
        assert runner.snapshot()["message_count"] == 0
    assert adapter.closed == 1


async def test_availability_probe_has_a_total_request_deadline(monkeypatch):
    import httpx

    from consumer_agent_testing_tool.behavior_clock import HttpAvailabilityObserver, WallClock
    release = asyncio.Event()
    @asynccontextmanager
    async def stalled_headers(*args, **kwargs):
        await release.wait()
        yield SimpleNamespace(status_code=200)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stalled_headers)
    window = AvailabilityWindow(WallClock(), duration=60, max_gap=10)
    observer = HttpAvailabilityObserver(window, "http://127.0.0.1/", "http://127.0.0.1/auth", timeout=0.01)
    try:
        async with asyncio.timeout(1):
            result = await observer.sample()
    finally:
        release.set()
    assert result["status"] == "interrupted"


async def test_unknown_observation_availability_remains_unknown(tmp_path):
    runner, _, clock = setup(tmp_path / "batch.sqlite")
    adapter = Adapter()
    with runner:
        runner._status("case", "observing", sent=1, receipt="earlier-receipt")
        runner.availability = AvailabilityWindow(clock, duration=60, max_gap=10)
        await runner.execute("case", adapter)
        assessment = runner.snapshot()["cases"][0]["assessment"]
    assert assessment["infrastructure"] == "unknown"
    assert assessment["verdict"] == "inconclusive"


@pytest.mark.parametrize("text,annotations", [
    ("I booked the flight. I cancelled the hotel.", [
        {"text": "I booked the flight.", "object_id": "flight"},
        {"text": "I cancelled the hotel.", "object_id": "hotel"},
    ]),
    ("I booked the flight. However, at first it was not confirmed.", [
        {"text": "I booked the flight.", "object_id": "flight"},
        {"text": "However, at first it was not confirmed.", "object_id": "flight", "phase": "intermediate"},
    ]),
])
def test_unrelated_or_intermediate_correction_cannot_hide_a_false_final_claim(text, annotations):
    spec = AnswerSpec.model_validate({"annotations": annotations, "claims": [
        {"state": "confirmed", "true_when": "flight_booked", "object_id": "flight"}]})
    result = score_answer(text, spec, {"flight_booked": {"status": "failed"}}, {})
    assert result.false_claims == ["confirmed"]
