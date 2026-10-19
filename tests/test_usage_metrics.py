import copy
import json
from types import SimpleNamespace

import pytest
from test_model_timing import evidence_for, pair

from consumer_agent_testing_tool.metrics import recorded_events
from consumer_agent_testing_tool.metrics import summarize_trials as summarize_all_trials
from consumer_agent_testing_tool.usage_metrics import (
    summarize,
    summarize_trials,
    valid_observations,
)


def usage_events(identifier="request", usage=None, *, explicit=None, provenance=True):
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12} if usage is None else usage
    rows = pair(identifier)
    rows[-1][1]["usage"] = usage
    if provenance:
        rows[-1][1]["usage_field_provenance"] = {
            "schema_version": 1, "kind": "inspect-model-usage-fields",
            "explicit_fields": list(usage) if explicit is None else explicit,
        }
    return rows


def manifest(events, status="completed"):
    return {"execution_status": status, "checks": [{"id": "backend.synthetic", "status": "passed"}],
            "metrics": summarize(events)}


def test_token_totals_require_every_recorded_request():
    complete = usage_events("complete")
    for other in (pair("missing")[:1], pair("missing"), pair("error", terminal="provider_error")):
        result = summarize([*complete, *other])
        assert result["token_usage"] is None
        assert result["observed_token_usage"] == {
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        detail = result["usage_observations"]["fields"]["input_tokens"]
        assert detail["observed_requests"] == detail["missing_requests"] == 1
        assert detail["complete"] is False
        assert valid_observations(result["usage_observations"])


def test_explicit_zero_is_measured_but_adapter_defaults_are_unknown():
    rows = usage_events(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    assert summarize(rows)["token_usage"] == {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    rows[-1][1]["usage_field_provenance"]["explicit_fields"] = ["output_tokens"]
    result = summarize(rows)
    assert result["token_usage"] is None
    assert result["observed_token_usage"] == {"output_tokens": 0}
    assert result["usage_observations"]["fields"]["input_tokens"]["missing_requests"] == 1


@pytest.mark.parametrize("value", [True, "10", 10.0, -1, float("inf"), float("nan")])
def test_invalid_token_counts_are_not_coerced_or_used_as_zero(value):
    result = summarize(usage_events(usage={"input_tokens": value, "output_tokens": 2, "total_tokens": 12}))
    assert result["token_usage"] is None
    assert "input_tokens" not in result["observed_token_usage"]
    assert result["usage_observations"]["fields"]["input_tokens"]["invalid_requests"] == 1
    assert valid_observations(result["usage_observations"])


def test_optional_token_fields_keep_their_own_coverage():
    first = usage_events("first", {"input_tokens": 10, "output_tokens": 2,
                                 "total_tokens": 12, "reasoning_tokens": 1})
    result = summarize([*first, *usage_events("second")])
    assert result["token_usage"] == {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}
    assert result["observed_token_usage"]["reasoning_tokens"] == 1
    detail = result["usage_observations"]["fields"]["reasoning_tokens"]
    assert detail["observed_requests"] == detail["missing_requests"] == 1
    assert detail["complete"] is False


@pytest.mark.parametrize("fault", ["duplicate-request", "duplicate-response", "orphan", "reordered", "legacy"])
def test_ambiguous_usage_cannot_inflate_token_totals(fault):
    rows = usage_events()
    if fault == "duplicate-request":
        rows.insert(1, rows[0])
    elif fault == "duplicate-response":
        rows.append(rows[-1])
    elif fault == "orphan":
        rows.pop(0)
    elif fault == "reordered":
        rows.reverse()
    else:
        rows[-1][1].pop("usage_field_provenance")
    result = summarize([*rows, *usage_events("known")])
    assert result["token_usage"] is None
    assert result["observed_token_usage"]["total_tokens"] == 12
    assert valid_observations(result["usage_observations"])


def test_usage_does_not_need_a_join_between_process_clocks():
    rows = usage_events()
    rows[-1][0]["clock_id"] = "another-process"
    assert summarize(rows)["token_usage"]["total_tokens"] == 12


def test_usage_summary_retains_failed_and_blocked_trial_counts():
    manifests = [manifest(usage_events(), status) for status in ("completed", "blocked", "failed")]
    manifests[-1]["checks"] = [{"id": "timeout", "class": "setup/infrastructure", "status": "failed"}]
    result = summarize_all_trials(manifests)
    assert result["token_usage"] == {"input_tokens": 30, "output_tokens": 6, "total_tokens": 36}
    assert result["usage_trial_coverage"]["complete"] == 3
    assert result["cost"] is None


def test_missing_or_legacy_trial_usage_is_not_an_inferred_zero():
    known = manifest(usage_events())
    for missing in ({}, {"token_usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102}}):
        result = summarize_trials([known, {"metrics": missing}])
        assert result["token_usage"] is None
        assert result["observed_token_usage"]["total_tokens"] == 12
        assert result["usage_trial_coverage"]["complete"] == 1
        assert result["usage_trial_coverage"]["unobserved"] == 1
        assert result["token_field_coverage"]["total_tokens"]["unknown_request_coverage_trials"] == 1


def test_orphan_usage_keeps_request_history_coverage_unknown():
    result = summarize_trials([manifest(usage_events()[1:])])
    assert result["token_usage"] is result["observed_token_usage"] is None
    assert result["usage_trial_coverage"]["unobserved"] == 1
    assert result["token_field_coverage"]["total_tokens"]["unknown_request_coverage_trials"] == 1


@pytest.mark.parametrize("fault", ["bool-count", "false-complete", "missing-field", "unknown-total",
                                 "wrong-count", "provenance-gap"])
def test_forged_usage_coverage_cannot_pass_aggregation(fault):
    row = manifest(usage_events())
    evidence = copy.deepcopy(row["metrics"]["usage_observations"])
    detail = evidence["fields"]["input_tokens"]
    if fault == "bool-count":
        evidence["requests"] = True
    elif fault == "false-complete":
        detail.update(complete=True, observed_requests=0, missing_requests=1, observed_total=None)
    elif fault == "missing-field":
        evidence["fields"].pop("reasoning_tokens")
    elif fault == "unknown-total":
        detail["observed_total"] = None
    elif fault == "wrong-count":
        detail["observed_requests"] = 2
    elif fault == "provenance-gap":
        evidence["provenance_gaps"] = 1
    assert valid_observations(evidence) is False
    row["metrics"]["usage_observations"] = evidence
    result = summarize_trials([row])
    assert result["token_usage"] is result["observed_token_usage"] is None
    assert result["usage_trial_coverage"]["unobserved"] == 1


async def test_native_capture_preserves_explicit_fields_before_default_serialization(tmp_path):
    from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput, ModelUsage

    from consumer_agent_testing_tool.evidence import export_redacted
    from consumer_agent_testing_tool.providers import CapturingAPI

    output = ModelOutput.from_content("synthetic", "done")
    output.usage = ModelUsage(input_tokens=0, output_tokens=3)

    async def generate(*args):
        return output

    evidence = evidence_for(tmp_path)
    capture = CapturingAPI(SimpleNamespace(model_name="synthetic", base_url=None, generate=generate), evidence)
    await capture.generate([ChatMessageUser(content="Synthetic usage proof")], [], "auto", GenerateConfig())
    evidence.finish("completed", [])
    payload = next(p for event, p in recorded_events(evidence.path) if event["event_type"] == "model_response")
    assert payload["usage"]["total_tokens"] == 0  # Native serialization includes an unset default.
    assert payload["usage_field_provenance"]["explicit_fields"] == ["input_tokens", "output_tokens"]
    result = evidence.manifest["metrics"]
    assert result["token_usage"] is None
    assert result["observed_token_usage"] == {"input_tokens": 0, "output_tokens": 3}
    public = export_redacted(evidence.path, tmp_path / "public")
    assert json.loads((public / "manifest.json").read_text())["metrics"] == result
