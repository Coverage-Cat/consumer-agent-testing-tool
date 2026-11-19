"""Bounded concurrent CI execution. The coordinator owns services, workers, and public reports."""
import asyncio
import copy
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, requires, version
from pathlib import Path

from jsonschema import Draft202012Validator

from .ci_outputs import SUMMARY_SCHEMA, check_reason
from .ci_results import (
    CASE_CODE,
    CODE_STATUS,
    CaseResult,
    CheckResult,
    Diagnostic,
    Outcome,
    judge,
    make_report,
    not_run,
    write_failed,
    write_report,
)
from .ci_suite import PlanError, read_document
from .ci_validation import script_urls
from .connector_scenario import substitute
from .errors import LabError
from .serialization import digest, file_digest, implementation_lock
from .service_start import Service, free_port, started


def harness_identity(root):
    package = Path(__file__).resolve().parent
    lock = implementation_lock(package.parents[1])
    bundled = package / "data/locks/uv.lock"
    if "uv.lock" not in lock["dependency_locks"] and bundled.is_file():
        lock["dependency_locks"]["uv.lock"] = file_digest(bundled)
    installed = {}
    for requirement in requires("consumer-agent-testing-tool") or []:
        if "extra" in requirement.partition(";")[2]:
            continue
        name = re.match(r"[A-Za-z0-9_.-]+", requirement)[0]
        try:
            installed[name] = version(name)
        except PackageNotFoundError:
            installed[name] = "not-installed"
    return {"version": version("consumer-agent-testing-tool"), "implementation": lock["implementation"],
            "dependency_locks": digest(lock["dependency_locks"]),
            "runtime": digest({"python": platform.python_version(), "os": platform.system(),
                               "architecture": platform.machine(), "packages": installed})}


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)


def snapshot(case, workspace, port, namespace):
    """Freeze validated inputs before launch. Workers never reread mutable scenario or script files."""
    workspace.mkdir(parents=True, mode=0o700)
    variables = {"PORT": str(port), "CASE": namespace} if port is not None else {}
    document = substitute(copy.deepcopy(case.scenario_document), variables)
    for connector in case.loaded.scenario.connectors:
        if connector.doc:
            relative = f"connector-docs/{connector.name}.md"
            next(row for row in document["connectors"] if row["name"] == connector.name)["doc"] = relative
            path = workspace / "input" / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(case.loaded.docs[connector.name])
    for extension, files in case.extension_documents.items():
        relative = f"extension-docs/{extension}"
        document["extensions"][extension]["docs"] = relative
        for name, text in files.items():
            path = workspace / "input" / relative / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(text)
    private_json(workspace / "input/scenario.json", document)
    if case.script is not None:
        private_json(workspace / "input/calls.json", script_urls(copy.deepcopy(case.script), variables))
    request = {"kind": case.spec.kind, "treatment": case.public.treatment.model_dump(),
               "port": port, "namespace": namespace}
    if case.manifest is not None:
        private_json(workspace / "input/manifest.json", case.manifest)
        connector = next(row for row in document["connectors"] if row["name"] == case.readiness_connector)
        private_json(workspace / "input/auth.json", {"connectors": [{"auth": connector.get("auth", {"mode": "none"})}]})
        request["endpoint"] = connector["endpoint"]
    private_json(workspace / "request.json", request)


async def wait_process(process):
    while process.poll() is None:
        await asyncio.sleep(0.05)
    return process.returncode


@asynccontextmanager
async def worker(workspace, environment):
    log = workspace / "worker.log"
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        process = subprocess.Popen(  # noqa: ASYNC220 — synchronous creation closes the cancellation ownership gap
            [sys.executable, "-m", "consumer_agent_testing_tool.ci_worker", "request.json"], cwd=workspace,
            env={**os.environ, **environment}, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True)
    owned = Service(process, [], 0, log, "")
    try:
        yield process
    finally:
        await owned.astop()


def observed_result(case, workspace, return_code, redactor):
    """Only verified, sealed public evidence can supply check details; worker output is never copied."""
    from .replay import playback

    summary = read_document(workspace / "summary.json", location="worker-summary")
    try:
        Draft202012Validator(SUMMARY_SCHEMA).validate(summary)
        if summary["exit_code"] != return_code or len(summary["trials"]) != 1:
            raise ValueError
        trial = summary["trials"][0]
        where = Path(trial["run"]).resolve()
        if where.parent != workspace / "runs" or where.is_symlink():
            raise ValueError
        if (where / "result.json").stat().st_size > 4 * 1024 * 1024:
            raise ValueError
        record = playback(where)
        result = record["result"]
        expected_kind = "connector-run" if case.spec.kind == "connector" else "surrogate-readiness-check"
        if record["integrity"] != "verified" or result["kind"] != expected_kind \
                or result["emulation_target"] != case.public.treatment.emulation_target \
                or result.get("reporting_status") != "completed" or trial["exit_code"] != return_code:
            raise ValueError
        if case.spec.kind == "connector" and (result["driver"] != "scripted" or result.get("agent_runs")
                                              or result.get("model_preflight_run")):
            raise ValueError
        if case.spec.kind == "connector" and result["scenario_id"] != case.loaded.scenario.id:
            raise ValueError
        checks = [CheckResult(id=redactor.text(row["id"]), status=row["status"], required=row.get("required", True),
                              detail=redactor.text(check_reason(row))[:2000],
                              failure_class=redactor.text(row["class"]) if row.get("class") else None)
                  for row in result["checks"]]
        if not checks and return_code == 0:
            raise ValueError
        if return_code != 2 and case.spec.kind == "connector" and not {
                row.id for row in case.loaded.scenario.checks if row.required
        }.issubset({row.id for row in checks}):
            raise ValueError
        outcome = Outcome(exit_code=return_code, status=CODE_STATUS[return_code],
                          failure_stage=redactor.text(result["failure_stage"]) if result.get("failure_stage") else None,
                          failure_class=redactor.text(result["failure_class"]) if result.get("failure_class") else None,
                          reason=redactor.text(result["detail"])[:2000] if result.get("detail") else None)
        status = judge(outcome, checks, case.public.expect)
        diagnostics = []
        if status not in ("passed", "expected-failure"):
            uncertain = any(row.id.startswith("reconcile.") for row in checks)
            diagnostics.append(Diagnostic(
                code="case.expectation" if case.public.expect.exit_code else "case.finding",
                message="Observed outcome differs from the declared expectation." if case.public.expect.exit_code else
                        "Inspect the recorded checks and underlying outcome.",
                action="reconcile-before-rerun" if uncertain else "inspect-checks"))
        return CaseResult(id=case.public.id, definition_sha256=case.public.definition_sha256, status=status,
                          exit_code=CASE_CODE[status], seconds=0, attempts=1, outcome=outcome, checks=checks,
                          compatibility_pass=bool(result.get("compatibility_pass") and status == "passed"),
                          run_id=where.name, diagnostics=diagnostics)
    except Exception:  # noqa: BLE001 — malformed evidence must not echo raw input into a public report
        raise PlanError("worker.evidence", "case",
                        "The worker's sealed evidence or summary is missing, invalid, or inconsistent.") from None


def stopped_result(case, status, code, message, *, attempted, seconds, action):
    return CaseResult(id=case.public.id, definition_sha256=case.public.definition_sha256,
                      status=status, exit_code=CASE_CODE[status], seconds=round(seconds, 6), attempts=int(attempted),
                      outcome=None, checks=[], compatibility_pass=False,
                      diagnostics=[Diagnostic(code=code, message=message, action=action)])


class Coordinator:
    def __init__(self, prepared, *, public, private, jobs=1, batch_id=None, build_id=None, handle_signals=False):
        from .ci_history import validate_binding

        validate_binding(prepared, batch_id, build_id)
        if type(jobs) is not int or not 1 <= jobs <= 64 or jobs > 1 and any(
                prepared.cases[key].public.state == "shared" for key in prepared.plan.scheduled):
            raise PlanError("execution.jobs", "jobs", "Use 1–64 workers and only one worker for shared state.")
        self.prepared, self.public, self.jobs = prepared, Path(public), jobs
        self.report_id = uuid.uuid4().hex
        self.batch_id, self.build_id = batch_id or self.report_id, build_id
        self.private = Path(private) / self.report_id
        self.rows = {key: not_run(prepared.cases[key].public) for key in prepared.plan.scheduled}
        self.harness = harness_identity(prepared.root)
        self.started_at = time.monotonic()
        self.ports = set()
        self.cancelled = False
        self.fatal, self.tasks, self.handle_signals = None, [], handle_signals

    def cancel(self):
        self.cancelled = True
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current and not task.done():
                task.cancel()

    def port(self):
        for _ in range(100):
            value = free_port()
            if value not in self.ports:
                self.ports.add(value)
                return value
        raise LabError("Cannot allocate another distinct loopback port")

    def report(self):
        return make_report(plan=self.prepared.plan, cases=list(self.rows.values()), harness=self.harness,
                           jobs=self.jobs, seconds=time.monotonic() - self.started_at, report_id=self.report_id,
                           batch_id=self.batch_id, build_id=self.build_id, fatal=self.fatal)

    def checkpoint(self):
        report = self.report()
        if self.fatal and self.fatal.code == "report.write":
            return report
        try:
            write_report(report, self.public)
        except OSError:
            self.fatal = write_failed(report).fatal
            self.cancel()
            return self.report()
        return report

    async def case(self, case):
        begin, attempted = time.monotonic(), False
        identifier = case.public.id
        workspace = self.private / identifier
        namespace = f"{self.report_id}-{digest(identifier)[:16]}"
        try:
            port = self.port() if case.service.command else None
            snapshot(case, workspace, port, namespace)
            environment = {"CATT_CASE_ID": namespace, "CATT_CASE_NAME": identifier, "CATT_RUN_ID": self.report_id}
            if port is not None:
                environment["PORT"] = str(port)

            async def execute():
                nonlocal attempted
                attempted = True
                async with worker(workspace, environment) as process:
                    code = await wait_process(process)
                return observed_result(case, workspace, code, self.prepared.redactor)

            async with asyncio.timeout(case.spec.timeout_seconds):
                if case.service.command:
                    command = shlex.join(part.replace("{case}", namespace) for part in case.service.command)
                    async with started(
                            command, port=port, output=workspace / "service",
                            ready_url=case.service.ready_url.replace("{case}", namespace),
                            timeout=case.service.startup_seconds, environment=environment,
                            handle_signals=False, async_cleanup=True):
                        result = await execute()
                else:
                    result = await execute()
        except TimeoutError:
            result = stopped_result(case, "blocked", "case.timeout",
                                    "Case deadline reached. Owned processes were stopped; the outcome may be incomplete.",
                                    attempted=attempted, seconds=0, action="reconcile-before-rerun")
        except asyncio.CancelledError:
            self.cancelled = True
            result = stopped_result(case, "cancelled", "case.cancelled",
                                    "Case interrupted. Owned processes were stopped; reconcile any possible writes.",
                                    attempted=attempted, seconds=0, action="reconcile-before-rerun")
        except Exception:  # noqa: BLE001 — service_start errors include raw log tails; keep them out of public CI
            result = stopped_result(case, "error", "case.infrastructure",
                                    "Service startup, worker execution, or evidence verification failed. Inspect local private logs.",
                                    attempted=attempted, seconds=0, action="inspect-private-evidence")
        result.seconds = round(time.monotonic() - begin, 6)
        self.rows[identifier] = result
        print(f"{identifier}: {result.status} ({result.seconds:.2f}s)", file=sys.stderr, flush=True)
        self.checkpoint()

    async def run(self):
        try:
            self.private.mkdir(parents=True, mode=0o700)
        except OSError:
            self.fatal = Diagnostic(code="suite.private", message="Cannot create private evidence storage; no cases ran.",
                                    action="inspect-private-evidence")
            return self.checkpoint()
        self.checkpoint()  # Interrupted or unfinished cases are already visible, never an empty green report.
        if self.fatal:
            return self.report()
        queue = asyncio.Queue()
        for identifier in self.prepared.plan.scheduled:
            queue.put_nowait(self.prepared.cases[identifier])

        async def consume():
            while not self.cancelled:
                try:
                    case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await self.case(case)

        tasks = [asyncio.create_task(consume()) for _ in range(min(self.jobs, queue.qsize()))]
        self.tasks = tasks
        loop = asyncio.get_running_loop()

        def cancel():
            if not self.cancelled:
                self.cancel()

        previous, handlers = {}, []
        for number in (signal.SIGTERM, signal.SIGINT) if self.handle_signals else ():
            try:
                previous[number] = signal.getsignal(number)
                loop.add_signal_handler(number, cancel)
                handlers.append(number)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        cancelled_by_caller = False
        try:
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                cancelled_by_caller = True
                self.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            except BaseException:
                self.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        finally:
            for number in handlers:
                loop.remove_signal_handler(number)
                signal.signal(number, previous[number])
        report = self.checkpoint()
        if cancelled_by_caller:
            raise asyncio.CancelledError
        return report


async def run_suite(prepared, *, public, private, jobs=1, batch_id=None, build_id=None, handle_signals=False):
    coordinator = Coordinator(prepared, public=public, private=private, jobs=jobs, batch_id=batch_id,
                              build_id=build_id, handle_signals=handle_signals)
    return await coordinator.run()
