"""Aggregate reviewed discovery observations without invoking an assistant or service."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import LabError

STAGES = ("discovered", "recommended", "invoked", "started", "completed")
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observation(Contract):
    status: Literal["observed", "absent", "unobserved"]
    source: Literal["transcript", "tool", "service"] | None = None
    evidence_ref: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def evidence_required(self):
        if self.status != "unobserved" and (not self.source or not self.evidence_ref or not self.evidence_ref.strip()):
            raise ValueError("observed and absent stages require an evidence source and reference")
        if self.status == "unobserved" and (self.source is not None or self.evidence_ref is not None):
            raise ValueError("unobserved stages cannot claim evidence")
        return self


class Stages(Contract):
    discovered: Observation
    recommended: Observation
    invoked: Observation
    started: Observation
    completed: Observation

    @model_validator(mode="after")
    def service_outcomes(self):
        for name in ("started", "completed"):
            observation = getattr(self, name)
            if observation.status != "unobserved" and observation.source != "service":
                raise ValueError("started and completed require service evidence, not assistant claims")
        return self


class Case(Contract):
    id: Identifier
    exposure: Literal["cold", "connected"]
    intent: Literal["purchase", "research", "negative"]
    host: Literal["hosted", "surrogate"]
    trials: Annotated[int, Field(ge=1, le=1000)]


class Trial(Contract):
    case_id: Identifier
    trial: Annotated[int, Field(ge=1)]
    target_seeded: bool
    stages: Stages


class DiscoveryStudy(Contract):
    schema_version: Literal[1]
    study_id: Identifier
    cases: Annotated[list[Case], Field(min_length=1, max_length=1000)]
    observations: Annotated[list[Trial], Field(max_length=100000)]

    @model_validator(mode="after")
    def unique_coverage(self):
        cases = {case.id: case for case in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("case ids must be unique")
        seen = set()
        for trial in self.observations:
            case = cases.get(trial.case_id)
            if case is None or trial.trial > case.trials:
                raise ValueError("observation must identify a declared case and planned trial")
            key = (trial.case_id, trial.trial)
            if key in seen:
                raise ValueError("duplicate trial observation")
            seen.add(key)
            if case.exposure == "cold" and trial.target_seeded:
                raise ValueError("cold discovery cannot seed the target brand, URL, connector or skill")
        return self


def summarize(study: DiscoveryStudy) -> dict:
    """Keep unrun trials and unobserved stages visible; never infer a prior stage."""
    observations = {(row.case_id, row.trial): row for row in study.observations}
    groups = defaultdict(list)
    for case in study.cases:
        groups[(case.exposure, case.intent, case.host)].append(case)
    summaries = []
    for (exposure, intent, host), cases in sorted(groups.items()):
        planned = sum(case.trials for case in cases)
        rows = [observations[(case.id, trial)] for case in cases for trial in range(1, case.trials + 1)
                if (case.id, trial) in observations]
        stages = {}
        for name in STAGES:
            observed = sum(getattr(row.stages, name).status == "observed" for row in rows)
            absent = sum(getattr(row.stages, name).status == "absent" for row in rows)
            measured = observed + absent
            stages[name] = {"observed": observed, "absent": absent,
                            "unobserved": len(rows) - measured,
                            "observed_rate": observed / measured if measured else None,
                            "measurement_coverage": measured / planned}
        summaries.append({"exposure": exposure, "intent": intent, "host": host,
                          "planned": planned, "recorded": len(rows), "unrun": planned - len(rows),
                          "stages": stages,
                          "unexpected_invocations": stages["invoked"]["observed"] if intent == "negative" else None})
    complete = len(observations) == sum(case.trials for case in study.cases) and all(
        getattr(row.stages, name).status != "unobserved" for row in study.observations for name in STAGES)
    return {"schema_version": 1, "evidence_kind": "reviewed_observations",
            "evidence_verified_by_tool": False, "status": "complete" if complete else "incomplete",
            "groups": summaries,
            "limitations": ["No assistant or service was run by this report command.",
                            "Evidence references are supplied by the reviewer and are not fetched or verified.",
                            "Rates describe this test set, not market share or causal lift.",
                            "Surrogate results do not establish hosted assistant behavior."]}


def run_from_cli(args):
    try:
        study = DiscoveryStudy.model_validate_json(Path(args.study).read_text())
    except (OSError, ValidationError) as error:
        # Validation errors can contain submitted text or credentials; keep them private.
        raise LabError("Invalid discovery study; check discovery-study-v1.schema.json. "
                       "No input values have been printed.") from error
    result = summarize(study)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "complete" else 3
