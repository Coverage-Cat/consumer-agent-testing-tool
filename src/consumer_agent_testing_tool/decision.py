"""Fixed-sample, scenario-specific decision contracts. No optional stopping."""
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .config import Strict
from .errors import LabError, Unsupported
from .metrics import DEFAULT_EXCLUSIONS, EXCLUSION_POLICIES, summarize_trials
from .replay import differences
from .serialization import digest, load

# The original name listed the same categories; it now means the corrected rule
# (a failure outranks a gap; harness exclusions only before the first model request).
LEGACY_EXCLUSIONS = "infrastructure-authentication-blocked-missing-evidence"


def aggregate(decisions):
    return ("failed" if any(d.startswith("failed") for d in decisions) else
            "passed" if decisions and all(d.startswith("passed") for d in decisions) else "inconclusive")


def threshold_decision(contract, metrics):
    invariants = metrics["invariant_observations"]
    interval = metrics["task_success"]["wilson_95"]
    if any(row["violations"] for row in invariants.values()):
        return "failed invariant"
    if (any(row["observed"] < row["required"] for row in invariants.values())
            or metrics["attempts"] != contract["trials_per_scenario"] or metrics["valid_completed"] < 30):
        return "inconclusive"
    if interval[0] >= contract["success_threshold"]:
        return "passed threshold"
    if interval[1] < contract["success_threshold"]:
        return "failed threshold"
    return "inconclusive"


def configuration_cohorts(trials, treatments):
    """Compare complete host locks, allowing variation only between declared arms.

    The scenario digest binds initial state as well as the task. Runtime additions
    remain locked: this function never drops timestamps, paths or other fields
    on the assumption that they are harmless. Only hashes and changed field paths
    are reported, not provider/target configuration values.
    """
    required = {
        "harness": str, "platform": str, "python": str, "dependencies": list,
        "dependency_lock_sha256": str, "source_hashes": dict, "interface_profile": dict,
        "model": dict, "target": dict, "scenario_sha256": str, "effective_policy": dict,
    }
    cohorts, gaps, baseline, changed = {}, [], None, set()
    allowed = ["/" + field.replace(".", "/") for field in treatments]
    for index, trial in enumerate(trials):
        lock = trial.get("run_lock")
        missing = ["run_lock"] if not isinstance(lock, dict) else [
            field for field, kind in required.items()
            if not isinstance(lock.get(field), kind) or not lock[field]]
        if isinstance(lock, dict) and (type(lock.get("schema_version")) is not int
                                      or lock["schema_version"] != 1):
            missing.append("schema_version")
        if not isinstance(trial.get("run_id"), str) or not trial["run_id"]:
            missing.append("run_id")
        if missing:
            gaps.append({"trial_index": index + 1, "missing_or_invalid": missing})
            continue
        if baseline is None:
            baseline = lock
        for row in differences(baseline, lock):
            if not any(row["path"] == field or row["path"].startswith(field + "/") for field in allowed):
                changed.add(row["path"])
        key = digest(lock)
        cohorts.setdefault(key, []).append(trial)
    return cohorts, {
        "status": "mismatch" if changed else "incomplete" if gaps or not trials else "matched",
        "observed": sum(len(items) for items in cohorts.values()), "required": len(trials),
        "gaps": gaps, "unexplained_lock_changes": sorted(changed),
        "declared_treatments": list(treatments),
    }


class DecisionContract(Strict):
    schema_version: Literal[1] = 1
    primary_scenarios: list[str] = Field(min_length=1)
    trials_per_scenario: int = Field(ge=30)
    success_threshold: float = Field(gt=0, lt=1)
    comparison_method: Literal["per-scenario-wilson-95"] = "per-scenario-wilson-95"
    treatment_fields: list[Literal["model", "interface_profile.exposure_strategy"]] = Field(
        default_factory=list)
    # A closed, preregistered set; see metrics.EXCLUSION_POLICIES for each rule.
    exclusions: Literal["pre-model-harness-and-unscored", "pre-model-harness", "none"] = DEFAULT_EXCLUSIONS
    stopping_rule: Literal["fixed-sample-no-optional-stopping"] = "fixed-sample-no-optional-stopping"

    @field_validator("exclusions", mode="before")
    @classmethod
    def legacy_name(cls, value):
        return DEFAULT_EXCLUSIONS if value == LEGACY_EXCLUSIONS else value

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.primary_scenarios)) != len(self.primary_scenarios):
            raise ValueError("Primary scenarios must be distinct")
        if len(set(self.treatment_fields)) != len(self.treatment_fields):
            raise ValueError("Treatment fields must be distinct")
        return self


def read_contract(path, scenarios, trials):
    contract = DecisionContract.model_validate(load(path))
    if trials != contract.trials_per_scenario:
        raise LabError("--trials must match the preregistered fixed sample budget")
    available = {scenario["id"] for scenario in scenarios}
    if set(contract.primary_scenarios) != available:
        raise LabError("Scenario selection must exactly match the declared primary scenarios")
    document = contract.model_dump(mode="json")
    return document | {"sha256": digest(document)}


def refuse_simulated(manifests):
    """Decision contracts count deterministic trials only (docs/spec-v0.3.md section 14): a trial whose run
    lock records a simulated user (an object under user_simulator, ext_user_simulator's entry) is refused."""
    found = [str(manifest.get("run_id") or "a trial") for manifest in manifests if isinstance(manifest, dict)
             and isinstance(lock := manifest.get("run_lock"), dict) and isinstance(lock.get("user_simulator"), dict)]
    if found:
        raise Unsupported(f"A decision contract cannot count trials with a simulated user ({', '.join(found[:5])}"
                          f"{', ...' if len(found) > 5 else ''}): its replies are exploratory evidence")


def evaluate_contract(contract, manifests):
    # Programmatic callers use the same declared-method/treatment allowlist as
    # the CLI; an arbitrary parent path must not waive every lock difference.
    refuse_simulated(manifests)
    declared = DecisionContract.model_validate(
        {key: value for key, value in contract.items() if key != "sha256"}).model_dump()
    contract = declared | {"sha256": contract["sha256"]}
    if any(manifest.get("scenario_id") not in contract["primary_scenarios"] for manifest in manifests):
        raise ValueError("Decision evidence contains an undeclared scenario")
    policy = contract["exclusions"]
    # Validate trial identity across scenario groups as well as within each group.
    summarize_trials(manifests, exclusions=policy)
    groups = {}
    for scenario in contract["primary_scenarios"]:
        trials = [m for m in manifests if m.get("scenario_id") == scenario]
        metrics = summarize_trials(trials, exclusions=policy)
        coverage = metrics["effect_observation_coverage"]
        invariants = metrics["invariant_observations"]
        cohorts, compatibility = configuration_cohorts(trials, contract.get("treatment_fields", []))
        configurations = {}
        for key, samples in cohorts.items():
            measured = summarize_trials(samples, exclusions=policy)
            configurations[key] = {
                "decision": threshold_decision(contract, measured),
                "task_success": measured["task_success"], "success_per_attempt": measured["success_per_attempt"],
                "attempts": measured["attempts"], "valid_completed": measured["valid_completed"],
                "failed": measured["failed"], "blocked": measured["blocked"],
                "infrastructure": measured["infrastructure"], "exclusions": measured["exclusions"],
                "invariant_observations": measured["invariant_observations"],
            }
        # A validated invariant blocks the lane even when locks/coverage are
        # incomplete. Otherwise incompatible evidence cannot qualify a threshold.
        if any(row["violations"] for row in invariants.values()):
            decision = "failed invariant"
        elif compatibility["status"] != "matched" or any(
                row["observed"] < row["required"] for row in invariants.values()):
            decision = "inconclusive"
        else:
            combined = aggregate([row["decision"] for row in configurations.values()])
            decision = combined + " threshold" if combined != "inconclusive" else combined
        groups[scenario] = {"decision": decision, "task_success": metrics["task_success"],
            "success_per_attempt": metrics["success_per_attempt"], "attempts": metrics["attempts"],
            "valid_completed": metrics["valid_completed"], "failed": metrics["failed"],
            "blocked": metrics["blocked"], "infrastructure": metrics["infrastructure"],
            "exclusions": metrics["exclusions"], "effect_observation_coverage": coverage,
            "invariant_observations": invariants, "lock_compatibility": compatibility,
            "configurations": configurations,
            "task_success_scope": "Descriptive across attempts; thresholds evaluated per locked configuration"}
    return {"behavioral_decision": aggregate([g["decision"] for g in groups.values()]), "per_scenario": groups,
        "exclusion_policy": {"name": policy, "rule": EXCLUSION_POLICIES[policy]},
        "contract_sha256": contract["sha256"],
        "scope": "These scenarios and locked configurations only; no generality across workflows",
        "comparison_scope": "Independent per-configuration thresholds; no superiority or noninferiority claim",
        "release_qualification": "Separate live-adapter and deterministic gates still apply"}
