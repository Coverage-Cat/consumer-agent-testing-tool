"""Coverage of explicit adapter token counts, independent of task success."""
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens",
                "input_tokens_cache_write", "input_tokens_cache_read", "reasoning_tokens")
REQUIRED = TOKEN_FIELDS[:3]


def count(value):
    return type(value) is int and value >= 0


def field_result(values, requests, gaps):
    observed = [value for value in values if count(value)]
    invalid = sum(value is not None and not count(value) for value in values)
    return {
        "observed_requests": len(observed), "invalid_requests": invalid,
        "missing_requests": requests - len(observed) - invalid,
        "observed_total": sum(observed) if observed else None,
        "complete": bool(requests) and not gaps and len(observed) == requests,
    }


def summarize(events):
    starts, ends = {}, {}
    requests, gaps, provenance_gaps = 0, 0, 0
    for position, (event, payload) in enumerate(events):
        kind = event["event_type"]
        if kind == "model_request":
            requests += 1
            identifier, collection = event.get("event_id"), starts
        elif kind in ("model_response", "provider_error"):
            identifier, collection = event.get("parent_id"), ends
        else:
            continue
        if not isinstance(identifier, str) or not identifier:
            gaps += 1
            continue
        collection.setdefault(identifier, []).append((position, event, payload))
    values = {name: [] for name in TOKEN_FIELDS}
    for identifier, rows in starts.items():
        terminal = ends.get(identifier, [])
        if len(rows) != 1 or len(terminal) != 1 or terminal[0][0] <= rows[0][0]:
            gaps += 1
            continue
        _, event, payload = terminal[0]
        if event["event_type"] != "model_response" or not isinstance(payload, dict):
            continue
        usage, provenance = payload.get("usage"), payload.get("usage_field_provenance")
        explicit = provenance.get("explicit_fields") if isinstance(provenance, dict) else None
        valid = (isinstance(usage, dict) and isinstance(provenance, dict)
                 and type(provenance.get("schema_version")) is int
                 and provenance["schema_version"] == 1
                 and provenance.get("kind") == "inspect-model-usage-fields"
                 and isinstance(explicit, list)
                 and all(isinstance(name, str) and name in usage for name in explicit)
                 and len(set(explicit)) == len(explicit))
        if not valid:
            provenance_gaps += 1
            continue
        for name in TOKEN_FIELDS:
            values[name].append(usage.get(name) if name in explicit else None)
    gaps += sum(len(rows) for identifier, rows in ends.items() if identifier not in starts)
    fields = {name: field_result(value, requests, gaps) for name, value in values.items()}
    complete = all(fields[name]["complete"] for name in REQUIRED)
    return {
        "token_usage": {name: detail["observed_total"] for name, detail in fields.items()
                        if detail["complete"]} if complete else None,
        "observed_token_usage": {name: detail["observed_total"] for name, detail in fields.items()
                                 if detail["observed_requests"]} or None,
        "usage_observations": {
            "schema_version": 1, "requests": requests, "observation_gaps": gaps,
            "required_fields": list(REQUIRED),
            "provenance_gaps": provenance_gaps, "fields": fields,
            "basis": "Explicit Inspect-adapter token fields across recorded model requests; "
                     "missing responses, usage, or explicit-field provenance are not zero. "
                     "Coverage does not establish provider billing or hidden provider work.",
        },
    }


def valid_observations(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int \
            or value["schema_version"] != 1 \
            or value.get("required_fields") != list(REQUIRED) \
            or not all(count(value.get(k)) for k in ("requests", "observation_gaps", "provenance_gaps")) \
            or not isinstance(value.get("fields"), dict) or set(value["fields"]) != set(TOKEN_FIELDS):
        return False
    requests = value["requests"]
    if value["provenance_gaps"] > requests:
        return False
    for detail in value["fields"].values():
        if not isinstance(detail, dict) or not all(count(detail.get(k)) for k in (
                "observed_requests", "missing_requests", "invalid_requests")):
            return False
        observed = detail["observed_requests"]
        if observed + detail["missing_requests"] + detail["invalid_requests"] != requests \
                or (not count(detail.get("observed_total")) if observed else detail.get("observed_total") is not None):
            return False
        expected = bool(requests) and not value["observation_gaps"] and observed == requests
        if type(detail.get("complete")) is not bool or detail["complete"] != expected \
                or observed > requests - value["provenance_gaps"]:
            return False
    return True


def summarize_trials(manifests):
    observations = [m.get("metrics", {}).get("usage_observations") for m in manifests]
    valid = [value for value in observations if valid_observations(value)]
    complete = sum(all(value["fields"][name]["complete"] for name in REQUIRED) for value in valid)
    partial = sum(any(value["fields"][name]["observed_requests"] for name in TOKEN_FIELDS)
                  and not all(value["fields"][name]["complete"] for name in REQUIRED) for value in valid)
    known, observed, coverage = {}, {}, {}
    for name in TOKEN_FIELDS:
        rows = [value["fields"][name] for value in valid]
        measured = [row for row in rows if row["observed_requests"]]
        if measured:
            observed[name] = sum(row["observed_total"] for row in measured)
        full = sum(row["complete"] for row in rows)
        if manifests and full == len(manifests):
            known[name] = observed[name]
        coverage[name] = {
            "complete_trials": full,
            "incomplete_trials": len(manifests) - full,
            "observed_requests": sum(row["observed_requests"] for row in rows),
            "recorded_requests": sum(value["requests"] for value in valid),
            "unknown_request_coverage_trials": len(manifests) - len(valid)
                + sum(bool(value["observation_gaps"]) for value in valid),
        }
    return {
        "token_usage": known if manifests and complete == len(manifests) else None,
        "observed_token_usage": observed or None,
        "usage_trial_coverage": {
            "trials": len(manifests), "complete": complete, "partial": partial,
            "unobserved": len(manifests) - complete - partial,
            "required_fields": list(REQUIRED),
        },
        "token_field_coverage": coverage,
        "usage_basis": "All trial outcomes, including blocked and infrastructure failures; "
                       "only explicit adapter counts with valid coverage are aggregated. "
                       "Legacy summaries without coverage cannot establish a complete total.",
    }
