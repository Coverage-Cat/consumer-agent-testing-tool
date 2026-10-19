"""Private, explicitly configured browser diagnostics; never model or export inputs."""
import html
import json
import re
from pathlib import Path

from pydantic import Field, StrictBool, StrictInt, model_validator

from .errors import LabError
from .replay import evidence_path, playback
from .serialization import digest, file_digest
from .surface import Strict


class DiagnosticPolicy(Strict):
    enabled: StrictBool = False
    retain_raw_trace: StrictBool = False
    mask_selectors: list[str] = Field(default_factory=list, max_length=30)
    max_screenshots: StrictInt = Field(default=100, ge=1, le=500)
    max_bytes: StrictInt = Field(default=50 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)

    @model_validator(mode="after")
    def explicit_capture(self):
        if not self.enabled and (self.retain_raw_trace or self.mask_selectors):
            raise ValueError("Diagnostic capture must be explicitly enabled")
        if any(not item.strip() or len(item) > 500 for item in self.mask_selectors):
            raise ValueError("Mask selectors must be nonempty and at most 500 characters")
        return self


def private_json(path, value):
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    with path.open("w") as stream:
        path.chmod(0o600)
        stream.write(encoded)


def artifact(directory, reference, extension):
    if not isinstance(reference, dict) or not re.fullmatch(
            rf"[0-9a-f]{{64}}\.{extension}", reference.get("path", "")):
        raise LabError("Invalid diagnostic artifact reference")
    file = evidence_path(directory, reference["path"])
    if not file.is_file() or file_digest(file) != reference.get("sha256") \
            or file.stem != reference["sha256"] or file.stat().st_size != reference.get("bytes"):
        raise LabError("Diagnostic artifact integrity check failed")
    return file


class DeveloperDiagnostics:
    def __init__(self, run, policy):
        self.run, self.policy = run, policy
        self.directory = run / "diagnostics.private"
        self.captures, self.errors = [], []
        self.trace = {"status": "disabled"}
        self.stopped = False

    def prepare(self):
        if self.policy.enabled:
            self.directory.mkdir(mode=0o700)
        return str(self.directory) if self.policy.enabled else None

    def accept(self, result, snapshot):
        row = {**result, "generation": snapshot["generation"],
               "observation_sha256": digest(snapshot)}
        if result.get("generation") != snapshot["generation"]:
            raise LabError("Diagnostic capture does not match the emitted generation")
        if result.get("status") == "captured":
            artifact(self.directory, result, "png")
        # The recorded (redacted) form of the snapshot the evaluated agent received.
        data = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
        import hashlib
        key = hashlib.sha256(data.encode()).hexdigest()
        file = self.directory / f"{key}.json"
        private_json(file, snapshot)
        row["observation"] = {"path": file.name, "sha256": key, "bytes": file.stat().st_size}
        self.captures.append(row)
        return {"generation": row["generation"], "status": row["status"],
                "observation_sha256": row["observation_sha256"]}

    def fail(self, stage, error):
        # Detailed exceptions may contain selectors or page data; public reports
        # get only the stage and category. No diagnostic is returned to the agent.
        self.errors.append({"stage": stage, "category": type(error).__name__, "detail": str(error)})

    def accept_trace(self, result):
        if result.get("status") == "captured":
            artifact(self.directory, result, "zip")
        self.trace = result
        self.stopped = True

    def finish(self):
        if not self.policy.enabled:
            return {"status": "disabled", "audience": "developer-only"}
        if self.policy.retain_raw_trace and not self.stopped:
            self.errors.append({"stage": "trace", "category": "NotFinalized"})
        if not self.captures:
            self.errors.append({"stage": "screenshot", "category": "NoObservations"})
        failed = bool(self.errors) or any(row["status"] != "captured" for row in self.captures) \
            or (self.policy.retain_raw_trace and self.trace["status"] != "captured")
        manifest = {"schema_version": 1, "status": "incomplete" if failed else "captured",
                    "audience": "developer-only", "export": "excluded",
                    "policy": self.policy.model_dump(), "captures": self.captures,
                    "trace": self.trace, "errors": self.errors,
                    "limits": ["Snapshot/screenshot pairing is sequential, not an atomic page capture.",
                               "Masks do not certify arbitrary pixels as free of personal data.",
                               "Media limits apply to retained PNG/ZIP output, not peak trace disk usage."]}
        private_json(self.directory / "manifest.json", manifest)
        write_index(self.directory, manifest)
        return {"status": manifest["status"], "audience": "developer-only", "export": "excluded",
                "captures": len(self.captures), "trace_status": self.trace["status"],
                "manifest_sha256": file_digest(self.directory / "manifest.json"),
                "index_sha256": file_digest(self.directory / "index.html")}


def write_index(directory, manifest):
    rows = []
    for record in manifest["captures"]:
        source = artifact(directory, record["observation"], "json")
        snapshot = json.loads(source.read_text())
        image = ""
        if record["status"] == "captured":
            file = artifact(directory, record, "png")
            image = f'<img alt="Masked viewport for this generation" src="{file.name}">'
        rows.append(f'<section><h2>Generation {record["generation"]}</h2>'
                    f'<p>{html.escape(record["status"])}</p><div class="pair">{image}'
                    f'<pre>{html.escape(snapshot["text"])}</pre></div></section>')
    text = """<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self';
style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Private browser diagnostics</title><style>body{font:16px system-ui;margin:2rem}
.pair{display:flex;gap:1rem;align-items:flex-start}.pair img{max-width:55%}
pre{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #ccc}</style>
<h1>Private browser diagnostics</h1>
<p>Developer only. Masked images and raw traces are excluded from standard exports.
Masks do not guarantee removal of all sensitive pixels. Screenshot and snapshot
are sequential captures; animation or canvas content can change between them.</p>
<p>Use <code>consumer-agent-testing-tool browser-diagnostics RUN --trace-command</code>
to open a verified trace with the matching local Playwright Trace Viewer.</p>"""
    target = directory / "index.html"
    target.write_text(text + "".join(rows))
    target.chmod(0o600)


def verify_diagnostics(run):
    public = playback(run)
    record = public["result"].get("diagnostics", {})
    if record.get("status") in (None, "disabled", "excluded"):
        raise LabError("This run has no retained private browser diagnostics")
    directory = evidence_path(Path(run), "diagnostics.private")
    manifest_file = evidence_path(directory, "manifest.json")
    index = evidence_path(directory, "index.html")
    if file_digest(manifest_file) != record.get("manifest_sha256") \
            or file_digest(index) != record.get("index_sha256"):
        raise LabError("Private diagnostic manifest or index integrity check failed")
    manifest = json.loads(manifest_file.read_text())
    snapshots = {}
    for event in public["events"]:
        if event["kind"] != "snapshot":
            continue
        # Pair media with the emitted observation, not the recorder's envelope.
        # Playback has already validated the referenced payload and its hash.
        row = json.loads(evidence_path(Path(run), event["artifacts"][0]["path"]).read_text()) \
            if event.get("event_schema_version") == 2 else event
        if "generation" in row:
            # A generation can also carry continued slices or refused actions.
            observation = {key: value for key, value in row.items() if key not in ("sequence", "kind")}
            snapshots.setdefault(row["generation"], {})[digest(observation)] = observation
    for row in manifest["captures"]:
        expected = snapshots.get(row["generation"], {}).get(row["observation_sha256"])
        source = artifact(directory, row["observation"], "json")
        if expected is None or json.loads(source.read_text()) != expected:
            raise LabError("Diagnostic observation differs from the emitted snapshot")
        if row["status"] == "captured":
            artifact(directory, row, "png")
    if manifest["trace"]["status"] == "captured":
        artifact(directory, manifest["trace"], "zip")
    return directory, manifest, public["result"]


def diagnostic_command(root, run, trace=False):
    directory, manifest, result = verify_diagnostics(run)
    if not trace:
        return {"status": manifest["status"], "index": str(directory / "index.html"),
                "audience": "developer-only", "export": "excluded"}
    if manifest["trace"]["status"] != "captured":
        raise LabError("This run has no finalized trace")
    expected = result["run_lock"]["browser_backend"]["playwright_version"]
    # Resolve only workspace packages; never use npx (which may install remotely).
    project = Path(root) / "tooling/payment-ui"
    for relative in ("node_modules/@playwright/mcp/node_modules/playwright", "node_modules/playwright"):
        package = evidence_path(project, relative)
        if (package / "package.json").is_file() \
                and json.loads((package / "package.json").read_text())["version"] == expected:
            script = evidence_path(package, "cli.js")
            if script.is_file():
                return {"status": manifest["status"], "audience": "developer-only",
                    "argv": ["node", str(script), "show-trace",
                             str(artifact(directory, manifest["trace"], "zip")),
                             "--host", "127.0.0.1", "--port", "0"]}
    raise LabError("Install the locked workspace Playwright version before viewing this trace")
