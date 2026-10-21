"""CI outputs of connector-run and check: a JUnit XML report (--junit FILE) and a machine-readable summary
(--summary-json FILE, or - for stdout).

Both are written from sealed, redacted evidence only: each trial's result.json (its checks, status, detail
and failure fields as redaction left them), never the run's in-memory checks or private values.

The summary (schema_version 1; JSON Schema: data/schemas/summary-v1.schema.json, SUMMARY_SCHEMA here) is one
JSON object:

    schema_version  1
    kind            "consumer-agent-testing-tool-summary"
    command         "connector-run" or "check"
    suite           the check suite (check only; null for connector-run)
    scenario_id     the scenario id (connector-run) or the suite's id (check); null before it was read
    exit_code       the command's exit code (0 passed, 1 failed, 2 setup/harness/reporting error, 3 blocked)
    status          "completed", "failed", "blocked", or "error" (stopped before or between trials; see error)
    error           {"class", "detail"} when status is "error", else null
    trials          one row per trial (check: one row for its run), in order:
                    {"trial", "run" (the sealed run directory), "status", "exit_code", "failure_stage",
                     "failure_class", "reason", "checks": [{"id", "status", "required"}]}
    pass_hat_k      {"1": float, ..., "k": float} when trials > 1, else null
    summary_run     the trials summary run directory when trials > 1, else null
    treatment       {"projection", "exposure", "client_profile", "output_schema", "emulation_target"}
                    (null fields when unknown; older version 1 reports omit emulation_target)
    service         the --start service ({"command", "port", "pid", "log", "ready_url"}) or null

With --summary-json -, stdout carries this one document and nothing else: everything the command would
otherwise print on stdout goes to stderr."""
import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

from .errors import LabError, normalize_error
from .evidence import Redactor

SKIPPED = ("blocked", "observation-gap", "unsupported", "not-evaluated")
# Passing rows: muse-guidelines reports "no-finding" (never "passed") and non-blocking warning and info findings.
PASSING = ("passed", "no-finding", "warning", "info")
REASON = 500  # characters of a trial's reason in the summary
MESSAGE = 1000  # characters of a JUnit message attribute (the element text keeps up to TEXT)
TEXT = 20_000
# XML 1.0 forbids these characters even escaped; JUnit parsers reject the file.
INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
STATUSES = ["completed", "failed", "blocked", "error"]

_text = {"type": "string"}
_nullable = {"type": ["string", "null"]}
_code = {"type": "integer", "minimum": 0, "maximum": 3}


def _closed(properties, *, optional=()):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": [name for name in properties if name not in optional]}


SUMMARY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:consumer-agent-testing-tool:summary:v1",
    "title": "consumer-agent-testing-tool CI summary (--summary-json), version 1",
    **_closed({
        "schema_version": {"const": 1},
        "kind": {"const": "consumer-agent-testing-tool-summary"},
        "command": {"enum": ["connector-run", "check"]},
        "suite": _nullable,
        "scenario_id": _nullable,
        "exit_code": _code,
        "status": {"enum": STATUSES},
        "error": {"oneOf": [{"type": "null"}, _closed({"class": _text, "detail": _text})]},
        "trials": {"type": "array", "items": _closed({
            "trial": {"type": "integer", "minimum": 1},
            "run": _text,
            "status": _nullable,
            "exit_code": _code,
            "failure_stage": _nullable,
            "failure_class": _nullable,
            "reason": _nullable,
            "checks": {"type": "array", "items": _closed({
                "id": _text, "status": {"enum": [*PASSING, "failed", *SKIPPED]}, "required": {"type": "boolean"}})},
        })},
        "pass_hat_k": {"oneOf": [{"type": "null"}, {"type": "object", "minProperties": 1,
                                                    "propertyNames": {"pattern": "^[1-9][0-9]*$"},
                                                    "additionalProperties": {"type": "number", "minimum": 0,
                                                                             "maximum": 1}}]},
        "summary_run": _nullable,
        "treatment": _closed({"projection": _nullable, "exposure": _nullable, "client_profile": _nullable,
                              "output_schema": _nullable, "emulation_target": {"enum": ["muse", "instinct", None]}},
                             optional=("emulation_target",)),
        "service": {"oneOf": [{"type": "null"}, _closed({
            "command": {"type": "array", "items": _text}, "port": {"type": "integer", "minimum": 1,
                                                                  "maximum": 65535},
            "pid": {"type": "integer"}, "log": _text, "ready_url": _text})]},
    }),
}


def _clip(text, limit):
    text = INVALID_XML.sub("\ufffd", str(text))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def check_reason(check) -> str:
    """A check's reason as result.json holds it: its detail, or what it expected and saw (cli.report_outcome)."""
    detail = check.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    if "expected" in check or "actual" in check:
        return (f"expected {json.dumps(check.get('expected'), default=str)}, "
                f"actual {json.dumps(check.get('actual'), default=str)}")
    return "no detail recorded"


def sealed_result(path) -> dict:
    """The trial's sealed result.json, the only source of what these outputs say about it; for a check suite
    that writes a run manifest instead (contract, static), the manifest's redacted checks and status."""
    path = Path(path)
    try:
        if (path / "result.json").is_file():
            return json.loads((path / "result.json").read_text())
        manifest = json.loads((path / "manifest.json").read_text())
    except (OSError, ValueError):
        return {}
    from .cli import problems
    checks = [check for check in manifest.get("checks") or [] if isinstance(check, dict)]
    lead = next(iter(problems(checks)), None)
    return {"status": manifest.get("execution_status"), "checks": checks,
            "reporting_status": manifest.get("reporting_status"),
            "failure_class": lead.get("class", "unclassified") if lead else None,
            "failure_stage": "checks" if lead else None,
            "detail": f"{lead.get('id')}: {check_reason(lead)}" if lead else None}


class CiOutputs:
    """--junit and --summary-json for one command invocation; the command fills scenario_id, trials,
    treatment, service and summary as it learns them, and write() (or write_error()) emits both."""

    def __init__(self, args, command, root):
        from .connector_run import within
        self.command, self.suite = command, getattr(args, "suite", None) if command == "check" else None
        junit, summary = getattr(args, "junit", None), getattr(args, "summary_json", None)
        # CLI paths stay inside the workspace, as every other output does.
        self.junit = within(root, junit, "--junit") if junit is not None else None
        self.stdout = str(summary) == "-"
        self.summary = within(root, summary, "--summary-json") if summary is not None and not self.stdout else None
        self.scenario_id, self.trials, self.pass_hat_k, self.summary_run, self.service = None, [], None, None, None
        self.treatment = {"projection": None, "exposure": None, "client_profile": None, "output_schema": None,
                          "emulation_target": None}
        self.real_stdout, self.stopped = sys.stdout, None  # stopped: why no trial ran, when the command says

    @property
    def wanted(self) -> bool:
        return bool(self.junit or self.summary or self.stdout)

    def quiet_stdout(self):
        """With --summary-json -, everything else the command prints on stdout goes to stderr."""
        return contextlib.redirect_stdout(sys.stderr) if self.stdout else contextlib.nullcontext()

    def add_trial(self, path, exit_code):
        """One sealed trial (or check run), read back from its result.json."""
        result = sealed_result(path)
        checks = [check for check in result.get("checks") or [] if isinstance(check, dict)]
        reason = result.get("detail")
        self.trials.append({
            "trial": len(self.trials) + 1, "run": str(path), "status": result.get("status"),
            "exit_code": exit_code if exit_code in (0, 1, 2, 3) else 2,
            "failure_stage": result.get("failure_stage"), "failure_class": result.get("failure_class"),
            "reason": _clip(" ".join(str(reason).split()), REASON) if reason else None,
            "checks": [{"id": str(check.get("id")), "status": check.get("status"),
                        "required": check.get("required", True) is not False} for check in checks],
            "_result": result})

    async def around(self, run):
        """Await run() (the command) and write both outputs however it ends; an exception still propagates."""
        with self.quiet_stdout():
            try:
                code = await run()
            except (asyncio.CancelledError, KeyboardInterrupt):
                self.write_error(LabError("Interrupted; reconcile the service state before another run", code=3,
                                          category="interrupted"))
                raise
            except Exception as error:
                self.write_error(error)
                raise
        if not self.trials and code:
            # Nothing ran (a failed model preflight, say): never an empty, passing report.
            self.write_error(LabError(self.stopped or "The command stopped before any run was sealed", code=code))
        else:
            self.write(code)
        return code

    # Writing -----------------------------------------------------------------------------------
    def document(self, code, error=None) -> dict:
        status = "error" if error is not None else "completed" if code == 0 else "blocked" if code == 3 else "failed"
        return {"schema_version": 1, "kind": "consumer-agent-testing-tool-summary", "command": self.command,
                "suite": self.suite, "scenario_id": self.scenario_id, "exit_code": code if code in (0, 1, 2, 3) else 2,
                "status": status, "error": error,
                "trials": [{key: value for key, value in row.items() if key != "_result"} for row in self.trials],
                "pass_hat_k": self.pass_hat_k if len(self.trials) > 1 else None, "summary_run": self.summary_run,
                "treatment": self.treatment, "service": self.service}

    def write(self, code, error=None):
        if not self.wanted:
            return
        # Every value is already redacted: sealed result.json fields, run paths, and the redacted service command.
        document = self.document(code, error)
        if self.junit:
            _atomic(self.junit, self.junit_xml(error))
        if self.summary:
            _atomic(self.summary, json.dumps(document, indent=2) + "\n")
        if self.stdout:
            print(json.dumps(document, indent=2), file=self.real_stdout, flush=True)

    def write_error(self, failure):
        """The command stopped with an error (before, between or after trials): both outputs still say so."""
        failure = failure if isinstance(failure, LabError) else normalize_error(failure)
        detail = " ".join(Redactor.environment().text(str(failure)).split())
        self.write(failure.code if failure.code in (1, 2, 3) else 2,
                   {"class": failure.category, "detail": _clip(detail, TEXT)})

    def junit_xml(self, error=None) -> str:
        """One <testsuite> per trial (name: scenario id and trial number), one <testcase> per check of its
        result.json (classname: scenario id, name: check id): <failure> for a failed check, <error> for a failed
        execution or reporting outcome of a trial that exited 2 (and for any status it does not know), <skipped>
        for blocked, observation-gap, unsupported and not-evaluated checks; properties name the run directory, status and exit code."""
        scenario = self.scenario_id or self.suite or self.command
        root = ElementTree.Element("testsuites", name=f"consumer-agent-testing-tool {self.command}")
        totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for row in self.trials:
            result = row["_result"]
            name = f"{scenario} trial {row['trial']}" if self.command == "connector-run" else scenario
            suite = ElementTree.SubElement(root, "testsuite", name=name)
            properties = ElementTree.SubElement(suite, "properties")
            for key, value in (("run_directory", row["run"]), ("status", row["status"]),
                               ("exit_code", row["exit_code"]), ("failure_class", row["failure_class"]),
                               ("treatment", result.get("treatment")),
                               ("emulation_target", result.get("emulation_target"))):
                if value is not None:
                    ElementTree.SubElement(properties, "property", name=key, value=_clip(value, MESSAGE))
            checks = [check for check in result.get("checks") or [] if isinstance(check, dict)]
            if result.get("reporting_status") == "failed" or row["failure_class"] == "reporting":
                checks = [{"id": "reporting", "status": "failed", "class": "reporting",
                           "detail": row["reason"] or "the evidence failed reporting"}, *checks]
            if not any(check.get("id") == "execution" for check in checks) and self.command == "connector-run":
                # Every trial reports its execution outcome; a result without one never passes.
                checks.insert(0, {"id": "execution", "status": "failed", "class": row["failure_class"] or "reporting",
                                  "detail": row["reason"] or "the trial recorded no execution outcome"})
            counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            for check in checks:
                counts["tests"] += 1
                case = ElementTree.SubElement(suite, "testcase", classname=scenario, name=str(check.get("id")),
                                              time="0")
                status, reason = check.get("status"), check_reason(check)
                if status in PASSING:
                    if status in ("warning", "info"):  # A finding that does not fail the suite: shown, not counted.
                        ElementTree.SubElement(case, "system-out").text = _clip(f"{status}: {reason}", TEXT)
                    continue
                if status in SKIPPED:
                    counts["skipped"] += 1
                    optional = "" if check.get("required", True) is not False else " (not required)"
                    ElementTree.SubElement(case, "skipped", message=_clip(f"{status}{optional}: {reason}", MESSAGE))
                    continue
                # A trial-level outcome that is not an assertion (setup, harness, reporting) is an error.
                kind = "error" if check.get("id") in ("execution", "reporting") and row["exit_code"] == 2 \
                    else "failure"
                counts["errors" if kind == "error" else "failures"] += 1
                element = ElementTree.SubElement(case, kind, message=_clip(reason, MESSAGE),
                                                 type=str(check.get("class") or "unclassified"))
                element.text = _clip(reason, TEXT)
            for key, value in counts.items():
                suite.set(key, str(value))
                totals[key] += value
            seconds = (result.get("metrics") or {}).get("total_seconds")
            suite.set("time", f"{seconds:.3f}" if isinstance(seconds, (int, float)) else "0")
        if error is not None:
            suite = ElementTree.SubElement(root, "testsuite", name=f"{scenario} setup", tests="1", failures="0",
                                           errors="1", skipped="0", time="0")
            case = ElementTree.SubElement(suite, "testcase", classname=scenario, name="setup", time="0")
            element = ElementTree.SubElement(case, "error", message=_clip(error["detail"], MESSAGE),
                                             type=error["class"])
            element.text = _clip(error["detail"], TEXT)
            totals["tests"] += 1
            totals["errors"] += 1
        for key, value in totals.items():
            root.set(key, str(value))
        ElementTree.indent(root)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"


def _atomic(path, text):
    """Write beside the target and rename: a CI step never reads a half-written report."""
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)  # A CI artifact, read by other steps; it holds redacted evidence only.
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
