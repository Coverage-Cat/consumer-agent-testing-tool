"""Subject-neutral, artifact-bound observations and isolated fixture controls."""
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .calibration_observations import Artifact, Pair, Side
from .config import Strict
from .serialization import loads

Family = Literal[
    "authorization", "interruption", "state-continuity", "recovery", "outcome-reporting",
    "background-work", "content-trust", "credentials", "routes", "checkout", "presentation",
    "capability-boundaries", "harness-validity",
]


class Source(Artifact):
    timestamp: str | None = None
    precision: Literal["millisecond", "second", "minute", "unknown"] = "unknown"
    origin: Literal["service", "native-transcript", "room-fetch", "operator-copy", "fixture", "unknown"] = "unknown"

    @model_validator(mode="after")
    def instant(self):
        if self.timestamp is not None and datetime.fromisoformat(self.timestamp).tzinfo is None:
            raise ValueError("Source timestamps require an offset")
        if self.timestamp is None and self.precision != "unknown":
            raise ValueError("Timestamp precision requires a timestamp")
        return self


class Identity(Strict):
    subject: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    account_cohort: str = Field(min_length=1)
    session_cohort: str = Field(min_length=1)
    prior_context: str = Field(min_length=1)


class Assessment(Strict):
    completion: Literal["completed", "incomplete", "unknown"]
    disposition: Literal["observed", "blocked", "forgone"]
    exposure: Literal["reached", "not-reached", "unknown"]
    infrastructure: Literal["observed-healthy", "interrupted", "unknown"]
    verdict: Literal["supported", "contradicted", "inconclusive"]
    evidence: list[str] = Field(default_factory=list)
    # This treatment is distinct from whether an agent acknowledged an instruction.
    dispatch_guard: bool = False

    @property
    def measurable(self):
        return (self.disposition == "observed" and self.exposure == "reached"
                and self.infrastructure == "observed-healthy" and bool(self.evidence))

    @model_validator(mode="after")
    def qualified(self):
        if self.verdict != "inconclusive" and not self.measurable:
            raise ValueError("A decisive verdict requires exposure, healthy infrastructure and evidence")
        return self


class ObservedSide(Side):
    sources: list[Source] = Field(min_length=1)
    identity: Identity
    assessment: Assessment

    @model_validator(mode="after")
    def bound_assessment(self):
        if set(self.assessment.evidence) - {source.path for source in self.sources}:
            raise ValueError("Assessment evidence must name hashed sources on this side")
        return self


class MigrationSource(Strict):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NeutralPair(Strict):
    schema_version: Literal[3]
    evidence_kind: Literal["synthetic", "observed"]
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    surface_revision: str = Field(min_length=1)
    fixture_revision: str = Field(min_length=1)
    case_revision: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    comparison: Literal["matched", "control"]
    local: ObservedSide
    observed: ObservedSide
    migrated_from: MigrationSource | None = None

    @model_validator(mode="after")
    def matched_surface(self):
        if self.comparison == "matched" and (
                self.local.surface is None or self.local.surface != self.observed.surface):
            raise ValueError("Unknown or different surfaces require comparison: control")
        return self

    @property
    def comparable(self):
        return self.comparison == "matched" and all(
            side.assessment.measurable for side in (self.local, self.observed))


class BehaviorCase(Strict):
    id: str = Field(min_length=1)
    family: Family
    revision: str = Field(min_length=1)
    fixture_revision: str = Field(min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct(self):
        names = self.prerequisites + self.controls
        if len(names) != len(set(names)):
            raise ValueError("Prerequisite and control identifiers must be distinct")
        return self


def assess_case(case, trial, observations, *, infrastructure="unknown", supported=True):
    """Use collector-bound receipts; controls belong to a separate fixture trial.

    Artifact integrity is checked by the observation reader. This helper selects
    receipts, without performing or modifying a participant's fixture.
    """
    def valid(name, control=False):
        row = observations.get(name, {})
        return (row.get("observed") is True and bool(row.get("artifact"))
                and bool(row.get("trial")) and (row["trial"] != trial if control else row["trial"] == trial))

    exposed = bool(case.prerequisites) and all(valid(name) for name in case.prerequisites)
    controlled = all(valid(name, True) for name in case.controls)
    evidence = [row["artifact"] for name, row in observations.items()
                if name in case.prerequisites + case.controls and valid(name, name in case.controls)]
    measurable = exposed and controlled and infrastructure == "observed-healthy" and bool(evidence)
    return Assessment(completion="unknown", disposition="observed",
                      exposure="reached" if exposed else "unknown", infrastructure=infrastructure,
                      verdict=("supported" if supported else "contradicted") if measurable else "inconclusive",
                      evidence=evidence).model_dump()


def validate_fixture(*, markers, benign_text, quoted_terms, displayed_terms):
    """Reject ambiguous payload markers and contradictory fixture disclosures."""
    if len(set(markers)) != len(markers) or any(not marker or marker in benign_text for marker in markers):
        raise ValueError("Fixture markers must be unique and absent from benign content")
    for key in ("total", "currency", "cancellation", "payment"):
        if key not in quoted_terms or quoted_terms[key] != displayed_terms.get(key):
            raise ValueError(f"Fixture disclosure disagrees about {key}")


def migrate_pair(source, destination, *, metadata, sides):
    """Create a new v3 artifact. Caller supplies previously unrecorded provenance."""
    raw = Path(source).read_bytes()
    old = Pair.model_validate(loads(raw.decode("utf-8"), json_only=True))
    data = old.model_dump()
    data.update(metadata, schema_version=3,
                evidence_kind="synthetic" if old.evidence_kind == "synthetic" else "observed",
                migrated_from={"path": str(source), "sha256": hashlib.sha256(raw).hexdigest()})
    data["observed"] = data.pop("muse")
    for name in ("local", "observed"):
        data[name].update(sides[name])
    new = NeutralPair.model_validate(data)
    with Path(destination).open("x") as stream:
        stream.write(new.model_dump_json(indent=2) + "\n")
    return new
