"""Reporting regressions reproduced from the final integration review."""
import copy
from pathlib import Path
from xml.etree import ElementTree

import pytest
from test_ci_execution import fixture_inputs
from test_ci_history import completed
from test_ci_plan import suite_document, write_inputs

from consumer_agent_testing_tool import ci_results
from consumer_agent_testing_tool.ci_history import merge_reports
from consumer_agent_testing_tool.ci_results import (
    CheckResult,
    SuiteReport,
    load_report,
    make_report,
    write_report,
)
from consumer_agent_testing_tool.ci_suite import (
    MAX_INPUT_BYTES,
    PlanError,
    prepare_suite,
    read_document,
)
from consumer_agent_testing_tool.cli import execute, parser


def assert_reporting_error_or_missing_xml(path):
    if path.exists():
        assert ElementTree.parse(path).find(".//testsuite[@name='suite-reporting']/testcase/error") is not None


@pytest.mark.parametrize("failed_checkpoint", [1, 2])
async def test_junit_failure_cannot_leave_a_green_shard(
        tmp_path, monkeypatch, capsys, failed_checkpoint):
    monkeypatch.chdir(tmp_path)
    path, _ = fixture_inputs(tmp_path, count=2)
    original, completed_writes = ci_results.atomic_write, 0

    def fail_junit(destination, text):
        nonlocal completed_writes
        if Path(destination).name == "junit.xml" and 'name="gate_status" value="passed"' in text:
            completed_writes += 1
            if completed_writes >= failed_checkpoint:
                raise OSError("RAW-PRIVATE-JUNIT-WRITE-ERROR")
        return original(destination, text)

    monkeypatch.setattr(ci_results, "atomic_write", fail_junit)
    code = await execute(parser().parse_args([
        "ci", "run", str(path), "--shard", "1/2", "--batch-id", "integration-1",
        "--build-id", "service-1", "--summary-json", "-"]))
    text = capsys.readouterr().out
    observed = SuiteReport.model_validate_json(text)
    assert code == observed.exit_code == 2 and observed.fatal
    assert observed.cases[0].status == "passed"
    assert "RAW-PRIVATE-JUNIT-WRITE-ERROR" not in text
    saved = load_report(tmp_path / "reports/ci/report.json")
    assert saved.exit_code == 2 and saved.fatal, "A reporting error left a passing JSON artifact"
    assert_reporting_error_or_missing_xml(tmp_path / "reports/ci/junit.xml")
    sibling = completed(prepare_suite(path, root=tmp_path, shard="2/2").plan,
                        batch=observed.batch_id, build=observed.build_id, harness=observed.harness)
    assert merge_reports([saved, sibling]).exit_code == 2


def test_failed_json_replacement_removes_an_older_green_report(tmp_path, monkeypatch):
    report = completed(prepare_suite(write_inputs(tmp_path), root=tmp_path).plan)
    public = tmp_path / "public"
    write_report(report, public)
    original = ci_results.atomic_write

    def fail_json(destination, text):
        if Path(destination).name == "report.json":
            raise OSError("cannot replace report")
        return original(destination, text)

    monkeypatch.setattr(ci_results, "atomic_write", fail_json)
    with pytest.raises(OSError):
        write_report(report, public)
    assert not (public / "report.json").exists()
    assert_reporting_error_or_missing_xml(public / "junit.xml")


def test_large_suite_can_read_its_own_report_without_raising_the_input_limit(tmp_path):
    suite = suite_document()
    suite["cases"] = [{**copy.deepcopy(suite["cases"][0]), "id": f"case-{index}"} for index in range(128)]
    plan = prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path).plan
    assert len(plan.cases) == 512
    rows = completed(plan).cases
    for row in rows:
        row.checks.extend(CheckResult(id=f"detail-{index}", status="info", required=False, detail="x" * 2000)
                          for index in range(5))
    report = make_report(plan=plan, cases=rows, harness={"version": "test"})
    public = tmp_path / "public"
    write_report(report, public)
    path = public / "report.json"
    assert path.stat().st_size > MAX_INPUT_BYTES
    assert load_report(path).content_sha256 == report.content_sha256
    with pytest.raises(PlanError, match="4 MiB"):
        read_document(path, location="suite")
