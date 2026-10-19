"""Measured host intervals and scenario-labeled approval burden."""
import math
import time
import uuid
from contextlib import contextmanager

CLOCK_ID = uuid.uuid4().hex
CATEGORIES = ("tool", "browser", "approval")


def clock_reading():
    return {"clock_id": CLOCK_ID, "tick_ns": time.monotonic_ns()}


def run_timing(manifest):
    """Wall timestamps are diagnostics; they do not establish an elapsed duration."""
    from datetime import datetime
    start, end = manifest.get("started_clock"), manifest.get("finished_clock")
    valid = (isinstance(start, dict) and isinstance(end, dict)
             and isinstance(start.get("clock_id"), str) and bool(start["clock_id"])
             and start["clock_id"] == end.get("clock_id")
             and type(start.get("tick_ns")) is int and type(end.get("tick_ns")) is int
             and 0 <= start["tick_ns"] <= end["tick_ns"])
    seconds = None
    if valid:
        try:
            seconds = (end["tick_ns"] - start["tick_ns"]) / 1e9
        except OverflowError:
            valid = False
    wall = None
    try:
        first = datetime.fromisoformat(manifest["started_at"])
        last = datetime.fromisoformat(manifest["finished_at"])
        if first.tzinfo is not None and last.tzinfo is not None:
            wall = (last - first).total_seconds()
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return {
        "seconds": seconds if valid else None,
        "observed_seconds": seconds if valid else None,
        "observation_gaps": int(not valid),
        "wall_timestamp_difference_seconds": wall,
        "wall_timestamp_order": "unknown" if wall is None else "backwards" if wall < 0 else "forward",
        "basis": "Host monotonic elapsed time from evidence creation to finalization in one clock epoch; "
                 "a restart with another epoch leaves the full duration unknown. Wall timestamp "
                 "differences are separate diagnostics, not duration measurements.",
    }


def enable(evidence, *categories):
    current = evidence.manifest.setdefault("measurement_contract", {
        "version": 1, "categories": [],
        "basis": "Host monotonic intervals; browser intervals include nested approval time",
    })
    for category in categories:
        if category not in CATEGORIES:
            raise ValueError("Unknown measurement category")
        if category not in current["categories"]:
            current["categories"].append(category)
            evidence.event("measurement_scope", {"category": category, "version": 1},
                           provenance="instrumented host boundary; absent calls can establish zero")


@contextmanager
def measure(evidence, category, *, identity=None, classification="unknown"):
    enable(evidence, category)
    start = {"category": category, "clock_id": CLOCK_ID, "tick_ns": time.monotonic_ns(),
             "identity": identity, "classification": classification}
    event = evidence.event("measurement_started", start, provenance="host boundary monotonic clock")
    result, status = {}, "interrupted"
    try:
        yield result
        status = "completed"
    finally:
        evidence.event("measurement_finished", {
            "category": category, "clock_id": CLOCK_ID, "tick_ns": time.monotonic_ns(),
            "status": status, **result}, parent_id=event, provenance="host boundary monotonic clock")


def model_timing(events):
    """Only pair unique request/terminal events from the same recorded clock."""
    starts, finishes = {}, {}
    gaps, started = 0, 0
    for position, (event, _) in enumerate(events):
        kind = event["event_type"]
        if kind == "model_request":
            started += 1
            identifier = event.get("event_id")
            collection = starts
        elif kind in ("model_response", "provider_error"):
            identifier = event.get("parent_id")
            collection = finishes
        else:
            continue
        if not isinstance(identifier, str) or not identifier:
            gaps += 1
            continue
        collection.setdefault(identifier, []).append((position, event))
    total, completed, failed = 0, 0, 0
    for identifier, requests in starts.items():
        ends = finishes.get(identifier, [])
        if len(requests) != 1 or len(ends) != 1:
            gaps += 1
            continue
        start_position, start = requests[0]
        end_position, end = ends[0]
        valid = (end_position > start_position
                 and isinstance(start.get("clock_id"), str) and bool(start["clock_id"])
                 and start["clock_id"] == end.get("clock_id")
                 and type(start.get("monotonic_ns")) is int
                 and type(end.get("monotonic_ns")) is int
                 and 0 <= start["monotonic_ns"] <= end["monotonic_ns"])
        if not valid:
            gaps += 1
            continue
        total += end["monotonic_ns"] - start["monotonic_ns"]
        if end["event_type"] == "model_response":
            completed += 1
        else:
            failed += 1
    gaps += sum(len(rows) for identifier, rows in finishes.items() if identifier not in starts)
    measured = completed + failed
    return {
        "seconds": total / 1e9 if started and not gaps else None,
        "observed_seconds": total / 1e9 if measured else None,
        "started": started, "completed": completed, "failed": failed,
        "observation_gaps": gaps,
        "basis": "Unique host model request/terminal pairs within a recorded process clock epoch; "
                 "provider errors have measured duration but do not establish model success. "
                 "Missing or ambiguous pairs leave the full total unknown.",
    }


def summarize(events, manifest):
    """Missing/interrupted intervals are gaps, and restarts never join clock epochs."""
    enabled, starts, finishes = set(), {}, {}
    prior_activity = dict.fromkeys(CATEGORIES, 0)
    for event, payload in events:
        legacy_category = {"action_executed": "tool", "scenario_approval": "approval"}.get(event["event_type"])
        if event["event_type"] == "action_proposed" and payload.get("name") == "browser_action":
            legacy_category = "browser"
        if legacy_category and legacy_category not in enabled:
            prior_activity[legacy_category] += 1
        if event["event_type"] == "measurement_scope" and payload.get("version") == 1:
            if payload.get("category") in CATEGORIES:
                enabled.add(payload["category"])
        elif event["event_type"] == "measurement_started":
            starts[event["event_id"]] = payload
        elif event["event_type"] == "measurement_finished":
            finishes.setdefault(event["parent_id"], []).append(payload)
    details = {}
    for category in enabled:
        rows = [(key, row) for key, row in starts.items() if row.get("category") == category]
        total, completed, interrupted, gaps = 0, 0, 0, prior_activity[category]
        for key, start in rows:
            ends = finishes.get(key, [])
            end = ends[0] if len(ends) == 1 else {}
            valid = (end.get("category") == category and isinstance(start.get("clock_id"), str)
                     and bool(start["clock_id"]) and start["clock_id"] == end.get("clock_id")
                     and type(start.get("tick_ns")) is int and type(end.get("tick_ns")) is int
                     and 0 <= start["tick_ns"] <= end["tick_ns"]
                     and end.get("status") in ("completed", "interrupted"))
            if not valid:
                gaps += 1
                continue
            total += end["tick_ns"] - start["tick_ns"]
            if end["status"] == "completed":
                completed += 1
            else:
                interrupted += 1
        gaps += sum(1 for key, ends in finishes.items() if key not in starts
                    and any(end.get("category") == category for end in ends))
        details[category] = {"seconds": total / 1e9 if not gaps and not interrupted else None,
            "observed_seconds": total / 1e9, "started": len(rows), "completed": completed,
            "interrupted": interrupted, "observation_gaps": gaps}
    burden = None
    if "approval" in enabled:
        requests, request_gaps = {}, prior_activity["approval"]
        for start in starts.values():
            if start.get("category") != "approval":
                continue
            identity = start.get("identity")
            if not isinstance(identity, str) or not identity:
                request_gaps += 1
                continue
            classification = start.get("classification", "unknown")
            if classification not in ("necessary", "redundant", "unknown"):
                classification = "unknown"
            if identity in requests and requests[identity] != classification:
                classification = "unknown"
            requests[identity] = classification
        completed_task = manifest.get("execution_status") == "completed"
        burden = {"requests": len(requests) if not request_gaps else None,
            "observation_gaps": request_gaps, "completed_tasks": int(completed_task),
            "requests_per_completed_task": len(requests) if completed_task and not request_gaps else None,
            **{label: sum(value == label for value in requests.values())
               for label in ("necessary", "redundant", "unknown")},
            "basis": "Unique domain approval proposals, including denials; classification declared by scenario"}
    model = model_timing(events)
    if model["started"] or model["observation_gaps"]:
        details["model"] = model
    elapsed = run_timing(manifest)
    if "started_at" in manifest or "started_clock" in manifest:
        details["run"] = elapsed
    return {"model_seconds": model["seconds"],
            "total_seconds": elapsed["seconds"],
            "tool_seconds": details.get("tool", {}).get("seconds"),
            "browser_seconds": details.get("browser", {}).get("seconds"),
            "approval_wait_seconds": details.get("approval", {}).get("seconds"),
            "approval_burden": burden, "timing_coverage": details,
            "timing_basis": "Each host interval stays within one recorded process clock epoch. "
                "Model time sums request durations, including failures, and can exceed wall time "
                "when requests overlap. Browser time includes "
                "nested approval processing; categories are not additive. Interrupted or missing intervals "
                "leave totals unknown; observed_seconds excludes suspended time between invocations."}


def summarize_trials(manifests):
    keys = ("model_seconds", "tool_seconds", "browser_seconds", "approval_wait_seconds", "total_seconds")
    totals, observed_totals, coverage = {}, {}, {}
    for key in keys:
        values = [row.get("metrics", {}).get(key) for row in manifests]
        observed = [value for value in values
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0]
        category = {"model_seconds": "model", "tool_seconds": "tool", "browser_seconds": "browser",
                    "approval_wait_seconds": "approval", "total_seconds": "run"}[key]
        partial = []
        for manifest, value in zip(manifests, values, strict=True):
            detail = manifest.get("metrics", {}).get("timing_coverage", {}).get(category, {})
            subtotal = detail.get("observed_seconds") if isinstance(detail, dict) else None
            if value is None and type(subtotal) in (int, float) \
                    and math.isfinite(subtotal) and subtotal >= 0:
                partial.append(subtotal)
        observed_totals[key] = sum(observed) + sum(partial) if observed or partial else None
        totals[key] = sum(observed) if values and len(observed) == len(values) else None
        coverage[key] = {"observed_trials": len(observed),
                        "unmeasured_trials": len(values) - len(observed),
                        "partially_measured_trials": len(partial)}
    observed_burdens, completed, missing_completed = [], [], 0
    for manifest in manifests:
        burden = manifest.get("metrics", {}).get("approval_burden")
        valid = isinstance(burden, dict) and all(
            type(burden.get(key)) is int and burden[key] >= 0
            for key in ("requests", "necessary", "redundant", "unknown"))
        if valid:
            valid = burden["requests"] == sum(burden[key] for key in ("necessary", "redundant", "unknown"))
        if valid:
            observed_burdens.append(burden)
        if manifest.get("execution_status") == "completed":
            if valid:
                completed.append(burden)
            else:
                missing_completed += 1
    burden = None
    if observed_burdens:
        burden = {key: sum(row[key] for row in observed_burdens)
                  for key in ("requests", "necessary", "redundant", "unknown")}
        burden.update(observed_trials=len(observed_burdens),
            unmeasured_trials=len(manifests) - len(observed_burdens),
            completed_tasks=len(completed), unmeasured_completed_tasks=missing_completed,
            requests_per_completed_task=sum(row["requests"] for row in completed) / len(completed)
                if completed and not missing_completed else None,
            basis="All observed requests retained; ratio uses completed tasks only, with explicit coverage")
    return {"latency_seconds": totals, "observed_latency_seconds": observed_totals,
            "latency_coverage": coverage, "approval_burden": burden}
