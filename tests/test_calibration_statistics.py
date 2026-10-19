import copy
import json
import math
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_calibration_observations import PROFILE, packet, run

from consumer_agent_testing_tool.calibration import ObservableCohort
from consumer_agent_testing_tool.calibration_statistics import ScenarioAnalysis, analyze
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.serialization import file_digest


def plan(weighting="paired-trials"):
    return ScenarioAnalysis(method="scenario-cluster-t-v1", independent_unit="scenario_id",
                            weighting=weighting)


def rows(scenario, successes, failures=0, unknown=0):
    values = ["agrees"] * successes + ["differs"] * failures + ["unknown"] * unknown
    return [(scenario, str(index), value) for index, value in enumerate(values)]


def test_cluster_interval_does_not_treat_repetitions_as_independent_scenarios():
    first = analyze(rows("a", 1) + rows("b", 0, 1) + rows("c", 1), plan())
    repeated = analyze(rows("a", 30) + rows("b", 0, 30) + rows("c", 30), plan())
    for result in (first, repeated):
        assert result["status"] == "computed"
        assert result["observed_scenarios"] == 3 and result["degrees_of_freedom"] == 2
        assert result["estimate"] == pytest.approx(2 / 3)
        assert result["standard_error"] == pytest.approx(1 / 3)
        # Independent analytic t reference for three cluster means, df=2.
        critical = 4.302652729749464
        assert result["unclipped_interval_95"] == pytest.approx({
            "lower": 2 / 3 - critical / 3, "upper": 2 / 3 + critical / 3})
        assert result["interval_95"] == {"lower": 0, "upper": 1}
    assert first["compared_pairs"] == 3 and repeated["compared_pairs"] == 90


def test_weighting_is_an_explicit_estimand_choice():
    observations = rows("many-successes", 100) + rows("one-failure", 0, 1)
    weighted = analyze(observations, plan())
    equal = analyze(observations, plan("equal-scenarios"))
    assert weighted["estimate"] == pytest.approx(100 / 101)
    assert equal["estimate"] == .5 and equal["standard_error"] == .5
    assert equal["interval_95"] == {"lower": 0, "upper": 1}
    assert weighted["observed_scenarios"] == equal["observed_scenarios"] == 2


@pytest.mark.parametrize("weighting", ["paired-trials", "equal-scenarios"])
def test_missing_pairs_and_single_scenario_do_not_establish_uncertainty(weighting):
    result = analyze(rows("observed", 20, unknown=1) + rows("missing", 0, unknown=30), plan(weighting))
    assert result["status"] == "insufficient-scenarios"
    assert result["estimate"] == 1 and result["interval_95"] is None
    assert result["standard_error"] is None
    assert result["observed_scenarios"] == 1 and result["total_scenarios"] == 2
    assert result["compared_pairs"] == 20 and result["unknown_pairs"] == 31
    assert result["wholly_unobserved_scenarios"] == result["partially_observed_scenarios"] == 1
    missing = analyze(rows("missing", 0, unknown=5), plan(weighting))
    assert missing["estimate"] is None and missing["interval_95"] is None
    empty = analyze([], plan(weighting))
    assert empty["estimate"] is None and empty["observed_scenarios"] == 0


@pytest.mark.parametrize("weighting", ["paired-trials", "equal-scenarios"])
@pytest.mark.parametrize("successes,failures", [(1, 0), (0, 1), (1, 9)])
def test_identical_scenario_rates_do_not_produce_false_certainty(weighting, successes, failures):
    observations = rows("a", successes * 30, failures * 30) + rows("b", successes, failures)
    result = analyze(observations, plan(weighting))
    assert result["status"] == "degenerate-variation"
    assert result["interval_95"] is result["standard_error"] is None
    assert result["estimate"] == pytest.approx(successes / (successes + failures))


def test_analysis_is_deterministic_and_order_independent():
    observations = rows("a", 5, 1) + rows("b", 3, 5) + rows("c", 1, 2)
    assert analyze(observations, plan()) == analyze(list(reversed(observations)), plan())
    result = analyze(observations, plan())
    assert math.isfinite(result["standard_error"])


@pytest.mark.parametrize("change", [
    {"method": "trial-bootstrap"}, {"independent_unit": "trial_id"},
    {"weighting": "automatic"}, {"confidence_level": .9},
    {"independent_unit": True}, {"seed": 42},
])
def test_analysis_rejects_undeclared_methods_and_assumptions(change):
    with pytest.raises(ValidationError):
        ScenarioAnalysis.model_validate({**plan().model_dump(), **change})


@pytest.mark.parametrize("missing", ["method", "independent_unit", "weighting"])
def test_analysis_requires_explicit_method_unit_and_weighting(missing):
    value = plan().model_dump()
    value.pop(missing)
    with pytest.raises(ValidationError):
        ScenarioAnalysis.model_validate(value)


def multi_scenario_packet(root):
    cohort, pair = packet(root)
    cohort["analysis"] = plan("equal-scenarios").model_dump()
    cohort["observations"] = []
    for scenario, agree, count in (("a", True, 10), ("b", False, 1)):
        for index in range(count):
            item = copy.deepcopy(pair)
            item.update(scenario_id=scenario, trial_id=str(index))
            item["muse"]["task_success"] = agree
            artifact = root / f"{scenario}-{index}.json"
            artifact.write_text(json.dumps(item))
            cohort["observations"].append({
                "scenario_id": scenario, "trial_id": str(index), "split": "held-out",
                "artifact": artifact.name, "artifact_sha256": file_digest(artifact)})
    (root / "cohort.json").write_text(json.dumps(cohort))
    return cohort


def test_full_packet_analysis_preserves_pairs_unknowns_and_qualification(tmp_path):
    cohort = multi_scenario_packet(tmp_path)
    result = run(tmp_path)
    metric = result["groups"]["held-out"]["dimensions"]["task_outcome"]
    assert metric["agreement"] == pytest.approx(10 / 11)
    assert metric["scenario_analysis"]["estimate"] == .5
    assert metric["scenario_analysis"]["compared_pairs"] == 11
    assert result["primary_heldout_coverage"] and not result["usable_for_comparison"]
    assert result["analysis"]["engine"] == "inspect_ai.scorer.ci"
    assert result["analysis"]["engine_version"]
    assert result["analysis"]["release_qualification"] is False
    assert result["groups"]["calibration"]["dimensions"]["task_outcome"]["scenario_analysis"]["estimate"] is None
    cohort["analysis"]["weighting"] = "paired-trials"
    (tmp_path / "cohort.json").write_text(json.dumps(cohort))
    changed = run(tmp_path)
    assert changed["source_sha256"] != result["source_sha256"]
    assert changed["groups"]["held-out"]["dimensions"]["task_outcome"]["scenario_analysis"]["estimate"] \
        == pytest.approx(10 / 11)


def test_existing_packet_does_not_silently_assume_scenario_independence(tmp_path):
    cohort, _ = packet(tmp_path)
    assert ObservableCohort.model_validate(cohort).analysis is None
    result = run(tmp_path)
    assert result["analysis"] is None
    assert all("scenario_analysis" not in row for row in result["groups"]["held-out"]["dimensions"].values())


async def test_cli_scenario_analysis_is_account_free_and_keeps_synthetic_exclusion(tmp_path, monkeypatch, capsys):
    import socket

    multi_scenario_packet(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("Unexpected network request"))
    args = parser().parse_args(["calibrate", "cohort.json", "--profile-sha256", PROFILE,
                               "--surface-revision", "fixture-v1"])
    assert await execute(args) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["analysis"]["method"] == "scenario-cluster-t-v1"
    assert result["groups"]["held-out"]["dimensions"]["task_outcome"]["scenario_analysis"]["status"] == "computed"
    assert result["evidence_kind"] == "synthetic" and not result["usable_for_comparison"]


async def test_packaged_scenario_analysis_example_keeps_small_sample_limits(tmp_path, monkeypatch, capsys):
    source = Path(__file__).resolve().parents[1] / "src/consumer_agent_testing_tool/data/calibration"
    shutil.copytree(source, tmp_path / "calibration-examples")
    monkeypatch.chdir(tmp_path)
    args = parser().parse_args([
        "calibrate", "calibration-examples/scenario-cluster-cohort.json",
        "--profile-sha256", "a" * 64, "--surface-revision", "synthetic-fixture-v1"])
    assert await execute(args) == 3
    result = json.loads(capsys.readouterr().out)
    dimensions = result["groups"]["held-out"]["dimensions"]
    assert dimensions["task_outcome"]["scenario_analysis"]["estimate"] == .5
    assert dimensions["task_outcome"]["scenario_analysis"]["interval_95"] == {"lower": 0, "upper": 1}
    assert dimensions["surface"]["scenario_analysis"]["status"] == "degenerate-variation"
    assert result["groups"]["calibration"]["dimensions"]["task_outcome"]["scenario_analysis"]["status"] \
        == "insufficient-scenarios"
