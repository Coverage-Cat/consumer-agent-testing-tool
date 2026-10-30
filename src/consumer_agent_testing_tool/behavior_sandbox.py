"""Bounded local orchestration, with durable sends and a separate public view."""
import asyncio
import fcntl
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .behavior_clock import WallClock
from .behavior_observations import Assessment, BehaviorCase, Family
from .config import Strict
from .errors import LabError
from .serialization import digest


class BatchCase(BehaviorCase):
    capabilities: list[str] = Field(default_factory=list)


class BatchSpec(Strict):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1)
    ends_at: str
    cases: list[BatchCase] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded(self):
        if self.schema_version != 1 or datetime.fromisoformat(self.ends_at).tzinfo is None:
            raise ValueError("Batch requires version 1 and an end time with an offset")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Batch case IDs must be distinct")
        return self


class SandboxRunner:
    """One active writer. Adapters own fixture-specific transport and observation.

    Each case authorizes exactly one send. An uncertain send can only continue
    after an independent receipt; the runner never retries it. Adapters must not
    implement hidden retries or interpret transport success as service success.
    """
    def __init__(self, path, spec, *, clock=None, availability=None):
        self.path, self.spec = Path(path), spec
        self.clock, self.availability = clock or WallClock(), availability
        self.lock = self.path.with_suffix(".writer.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise LabError("This batch already has an active writer", code=3) from None
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS meta (
                    id INTEGER PRIMARY KEY CHECK(id=1), spec TEXT, closed INTEGER, paused INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, document TEXT, status TEXT, receipt TEXT,
                    assessment TEXT, sent INTEGER DEFAULT 0, reconciliation TEXT, reason TEXT);
            """)
            document = spec.model_dump_json()
            existing = self.db.execute("SELECT spec FROM meta").fetchone()
            if existing and existing["spec"] != document:
                raise LabError("A saved batch cannot change its authorized scope", code=3)
            if not existing:
                with self.db:
                    self.db.execute("INSERT INTO meta(id,spec,closed) VALUES(1,?,0)", (document,))
                    self.db.executemany("INSERT INTO cases(id,document,status) VALUES(?,?,'new')",
                                        [(case.id, case.model_dump_json()) for case in spec.cases])
            self.path.chmod(0o600)
            remaining = (datetime.fromisoformat(spec.ends_at) - self.clock.utc()).total_seconds()
            self.deadline = self.clock.monotonic() + max(0, remaining)
        except BaseException:
            self.__exit__()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()
        self.lock.close()

    def _gate(self):
        meta = self.db.execute("SELECT closed,paused FROM meta").fetchone()
        if meta["closed"]:
            raise LabError("Batch scope is closed", code=3)
        if meta["paused"]:
            raise LabError("Observation was interrupted; create a new bounded batch", code=3)
        if self.clock.monotonic() >= self.deadline or self.clock.utc() >= datetime.fromisoformat(self.spec.ends_at):
            self.close_scope()
            raise LabError("Batch deadline reached; scope is closed", code=3)
        if self.availability is not None and not self.availability.release_allowed:
            if self.availability.status == "interrupted":
                with self.db:
                    self.db.execute("UPDATE meta SET paused=1")
                raise LabError("Observation window interrupted; releases are paused", code=3)
            raise LabError("Observation window is not ready or has ended", code=3)

    async def _bounded(self, awaitable):
        remaining = max(0, self.deadline - self.clock.monotonic())
        async with asyncio.timeout(remaining):
            return await awaitable

    def _row(self, identifier):
        row = self.db.execute("SELECT * FROM cases WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LabError("Case is outside the authorized batch", code=3)
        return row

    def _status(self, identifier, status, **fields):
        columns = {"receipt", "assessment", "sent", "reconciliation", "reason"}
        if fields.keys() - columns:
            raise ValueError("Unknown case field")
        with self.db:
            self.db.execute("UPDATE cases SET status=?" + "".join(f",{key}=?" for key in fields) + " WHERE id=?",
                            [status, *fields.values(), identifier])

    async def execute(self, identifier, adapter):
        row = self._row(identifier)
        case = BatchCase.model_validate_json(row["document"])
        if row["status"] == "closing":
            await self._cleanup(case, adapter)
            self._status(identifier, "completed")
            return
        if row["status"] == "preparing":
            self._status(identifier, "blocked", reason="preparation-interrupted")
            await self._cleanup(case, adapter)
            return
        if not row["sent"]:
            self._gate()
        if row["status"] in ("completed", "blocked", "forgone"):
            return
        if row["status"] == "sending":
            raise LabError("Uncertain work: reconcile before continuing this case", code=3)
        if not set(case.capabilities) <= set(adapter.capabilities):
            self._status(identifier, "blocked")
            return
        try:
            if row["status"] == "new":
                self._status(identifier, "preparing")
                await self._bounded(adapter.provision(case))
                if await self._bounded(adapter.ready(case)) is not True:
                    self._status(identifier, "blocked", reason="prerequisite-unavailable")
                    return
                self._gate()
                # Commit before calling out. A crash leaves an uncertain send.
                self._status(identifier, "sending", sent=1)
                message_id = digest({"batch": self.spec.id, "case": identifier})
                receipt = await self._bounded(adapter.send(message_id, case))
                if not isinstance(receipt, str) or not receipt:
                    raise LabError("Dispatch returned no receipt; reconcile before continuing", code=3)
                self._status(identifier, "observing", receipt=receipt)
            row = self._row(identifier)
            async with asyncio.timeout(30):
                observed = await adapter.observe(case, row["receipt"])
            paused = self.db.execute("SELECT paused FROM meta").fetchone()[0]
            if paused or self.availability is not None and self.availability.status != "observed-healthy":
                infrastructure = "interrupted" if paused else self.availability.status
                observed = {**observed, "infrastructure": infrastructure, "verdict": "inconclusive"}
            assessment = Assessment.model_validate(observed)
            self._status(identifier, "closing", assessment=assessment.model_dump_json())
        except BaseException as error:
            if not self._row(identifier)["sent"]:
                self._status(identifier, "blocked", reason=f"preparation-{type(error).__name__}")
            raise
        finally:
            await self._cleanup(case, adapter)
        self._status(identifier, "completed")

    async def _cleanup(self, case, adapter):
        # A persisted closing state can finish after the send deadline.
        async with asyncio.timeout(10):
            await adapter.close(case)

    def reconcile(self, identifier, *, receipt, evidence):
        row = self._row(identifier)
        if row["status"] != "sending" or not receipt or not evidence:
            raise LabError("An uncertain send needs an independent delivery receipt", code=3)
        self._status(identifier, "observing", receipt=receipt, reconciliation=evidence)

    def close_scope(self):
        with self.db:
            self.db.execute("UPDATE meta SET closed=1")
            self.db.execute("UPDATE cases SET status='forgone' WHERE status='new'")

    def snapshot(self):
        return _snapshot(self.db)


def _snapshot(db):
    meta = db.execute("SELECT * FROM meta").fetchone()
    spec = json.loads(meta["spec"])
    rows = [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY rowid")]
    return {"schema_version": 1, "closed": bool(meta["closed"]), "paused": bool(meta["paused"]),
            "exercise_count": len(rows),
            "message_limit": len(spec["cases"]), "message_count": sum(row["sent"] for row in rows),
            "cases": [{"family": json.loads(row["document"])["family"], "id": row["id"],
                       "status": row["status"], "reason": row.get("reason"),
                       "assessment": json.loads(row["assessment"])
                       if row["assessment"] else None} for row in rows]}


def inspect_batch(path):
    """Read-only: no writer, schema creation, export or checkpoint side effect."""
    path = Path(path).resolve()
    if not path.is_file():
        raise LabError("Batch file does not exist")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return _snapshot(db)
    finally:
        db.close()


def public_summary(rows):
    """Allowlisted aggregates only: no IDs, free text, timestamps or paths."""
    from typing import get_args
    families = {}
    for row in rows:
        family = row["family"]
        if family not in get_args(Family):
            raise ValueError("Unknown public behavior family")
        counts = families.setdefault(family, {"family": family, "observed": 0, "blocked": 0, "forgone": 0,
                                               "supported": 0, "contradicted": 0, "inconclusive": 0, "incomplete": 0})
        if row.get("assessment") is None:
            status = row.get("status")
            if status in ("new", "preparing", "sending", "observing", "closing"):
                counts["incomplete"] += 1
                counts["inconclusive"] += 1
                continue
            if status not in ("blocked", "forgone"):
                raise ValueError("Missing assessment has no known disposition")
            counts[status] += 1
            counts["inconclusive"] += 1
            continue
        assessment = Assessment.model_validate(row["assessment"])
        counts[assessment.disposition] += 1
        counts[assessment.verdict] += 1
    return {"schema_version": 1, "kind": "sandbox-research-summary",
            "method": "Isolated service fixtures and independent outcome observations.",
            "limits": "Exploratory coverage; no claim about private implementation or general reliability.",
            "families": [families[key] for key in sorted(families)]}


class ConnectorScriptAdapter:
    """Compose the existing runner with reviewed, loopback-only scripted cases."""
    capabilities = frozenset({"scripted", "connector"})

    def __init__(self, root, executions):
        self.root, self.executions, self.loaded = Path(root).resolve(), executions, {}

    async def provision(self, case):
        from .connector_scenario import is_loopback, load_scenario
        from .surface import inside
        entry = self.executions[case.id]
        loaded = load_scenario(inside(self.root, entry["scenario"]), variables=entry.get("variables"))
        # The convenience adapter deliberately supports a narrow local surface.
        # Other adapters must supply their own bounded authorization and gates.
        if set(loaded.scenario.extensions) - {"clock", "behavior", "content_trust"}:
            raise LabError("The local batch adapter accepts clock, behavior and content_trust extensions only")
        document = loaded.scenario.model_dump(mode="json")

        def urls(value):
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from urls(item)
            elif isinstance(value, list):
                for item in value:
                    yield from urls(item)
        if any(not is_loopback(url) for url in urls(document)):
            raise LabError("The local batch adapter accepts loopback URLs only")
        self.loaded[case.id] = loaded

    async def ready(self, case):
        import httpx
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            for connector in self.loaded[case.id].scenario.connectors:
                try:
                    reply = await client.get(connector.endpoint)
                    if reply.status_code >= 500:
                        return False
                except httpx.HTTPError:
                    return False
        return True

    async def send(self, message_id, case):
        from .connector_run import ScriptedDriver, run_trials
        from .serialization import load
        from .surface import inside
        script = load(inside(self.root, self.executions[case.id]["script"]))
        _, runs, _ = await run_trials(self.loaded[case.id], driver=ScriptedDriver(script), root=self.root)
        return str((runs[0].path / "result.json").relative_to(self.root))

    async def observe(self, case, receipt):
        from .serialization import load
        from .surface import inside
        result = load(inside(self.root, receipt))
        checks = [row for row in result.get("checks", []) if row.get("required", True)]
        decisive = checks and all(row["status"] in ("passed", "failed") for row in checks)
        completed = result.get("status") == "completed"
        return Assessment(
            completion="completed" if completed else "incomplete",
            disposition="observed" if decisive else "blocked",
            exposure="unknown", infrastructure="observed-healthy" if decisive else "unknown",
            # Case exposure/control receipts are not inferred from a successful
            # connector run. A collector must bind them for a decisive verdict.
            verdict="inconclusive", evidence=[receipt]).model_dump()

    async def close(self, case):
        # connector-run owns and closes its client/process resources.
        return None


def _main(argv=None):
    import argparse

    from .serialization import load
    parser = argparse.ArgumentParser(description="Run bounded local scripted behavior cases.")
    parser.add_argument("command", choices=["run", "inspect", "summary"])
    parser.add_argument("state", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.manifest is None:
            parser.error("run requires --manifest")
        document = load(args.manifest)
        spec = BatchSpec.model_validate(document["batch"])
        adapter = ConnectorScriptAdapter(args.manifest.resolve().parent, document["executions"])

        async def run():
            with SandboxRunner(args.state, spec) as runner:
                for case in spec.cases:
                    await runner.execute(case.id, adapter)
                runner.close_scope()
                return runner.snapshot()
        result = asyncio.run(run())
    else:
        result = inspect_batch(args.state)
    if args.command == "summary":
        result = public_summary(result["cases"])
    code = 0
    if args.command == "run":
        assessments = [row["assessment"] for row in result["cases"]]
        if any(row and row["verdict"] == "contradicted" for row in assessments):
            code = 1
        elif any(not row or row["verdict"] == "inconclusive" for row in assessments):
            code = 3
        result["exit_code"] = code
    print(json.dumps(result, indent=2))
    return code


def main(argv=None):
    import sys

    from pydantic import ValidationError
    try:
        return _main(argv)
    except LabError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except (ValidationError, ValueError, KeyError, OSError, sqlite3.Error) as error:
        print(f"Behavior runner setup or storage error: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
