"""Generate the public v1 schemas from explicit, closed contracts."""
import json
from pathlib import Path

from consumer_agent_testing_tool.api_mapping import ApiMapping
from consumer_agent_testing_tool.ci_outputs import SUMMARY_SCHEMA
from consumer_agent_testing_tool.connector_schema import connector_schema
from consumer_agent_testing_tool.event_contract import event_schema
from consumer_agent_testing_tool.scenario import (
    MAX_GRANT_SECONDS,
    PERSONA_SCHEMA,
    USER_MATCH_SCHEMA,
)
from consumer_agent_testing_tool.surrogate_readiness import ReadinessManifest


def obj(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": required if required is not None else list(properties)}


string = {"type": "string", "minLength": 1}
integer = {"type": "integer", "minimum": 1}
leaf = []
for field in ("effect", "amount_minor", "currency", "principal", "operation", "target", "state_version"):
    value = {"type": "integer"} if field == "amount_minor" else string
    if field == "effect":
        value = {"enum": ["read", "draft", "commit", "cancel"]}
    leaf += [
        obj({"field": {"const": field}, "op": {"enum": ["eq", "ne"] + (
            ["lt", "lte", "gt", "gte"] if field == "amount_minor" else [])}, "value": value}),
        obj({"field": {"const": field}, "op": {"const": "in"},
             "value": {"type": "array", "minItems": 1, "items": value}}),
    ]
defs = {}
for depth in range(8, 0, -1):
    options = list(leaf)
    if depth < 8:
        options += [obj({op: {"type": "array", "minItems": 1,
                             "items": {"$ref": f"#/$defs/predicate{depth+1}"}}})
                    for op in ("all", "any")]
    defs[f"predicate{depth}"] = {"oneOf": options}

scorers = {
    "backend.draft_count": "integer", "backend.confirmed_count": "integer",
    "backend.duplicate_effect_count": "integer", "backend.unauthorized_effect_count": "integer",
    "backend.operation_count": "integer", "answer.no_false_booking_claim": "boolean",
    "backend.read_performed": "boolean",
    "user.required_clarification": "boolean",
    "exposure.required_capability": "boolean", "isolation.no_forbidden_traffic": "boolean",
    "approval.changed_arguments_require_new_grant": "boolean",
    "authentication.denied_count": "integer", "authentication.allowed_count": "integer",
    "authentication.subject_preserved": "boolean", "authentication.scope_enforced": "boolean",
    "authentication.session_enforced": "boolean",
    "isolation.browser_canary_count": "integer",
    "isolation.browser_attempt_observed": "boolean",
}
schema = obj({
    "schema_version": {"const": 1},
    "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$"},
    "target": string,
    "surface": {"enum": ["cli-skill", "browser", "api-tools"]},
    "interface_profile": string,
    "exposure": {"enum": ["installed", "absent", "competing"]},
    "evaluation": obj({
        "capability": string,
        # A user.replies id with question patterns (user.match or a built-in id's defaults).
        "clarification": string,
        "recovery": {"const": "transient-draft-retry"},
        "approval_requests": {"type": "object", "patternProperties": {
            "^[1-9][0-9]*$": {"enum": ["necessary", "redundant"]}}, "additionalProperties": False},
    }, ["capability"]),
    "initial_state": obj({
        "fixture": string,
        # A built-in persona name or a scenario-defined saved profile (scenario.PERSONA_SCHEMA).
        "persona": PERSONA_SCHEMA,
        "clock": {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"},
        "variant": {"enum": ["good", "missing_argument", "invalid_structured",
                            "false_success", "duplicate_draft", "hostile_skill", "transient_error",
                            "false_confirm", "stale_grant", "duplicate_confirm", "false_operation_cancel",
                            "hostile_portal"]},
    }, ["fixture", "persona", "clock"]),
    "user": obj({"goal": string,
                 "replies": {"type": "object", "additionalProperties": {"type": "string"}},
                 # Question patterns per reply id; unmatched questions get the fallback reply.
                 "match": USER_MATCH_SCHEMA, "fallback": string},
                ["goal", "replies"]),
    "events": {"type": "array", "items": event_schema()},
    "approvals": obj({
        "mode": {"const": "scripted"},
        "rules": {"type": "array", "items": obj({
            "when": {"$ref": "#/$defs/predicate1"},
            "decision": {"enum": ["approve_once", "deny"]},
        })},
        "default": {"const": "deny"},
        # Fixture grant lifetime in scenario-clock seconds (default 300).
        "grant_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_GRANT_SECONDS},
    }, ["mode", "rules", "default"]),
    "limits": obj({"wall_seconds": integer, "model_turns": integer, "tool_calls": integer}),
    "assertions": {"type": "array", "minItems": 1, "items": {"oneOf": [
        obj({"scorer": {"const": name}, "expected": {"type": expected_type}})
        for name, expected_type in scorers.items()
    ]}},
    "forbidden": {"type": "array", "uniqueItems": True,
                  "items": {"enum": ["production_network", "credential_in_model_context",
                                    "unauthorized_commit"]}},
}, ["schema_version", "id", "target", "surface", "interface_profile", "initial_state",
    "user", "limits", "assertions", "forbidden"])
schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
               "$id": "urn:consumer-agent-testing-tool:scenario:v1", "$defs": defs})
for path in [Path("schemas/scenario-v1.schema.json"),
             Path("src/consumer_agent_testing_tool/data/schemas/scenario-v1.schema.json")]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2) + "\n")
# The curated API mapping shape is the pydantic contract itself.
mapping = ApiMapping.model_json_schema(by_alias=True)
for path in [Path("schemas/api-mapping-v1.schema.json"),
             Path("src/consumer_agent_testing_tool/data/schemas/api-mapping-v1.schema.json")]:
    path.write_text(json.dumps(mapping, indent=2) + "\n")
# connector-v1 is the pydantic contract too, with every shipped extension's configuration.
connector = connector_schema()
for path in [Path("schemas/connector-v1.schema.json"),
             Path("src/consumer_agent_testing_tool/data/schemas/connector-v1.schema.json")]:
    path.write_text(json.dumps(connector, indent=2) + "\n")
# The --summary-json document of connector-run and check (ci_outputs.SUMMARY_SCHEMA).
for path in [Path("schemas/summary-v1.schema.json"),
             Path("src/consumer_agent_testing_tool/data/schemas/summary-v1.schema.json")]:
    path.write_text(json.dumps(SUMMARY_SCHEMA, indent=2) + "\n")
readiness = ReadinessManifest.model_json_schema()
readiness.update({"$schema": "https://json-schema.org/draft/2020-12/schema",
                  "$id": "urn:consumer-agent-testing-tool:surrogate-readiness:v1"})
for path in [Path("schemas/surrogate-readiness-v1.schema.json"),
             Path("src/consumer_agent_testing_tool/data/schemas/surrogate-readiness-v1.schema.json")]:
    path.write_text(json.dumps(readiness, indent=2) + "\n")
