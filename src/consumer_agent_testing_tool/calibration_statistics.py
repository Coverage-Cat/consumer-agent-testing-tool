"""Scenario-cluster uncertainty through the pinned Inspect scorer metrics."""
import math
from fractions import Fraction
from importlib.metadata import version
from numbers import Real
from typing import Literal

from .errors import LabError
from .surface import Strict


class ScenarioAnalysis(Strict):
    method: Literal["scenario-cluster-t-v1"]
    independent_unit: Literal["scenario_id"]
    weighting: Literal["paired-trials", "equal-scenarios"]
    confidence_level: Literal[0.95] = 0.95


def provenance(plan):
    return {
        **plan.model_dump(), "engine": "inspect_ai.scorer.ci",
        "engine_version": version("inspect-ai"),
        "standard_error": "inspect_ai.scorer.stderr(cluster='scenario_id')",
        "independence_basis": "Operator-declared scenario independence; not verified by identifiers",
        "scope": "Agreement among observed pairs; unknown annotations are not imputed",
        "release_qualification": False,
    }


def analyze(rows, plan):
    """Keep paired outcomes together and compute uncertainty across scenarios.

    Each row is (scenario ID, trial ID, agrees/differs/unknown). The upstream
    cluster metric retains all observed trials for paired-trial weighting.
    Equal-scenario weighting gives it one mean per observed scenario.
    """
    from inspect_ai.scorer import SampleScore, Score, ci, mean, stderr

    groups = {}
    for scenario, trial, status in sorted(rows):
        if not scenario or not trial or status not in ("agrees", "differs", "unknown"):
            raise LabError("Invalid paired observation for scenario analysis")
        group = groups.setdefault(scenario, {"observed": [], "unknown": 0})
        if status == "unknown":
            group["unknown"] += 1
        else:
            group["observed"].append((trial, int(status == "agrees")))
    observed = {name: group for name, group in groups.items() if group["observed"]}
    sizes = [{"scenario_id": name, "compared_pairs": len(group["observed"]),
              "unknown_pairs": group["unknown"]}
             for name, group in groups.items()]
    result = {
        "status": "insufficient-scenarios", "estimate": None,
        "standard_error": None, "interval_95": None, "unclipped_interval_95": None,
        "observed_scenarios": len(observed), "total_scenarios": len(groups),
        "compared_pairs": sum(len(group["observed"]) for group in groups.values()),
        "unknown_pairs": sum(group["unknown"] for group in groups.values()),
        "wholly_unobserved_scenarios": len(groups) - len(observed),
        "partially_observed_scenarios": sum(bool(group["unknown"]) for group in observed.values()),
        "degrees_of_freedom": len(observed) - 1 if len(observed) > 1 else None,
        "scenario_sizes": sizes, "weighting": plan.weighting,
    }
    if not observed:
        return result
    scores = []
    for scenario, group in observed.items():
        values = group["observed"]
        if plan.weighting == "equal-scenarios":
            values = [("scenario-mean", sum(value for _, value in values) / len(values))]
        scores.extend(SampleScore(score=Score(value=value), sample_id=trial,
                                  sample_metadata={"scenario_id": scenario})
                      for trial, value in values)
    result["estimate"] = float(mean()(scores))
    if len(observed) < 2:
        return result
    rates = {Fraction(sum(value for _, value in group["observed"]), len(group["observed"]))
             for group in observed.values()}
    if len(rates) == 1:
        result["status"] = "degenerate-variation"
        return result
    error = float(stderr(cluster="scenario_id")(scores))
    if not math.isfinite(error) or error < 0:
        raise LabError("Inspect returned an invalid clustered standard error")
    if error == 0:
        # Repeated identical outcomes do not demonstrate zero population
        # uncertainty. Do not publish the upstream collapsed interval as proof.
        result["status"] = "degenerate-variation"
        return result
    interval = ci(level=plan.confidence_level, method="t", cluster="scenario_id")(scores)
    if not isinstance(interval, dict) or set(interval) != {"lower", "upper"} \
            or any(not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(value)
                   for value in interval.values()) \
            or interval["lower"] > interval["upper"]:
        raise LabError("Inspect returned an invalid clustered confidence interval")
    interval = {key: float(value) for key, value in interval.items()}
    result.update(status="computed", standard_error=error, unclipped_interval_95=interval,
                  interval_95={"lower": max(0.0, interval["lower"]),
                               "upper": min(1.0, interval["upper"])})
    return result
