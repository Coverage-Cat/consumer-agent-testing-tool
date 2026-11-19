"""Distributed CI evidence must not combine stale builds or hide changed or missing coverage."""
import copy
import json

import pytest
from test_ci_plan import suite_document, write_inputs
from test_ci_results import check, outcome

from consumer_agent_testing_tool.ci_history import (
    compare_reports,
    merge_reports,
    select_failures,
    validate_binding,
)
from consumer_agent_testing_tool.ci_results import CaseResult, make_report, not_run
from consumer_agent_testing_tool.ci_suite import PlanError, prepare_suite
from consumer_agent_testing_tool.cli import execute, parser


def completed(plan, *, batch="ci-100-attempt-1", build="build-a", failures=(), harness=None):
    rows = []
    for case in plan.cases:
        if case.id not in plan.scheduled:
            continue
        failed = case.id in failures
        rows.append(CaseResult(
            id=case.id, definition_sha256=case.definition_sha256, status="failed" if failed else "passed",
            exit_code=int(failed), seconds=0.25, attempts=1, outcome=outcome(int(failed)),
            checks=[check(key, "failed" if failed else "passed") for key in case.required_checks],
            compatibility_pass=not failed, run_id=f"connector-{len(rows)}"))
    return make_report(plan=plan, cases=rows, harness=harness or {"version": "test"},
                       batch_id=batch, build_id=build)


def shards(tmp_path):
    path = write_inputs(tmp_path)
    return [completed(prepare_suite(path, root=tmp_path, shard=f"{index}/3").plan) for index in range(1, 4)]


def test_shards_merge_once_in_any_order_with_complete_coverage(tmp_path):
    reports = shards(tmp_path)
    result = merge_reports(list(reversed(reports)))
    assert result.exit_code == 0 and result.compatibility_pass
    assert result.plan.shard_count == 1
    assert result.plan.scheduled == result.plan.selected
    assert result.batch_id == "ci-100-attempt-1" and result.build_id == "build-a"
    assert result.merged_from == [report.report_id for report in reports]


def test_missing_shard_records_every_missing_case_as_unrun(tmp_path):
    reports = shards(tmp_path)
    result = merge_reports(reports[:1])
    assert result.exit_code == 3 and not result.compatibility_pass
    assert {case.id for case in result.cases if case.status == "not-run"} == {
        key for report in reports[1:] for key in report.plan.scheduled}
    assert result.diagnostics[0].code == "merge.incomplete"
    assert compare_reports(result, result).exit_code == 3


@pytest.mark.parametrize("field,value", [
    ("batch", "ci-99-attempt-1"), ("batch", "ci-100-attempt-2"),
    ("build", "build-b"), ("harness", {"version": "different"}),
])
def test_stale_or_mismatched_shards_cannot_complete_a_build(tmp_path, field, value):
    reports = shards(tmp_path)
    reports[-1] = completed(reports[-1].plan, **{field: value})
    with pytest.raises(PlanError, match="identities"):
        merge_reports(reports)


def test_duplicate_shards_reports_and_remerging_are_rejected(tmp_path):
    reports = shards(tmp_path)
    for inputs in ([*reports, reports[0]], [reports[0], completed(reports[0].plan)],
                   [merge_reports(reports)]):
        with pytest.raises(PlanError):
            merge_reports(inputs)


def test_changed_definition_or_selection_cannot_be_merged(tmp_path):
    reports = shards(tmp_path)
    suite = suite_document()
    suite["cases"][0]["timeout_seconds"] = 90
    changed = prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path, shard="3/3").plan
    with pytest.raises(PlanError, match="identities"):
        merge_reports([*reports[:2], completed(changed)])


@pytest.mark.parametrize("batch,build", [(None, None), ("attempt", None), (None, "build")])
def test_distributed_execution_requires_an_explicit_binding(tmp_path, batch, build):
    prepared = prepare_suite(write_inputs(tmp_path), root=tmp_path, shard="1/2")
    with pytest.raises(PlanError, match="batch-id"):
        validate_binding(prepared, batch, build)


def test_build_identity_cannot_contain_private_values(tmp_path):
    prepared = prepare_suite(write_inputs(tmp_path), root=tmp_path)
    prepared.redactor.add(["private-build-id"])
    with pytest.raises(PlanError, match="nonsecret"):
        validate_binding(prepared, "batch", "private-build-id")


def test_comparison_requires_complete_matching_tests_and_identifies_new_check_failures(tmp_path):
    plan = prepare_suite(write_inputs(tmp_path), root=tmp_path).plan
    baseline = completed(plan)
    candidate = completed(plan, batch="next", build="build-b")
    assert compare_reports(baseline, candidate).exit_code == 0
    failed = completed(plan, build="build-b", failures=[plan.cases[0].id])
    comparison = compare_reports(baseline, failed)
    assert comparison.exit_code == 1
    assert any(row.code == "diff.check" and row.check_id == "one-confirmed-booking"
               for row in comparison.differences)
    assert compare_reports(failed, failed).exit_code == 1  # Existing unexpected failures never make a green gate.


def test_comparison_rejects_changed_checks_harness_unbound_and_missing_coverage(tmp_path):
    path = write_inputs(tmp_path)
    plan = prepare_suite(path, root=tmp_path).plan
    baseline = completed(plan)
    suite = suite_document()
    suite["cases"][0]["timeout_seconds"] = 90
    changed = completed(prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path).plan)
    partial = make_report(plan=plan, cases=[not_run(row) for row in plan.cases],
                          harness=baseline.harness, build_id="build-b")
    for candidate in (changed, completed(plan, harness={"version": "new"}), partial, completed(plan, build=None)):
        assert compare_reports(baseline, candidate).exit_code == 3


def test_removing_a_matrix_case_is_a_coverage_regression(tmp_path):
    path = write_inputs(tmp_path)
    baseline = completed(prepare_suite(path, root=tmp_path).plan)
    suite = suite_document()
    suite["cases"][0]["matrix"]["projection"] = ["native"]
    candidate = completed(prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path).plan)
    result = compare_reports(baseline, candidate)
    assert result.exit_code == 1
    assert len([row for row in result.differences if row.code == "diff.removed"]) == 2


def test_failure_selection_is_explicit_fresh_and_never_empty_green(tmp_path):
    path = write_inputs(tmp_path)
    prepared = prepare_suite(path, root=tmp_path)
    selected = prepared.plan.cases[0].id
    previous = completed(prepared.plan, failures=[selected])
    retry = select_failures(prepared, previous)
    assert retry.plan.selected == retry.plan.scheduled == [selected]
    assert retry.plan.suite_sha256 == prepared.plan.suite_sha256
    with pytest.raises(PlanError, match="No unresolved"):
        select_failures(prepared, completed(prepared.plan))
    suite = suite_document()
    suite["cases"][0]["timeout_seconds"] = 90
    changed = prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path)
    with pytest.raises(PlanError, match="same complete suite"):
        select_failures(changed, previous)


def test_shared_service_failures_need_manual_reconciliation_before_new_writes(tmp_path):
    suite = suite_document()
    suite["services"]["hotel"] = {"state": "shared"}
    prepared = prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path)
    previous = completed(prepared.plan, failures=[prepared.plan.cases[0].id])
    with pytest.raises(PlanError, match="Reconcile"):
        select_failures(prepared, previous)


async def test_cli_merge_diff_and_failure_plan_are_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    reports = shards(tmp_path)
    paths = []
    for index, report in enumerate(reports):
        path = tmp_path / f"shard-{index}.json"
        path.write_text(report.model_dump_json())
        paths.append(str(path))
    assert await execute(parser().parse_args(["ci", "merge", *paths, "--summary-json", "-"])) == 0
    merged = json.loads(capsys.readouterr().out)
    assert merged["kind"] == "catt-ci-report" and merged["compatibility_pass"]
    final = tmp_path / "reports/ci-merged/report.json"
    assert await execute(parser().parse_args(["ci", "diff", str(final), str(final)])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no-regression"
    path = tmp_path / "failed.json"
    whole = prepare_suite(tmp_path / "suite.json", root=tmp_path).plan
    path.write_text(completed(whole, failures=[whole.cases[0].id]).model_dump_json())
    assert await execute(parser().parse_args(["ci", "plan", "suite.json", "--failed-from", str(path)])) == 0
    assert json.loads(capsys.readouterr().out)["selected"] == [whole.cases[0].id]
    invalid = tmp_path / "bad.json"
    invalid.write_text('{"secret": "private-broken-input",')
    assert await execute(parser().parse_args(["ci", "diff", str(final), str(invalid)])) == 2
    text = capsys.readouterr().out
    assert json.loads(text)["status"] == "error" and "private-broken-input" not in text


def test_selected_coverage_stays_partial_after_merge(tmp_path):
    suite = suite_document()
    suite["cases"].append({**copy.deepcopy(suite["cases"][0]), "id": "second"})
    path = write_inputs(tmp_path, suite=suite)
    reports = [completed(prepare_suite(path, root=tmp_path, cases=["book"], shard=f"{index}/2").plan)
               for index in (1, 2)]
    merged = merge_reports(reports)
    assert merged.exit_code == 0 and not merged.compatibility_pass
    assert compare_reports(merged, merged).exit_code == 3


def test_service_dependency_changes_do_not_relabel_the_installed_harness(tmp_path):
    from consumer_agent_testing_tool.ci_run import harness_identity

    first, second = tmp_path / "service-a", tmp_path / "service-b"
    first.mkdir()
    second.mkdir()
    (first / "uv.lock").write_text("service dependencies before")
    (second / "uv.lock").write_text("service dependencies after")
    assert harness_identity(first) == harness_identity(second)


def test_changed_runtime_dependency_is_part_of_harness_identity(tmp_path, monkeypatch):
    from consumer_agent_testing_tool import ci_run

    before = ci_run.harness_identity(tmp_path)
    original = ci_run.version
    monkeypatch.setattr(ci_run, "version", lambda name: "synthetic-different-version" if name == "mcp" else original(name))
    assert ci_run.harness_identity(tmp_path) != before


def test_fatal_reporting_error_cannot_disappear_during_merge(tmp_path):
    from consumer_agent_testing_tool.ci_results import write_failed

    reports = shards(tmp_path)
    reports[0] = write_failed(reports[0])
    merged = merge_reports(reports)
    assert merged.exit_code == 2 and not merged.compatibility_pass and merged.fatal
