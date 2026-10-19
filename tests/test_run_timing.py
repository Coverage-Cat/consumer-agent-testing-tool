import copy
from unittest.mock import patch

import pytest
from test_model_timing import evidence_for

from consumer_agent_testing_tool.measurements import run_timing, summarize_trials


def timed_manifest():
    return {
        "started_at": "2026-10-15T00:10:00+00:00",
        "finished_at": "2026-10-15T00:09:00+00:00",
        "started_clock": {"clock_id": "process", "tick_ns": 1_000_000_000},
        "finished_clock": {"clock_id": "process", "tick_ns": 3_000_000_000},
    }


def test_backwards_wall_clock_cannot_produce_negative_elapsed_time():
    result = run_timing(timed_manifest())
    assert result["seconds"] == 2
    assert result["wall_timestamp_order"] == "backwards"
    assert result["wall_timestamp_difference_seconds"] == -60
    assert result["observation_gaps"] == 0


@pytest.mark.parametrize("fault", ["legacy", "restart", "bool", "negative", "backwards", "huge", "missing-end"])
def test_run_duration_requires_one_valid_recorded_clock_epoch(fault):
    manifest = timed_manifest()
    if fault == "legacy":
        manifest.pop("started_clock")
    elif fault == "restart":
        manifest["finished_clock"]["clock_id"] = "new-process"
    elif fault == "bool":
        manifest["started_clock"]["tick_ns"] = True
    elif fault == "negative":
        manifest["started_clock"]["tick_ns"] = -1
    elif fault == "backwards":
        manifest["finished_clock"]["tick_ns"] = 0
    elif fault == "huge":
        manifest["finished_clock"]["tick_ns"] = 10 ** 1000
    else:
        manifest.pop("finished_clock")
    result = run_timing(manifest)
    assert result["seconds"] is result["observed_seconds"] is None
    assert result["observation_gaps"] == 1


def test_wall_timestamp_metadata_is_not_used_to_invent_missing_duration():
    manifest = timed_manifest()
    manifest.pop("started_clock")
    manifest["finished_at"] = "2026-10-15T00:12:00+00:00"
    assert run_timing(manifest)["seconds"] is None
    manifest = timed_manifest()
    manifest["finished_at"] = "unavailable"
    result = run_timing(manifest)
    assert result["seconds"] == 2
    assert result["wall_timestamp_difference_seconds"] is None


def test_evidence_records_clock_origin_and_does_not_replace_it_on_later_finish(tmp_path):
    with patch("consumer_agent_testing_tool.measurements.time.monotonic_ns", return_value=1_000_000_000):
        evidence = evidence_for(tmp_path)
    original = copy.deepcopy(evidence.manifest["started_clock"])
    with patch("consumer_agent_testing_tool.measurements.time.monotonic_ns", return_value=3_000_000_000):
        evidence.finish("blocked", [])
    assert evidence.manifest["metrics"]["total_seconds"] == 2
    with patch("consumer_agent_testing_tool.measurements.CLOCK_ID", "another-process"), \
            patch("consumer_agent_testing_tool.measurements.time.monotonic_ns", return_value=4_000_000_000):
        evidence.finish("completed", [])
    assert evidence.manifest["started_clock"] == original
    assert evidence.manifest["metrics"]["total_seconds"] is None


def test_latency_aggregate_retains_partial_intervals_without_claiming_complete_time():
    result = summarize_trials([
        {"metrics": {"model_seconds": 4}},
        {"metrics": {"model_seconds": None, "timing_coverage": {"model": {"observed_seconds": 2}}}},
    ])
    assert result["latency_seconds"]["model_seconds"] is None
    assert result["observed_latency_seconds"]["model_seconds"] == 6
    assert result["latency_coverage"]["model_seconds"] == {
        "observed_trials": 1, "unmeasured_trials": 1, "partially_measured_trials": 1}
