"""Structured, artifact-bound observations; no inference about hidden Muse state."""
import hashlib
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .config import Strict
from .errors import LabError
from .serialization import canonical_bytes, file_digest, loads
from .surface import inside

DIMENSIONS = (
    "task_outcome", "backend_state", "surface", "operation_sequence", "arguments",
    "request_repetition", "visible_approvals", "handoffs", "final_response_consistency",
)
Dimension = Literal[
    "task_outcome", "backend_state", "surface", "operation_sequence", "arguments",
    "request_repetition", "visible_approvals", "handoffs", "final_response_consistency",
]


class Artifact(Strict):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["transcript", "screenshot", "target-requests", "backend-state"]


class Operation(Strict):
    # Assigned by the collector from the scenario, not inferred by the analyzer.
    milestone: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    arguments: dict | None = None


class Operations(Strict):
    completeness: Literal["complete", "partial"]
    items: list[Operation]


class Intervention(Strict):
    milestone: str = Field(min_length=1)
    kind: Literal["approval", "handoff"]
    outcome: str | None = Field(default=None, min_length=1)
    details: dict | None = None


class Interventions(Strict):
    completeness: Literal["complete", "partial"]
    items: list[Intervention]


class Side(Strict):
    sources: list[Artifact] = Field(min_length=1)
    task_success: bool | None = None
    backend_state: dict | None = None
    surface: Literal["cli-skill", "api-tools", "browser", "mixed"] | None = None
    operations: Operations | None = None
    interventions: Interventions | None = None
    final_response: str | None = Field(default=None, min_length=1)
    final_consistent: bool | None = None

    @model_validator(mode="after")
    def supported_annotations(self):
        kinds = {source.kind for source in self.sources}
        visible = bool(kinds & {"transcript", "screenshot"})
        if self.backend_state is not None and "backend-state" not in kinds:
            raise ValueError("Backend observations require a backend-state source")
        if self.task_success is not None and self.backend_state is None:
            raise ValueError("Task outcome annotations require independent backend observations")
        if self.operations is not None and "target-requests" not in kinds:
            raise ValueError("Operation observations require a target-request source")
        if (self.surface is not None or self.interventions is not None or self.final_response is not None) \
                and not visible:
            raise ValueError("Surface, interventions and final response require visible source evidence")
        if self.final_consistent is not None and (self.final_response is None or self.backend_state is None):
            raise ValueError("Final consistency annotations require the response and independent backend state")
        return self


class Pair(Strict):
    schema_version: Literal[2]
    evidence_kind: Literal["synthetic", "muse-observed"]
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    surface_revision: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    local: Side
    muse: Side

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Pair artifact version must be an integer")
        return value


def read_pair(root, row, cohort):
    path = inside(root, row.artifact)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row.artifact_sha256:
        raise LabError("Calibration pair artifact digest mismatch")
    document = loads(raw.decode("utf-8"), json_only=True)
    from .behavior_observations import NeutralPair
    pair = (NeutralPair if document.get("schema_version") == 3 else Pair).model_validate(document)
    if (pair.scenario_id, pair.trial_id) != (row.scenario_id, row.trial_id):
        raise LabError("Calibration artifact belongs to another scenario or trial")
    if (pair.evidence_kind, pair.profile_sha256, pair.surface_revision) != (
            cohort.evidence_kind, cohort.profile_sha256, cohort.surface_revision):
        raise LabError("Calibration artifact provenance differs from its cohort")
    for side in (pair.local, observed_side(pair)):
        for source in side.sources:
            if file_digest(inside(root, source.path)) != source.sha256:
                raise LabError("Calibration source artifact digest mismatch")
    return pair


def equal(left, right):
    # JSON equality preserves scalar types, unlike Python's True == 1.
    return canonical_bytes(left) == canonical_bytes(right)


def observed_side(pair):
    return pair.observed if pair.schema_version == 3 else pair.muse


def aligned_operations(pair):
    left, right = pair.local.operations, observed_side(pair).operations
    if left is None or right is None:
        return None, "operations_not_observed"
    if left.completeness != "complete" or right.completeness != "complete":
        return None, "partial_operation_trace"
    # Arguments compare within scenario-declared milestones. Arrival order is a
    # separate dimension; repeated attempts keep their order within a milestone.
    def grouped(trace):
        result = {}
        for item in trace.items:
            result.setdefault(item.milestone, []).append(item)
        return result
    a, b = grouped(left), grouped(right)
    if a.keys() != b.keys() or any(len(a[key]) != len(b[key]) for key in a):
        return None, "unmatched_milestones_or_attempts"
    pairs = [(x, y) for key in a for x, y in zip(a[key], b[key])]
    if any(x.operation != y.operation for x, y in pairs):
        return None, "unmatched_operations"
    if any(x.arguments is None or y.arguments is None for x, y in pairs):
        return None, "arguments_not_observed"
    return pairs, None


def compare_pair(pair):
    if pair.schema_version == 3 and not pair.comparable:
        return {name: {"status": "unknown", "reason": "control_or_invalid_measurement"}
                for name in DIMENSIONS}
    left, right = pair.local, observed_side(pair)
    results = {}

    def comparison(name, a, b, reason="not_observed"):
        results[name] = {"status": "unknown", "reason": reason} if a is None or b is None else {
            "status": "agrees" if equal(a, b) else "differs", "reason": "observed_values"}

    comparison("task_outcome", left.task_success, right.task_success)
    comparison("backend_state", left.backend_state, right.backend_state)
    comparison("surface", left.surface, right.surface)
    a, b = left.operations, right.operations
    complete = a is not None and b is not None and a.completeness == b.completeness == "complete"
    comparison("operation_sequence",
        [(i.milestone, i.operation) for i in a.items] if complete else None,
        [(i.milestone, i.operation) for i in b.items] if complete else None,
        "missing_or_partial_operation_trace")
    pairs, reason = aligned_operations(pair)
    if pairs is None:
        results["arguments"] = {"status": "unknown", "reason": reason}
    else:
        results["arguments"] = {"status": "agrees" if all(equal(x.arguments, y.arguments) for x, y in pairs)
                               else "differs", "reason": "matched_scenario_milestones"}

    def repetitions(trace):
        if trace is None or trace.completeness != "complete" or any(i.arguments is None for i in trace.items):
            return None
        seen, pattern = {}, []
        for item in trace.items:
            key = item.milestone, item.operation
            previous = seen.setdefault(key, [])
            classification = ("first" if not previous else "repeated_arguments"
                              if any(equal(item.arguments, old) for old in previous) else "changed_arguments")
            pattern.append([item.milestone, item.operation, classification])
            previous.append(item.arguments)
        return pattern
    comparison("request_repetition", repetitions(a), repetitions(b), "missing_or_partial_arguments")
    for name, kind in (("visible_approvals", "approval"), ("handoffs", "handoff")):
        def visible(side, kind):
            trace = side.interventions
            if trace is None or trace.completeness != "complete":
                return None
            items = [row for row in trace.items if row.kind == kind]
            if any(row.outcome is None or row.details is None for row in items):
                return None
            return [row.model_dump() for row in items]
        comparison(name, visible(left, kind), visible(right, kind), "missing_or_partial_visible_trace")
    comparison("final_response_consistency", left.final_consistent, right.final_consistent,
               "consistency_not_annotated")
    return results
