"""Exact negative expectations cannot hide new failures, errors, or incomplete CI coverage."""
import copy
from xml.etree import ElementTree

import pytest
from test_ci_plan import suite_document, write_inputs

from consumer_agent_testing_tool.ci_results import (
    CaseResult,
    CheckResult,
    Outcome,
    SuiteReport,
    judge,
    junit,
    make_report,
    not_run,
    output_directories,
)
from consumer_agent_testing_tool.ci_suite import Expectation, PlanError, prepare_suite
from consumer_agent_testing_tool.serialization import digest


def check(identifier="one-policy", status="passed", **kwargs):
    return CheckResult(id=identifier, status=status, required=True, detail="A synthetic check.", **kwargs)


def outcome(code, **kwargs):
    return Outcome(exit_code=code, status={0: "completed", 1: "failed", 2: "error", 3: "blocked"}[code],
                   failure_stage="checks", failure_class=kwargs.get("failure_class"), reason=None)


@pytest.mark.parametrize("code, checks, expected, status", [
    (0, [check()], {}, "passed"),
    (1, [check(status="failed")], {}, "failed"),
    (3, [check(status="blocked")], {}, "blocked"),
    (1, [check(status="failed")], {"exit_code": 1, "checks": {"one-policy": "failed"}}, "expected-failure"),
    (3, [check(status="blocked")], {"exit_code": 3, "checks": {"one-policy": "blocked"}}, "expected-failure"),
    (0, [check()], {"exit_code": 1, "checks": {"one-policy": "failed"}}, "failed"),
    (1, [check(status="failed"), check("approval.dispatches_match", "failed")],
     {"exit_code": 1, "checks": {"one-policy": "failed"}}, "failed"),
    (1, [check(status="failed"), check("answer.no_false_claims", "failed")],
     {"exit_code": 1, "checks": {"one-policy": "failed"}}, "failed"),
    (1, [check(status="failed"), check("other", "blocked")],
     {"exit_code": 1, "checks": {"one-policy": "failed"}}, "failed"),
    (2, [check(status="failed")], {"exit_code": 1, "checks": {"one-policy": "failed"}}, "error"),
    (1, [check(status="failed", failure_class="setup/infrastructure")],
     {"exit_code": 1, "checks": {"one-policy": "failed"}}, "error"),
    (1, [check(status="failed", failure_class="verification")],
     {"exit_code": 1, "checks": {"one-policy": "failed"}}, "expected-failure"),
])
def test_gate_verdict_requires_exact_findings(code, checks, expected, status):
    assert judge(outcome(code), checks, Expectation.model_validate(expected)) == status


def test_suite_errors_outrank_failures_and_unrun_cases_are_not_green(tmp_path):
    plan = prepare_suite(write_inputs(tmp_path), root=tmp_path).plan
    rows = [not_run(case) for case in plan.cases]
    report = make_report(plan=plan, cases=rows, harness={"version": "test"})
    assert report.exit_code == 3 and not report.compatibility_pass
    rows[0].status, rows[0].exit_code = "error", 2
    rows[1].status, rows[1].exit_code = "failed", 1
    assert make_report(plan=plan, cases=rows, harness={"version": "test"}).exit_code == 2


def test_report_requires_every_scheduled_case_once_and_a_consistent_digest(tmp_path):
    plan = prepare_suite(write_inputs(tmp_path), root=tmp_path).plan
    rows = [not_run(case) for case in plan.cases]
    report = make_report(plan=plan, cases=rows, harness={"version": "test"})
    with pytest.raises(ValueError):
        make_report(plan=plan, cases=rows[:-1], harness={"version": "test"})
    value = report.model_dump()
    value["exit_code"] = 0
    with pytest.raises(ValueError):
        SuiteReport.model_validate(value)
    value["content_sha256"] = digest({key: item for key, item in value.items() if key != "content_sha256"})
    with pytest.raises(ValueError):
        SuiteReport.model_validate(value)
    with pytest.raises(ValueError):
        make_report(plan=plan, cases=[*rows[:-1], copy.deepcopy(rows[0])], harness={"version": "test"})


def test_junit_preserves_underlying_blocked_and_expected_failure_status(tmp_path):
    suite = suite_document()
    suite["cases"][0]["expect"] = {"exit_code": 3, "checks": {"one-confirmed-booking": "blocked"}}
    prepared = prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path)
    plan = prepared.plan
    row = plan.cases[0]
    rows = [CaseResult(id=row.id, definition_sha256=row.definition_sha256, status="expected-failure",
                       exit_code=0, seconds=1.0, attempts=1, outcome=outcome(3),
                       checks=[check("one-confirmed-booking", "blocked")],
                       compatibility_pass=False, run_id="connector-test"),
            *[not_run(case) for case in plan.cases[1:]]]
    report = make_report(plan=plan, cases=rows, harness={"version": "test"})
    xml = ElementTree.fromstring(junit(report))
    observed = xml.find("testsuite")
    assert observed.find("testcase[@name='case-expectation']/failure") is None
    assert observed.find("testcase[@name='one-confirmed-booking']/skipped") is not None
    assert "did not pass" in observed.find("testcase[@name='one-confirmed-booking']/system-out").text
    assert report.compatibility_pass is False and report.exit_code == 3


@pytest.mark.parametrize("public, private", [("reports", "reports/private"), ("private/reports", "private"),
                                            ("reports", "reports"), ("../outside", "private")])
def test_public_and_private_output_directories_are_disjoint(tmp_path, public, private):
    with pytest.raises(PlanError):
        output_directories(tmp_path, public, private)


def test_artifact_directory_cannot_mix_existing_private_data(tmp_path):
    public = tmp_path / "reports"
    public.mkdir()
    (public / "private.log").write_text("do not upload")
    with pytest.raises(PlanError, match="empty report directory"):
        output_directories(tmp_path, "reports", "private")
    assert (public / "private.log").read_text() == "do not upload"
