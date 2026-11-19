"""Offline shard aggregation, conservative build comparison, and explicit failure selection."""
import re
from dataclasses import replace
from typing import Literal

from pydantic import Field

from .ci_outputs import PASSING
from .ci_results import Diagnostic, SuiteReport, make_report, not_run
from .ci_suite import PlanError, Strict, SuitePlan, parse_shard
from .serialization import digest


def validate_binding(prepared, batch_id, build_id):
    for value in (batch_id, build_id):
        if value is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
                                  or prepared.redactor.text(value) != value):
            raise PlanError("run.identity", "run", "Batch and build IDs must be public, nonsecret identifiers.")
    if prepared.plan.shard_count > 1 and (not batch_id or not build_id):
        raise PlanError("run.binding", "shard",
                        "Sharded runs require --batch-id and --build-id shared by this build's shards.")


def selection_plan(plan, selected, shard="1/1"):
    index, count = parse_shard(shard)
    selected = sorted(selected)
    if not selected:
        raise PlanError("selection.no-failures", "failed-from", "No unresolved cases match this selection.")
    if count > len(selected):
        raise PlanError("selection.empty-shard", "shard", "Shard count exceeds selected unresolved cases.")
    identity = {case.id: case.definition_sha256 for case in plan.cases}
    return SuitePlan.model_validate({
        **plan.model_dump(), "selected": selected, "scope_sha256": digest({key: identity[key] for key in selected}),
        "scheduled": selected[index - 1::count], "shard_index": index, "shard_count": count})


def select_failures(prepared, previous, *, shard="1/1"):
    if previous.plan is None or previous.plan.suite_id != prepared.plan.suite_id \
            or previous.plan.suite_sha256 != prepared.plan.suite_sha256:
        raise PlanError("selection.changed", "failed-from",
                        "Failure selection requires the same complete suite definitions as the earlier report.")
    selected = [row.id for row in previous.cases if row.exit_code and row.id in prepared.plan.selected]
    if any(prepared.cases[key].public.state == "shared" and prepared.cases[key].public.kind == "connector"
           for key in selected):
        raise PlanError("selection.shared-write", "failed-from",
                        "Reconcile shared-service writes before selecting cases manually; failure selection needs fresh state.")
    return replace(prepared, plan=selection_plan(prepared.plan, selected, shard))


def merge_reports(reports: list[SuiteReport]):
    if not reports or any(report.plan is None for report in reports):
        raise PlanError("merge.plan", "reports", "Merge needs reports with validated plans.")
    first = reports[0]
    if first.plan.shard_count == 1 or any(report.merged_from for report in reports):
        raise PlanError("merge.shards", "reports", "Merge accepts original shard reports, not unsharded or merged reports.")
    if len({report.report_id for report in reports}) != len(reports) or len({
            report.plan.shard_index for report in reports}) != len(reports):
        raise PlanError("merge.duplicate", "reports", "Each shard and report must appear exactly once.")
    for report in reports:
        if (report.batch_id, report.build_id, report.harness, report.plan.suite_id,
            report.plan.suite_sha256, report.plan.scope_sha256, report.plan.shard_count) != (
                first.batch_id, first.build_id, first.harness, first.plan.suite_id,
                first.plan.suite_sha256, first.plan.scope_sha256, first.plan.shard_count):
            raise PlanError("merge.identity", "reports",
                            "Shards must share batch, build, harness, suite, selection and shard-count identities.")
    ordered = sorted(reports, key=lambda report: report.plan.shard_index)
    plan = selection_plan(first.plan, first.plan.selected)
    received = {row.id: row for report in ordered for row in report.cases}
    rows = [received.get(case.id, not_run(case)) for case in plan.cases if case.id in plan.selected]
    diagnostics = []
    if len(ordered) != first.plan.shard_count:
        diagnostics.append(Diagnostic(code="merge.incomplete", message="Some shards are missing; their cases remain unrun.",
                                      action="complete-coverage"))
    return make_report(plan=plan, cases=rows, harness=first.harness, batch_id=first.batch_id, build_id=first.build_id,
                       jobs=max(report.jobs for report in reports), seconds=max(report.seconds for report in reports),
                       diagnostics=diagnostics, merged_from=[report.report_id for report in ordered],
                       fatal=Diagnostic(code="merge.source-error",
                                        message="A source shard has a fatal harness or reporting error.",
                                        action="inspect-private-evidence") if any(report.fatal for report in reports) else None)


class Difference(Strict):
    code: str
    case_id: str | None = None
    check_id: str | None = None
    message: str


class Comparison(Strict):
    schema_version: Literal[1] = 1
    kind: Literal["catt-ci-diff"] = "catt-ci-diff"
    baseline_report: str | None
    candidate_report: str | None
    baseline_build: str | None
    candidate_build: str | None
    exit_code: Literal[0, 1, 2, 3]
    status: Literal["no-regression", "regression", "error", "incomparable"]
    evidence_label: Literal["scripted surrogate comparison; no model inference"] = (
        "scripted surrogate comparison; no model inference")
    differences: list[Difference] = Field(default_factory=list)


def compare_reports(baseline, candidate):
    differences, codes = [], set()

    def add(code, name, message, case=None, check=None):
        codes.add(code)
        differences.append(Difference(code=name, case_id=case, check_id=check, message=message))

    if not baseline.build_id or not candidate.build_id:
        add(3, "diff.build", "Both runs need declared --build-id values to compare builds.")
    if baseline.harness != candidate.harness:
        add(3, "diff.harness", "Harness implementations or dependency locks differ.")
    for report in (baseline, candidate):
        if report.plan is None or report.exit_code == 2:
            add(2, "diff.error", "A source report contains a setup, execution or reporting error.")
        elif report.plan.scheduled != [case.id for case in report.plan.cases] or any(
                row.outcome is None or row.status in ("not-run", "cancelled", "blocked") for row in report.cases):
            add(3, "diff.coverage", "Comparison needs full-suite observations; a selection, shard or case is incomplete.")
    if baseline.plan and candidate.plan:
        if baseline.plan.suite_id != candidate.plan.suite_id:
            add(3, "diff.suite", "The reports name different suites.")
        before = {case.id: case for case in baseline.plan.cases}
        after = {case.id: case for case in candidate.plan.cases}
        for key in sorted(before.keys() - after.keys()):
            add(1, "diff.removed", "A baseline case is absent from the candidate suite.", key)
        for key in sorted(before.keys() & after.keys()):
            if before[key].definition_sha256 != after[key].definition_sha256:
                add(3, "diff.definition", "A case's input, treatment, expectation or state contract changed.", key)
        old_rows = {row.id: row for row in baseline.cases}
        for row in candidate.cases:
            if row.exit_code == 1:
                add(1, "diff.finding", "The candidate has an unexpected finding.", row.id)
            elif row.exit_code == 3:
                add(3, "diff.blocked", "The candidate case lacks a conclusive outcome.", row.id)
            old = old_rows.get(row.id)
            if not old:
                continue
            checks = {check.id: check for check in row.checks}
            for check in old.checks:
                new = checks.get(check.id)
                if check.required and (new is None or not new.required) and row.outcome is not None:
                    add(3, "diff.check-missing", "A previously required observation is missing or optional.",
                        row.id, check.id)
                elif new and check.status in PASSING and new.status not in PASSING and (
                        check.required or new.status == "failed"):
                    add(1 if new.status == "failed" else 3, "diff.check", "A previously passing observation regressed.",
                        row.id, check.id)
    code = next((code for code in (2, 1, 3) if code in codes), 0)
    return Comparison(baseline_report=baseline.report_id, candidate_report=candidate.report_id,
                      baseline_build=baseline.build_id, candidate_build=candidate.build_id, exit_code=code,
                      status={0: "no-regression", 1: "regression", 2: "error", 3: "incomparable"}[code],
                      differences=differences)
