"""Closed M2 scenario hook/action registry shared by schema and runtime."""

from datetime import datetime

from jsonschema import Draft202012Validator

from .errors import LabError
from .fixture_auth import SCOPES, STATES

HOOKS = ("task_started", "approval_requested", "tool_completed")
STRING = {"type": "string", "minLength": 1}
CLOCK = {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"}


def shape(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


EVENT_ARGUMENTS = {
    "set_authentication": shape({
        "principal": {"enum": ["alice", "bob"]}, "state": {"enum": list(STATES)},
        "subject": {"enum": ["alice", "bob", None]},
        "scopes": {"type": "array", "items": {"enum": list(SCOPES)}, "uniqueItems": True},
        "expires_at": {"anyOf": [CLOCK, {"type": "null"}]},
    }),
    "change_total": shape({"amount_minor": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}}),
    "advance_time": shape({"clock": CLOCK}),
    "defer_operation": shape({
        "operation": {"enum": ["reservations.confirm", "reservations.cancel"]},
        "key": STRING, "ready_at": CLOCK, "principal": {"enum": ["alice", "bob"]}}),
    "lose_response": shape({
        "operation": {"enum": ["reservations.confirm", "reservations.cancel"]}, "key": STRING}),
    "interrupt": shape({}),
}


def event_schema():
    return {"oneOf": [shape({
        "on": {"enum": list(HOOKS)}, "occurrence": {"type": "integer", "minimum": 1},
        "action": {"const": action}, "args": arguments})
        for action, arguments in EVENT_ARGUMENTS.items()]}


def validate_event(event):
    errors = list(Draft202012Validator(event_schema()).iter_errors(event))
    if errors:
        raise LabError("Unknown scenario event or invalid hook/action arguments")
    # JSON Schema's integer accepts 1.0. The public contract requires actual integers.
    if type(event["occurrence"]) is not int:
        raise LabError("Event occurrence must be an integer")
    if "amount_minor" in event["args"] and type(event["args"]["amount_minor"]) is not int:
        raise LabError("Event amount must be an integer")
    for name in ("clock", "ready_at", "expires_at"):
        if name in event["args"] and event["args"][name] is not None:
            try:
                datetime.fromisoformat(event["args"][name])
            except ValueError as error:
                raise LabError("Event clock is not a valid calendar timestamp") from error
