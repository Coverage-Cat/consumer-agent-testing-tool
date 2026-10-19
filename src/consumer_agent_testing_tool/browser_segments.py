"""Preserved browser attempts belonging to one scenario trial."""
import re

from .errors import LabError
from .replay import evidence_path, playback
from .serialization import file_digest, loads


def references(manifest):
    rows = manifest.get("browser_segments") or (
        [manifest["browser_evidence"]] if manifest.get("browser_evidence") else [])
    paths = [row.get("path") for row in rows]
    if any(not isinstance(path, str) or not re.fullmatch(r"browser(?:-attempt-[2-9][0-9]*|-attempt-1[0-9]+)?",
                                                        path) for path in paths) \
            or len(set(paths)) != len(paths):
        raise LabError("Invalid browser evidence reference")
    if rows and manifest.get("browser_evidence") != rows[-1]:
        raise LabError("Latest browser evidence does not match the preserved segments")
    return rows


def verify(root, reference):
    child = evidence_path(root, reference["path"])
    seal = evidence_path(root, reference["path"] + "/evidence.sha256.json")
    if file_digest(seal) != reference.get("seal_sha256"):
        raise LabError("Browser evidence seal digest mismatch")
    playback(child)
    return child


def publish(evidence, path):
    reference = {"path": path.name, "seal_sha256": file_digest(path / "evidence.sha256.json")}
    rows = list(references(evidence.manifest))
    if any(row["path"] == reference["path"] for row in rows):
        raise LabError("Cannot replace a preserved browser attempt")
    rows.append(reference)
    evidence.manifest.update(browser_evidence=reference, browser_segments=rows)
    evidence.save()
    return reference


def recover_interrupted(session):
    """Seal a stopped attempt's retained events without rerunning its browser."""
    attempt = session.state.get("browser_attempt")
    if not attempt or attempt["status"] == "sealed":
        return
    evidence = session.evidence
    path = evidence_path(evidence.path, attempt["path"])
    current = next((row for row in references(evidence.manifest)
                    if row["path"] == attempt["path"]), None)
    if current:
        verify(evidence.path, current)
    else:
        if not (path / "evidence.sha256.json").exists():
            events = evidence_path(path, "events.jsonl")
            if not events.exists():
                events.write_text("")
            rows = [loads(line, json_only=True) for line in events.read_text().splitlines()]
            for index, row in enumerate(rows, 1):
                if row.get("sequence") != index:
                    raise LabError("Interrupted browser events are incomplete")
            from .sealed_events import recovery_metadata
            from .sealed_report import finalize
            finalize(path, {**recovery_metadata(path, rows, driver="agent"),
                "status": "paused", "execution_status": "interrupted",
                "driver": "agent", "surface": "restricted snapshot browser surrogate",
                "run_lock": attempt["run_lock"], "compatibility_pass": False,
                "checks": [], "verified_service_outcome": None,
                "detail": "Prior browser process stopped; only retained events are available.",
                "limits": ["No previous-attempt backend outcome is inferred from these events."]})
        else:
            playback(path)
        publish(evidence, path)
    attempt["status"] = "sealed"
    session.persist()
    evidence.event("browser_attempt_recovered", {"path": attempt["path"]},
                   provenance="retained browser events; no action replay")
