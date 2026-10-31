"""Public CI reports: exact expectations, honest incomplete coverage, JSON and JUnit."""
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import Field, model_validator

from .ci_outputs import INVALID_XML, PASSING, SKIPPED
from .ci_suite import PlanError, Strict, SuitePlan, read_document, validate_model
from .serialization import digest

CaseStatus = Literal["passed", "expected-failure", "failed", "blocked", "error", "cancelled", "not-run"]
CODE_STATUS = {0: "completed", 1: "failed", 2: "error", 3: "blocked"}
CASE_CODE = {"passed": 0, "expected-failure": 0, "failed": 1, "error": 2,
             "blocked": 3, "cancelled": 3, "not-run": 3}
ERROR_CLASSES = frozenset({"setup/infrastructure", "reporting", "harness", "infrastructure"})


class Diagnostic(Strict):
    code: str
    message: str
    action: Literal["fix-input", "inspect-checks", "inspect-private-evidence", "reconcile-before-rerun",
                    "complete-coverage", "supply-observation"]


class CheckResult(Strict):
    id: str
    status: Literal["passed", "no-finding", "warning", "info", "failed", "blocked", "observation-gap",
                    "unsupported", "not-evaluated"]
    required: bool
    detail: str = Field(max_length=2000)
    failure_class: str | None = None


class Outcome(Strict):
    exit_code: Literal[0, 1, 2, 3]
    status: Literal["completed", "failed", "blocked", "error"]
    failure_stage: str | None
    failure_class: str | None
    reason: str | None = Field(max_length=2000)


def significant(checks):
    return {row.id: row.status for row in checks
            if row.status == "failed" or row.required and row.status not in PASSING}


def judge(outcome, checks, expectation):
    """A broad nonzero exit never counts as an expected finding; errors cannot be expected passes."""
    if outcome.exit_code == 2 or outcome.failure_class in ERROR_CLASSES or any(
            (row.failure_class in ERROR_CLASSES or row.id == "verification") and row.status not in PASSING
            for row in checks):
        return "error"
    if outcome.exit_code == 0:
        return "passed" if expectation.exit_code == 0 and not significant(checks) else "failed"
    if expectation.exit_code != 0 and outcome.exit_code == expectation.exit_code \
            and significant(checks) == expectation.checks:
        return "expected-failure"
    if expectation.exit_code != 0:
        return "failed"
    return "failed" if outcome.exit_code == 1 else "blocked"


class CaseResult(Strict):
    id: str
    definition_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: CaseStatus
    exit_code: Literal[0, 1, 2, 3]
    seconds: float = Field(ge=0, allow_inf_nan=False)
    attempts: Literal[0, 1]
    outcome: Outcome | None
    checks: list[CheckResult]
    compatibility_pass: bool
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,120}$")
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def honest(self):
        if self.exit_code != CASE_CODE[self.status]:
            raise ValueError("Case status disagrees with its exit code")
        if len({row.id for row in self.checks}) != len(self.checks):
            raise ValueError("Check IDs must be unique")
        if self.status in ("passed", "expected-failure") and (
                self.outcome is None or not self.checks or self.attempts != 1 or not self.run_id):
            raise ValueError("A passing gate needs a completed, observed attempt")
        if self.compatibility_pass and (self.status != "passed" or self.outcome is None
                                        or self.outcome.exit_code != 0):
            raise ValueError("An expected failure is not a compatibility pass")
        if self.status == "not-run" and (self.attempts or self.outcome or self.checks or self.run_id):
            raise ValueError("An unrun case cannot claim observations")
        return self


def aggregate_code(cases):
    codes = {row.exit_code for row in cases}
    return next((code for code in (2, 1, 3) if code in codes), 0)


class SuiteReport(Strict):
    schema_version: Literal[1] = 1
    kind: Literal["catt-ci-report"] = "catt-ci-report"
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    created_at: str
    evidence_label: Literal["scripted surrogate compatibility; no model inference"] = (
        "scripted surrogate compatibility; no model inference")
    harness: dict[str, str]
    plan: SuitePlan | None
    cases: list[CaseResult]
    exit_code: Literal[0, 1, 2, 3]
    status: Literal["completed", "failed", "error", "blocked"]
    seconds: float = Field(ge=0, allow_inf_nan=False)
    jobs: int = Field(ge=1, le=64)
    model_requests: Literal[0] = 0
    compatibility_pass: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    merged_from: list[str] = Field(default_factory=list)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent(self):
        document = self.model_dump(exclude={"content_sha256"})
        if digest(document) != self.content_sha256:
            raise ValueError("Report digest does not match its content")
        if self.status != CODE_STATUS[self.exit_code]:
            raise ValueError("Report status disagrees with its exit code")
        if self.plan is None:
            if self.exit_code != 2 or self.cases or not self.diagnostics or self.compatibility_pass:
                raise ValueError("A report without a validated plan is a setup error")
            return self
        expected = {row.id: row for row in self.plan.cases}
        if sorted(row.id for row in self.cases) != sorted(self.plan.scheduled):
            raise ValueError("Report must contain exactly one row per scheduled case, including unrun cases")
        for row in self.cases:
            if row.definition_sha256 != expected[row.id].definition_sha256:
                raise ValueError("Case definition differs from the plan")
            if row.outcome and row.status not in ("cancelled", "blocked", "error") and row.status != judge(
                    row.outcome, row.checks, expected[row.id].expect):
                raise ValueError("Gate result does not satisfy its exact expectation")
        if self.exit_code != aggregate_code(self.cases):
            raise ValueError("Report exit code disagrees with its cases")
        full = self.plan.scheduled == [row.id for row in self.plan.cases]
        if self.compatibility_pass != (full and all(row.compatibility_pass for row in self.cases)):
            raise ValueError("Incomplete or negative coverage cannot claim full-suite compatibility")
        return self


def make_report(*, plan, cases, harness, jobs=1, seconds=0, diagnostics=(), report_id=None, merged_from=()):
    code = aggregate_code(cases) if plan else 2
    document = {
        "schema_version": 1, "kind": "catt-ci-report", "report_id": report_id or uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "evidence_label": "scripted surrogate compatibility; no model inference",
        "harness": harness, "plan": plan.model_dump() if plan else None,
        "cases": [row.model_dump() for row in cases], "exit_code": code, "status": CODE_STATUS[code],
        "seconds": float(round(seconds, 6)), "jobs": jobs, "model_requests": 0,
        "compatibility_pass": bool(plan and plan.scheduled == [row.id for row in plan.cases]
                                   and all(row.compatibility_pass for row in cases)),
        "diagnostics": [row.model_dump() for row in diagnostics], "merged_from": list(merged_from),
    }
    return SuiteReport.model_validate({**document, "content_sha256": digest(document)})


def not_run(case):
    return CaseResult(id=case.id, definition_sha256=case.definition_sha256, status="not-run", exit_code=3,
                      seconds=0, attempts=0, outcome=None, checks=[], compatibility_pass=False,
                      diagnostics=[Diagnostic(code="case.not-run", message="This case has no observations.",
                                              action="complete-coverage")])


def load_report(path):
    return validate_model(SuiteReport, read_document(Path(path), location="report"), "report")


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".catt-ci-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def xml_text(value):
    return INVALID_XML.sub("\ufffd", str(value))


def junit(report):
    root = ElementTree.Element("testsuites", name="CATT scripted surrogate CI")
    if report.plan is None:
        suite = ElementTree.SubElement(root, "testsuite", name="suite-input", tests="1", errors="1",
                                       failures="0", skipped="0")
        node = ElementTree.SubElement(suite, "testcase", name="validation")
        ElementTree.SubElement(node, "error", message="Suite validation failed").text = xml_text(
            "\n".join(row.message for row in report.diagnostics))
    else:
        plans = {row.id: row for row in report.plan.cases}
        for case in report.cases:
            planned = plans[case.id]
            suite = ElementTree.SubElement(root, "testsuite", name=case.id, time=f"{case.seconds:.6f}")
            properties = ElementTree.SubElement(suite, "properties")
            for key, value in {**planned.treatment.model_dump(), "evidence": report.evidence_label,
                               "gate_status": case.status, "underlying_exit_code": (
                                   case.outcome.exit_code if case.outcome else "unobserved")}.items():
                ElementTree.SubElement(properties, "property", name=key, value=xml_text(value))
            gate = ElementTree.SubElement(suite, "testcase", name="case-expectation", classname=case.id)
            if case.status not in ("passed", "expected-failure"):
                kind = "error" if case.exit_code == 2 else "skipped" if case.exit_code == 3 else "failure"
                ElementTree.SubElement(gate, kind, message=case.status).text = xml_text(
                    "\n".join(row.message for row in case.diagnostics))
            for check in case.checks:
                node = ElementTree.SubElement(suite, "testcase", name=xml_text(check.id), classname=case.id)
                expected = case.status == "expected-failure" and planned.expect.checks.get(check.id) == check.status
                if check.status in SKIPPED:
                    ElementTree.SubElement(node, "skipped", message=check.status).text = xml_text(check.detail)
                elif check.status == "failed" and not expected:
                    ElementTree.SubElement(node, "failure", message="failed").text = xml_text(check.detail)
                if expected:
                    ElementTree.SubElement(node, "system-out").text = xml_text(
                        f"Expected {check.status}; underlying check did not pass. {check.detail}")
            suite.set("tests", str(len(suite.findall("testcase"))))
            for attribute, element in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
                suite.set(attribute, str(len(suite.findall(f"testcase/{element}"))))
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def write_report(report, directory):
    directory = Path(directory)
    atomic_write(directory / "report.json", report.model_dump_json(indent=2) + "\n")
    atomic_write(directory / "junit.xml", junit(report))


def output_directories(root, report_dir, private_dir):
    root = Path(root).resolve()
    public, private = (root / report_dir).resolve(), (root / private_dir).resolve()
    if not public.is_relative_to(root) or not private.is_relative_to(root):
        raise PlanError("output.path", "output", "CI output directories must stay inside the working directory.")
    if public.is_relative_to(private) or private.is_relative_to(public):
        raise PlanError("output.private", "output", "Public reports and private evidence need separate directories.")
    if public.exists() and (not public.is_dir() or any(
            row.name not in {"report.json", "junit.xml"} or not row.is_file() or row.is_symlink()
            for row in public.iterdir())):
        raise PlanError("output.mixed", "report-dir",
                        "Use an empty report directory or one containing only CI report.json and junit.xml.")
    return public, private
