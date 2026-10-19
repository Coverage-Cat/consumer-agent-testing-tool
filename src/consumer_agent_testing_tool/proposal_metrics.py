"""Native proposal validation against the exact locally submitted tool definitions.

This is measurement, never authorization. Do not resolve schemas over a network
or infer business validity from successful JSON Schema validation.
"""
import json

from jsonschema import Draft202012Validator, FormatChecker, validators
from referencing import Registry
from referencing.exceptions import NoSuchResource


def no_retrieval(uri):
    raise NoSuchResource(ref=uri)


def schema_supported(value):
    """Walk schema positions, never property names or enum/default instance data."""
    if isinstance(value, dict):
        for key in ("$ref", "$dynamicRef"):
            item = value.get(key)
            if key in value and (
                    not isinstance(item, str) or not item.startswith("#")):
                return False
        if "format" in value and (not isinstance(value["format"], str)
                                   or value["format"] not in FormatChecker.checkers):
            return False
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"):
            mapping = value.get(key)
            if isinstance(mapping, dict) and not all(schema_supported(item) for item in mapping.values()):
                return False
        for key in ("items", "additionalItems", "additionalProperties", "unevaluatedItems",
                    "unevaluatedProperties", "contains", "propertyNames", "if", "then", "else", "not",
                    "allOf", "anyOf", "oneOf", "prefixItems"):
            if key in value and not schema_supported(value[key]):
                return False
    elif isinstance(value, list):
        return all(schema_supported(item) for item in value)
    return True


def validate(call, tools):
    """Evaluate before redaction; retain no argument values or exception messages."""
    matches = [tool for tool in tools if tool["name"] == call.function]
    if not matches:
        return {"valid": False, "reason": "tool_not_exposed"}
    if len(matches) != 1:
        return {"valid": None, "reason": "ambiguous_tool_catalog"}
    if call.parse_error:
        return {"valid": False, "reason": "native_argument_parse_error"}
    try:
        encoded = json.dumps(call.arguments, allow_nan=False, ensure_ascii=False)
        encoded.encode("utf-8")
        if json.loads(encoded) != call.arguments:
            return {"valid": False, "reason": "non_json_arguments"}
    except (ValueError, TypeError, UnicodeError):
        return {"valid": False, "reason": "non_json_arguments"}
    schema = matches[0]["parameters"]
    try:
        if "$schema" in schema and schema["$schema"] not in (
                "https://json-schema.org/draft/2020-12/schema",
                "https://json-schema.org/draft/2019-09/schema",
                "http://json-schema.org/draft-07/schema#",
                "http://json-schema.org/draft-06/schema#",
                "http://json-schema.org/draft-04/schema#"):
            return {"valid": None, "reason": "unsupported_schema_dialect"}
        if not schema_supported(schema):
            return {"valid": None, "reason": "unsupported_schema_reference_or_format"}
        implementation = validators.validator_for(schema, default=Draft202012Validator)
        implementation.check_schema(schema)
        validator = implementation(schema, registry=Registry(retrieve=no_retrieval),
                                   format_checker=FormatChecker())
        error = next(validator.iter_errors(call.arguments), None)
        return {"valid": error is None, "reason": "schema_valid" if error is None else "schema_violation",
                **({"keyword": error.validator} if error is not None else {})}
    except Exception:  # noqa: BLE001 — unavailable measurement must not alter execution
        return {"valid": None, "reason": "schema_validation_unavailable"}


def record(evidence, response, observation, calls, tools):
    schemas = [{"name": tool.name, "parameters": tool.parameters.model_dump(mode="json", exclude_none=True)}
               for tool in tools]
    evidence.event("proposal_validation", {
        "version": 1, "observation_event": observation,
        "schema_projection": schemas,
        "schema_serialization": "Inspect ToolParams.model_dump(mode=json, exclude_none=True); "
            "before provider-specific transformations",
        "proposals": [{"index": index, "call_id": call.id, "name": call.function, **validate(call, schemas)}
                      for index, call in enumerate(calls)],
        "basis": "Native selected-response proposals against locally serialized Inspect schemas before redaction; "
                 "registered format assertions enabled; no default insertion, permission decision "
                 "or business-state inference",
    }, parent_id=response, provenance="host schema measurement before budget and dispatch checks")


def summarize(events):
    responses, records, requests, observations = {}, {}, {}, set()
    for event, payload in events:
        if event["event_type"] == "observation_emitted":
            observations.add(event["event_id"])
        elif event["event_type"] == "model_request":
            requests[event["event_id"]] = payload.get("observation_event")
        elif event["event_type"] == "model_response":
            choices = payload.get("choices", [])
            calls = choices[0].get("message", {}).get("tool_calls") if choices else None
            responses[event["event_id"]] = (event["parent_id"], calls or [])
        elif event["event_type"] == "proposal_validation":
            records.setdefault(event["parent_id"], []).append(payload)
    if not records:
        return None
    valid, invalid, unknown, gaps, observed = 0, 0, 0, 0, 0
    for response, (request, calls) in responses.items():
        bundles = records.get(response, [])
        bundle = bundles[0] if len(bundles) == 1 else {}
        rows = bundle.get("proposals")
        observation = requests.get(request)
        if type(bundle.get("version")) is not int or bundle["version"] != 1 \
                or not isinstance(rows, list) or len(rows) != len(calls) \
                or observation not in observations or bundle.get("observation_event") != observation:
            unknown += len(calls)
            gaps += 1
            continue
        observed += 1
        for index, (row, call) in enumerate(zip(rows, calls)):
            matches = (isinstance(row, dict) and type(row.get("index")) is int
                       and row["index"] == index and row.get("call_id") == call.get("id")
                       and row.get("name") == call.get("function"))
            value = row.get("valid") if matches else None
            if value is True:
                valid += 1
            elif value is False:
                invalid += 1
            else:
                unknown += 1
    gaps += sum(len(rows) for parent, rows in records.items() if parent not in responses)
    return {"version": 1, "valid": valid, "invalid": invalid, "unknown": unknown,
            "proposals": valid + invalid + unknown, "observed_responses": observed,
            "responses": len(responses), "observation_gaps": gaps}


def trial_observations(manifests):
    keys = ("valid", "invalid", "unknown", "proposals", "observation_gaps")
    totals = dict.fromkeys(keys, 0)
    observed, complete = 0, 0
    for manifest in manifests:
        row = manifest.get("metrics", {}).get("argument_observations")
        if not isinstance(row, dict) or type(row.get("version")) is not int or row["version"] != 1:
            continue
        if not all(type(row.get(key)) is int and row[key] >= 0
                   for key in (*keys, "observed_responses", "responses")):
            continue
        if row["proposals"] != row["valid"] + row["invalid"] + row["unknown"] \
                or row["observed_responses"] > row["responses"]:
            continue
        observed += 1
        complete += not row["unknown"] and not row["observation_gaps"] \
            and row["observed_responses"] == row["responses"]
        for key in keys:
            totals[key] += row[key]
    return totals, {"observed": observed, "complete": complete, "unmeasured": len(manifests) - observed}
