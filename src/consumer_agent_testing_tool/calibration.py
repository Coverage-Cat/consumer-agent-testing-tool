"""Authorized external observations: paired cohorts, held-out data, explicit visibility gaps."""
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from .calibration_observations import DIMENSIONS, Dimension, compare_pair, read_pair
from .calibration_statistics import ScenarioAnalysis, analyze, provenance
from .errors import LabError
from .metrics import wilson
from .serialization import digest, file_digest, load
from .surface import Strict, inside


class Observation(Strict):
    scenario_id: str
    trial_id: str
    split: Literal["calibration", "held-out"]
    local_success: StrictBool
    muse_success: StrictBool
    observable_operations: list[str] = Field(default_factory=list)
    artifact: str
    artifact_sha256: str


class Cohort(Strict):
    schema_version: Literal[1] = 1
    cohort: str
    authorized: Literal[True]
    observed_at: str
    expires_at: str
    surface_revision: str
    profile_sha256: str
    observations: list[Observation] = Field(min_length=1)
    visibility: dict[Literal["model", "policy", "browser", "discovery"],
                     Literal["observed", "partial", "unknown"]]

    @field_validator("authorized", mode="before")
    @classmethod
    def explicit_authorization(cls, value):
        if value is not True:
            raise ValueError("An explicit true authorization declaration is required")
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Calibration schema version must be an integer")
        return value

    @model_validator(mode="after")
    def disjoint(self):
        splits, keys = {}, set()
        for row in self.observations:
            if row.scenario_id in splits and splits[row.scenario_id] != row.split:
                raise ValueError("Calibration and held-out scenarios must be disjoint")
            splits[row.scenario_id] = row.split
            key = row.scenario_id, row.trial_id
            if key in keys:
                raise ValueError("Duplicate paired observation")
            keys.add(key)
        if set(self.visibility) != {"model", "policy", "browser", "discovery"}:
            raise ValueError("All visibility dimensions must be explicit")
        return self


class PairReference(Strict):
    scenario_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    split: Literal["calibration", "held-out"]
    artifact: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ObservableCohort(Cohort):
    schema_version: Literal[2]
    evidence_kind: Literal["synthetic", "muse-observed"]
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    primary_dimensions: list[Dimension] = Field(default_factory=lambda: list(DIMENSIONS), min_length=1)
    consistency_rubric: str = Field(min_length=1)
    observations: list[PairReference] = Field(min_length=1)
    analysis: ScenarioAnalysis | None = None

    @model_validator(mode="after")
    def distinct_dimensions(self):
        if len(set(self.primary_dimensions)) != len(self.primary_dimensions):
            raise ValueError("Primary comparison dimensions must be distinct")
        return self


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise LabError("Calibration timestamps must include a timezone")
    return result


def evaluate(root, packet, *, profile_sha256, now=None, surface_revision=None):
    root = Path(root).resolve()
    document = load(inside(root, packet))
    cohort = (ObservableCohort if document.get("schema_version") == 2 else Cohort).model_validate(document)
    now = now or datetime.now(UTC)
    if timestamp(cohort.observed_at) > now or timestamp(cohort.expires_at) <= timestamp(cohort.observed_at):
        raise LabError("Invalid observation dates")
    for row in cohort.observations:
        artifact = inside(root, row.artifact)
        if file_digest(artifact) != row.artifact_sha256:
            raise LabError(f"Observation artifact mismatch for {row.scenario_id}/{row.trial_id}")
    stale = timestamp(cohort.expires_at) <= now or cohort.profile_sha256 != profile_sha256 \
        or surface_revision is not None and cohort.surface_revision != surface_revision
    if isinstance(cohort, ObservableCohort):
        if not surface_revision:
            raise LabError("Version 2 calibration requires the current --surface-revision")
        return evaluate_observations(root, cohort, stale)
    groups = {}
    for split in ("calibration", "held-out"):
        rows = [row for row in cohort.observations if row.split == split]
        n = len(rows)
        agree = sum(row.local_success == row.muse_success for row in rows)
        groups[split] = {"paired_trials": n, "agreement": agree / n if n else None,
                        "agreement_wilson_95": wilson(agree, n) if n else None,
                        "false_local_passes": sum(row.local_success and not row.muse_success for row in rows),
                        "false_local_failures": sum(not row.local_success and row.muse_success for row in rows)}
    return {"schema_version": 1, "comparison_scope": ["task_outcome"],
            "observation_gaps": [("Version 1 does not compare backend state, surfaces, arguments, "
                "request repetition, visible interventions or response consistency")],
            "label": "Muse observed; externally supplied evidence", "cohort": cohort.cohort,
            "source_sha256": digest(cohort.model_dump()), "stale": stale, "visibility": cohort.visibility,
            "observed_at": cohort.observed_at, "surface_revision": cohort.surface_revision,
            "groups": groups, "usable_for_comparison": not stale and bool(groups["held-out"]["paired_trials"]),
            "limits": "Artifact integrity and authorized-source declaration are checked; authenticity "
                      "is supplied by the operator. No hidden context or general Muse certification."}


def evaluate_observations(root, cohort, stale):
    pairs = [(row, read_pair(root, row, cohort)) for row in cohort.observations]
    comparisons = [{"scenario_id": row.scenario_id, "trial_id": row.trial_id, "split": row.split,
                    "artifact": row.artifact, "artifact_sha256": row.artifact_sha256,
                    "dimensions": compare_pair(pair)} for row, pair in pairs]
    groups = {}
    for split in ("calibration", "held-out"):
        rows = [row for row in comparisons if row["split"] == split]
        dimensions = {}
        for dimension in DIMENSIONS:
            states = [row["dimensions"][dimension]["status"] for row in rows]
            agreements, differences = states.count("agrees"), states.count("differs")
            observed = agreements + differences
            dimensions[dimension] = {
                "agreements": agreements, "differences": differences, "compared_pairs": observed,
                "unknown_pairs": states.count("unknown"),
                "agreement": agreements / observed if observed else None,
                "agreement_wilson_95": wilson(agreements, observed) if observed else None,
            }
            if cohort.analysis is not None:
                dimensions[dimension]["scenario_analysis"] = analyze(
                    [(row["scenario_id"], row["trial_id"], row["dimensions"][dimension]["status"])
                     for row in rows], cohort.analysis)
        for dimension, attribute in (("task_outcome", "task_success"),
                                     ("final_response_consistency", "final_consistent")):
            observed = [(getattr(pair.local, attribute), getattr(pair.muse, attribute))
                        for row, pair in pairs if row.split == split
                        and getattr(pair.local, attribute) is not None and getattr(pair.muse, attribute) is not None]
            dimensions[dimension].update(
                false_local_passes=sum(a and not b for a, b in observed),
                false_local_failures=sum(not a and b for a, b in observed))
        groups[split] = {"paired_trials": len(rows),
                        "independent_scenarios": len({row["scenario_id"] for row in rows}),
                        "dimensions": dimensions}
    covered = all(groups["held-out"]["dimensions"][name]["compared_pairs"] for name in cohort.primary_dimensions)
    return {"schema_version": 2,
        "label": "Synthetic calibration analysis" if cohort.evidence_kind == "synthetic"
                 else "Muse observed; operator-supplied structured evidence",
        "evidence_kind": cohort.evidence_kind, "cohort": cohort.cohort,
        "source_sha256": digest(cohort.model_dump()), "stale": bool(stale),
        "visibility": cohort.visibility, "observed_at": cohort.observed_at,
        "surface_revision": cohort.surface_revision, "profile_sha256": cohort.profile_sha256,
        "primary_dimensions": cohort.primary_dimensions, "groups": groups, "comparisons": comparisons,
        "primary_heldout_coverage": bool(covered),
        "usable_for_comparison": bool(covered) and not stale and cohort.evidence_kind == "muse-observed",
        "annotation_basis": "Operator-normalized observations and operator-applied consistency rubric",
        "consistency_rubric_sha256": digest(cohort.consistency_rubric),
        "analysis": provenance(cohort.analysis) if cohort.analysis is not None else None,
        "limits": "Integrity and declared source kinds are checked, not external authenticity or annotation "
            "correctness. Request traces show only requests reaching the target; visible approvals do not "
            "establish hidden policy. Repetition measures observed argument changes, not retry intent. "
            "Wilson intervals are descriptive within each dimension; repeated trials may be correlated. "
            "Optional scenario analysis relies on declared independent scenarios and the chosen weighting; "
            "unknown pairs remain excluded and degenerate variation cannot establish certainty. "
            "Coverage is not statistical sufficiency or a release qualification."}


def partner_packet(scenario, profile_sha256):
    document = load(Path(scenario))
    return {"schema_version": 1, "purpose": "Authorized Muse partner test; not an observed result",
            "scenario": document, "profile_sha256": profile_sha256,
            "setup": "Use an explicitly authorized staging target; do not create a tunnel automatically.",
            "capture": ["Visible transcript where permitted", "Target-side request/effect logs",
                       "Observation date, account/cohort and surface revision",
                       "Independent backend state for both sides", "Observed operations and arguments",
                       "Visible approvals and handoffs", "Final response and operator consistency rubric"],
            "observation_packet_version": 2, "comparison_dimensions": list(DIMENSIONS),
            "collection": "Use disjoint scenario splits, explicit trace completeness and hashed source artifacts. "
                "Unknown observations remain null. Synthetic packets must declare evidence_kind: synthetic.",
            "analysis": "Before collection, declare the independent scenario unit and paired-trial or "
                "equal-scenario weighting. Version 2 can request scenario-cluster-t-v1 analysis; "
                "identifiers and repetitions alone do not establish independence or sufficient coverage.",
            "unanswered": ["Discovery and tool projection", "Browser serialization",
                           "Visible approvals and handoffs", "Unobserved blocked actions"]}
