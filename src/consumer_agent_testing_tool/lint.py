import json
import re
import shlex

from jsonschema import Draft202012Validator


def usability_checks(tools):
    checks = []
    for tool in tools:
        description = tool.description or ""
        name = tool.name
        properties = tool.input_schema.get("properties", {})
        required = tool.input_schema.get("required", [])

        def warning(rule, detail, name=name):
            checks.append({"id": f"{rule}:{name}", "status": "warning",
                           "class": "heuristic usability warning", "detail": detail})

        if name.lower() in ("do", "run", "process", "action", "execute", "handle"):
            warning("ambiguous-name", "The name does not identify a domain capability")
        if tool.input_schema.get("additionalProperties") is False and set(required) - properties.keys():
            checks.append({"id": f"contradictory-required:{name}", "status": "failed",
                           "class": "schema error",
                           "detail": "Required fields are forbidden by additionalProperties:false"})
        mutation = any(word in name.lower() for word in ("create", "revise", "submit", "confirm", "cancel", "update"))
        if mutation and not re.search(r"retry|409|reconcil|error|fail", description, re.IGNORECASE):
            warning("error-recovery", "Mutation description does not explain recovery")
        if mutation and not re.search(r"draft|commit|reserv|side.effect|state|nonbinding|mutat", description, re.IGNORECASE):
            warning("side-effects", "Mutation description does not state its business effect")
        if mutation and not re.search(r"require|using|available|prior|before|expected|draft", description, re.IGNORECASE):
            warning("preconditions", "Mutation preconditions are not stated")
        if re.search(r"muse.certified|guaranteed.compatib|identical.to.muse", description, re.IGNORECASE):
            warning("unsupported-claim", "Description makes a host-equivalence or certification claim")
        for parameter, schema in properties.items():
            text = description + " " + schema.get("description", "")
            if any(word in parameter.lower() for word in ("amount", "price", "duration", "timestamp")) \
                    and not re.search(r"minor|cent|second|millisecond|utc|timezone|iso.?8601|unit", text, re.IGNORECASE):
                warning("units", f"Units/time zone for {parameter} are unstated")
    return checks


def skill_example_checks(text, tools, command_prefix=("booking",)):
    checks = []
    by_name = {tool.name.replace("_", "."): tool for tool in tools}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith(" ".join(command_prefix) + " "):
            continue
        try:
            tokens = shlex.split(line.strip())
            name = tokens[len(command_prefix)]
            if name not in by_name:
                continue
            arguments = json.loads(tokens[tokens.index("--args") + 1]) if "--args" in tokens else {}
            errors = list(Draft202012Validator(by_name[name].input_schema).iter_errors(arguments))
            if errors:
                checks.append({"id": f"skill-example:{line_number}", "status": "failed",
                    "class": "discovery/exposure", "detail": errors[0].message,
                    "basis": "Example contradicts the published required argument schema",
                    "line": line_number})
        except (ValueError, IndexError) as exc:
            checks.append({"id": f"skill-example:{line_number}", "status": "warning",
                           "class": "heuristic usability warning", "detail": str(exc)})
    return checks
