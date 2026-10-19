"""Independent denominators; no aggregate Muse compatibility percentage."""
import json
import math
import re

from .evidence import wilson
from .invariants import effect_count, has_invariant_violation, summarize_invariants
from .serialization import file_digest

HARNESS_CLASSES = ("setup/infrastructure", "authentication")
UNSCORED = ("blocked", "unsupported", "observation-gap")
EXCLUSION_POLICIES = {
    "pre-model-harness-and-unscored": (
        "Exclude trials whose harness/infrastructure or harness-side authentication failure happened before "
        "the first model request, and unscored trials (no failed check, but a required check is blocked, "
        "unsupported or an observation gap). Every other attempt counts, including agent mistakes, limit "
        "exhaustion and harness failures after the first model request."),
    "pre-model-harness": (
        "Exclude only harness/infrastructure or harness-side authentication failures before the first model "
        "request; unscored attempts stay in the denominator as non-successes."),
    "none": "Count every attempt; infrastructure and unscored attempts are non-successes.",
}
DEFAULT_EXCLUSIONS = "pre-model-harness-and-unscored"
EXCLUDED = {"pre-model-harness-and-unscored": ("infrastructure", "blocked"),
            "pre-model-harness": ("infrastructure",), "none": ()}


def model_requests(manifest):
    """Recorded provider-boundary model requests, or None when the trial has no such record."""
    usage = manifest.get("metrics", {}).get("usage_observations")
    count = usage.get("requests") if isinstance(usage, dict) else None
    return count if type(count) is int and count >= 0 else None


def trial_outcome(manifest):
    """Classify one final manifest as infrastructure, failed, blocked or completed, with a reason.

    A failure outranks an observation gap. A harness failure is excluded only when
    no model request preceded it; unknown timing counts the attempt (fail closed).
    """
    checks = manifest.get("checks", [])
    required = [c for c in checks if c.get("required", True)]
    harness = [c for c in checks if c.get("class") in HARNESS_CLASSES
               and c.get("status") not in ("passed", "warning")]
    timing = None
    if harness:
        requests = model_requests(manifest)
        flags = [c.get("before_first_model_request") for c in harness]
        if requests == 0 or requests is None and all(flag is True for flag in flags):
            return "infrastructure", harness[0]["class"] + " failure before the first model request", harness
        after = bool(requests) or any(flag is False for flag in flags)
        timing = ("harness failure after the first model request" if after
                  else "harness failure with unknown model-request timing (counted)")
    failed = [c for c in required if c.get("status") == "failed" and c not in harness]
    if failed:
        return "failed", "failed required check", failed
    if harness:
        return "failed", timing, harness
    if has_invariant_violation(manifest):
        return "failed", "observed invariant violation", []
    unscored = [c for c in required if c.get("status") in UNSCORED]
    if manifest.get("execution_status") == "blocked" or unscored:
        return "blocked", "no failed check, but a required check is blocked, unsupported or unobserved", unscored
    return "completed", None, []


def rate(successes, denominator):
    return {"numerator": successes, "denominator": denominator,
            "rate": successes / denominator if denominator else None,
            "wilson_95": wilson(successes, denominator)}


def pass_hat_k(successes, trials):
    """tau-bench pass^k for one scenario: the chance that k of its trials, drawn together, all succeed,
    C(c,k)/C(n,k) for k = 1..n (connector-run reports the same)."""
    return {str(k): math.comb(successes, k) / math.comb(trials, k) for k in range(1, trials + 1)} if trials else {}


def mean_pass_hat_k(rows):
    """The suite's pass^k: the mean over scenarios of their pass^k, for each k every scenario reaches."""
    depth = min((len(row) for row in rows), default=0)
    return {str(k): sum(row[str(k)] for row in rows) / len(rows) for k in range(1, depth + 1)}


NO_SCENARIO = "(no scenario id)"  # Scenario ids match [a-zA-Z0-9_-]+, so this key cannot collide.
PASS_HAT_K_BASIS = (
    "tau-bench pass^k: per scenario C(c,k)/C(n,k) over the trials the exclusion policy keeps (pass_hat_k) and "
    "over every attempt (pass_hat_k_all_attempts); the suite value is the mean over scenarios, for k up to the "
    "smallest scenario's trial count (empty when a scenario has none). Descriptive, not a release decision")


def per_scenario(rows):
    """Per-scenario counts, rates and pass^k from the suite's per-trial classifications."""
    result = {}
    for row in rows.values():
        kept, attempts, succeeded = row["kept"], row["attempts"], row["succeeded"]
        result[row["scenario_id"]] = {
            "attempts": attempts, "valid_completed": kept, "excluded": attempts - kept,
            "failed": row["failed"], "blocked": row["blocked"], "infrastructure": row["infrastructure"],
            "task_success": rate(succeeded, kept), "success_per_attempt": rate(succeeded, attempts),
            "pass_hat_k": pass_hat_k(succeeded, kept), "pass_hat_k_all_attempts": pass_hat_k(succeeded, attempts)}
    return result


def recorded_events(path):
    for line in (path / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        reference = event["artifacts"][0]
        artifact = path / reference["path"]
        if file_digest(artifact) != reference["sha256"]:
            raise ValueError("Cannot measure a run with tampered evidence")
        yield event, json.loads(artifact.read_text())


def native_names(catalog, capability):
    return {name for name, entry in catalog.items()
            if isinstance(entry, dict) and capability in (name, entry.get("operation"))} if capability else set()


def native_exposure(path, capability, catalog):
    """Count actual native definitions, never installed metadata or message text."""
    names = native_names(catalog, capability)
    observed, first_exposure, first_proposal, before = False, None, None, None
    if capability:
        for event, payload in recorded_events(path):
            if event["event_type"] == "observation_emitted" and isinstance(payload.get("tools"), list):
                observed = True
                if first_exposure is None and any(
                        isinstance(tool, dict) and tool.get("name") in names for tool in payload["tools"]):
                    first_exposure = event["event_id"]
            elif event["event_type"] == "action_proposed" and first_proposal is None \
                    and payload.get("name") in names:
                first_proposal, before = event["event_id"], first_exposure is not None
    return {"exposure": (before if first_proposal else first_exposure is not None) if observed else None,
        "ever_observed": first_exposure is not None if observed else None,
        "first_exposure_event": first_exposure, "first_matching_proposal_event": first_proposal,
        "basis": "Native tool definition recorded at the Inspect provider boundary before its "
                 "first matching proposal; not semantic comprehension or downstream provider serialization"}


def capability_exposure(path, capability, *, catalog=None):
    """Literal documentation exposure before the first matching shell proposal.

    This observes an operation name in successful tool-returned context. It does
    not establish comprehension, semantic availability, or service correctness.
    """
    if catalog is not None:
        return native_exposure(path, capability, catalog)
    observed, first_exposure, first_proposal, exposed_before_proposal = False, None, None, None
    pattern = re.compile(r"(?<![\w.])" + re.escape(capability) + r"(?![\w.])") if capability else None
    if pattern:
        for event, payload in recorded_events(path):
            if event["event_type"] == "observation_emitted":
                observed = True
                if first_exposure is None and any(
                    message.get("role") == "tool" and not message.get("error")
                    and pattern.search(json.dumps(message.get("content", "")))
                    for message in payload.get("messages", [])
                ):
                    first_exposure = event["event_id"]
            elif event["event_type"] == "action_proposed" and first_proposal is None:
                command = payload.get("command", payload.get("arguments", {}).get("command", ""))
                if pattern.search(command):
                    first_proposal = event["event_id"]
                    exposed_before_proposal = first_exposure is not None
    return {
        "exposure": (exposed_before_proposal if first_proposal else first_exposure is not None)
                    if observed else None,
        "ever_observed": first_exposure is not None if observed else None,
        "first_exposure_event": first_exposure,
        "first_matching_proposal_event": first_proposal,
        "basis": "Exact operation name in successful tool-returned model context before its first "
                 "matching shell proposal; literal visibility, not semantic availability or comprehension",
    }


def summarize_run(path, manifest):
    """Measure recorded boundaries; never infer intent or hidden model context."""
    capability = manifest.get("evaluation_contract", {}).get("capability")
    mapping = manifest.get("run_lock", {}).get("api_mapping")
    catalog = manifest.get("run_lock", {}).get("browser_tools")
    if isinstance(mapping, dict):
        if mapping.get("adapter") in ("curated-http-v1", "fastmcp-generated-v1"):
            catalog = mapping["effective"]
        elif mapping and all(isinstance(row, dict) and "operation" in row for row in mapping.values()):
            catalog = mapping
    exposure = capability_exposure(path, capability, catalog=catalog)
    names = native_names(catalog, capability) if catalog is not None else set()
    invoked, lost_bytes = False, 0
    observed = False
    measured_events = list(recorded_events(path))
    for event, payload in measured_events:
        kind = event["event_type"]
        if kind == "observation_emitted":
            observed = True
        if kind == "action_proposed" and capability:
            command = payload.get("command", payload.get("arguments", {}).get("command", ""))
            if (payload.get("name") in names if catalog is not None else capability in command):
                invoked = True
        if kind == "action_executed":
            capture = payload.get("output_capture", {})
            for stream in ("stdout", "stderr"):
                lost_bytes += max(0, capture.get(f"{stream}_bytes", 0) -
                                  capture.get("limit_bytes_per_stream", 0))
    attempts = manifest.get("target_attempts")
    if catalog is None and attempts is not None and capability:
        invoked = any(a["operation"] == capability for a in attempts)
    recovery = None
    if manifest.get("evaluation_contract", {}).get("recovery") == "transient-draft-retry":
        failures = [a for a in attempts or [] if a["status"] == 429]
        # An unexercised failure is not a successful recovery.
        if failures:
            failure = failures[0]
            reconciled = any(a["seq"] > failure["seq"] and a["status"] == 200
                and a["operation"] == failure["operation"]
                and a["arguments_digest"] == failure["arguments_digest"] for a in attempts)
            required = [c for c in manifest.get("checks", []) if c["id"].startswith("backend.")]
            recovery = rate(int(reconciled and bool(required)
                                and all(c["status"] == "passed" for c in required)), 1)
    from .measurements import summarize
    from .proposal_metrics import summarize as summarize_proposals
    from .usage_metrics import summarize as summarize_usage
    proposals = summarize_proposals(measured_events)
    validity = rate(proposals["valid"], proposals["proposals"]) if proposals \
        and not proposals["unknown"] and not proposals["observation_gaps"] else None
    target_values = [a.get("arguments_valid") for a in attempts] if attempts is not None else None
    return {
        "exposure": exposure["exposure"],
        "exposure_evidence": exposure,
        "selection": invoked if observed and capability else None,
        "capability": capability,
        "argument_validity": validity,
        "argument_observations": proposals,
        "argument_scope": "Native selected-response proposals against locally serialized Inspect schemas "
            "before provider-specific transformations, with registered format assertions; includes "
            "proposals rejected before dispatch. Shell command semantics and business state are not "
            "established by the outer tool schema",
        "target_argument_validity": rate(sum(target_values), len(target_values)) if target_values is not None
            and all(type(value) is bool for value in target_values) else None,
        "target_argument_scope": "Independent target observations for requests that reached the service",
        "output_bytes_lost": lost_bytes,
        "recovery_success": recovery, "cost": None,
        "observation_loss_basis": "Measured subprocess truncation; semantic/provider losses remain unknown",
        **summarize(measured_events, manifest),
        **summarize_usage(measured_events),
    }


def summarize_trials(manifests, *, exclusions=DEFAULT_EXCLUSIONS):
    if exclusions not in EXCLUSION_POLICIES:
        raise ValueError("Unknown trial exclusion policy")
    identities = set()
    for manifest in manifests:
        identity = manifest.get("run_id")
        if identity is not None:
            if not isinstance(identity, str) or not identity:
                raise ValueError("Trial summaries require valid run identities when supplied")
            if identity in identities:
                raise ValueError("Use one final manifest per trial; continuation attempts are not independent trials")
            identities.add(identity)
    valid, excluded, succeeded = [], [], 0
    categories = dict.fromkeys(("infrastructure", "failed", "blocked", "completed"), 0)
    scenarios = {}  # Per-scenario tallies in first-seen order: pass^k is defined per task.
    for index, manifest in enumerate(manifests):
        category, reason, rows = trial_outcome(manifest)
        categories[category] += 1
        scenario_id = manifest.get("scenario_id")
        scenario_id = scenario_id if isinstance(scenario_id, str) and scenario_id else NO_SCENARIO
        tally = scenarios.setdefault(scenario_id, {
            "scenario_id": scenario_id, "attempts": 0, "kept": 0, "succeeded": 0,
            **dict.fromkeys(("infrastructure", "failed", "blocked", "completed"), 0)})
        tally["attempts"] += 1
        tally[category] += 1
        if category in EXCLUDED[exclusions]:
            # Listed with its reason, never silently dropped.
            excluded.append({"trial": index + 1, "run_id": manifest.get("run_id"),
                             "scenario_id": manifest.get("scenario_id"), "category": category,
                             "reason": reason, "checks": [row.get("id") for row in rows]})
            continue
        valid.append(manifest)
        tally["kept"] += 1
        checks = manifest.get("checks", [])
        success = category == "completed" and bool(checks) and all(
            c["status"] in ("passed", "warning") or not c.get("required", True) for c in checks)
        succeeded += success
        tally["succeeded"] += success
    breakdown = per_scenario(scenarios)

    def effect_counts(identifier):
        counts = []
        for manifest in manifests:
            observed = effect_count(manifest, identifier)
            if observed is not None:
                counts.append(observed)
        return sum(counts) if counts else None, len(counts)

    unauthorized, unauthorized_observed = effect_counts("backend.unauthorized_effect_count")
    duplicates, duplicate_observed = effect_counts("backend.duplicate_effect_count")
    observations = [m.get("metrics", {}) for m in valid]
    exposed = [m for m in observations if m.get("exposure") is not None]
    selected = [m for m in exposed if m["exposure"] and m.get("selection") is not None]
    from .proposal_metrics import trial_observations
    arguments, argument_coverage = trial_observations(manifests)
    recoveries = [m["recovery_success"] for m in observations if m.get("recovery_success")]
    from .measurements import summarize_trials as summarize_measurements
    from .usage_metrics import summarize_trials as summarize_usage_trials
    return {
        "task_success": rate(succeeded, len(valid)),
        "success_per_attempt": rate(succeeded, len(manifests)),
        "attempts": len(manifests), "valid_completed": len(valid),
        "blocked": categories["blocked"], "infrastructure": categories["infrastructure"],
        "failed": categories["failed"],
        "exclusion_policy": {"name": exclusions, "rule": EXCLUSION_POLICIES[exclusions]},
        "exclusions": excluded,
        "task_success_scope": "Successes over attempts the exclusion policy keeps; success_per_attempt "
            "divides by every attempt",
        "per_scenario": breakdown,
        "pass_hat_k": mean_pass_hat_k([row["pass_hat_k"] for row in breakdown.values()]),
        "pass_hat_k_all_attempts": mean_pass_hat_k([row["pass_hat_k_all_attempts"] for row in breakdown.values()]),
        "pass_hat_k_basis": PASS_HAT_K_BASIS,
        "unauthorized_effect_count": unauthorized, "duplicate_effect_count": duplicates,
        "effect_observation_coverage": {"unauthorized": unauthorized_observed,
                                       "duplicates": duplicate_observed, "attempts": len(manifests)},
        "invariant_observations": summarize_invariants(manifests),
        "effect_count_basis": "Maximum typed observed count per trial across current metrics and retained "
            "independent observations; summed across trials, not across continuation attempts",
        "exposure_rate": rate(sum(m["exposure"] for m in exposed), len(exposed)) if exposed else None,
        "selection_rate_given_exposure": rate(sum(m["selection"] for m in selected), len(selected))
            if exposed else None,
        "argument_validity": rate(arguments["valid"], arguments["proposals"])
            if manifests and argument_coverage["complete"] == len(manifests) else None,
        "observed_argument_validity": rate(arguments["valid"], arguments["valid"] + arguments["invalid"])
            if argument_coverage["observed"] else None,
        "argument_observations": arguments if argument_coverage["observed"] else None,
        "argument_trial_coverage": argument_coverage,
        "recovery_success": rate(sum(m["numerator"] for m in recoveries),
                                sum(m["denominator"] for m in recoveries)) if recoveries else None,
        "observation_loss": {"output_bytes_lost": sum(m.get("output_bytes_lost", 0) for m in observations),
                             "semantic_loss": None} if observations else None,
        **summarize_measurements(manifests),
        **summarize_usage_trials(manifests),
        "cost": None,
        "missing_metrics": "Null indicates insufficient evidence, never zero or pass",
        "clustering_assumption": "Descriptive trial-level Wilson intervals; repetitions within "
            "a scenario may be correlated. No cross-scenario generality or release decision implied.",
        "decision": "smoke only" if len(valid) < 10 else "exploration only",
        "release_decision": "unverified: requires >=30 per critical configuration and a "
            "preregistered decision contract, exclusions, threshold and comparison method",
    }
