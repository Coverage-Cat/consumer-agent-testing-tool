"""The common evidence envelope for sealed browser and consumer trajectories."""
import json
import re
from datetime import datetime

from .errors import LabError
from .evidence import dependency_notice, event_envelope, write_artifact
from .serialization import file_digest, load

VERSION = 2
ENVELOPE_FIELDS = {
    "event_schema_version", "run_id", "scenario_id", "event_id", "parent_id", "agent_id",
    "task_id", "wall_timestamp", "monotonic_ns", "clock_id", "event_type", "surface",
    "profile", "visibility", "provenance", "redaction_status", "artifacts",
}


def record(path, redactor, sequence, kind, value, *, scenario_id, profile, clock_id, driver, run_id=None):
    run_id = run_id or path.name
    # The envelope identity is the harness's own. Registered as public, a token that equals
    # the scenario ID or profile name cannot rewrite the result these envelopes are sealed with.
    redactor.protect(run_id, scenario_id, clock_id, *(profile.get(key) for key in ("name", "surface", "label")))
    payload = redactor.clean_evidence({"kind": kind, **value})
    if not isinstance(payload["kind"], str) or not payload["kind"]:
        raise LabError("Event kind must be a nonempty string")
    # Keep the original flat payload for readers of the first format, but
    # never let a payload replace the host's identity, clock or artifact binding.
    # The artifact stores this same cleaned payload: redacting twice could differ
    # and break the payload/artifact agreement that sealing verifies.
    envelope = event_envelope(run_id, sequence, scenario_id, profile, payload["kind"],
        [write_artifact(path, redactor, payload, redacted=True)],
        agent_id="evaluated-agent" if driver == "agent" else "scripted-host",
        audience="model" if payload["kind"] == "agent_observation" else "developer",
        provenance="trusted host observation; not an independent target assertion")
    return {**payload, **envelope, "sequence": sequence,
            "event_schema_version": VERSION, "clock_id": clock_id}


def metadata(path, *, scenario_id, profile, driver, locations, run_id=None):
    notice = dependency_notice(path)
    return {
        "event_schema_version": VERSION, "run_id": run_id or path.name, "scenario_id": scenario_id,
        "lane": "behavioral" if driver == "agent" else "contract",
        "evidence_label": "surrogate behavior" if driver == "agent" else "protocol-tested",
        "fidelity": profile["label"], "interface_profile": profile,
        "locations": locations, "dependency_notices": notice,
        "observation_gaps": [] if notice else ["Distribution dependency notices unavailable"],
    }


def recovery_metadata(path, events, *, driver):
    if not events or events[0].get("event_schema_version") != VERSION:
        return {}
    first = events[0]
    result = metadata(path, scenario_id=first["scenario_id"], profile={
        "name": first["profile"], "surface": first["surface"],
        "label": "surrogate; interrupted browser attempt",
    }, driver=driver, run_id=first["run_id"], locations={key: "unknown; interrupted attempt"
        for key in ("inference", "browser", "target", "telemetry", "egress")})
    result["observation_gaps"].append("Interrupted attempt location metadata was not retained")
    return result


def reference_path(root, reference, *, suffix):
    from .replay import evidence_path
    if not isinstance(reference, dict) or not isinstance(reference.get("sha256"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) \
            or reference.get("path") != f'artifacts/{reference["sha256"]}.{suffix}':
        raise LabError("Invalid sealed artifact reference")
    path = evidence_path(root, reference["path"])
    if not path.is_file() or file_digest(path) != reference["sha256"]:
        raise LabError("Missing or changed sealed event artifact")
    return path


def public_files(root, result, events):
    try:
        return _public_files(root, result, events)
    except (KeyError, TypeError, ValueError) as error:
        raise LabError("Invalid sealed event metadata") from error


def _public_files(root, result, events):
    """Validate references before including only linked public artifacts."""
    if not isinstance(result, dict) or any(not isinstance(event, dict) for event in events):
        raise LabError("Sealed event records must be objects")
    names = set()
    clocks = {}
    if any("event_schema_version" in event for event in events) \
            and (type(result.get("event_schema_version")) is not int
                 or result["event_schema_version"] != VERSION):
        raise LabError("Versioned events require matching result provenance")
    if result.get("event_schema_version") == VERSION and not isinstance(result.get("interface_profile"), dict):
        raise LabError("Sealed result requires an interface profile")
    for event in events:
        if "event_schema_version" not in event:
            continue  # Historical records are preserved, never supplied invented metadata.
        if type(event["event_schema_version"]) is not int or event["event_schema_version"] != VERSION \
                or not ENVELOPE_FIELDS.issubset(event):
            raise LabError("Invalid sealed event envelope")
        profile = result.get("interface_profile", {})
        problems = {
            "run_id": event["run_id"] != result.get("run_id"),
            "scenario_id": event["scenario_id"] != result.get("scenario_id"),
            "sequence": type(event.get("sequence")) is not int or event["sequence"] < 1,
            "event_id": event["event_id"] != f'{event["run_id"]}:{event.get("sequence")}',
            "monotonic_ns": type(event["monotonic_ns"]) is not int or event["monotonic_ns"] < 0,
            "clock_id": not isinstance(event["clock_id"], str) or not event["clock_id"],
            "wall_timestamp": datetime.fromisoformat(event["wall_timestamp"]).tzinfo is None,
            "redaction_status": event["redaction_status"] != "redacted",
            "profile": event["profile"] != profile.get("name"),
            "surface": event["surface"] != profile.get("surface"),
            "visibility": event["visibility"] not in ("model", "developer"),
            "agent_id": event["agent_id"] not in ("evaluated-agent", "scripted-host"),
            "provenance": not isinstance(event["provenance"], str) or not event["provenance"].strip(),
            "artifacts": not isinstance(event["artifacts"], list) or len(event["artifacts"]) != 1,
        }
        if failed := [name for name, problem in problems.items() if problem]:
            # Name the fields, never their values: identities can embed private paths.
            raise LabError("Sealed event identity, clock or provenance is invalid: " + ", ".join(failed))
        if event["monotonic_ns"] < clocks.get(event["clock_id"], 0):
            raise LabError("Sealed event monotonic clock moved backwards within its epoch")
        clocks[event["clock_id"]] = event["monotonic_ns"]
        reference = event["artifacts"][0]
        path = reference_path(root, reference, suffix="json")
        payload = load(path)
        if not isinstance(payload, dict) or payload.get("kind") != event.get("kind") \
                or event["event_type"] != event.get("kind") \
                or any(event.get(key) != value or type(event.get(key)) is not type(value)
                       for key, value in payload.items() if key not in ENVELOPE_FIELDS | {"sequence"}) \
                or set(event) - ENVELOPE_FIELDS - {"sequence"} != set(payload) - ENVELOPE_FIELDS - {"sequence"}:
            raise LabError("Sealed event payload differs from its artifact")
        if reference.get("bytes") != path.stat().st_size:
            raise LabError("Sealed event artifact size differs")
        names.add(reference["path"])
    if "event_schema_version" in result:
        if type(result["event_schema_version"]) is not int or result["event_schema_version"] != VERSION:
            raise LabError("Invalid sealed result evidence version")
        if any(event.get("event_schema_version") != VERSION for event in events):
            raise LabError("A current result cannot claim historical event coverage")
        for field in ("run_id", "lane", "evidence_label", "fidelity", "interface_profile", "locations"):
            if not result.get(field):
                raise LabError("Sealed result is missing required provenance")
        if notice := result.get("dependency_notices"):
            reference_path(root, notice, suffix="zip")
            names.add(notice["path"])
    return names


def export_events(source, destination, events, result, redactor):
    from .evidence import export_notice
    output = []
    for event in events:
        cleaned = redactor.clean_evidence(event)
        if event.get("event_schema_version") == VERSION:
            payload = load(reference_path(source, event["artifacts"][0], suffix="json"))
            # The row and its artifact are cleaned from equal verified inputs, once each,
            # by context-free rules, so their shared payload fields still agree.
            cleaned["artifacts"] = [write_artifact(destination, redactor, payload)]
        output.append(cleaned)
    if result.get("event_schema_version") == VERSION and (notice := result.get("dependency_notices")):
        export_notice(source, notice, destination)
    return "".join(json.dumps(event) + "\n" for event in output)
