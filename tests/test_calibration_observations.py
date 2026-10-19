import copy
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.calibration import evaluate, partner_packet
from consumer_agent_testing_tool.calibration_observations import DIMENSIONS
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import file_digest

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
PROFILE = "a" * 64


def packet(root, *, kind="synthetic"):
    sources = []
    for name, content in {
        "transcript": "Synthetic visible response and approval record.",
        "target-requests": '{"operation":"confirm","arguments":{"amount_minor":12500}}',
        "backend-state": '{"confirmed":1,"duplicate":0}',
    }.items():
        path = root / (name + ".txt")
        path.write_text(content)
        sources.append({"path": path.name, "sha256": file_digest(path), "kind": name})
    side = {
        "sources": sources, "task_success": True, "backend_state": {"confirmed": 1, "duplicate": 0},
        "surface": "api-tools", "operations": {"completeness": "complete", "items": [
            {"milestone": "quote", "operation": "read", "arguments": {"id": "item"}},
            {"milestone": "confirm", "operation": "confirm", "arguments": {"amount_minor": 12500}}]},
        "interventions": {"completeness": "complete", "items": [
            {"milestone": "confirm", "kind": "approval", "outcome": "approved", "details": {"amount_minor": 12500}},
            {"milestone": "login", "kind": "handoff", "outcome": "returned", "details": {}}]},
        "final_response": "The synthetic reservation is confirmed.", "final_consistent": True,
    }
    pair = {"schema_version": 2, "evidence_kind": kind, "profile_sha256": PROFILE,
            "surface_revision": "fixture-v1", "scenario_id": "heldout", "trial_id": "one",
            "local": side, "muse": copy.deepcopy(side)}
    cohort = {
        "schema_version": 2, "cohort": "synthetic-software-test", "authorized": True, "evidence_kind": kind,
        "observed_at": "2026-10-12T08:00:00Z", "expires_at": "2026-10-13T08:00:00Z",
        "surface_revision": "fixture-v1", "profile_sha256": PROFILE,
        "visibility": {"model": "unknown", "policy": "partial", "browser": "unknown", "discovery": "unknown"},
        "consistency_rubric": "The visible final response must agree with independent committed state.",
        "observations": [{"scenario_id": "heldout", "trial_id": "one", "split": "held-out",
                          "artifact": "pair.json", "artifact_sha256": ""}],
    }
    save(root, cohort, pair)
    return cohort, pair


def save(root, cohort, pair):
    pair_path = root / "pair.json"
    pair_path.write_text(json.dumps(pair))
    cohort["observations"][0]["artifact_sha256"] = file_digest(pair_path)
    (root / "cohort.json").write_text(json.dumps(cohort))


def run(root, **kwargs):
    return evaluate(root, "cohort.json", profile_sha256=kwargs.pop("profile_sha256", PROFILE),
                    surface_revision=kwargs.pop("surface_revision", "fixture-v1"),
                    now=kwargs.pop("now", NOW), **kwargs)


def test_calibration_all_observable_dimensions_and_synthetic_exclusion(tmp_path):
    packet(tmp_path)
    result = run(tmp_path)
    assert result["primary_heldout_coverage"]
    assert not result["usable_for_comparison"]
    assert result["label"] == "Synthetic calibration analysis"
    group = result["groups"]["held-out"]
    assert group["independent_scenarios"] == group["paired_trials"] == 1
    for dimension in DIMENSIONS:
        metric = group["dimensions"][dimension]
        assert metric["compared_pairs"] == metric["agreements"] == 1
        assert metric["unknown_pairs"] == metric["differences"] == 0
    assert "Synthetic visible response" not in json.dumps(result)
    assert "synthetic reservation is confirmed" not in json.dumps(result)


def test_calibration_differences_are_dimension_specific(tmp_path):
    cohort, pair = packet(tmp_path)
    other = pair["muse"]
    other.update(task_success=False, backend_state={"confirmed": 0, "duplicate": 0},
                 surface="browser", final_consistent=False)
    other["operations"]["items"][1]["arguments"]["amount_minor"] = 13500
    other["interventions"]["items"][0]["outcome"] = "denied"
    save(tmp_path, cohort, pair)
    metrics = run(tmp_path)["groups"]["held-out"]["dimensions"]
    for key in ("task_outcome", "backend_state", "surface", "arguments",
                "visible_approvals", "final_response_consistency"):
        assert metrics[key]["differences"] == 1
    for key in ("operation_sequence", "request_repetition", "handoffs"):
        assert metrics[key]["agreements"] == 1
    assert metrics["task_outcome"]["false_local_passes"] == 1
    assert metrics["final_response_consistency"]["false_local_passes"] == 1


def test_calibration_partial_traces_do_not_establish_absence_or_disagreement(tmp_path):
    cohort, pair = packet(tmp_path)
    pair["muse"]["operations"] = {"completeness": "partial", "items": []}
    pair["muse"]["interventions"] = None
    pair["muse"]["final_consistent"] = None
    save(tmp_path, cohort, pair)
    result = run(tmp_path)
    for key in ("operation_sequence", "arguments", "request_repetition",
                "visible_approvals", "handoffs", "final_response_consistency"):
        metric = result["groups"]["held-out"]["dimensions"][key]
        assert metric["agreement"] is None and metric["unknown_pairs"] == 1
    assert not result["primary_heldout_coverage"]
    assert result["groups"]["held-out"]["dimensions"]["backend_state"]["agreements"] == 1


def test_calibration_aligns_arguments_by_declared_milestone(tmp_path):
    cohort, pair = packet(tmp_path)
    pair["muse"]["operations"]["items"].reverse()
    save(tmp_path, cohort, pair)
    metrics = run(tmp_path)["groups"]["held-out"]["dimensions"]
    assert metrics["operation_sequence"]["differences"] == 1
    assert metrics["arguments"]["agreements"] == 1
    pair["muse"]["operations"]["items"][0]["milestone"] = "unmatched"
    save(tmp_path, cohort, pair)
    assert run(tmp_path)["groups"]["held-out"]["dimensions"]["arguments"]["unknown_pairs"] == 1


def test_calibration_records_repeated_and_changed_arguments_without_retry_intent(tmp_path):
    cohort, pair = packet(tmp_path)
    for side in (pair["local"], pair["muse"]):
        side["operations"]["items"].append(copy.deepcopy(side["operations"]["items"][-1]))
    pair["muse"]["operations"]["items"][-1]["arguments"]["amount_minor"] = 13500
    save(tmp_path, cohort, pair)
    result = run(tmp_path)
    assert result["groups"]["held-out"]["dimensions"]["request_repetition"]["differences"] == 1
    assert "not retry intent" in result["limits"]


@pytest.mark.parametrize("field", ["task_success", "final_consistent"])
def test_calibration_boolean_annotations_do_not_coerce_numbers(tmp_path, field):
    cohort, pair = packet(tmp_path)
    pair["muse"][field] = 1
    save(tmp_path, cohort, pair)
    with pytest.raises(ValidationError):
        run(tmp_path)


def test_calibration_backend_comparison_preserves_boolean_numeric_distinction(tmp_path):
    cohort, pair = packet(tmp_path)
    pair["local"]["backend_state"]["confirmed"] = True
    save(tmp_path, cohort, pair)
    assert run(tmp_path)["groups"]["held-out"]["dimensions"]["backend_state"]["differences"] == 1


@pytest.mark.parametrize("change", ["artifact", "source", "identity", "source_path", "source_kind",
                                   "pair_profile", "pair_revision", "pair_kind"])
def test_calibration_requires_matching_artifacts_identity_and_sources(tmp_path, change):
    cohort, pair = packet(tmp_path)
    if change == "artifact":
        (tmp_path / "pair.json").write_text("{}")
    elif change == "source":
        (tmp_path / "backend-state.txt").write_text("changed")
    else:
        if change == "identity":
            pair["trial_id"] = "another-trial"
        elif change == "source_path":
            pair["local"]["sources"][0]["path"] = "../not-in-this-workspace"
        elif change == "source_kind":
            pair["local"]["sources"] = [source for source in pair["local"]["sources"]
                                        if source["kind"] != "backend-state"]
        else:
            key, value = {"pair_profile": ("profile_sha256", "b" * 64),
                          "pair_revision": ("surface_revision", "changed"),
                          "pair_kind": ("evidence_kind", "muse-observed")}[change]
            pair[key] = value
        save(tmp_path, cohort, pair)
    with pytest.raises((LabError, ValidationError)):
        run(tmp_path)


@pytest.mark.parametrize("change", ["profile", "revision", "expired", "missing_revision"])
def test_calibration_current_profile_revision_and_date_are_required(tmp_path, change):
    packet(tmp_path)
    kwargs = {
        "profile": {"profile_sha256": "b" * 64}, "revision": {"surface_revision": "changed"},
        "expired": {"now": datetime(2026, 10, 14, tzinfo=UTC)}, "missing_revision": {"surface_revision": None},
    }[change]
    if change == "missing_revision":
        with pytest.raises(LabError, match="surface-revision"):
            run(tmp_path, **kwargs)
    else:
        result = run(tmp_path, **kwargs)
        assert result["stale"] and not result["usable_for_comparison"]


def test_calibration_declared_muse_fixture_tests_eligibility_not_authenticity(tmp_path):
    # Deliberately tests the operator-declaration branch; this remains a synthetic
    # unit fixture and must never be attached to the real-Muse release gate.
    cohort, pair = packet(tmp_path, kind="muse-observed")
    assert run(tmp_path)["usable_for_comparison"]
    cohort["observations"][0]["split"] = "calibration"
    save(tmp_path, cohort, pair)
    assert not run(tmp_path)["usable_for_comparison"]
    cohort["observations"].append({**cohort["observations"][0], "trial_id": "two", "split": "held-out"})
    (tmp_path / "cohort.json").write_text(json.dumps(cohort))
    with pytest.raises(ValidationError, match="disjoint"):
        run(tmp_path)


def test_calibration_unknown_visible_details_are_not_an_agreement(tmp_path):
    cohort, pair = packet(tmp_path)
    for side in (pair["local"], pair["muse"]):
        side["interventions"]["items"][0]["details"] = None
    save(tmp_path, cohort, pair)
    metrics = run(tmp_path)["groups"]["held-out"]["dimensions"]
    assert metrics["visible_approvals"]["unknown_pairs"] == 1
    assert metrics["handoffs"]["agreements"] == 1


async def test_calibration_cli_and_partner_packet_keep_synthetic_analysis_unqualified(tmp_path, monkeypatch, capsys):
    packet(tmp_path)
    monkeypatch.chdir(tmp_path)
    args = parser().parse_args(["calibrate", "cohort.json", "--profile-sha256", PROFILE,
                               "--surface-revision", "fixture-v1"])
    assert await execute(args) == 3
    assert json.loads(capsys.readouterr().out)["evidence_kind"] == "synthetic"
    spec = tmp_path / "scenario.json"
    spec.write_text('{"id":"synthetic-scenario","assertions":[]}')
    result = partner_packet(spec, PROFILE)
    assert result["observation_packet_version"] == 2
    assert result["comparison_dimensions"] == list(DIMENSIONS)


async def test_calibration_packaged_examples_work_without_accounts_or_network(tmp_path, monkeypatch, capsys):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Calibration analysis attempted network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.chdir(tmp_path)
    assert await execute(parser().parse_args(["init", "--provider", "none"])) == 0
    capsys.readouterr()
    assert await execute(parser().parse_args([
        "calibrate", "calibration-examples/cohort.json", "--profile-sha256", PROFILE,
        "--surface-revision", "synthetic-fixture-v1"])) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["primary_heldout_coverage"]
    metrics = result["groups"]["held-out"]["dimensions"]
    assert metrics["task_outcome"]["false_local_passes"] == 1
    assert metrics["backend_state"]["differences"] == 1
    assert metrics["final_response_consistency"]["false_local_passes"] == 1
    assert not result["usable_for_comparison"]


@pytest.mark.parametrize("change", ["authorization", "cohort_version", "pair_version", "dimensions", "rubric"])
def test_calibration_collection_contract_rejects_ambiguous_metadata(tmp_path, change):
    cohort, pair = packet(tmp_path)
    if change == "authorization":
        cohort["authorized"] = 1
    elif change == "cohort_version":
        cohort["schema_version"] = 2.0
    elif change == "pair_version":
        pair["schema_version"] = 2.0
    elif change == "dimensions":
        cohort["primary_dimensions"] = ["task_outcome", "task_outcome"]
    else:
        cohort["consistency_rubric"] = ""
    save(tmp_path, cohort, pair)
    with pytest.raises(ValidationError):
        run(tmp_path)
