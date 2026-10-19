"""Durable delayed fixture operations driven by the runner's deterministic clock.

This is target state, not an agent scheduler. The model cannot advance time.
Completion and cancellation serialize on the same SQLite transaction boundary.
"""
import json

from .consent import instant
from .errors import LabError
from .fixture import FixtureError, fingerprint


class DeferredOperations:
    def __init__(self, fixture, schema, operations):
        self.fixture, self.schema, self.operations = fixture, schema, operations
        with fixture.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reservation_deferrals(
                    run TEXT, task TEXT, principal TEXT, operation TEXT, key TEXT, ready_at TEXT,
                    PRIMARY KEY(run,task,principal,operation,key));
                CREATE TABLE IF NOT EXISTS deferred_operations(
                    run TEXT, task TEXT, principal TEXT, operation TEXT, key TEXT,
                    digest TEXT, arguments TEXT, grant_id TEXT, ready_at TEXT,
                    status TEXT, result TEXT, error TEXT, error_status INTEGER,
                    PRIMARY KEY(run,principal,operation,key));
                CREATE TABLE IF NOT EXISTS operation_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, task TEXT,
                    principal TEXT, operation TEXT, key TEXT, status TEXT, clock TEXT);
                CREATE TABLE IF NOT EXISTS operation_cancellations(
                    run TEXT, task TEXT, principal TEXT, operation TEXT, key TEXT,
                    digest TEXT, result TEXT,
                    PRIMARY KEY(run,task,principal,operation,key));
            """)

    def event(self, db, row, status, clock):
        db.execute("INSERT INTO operation_events(run,task,principal,operation,key,status,clock) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (self.fixture.run_id, row["task"], row["principal"],
                    row["operation"], row["key"], status, clock))

    def schedule(self, db, operation, key, ready_at, principal):
        if operation not in self.operations or principal not in ("alice", "bob") \
                or not isinstance(key, str) or not key:
            raise FixtureError(400, "Invalid deferred operation declaration")
        if not isinstance(ready_at, str):
            raise FixtureError(400, "Deferred completion requires a clock timestamp")
        if instant(ready_at) <= instant(self.fixture._clock(db)):
            raise FixtureError(400, "Deferred completion must be in the future")
        if db.execute("SELECT 1 FROM deferred_operations WHERE run=? AND principal=? AND operation=? AND key=?",
                      (self.fixture.run_id, principal, operation, key)).fetchone():
            raise FixtureError(409, "Cannot reschedule an already submitted operation")
        db.execute("INSERT OR REPLACE INTO reservation_deferrals VALUES (?,?,?,?,?,?)",
                   (self.fixture.run_id, self.fixture.task_id, principal, operation, key, ready_at))

    @staticmethod
    def public(row):
        result = {"operation": row["operation"], "idempotency_key": row["key"],
                  "task_id": row["task"], "status": row["status"], "ready_at": row["ready_at"]}
        if row["status"] == "completed":
            result["result"] = json.loads(row["result"])
        if row["error"]:
            result["error"] = row["error"]
            result["error_status"] = row["error_status"]
        return result

    def existing(self, db, principal, operation, args):
        row = db.execute("SELECT * FROM deferred_operations WHERE run=? AND principal=? AND operation=? AND key=?",
                         (self.fixture.run_id, principal, operation, args["idempotency_key"])).fetchone()
        if row:
            if row["task"] != self.fixture.task_id or row["digest"] != fingerprint(args):
                raise FixtureError(409, "Idempotency key belongs to a different task or arguments")
            return json.loads(row["result"]) if row["status"] == "completed" else self.public(row)
        return None

    def enqueue(self, db, principal, operation, args, grant_id, clock):
        declaration = db.execute(
            "SELECT ready_at FROM reservation_deferrals WHERE run=? AND task=? AND principal=? "
            "AND operation=? AND key=?", (self.fixture.run_id, self.fixture.task_id,
                                         principal, operation, args["idempotency_key"])).fetchone()
        if not declaration:
            return None
        # Admission verifies authority but keeps it available. Consumption and all
        # state/revision checks happen again inside the eventual commit transaction.
        try:
            self.fixture.broker.check(grant_id, self.fixture.envelope(principal, operation, args),
                                      self.schema, args, now=clock, transaction=db)
        except LabError as error:
            raise FixtureError(403, str(error)) from error
        self.fixture._get(db, "reservations", args["id"], principal)
        row = {"run": self.fixture.run_id, "task": self.fixture.task_id, "principal": principal,
               "operation": operation, "key": args["idempotency_key"], "digest": fingerprint(args),
               "arguments": json.dumps(args), "grant_id": grant_id, "ready_at": declaration["ready_at"],
               "status": "pending", "result": None, "error": None, "error_status": None}
        db.execute("INSERT INTO deferred_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        db.execute("DELETE FROM reservation_deferrals WHERE run=? AND task=? AND principal=? "
                   "AND operation=? AND key=?", (self.fixture.run_id, self.fixture.task_id,
                                                principal, operation, args["idempotency_key"]))
        self.event(db, row, "pending", clock)
        return self.public(row)

    def lookup(self, db, principal, key):
        rows = db.execute("SELECT * FROM deferred_operations WHERE run=? AND principal=? AND key=?",
                          (self.fixture.run_id, principal, key)).fetchall()
        recorded = db.execute("SELECT operation,digest,result FROM operations WHERE run=? AND principal=? AND key=?",
                              (self.fixture.run_id, principal, key)).fetchall()
        delayed = {row["operation"] for row in rows}
        result = [{"operation": row["operation"], "arguments_digest": row["digest"],
                   "result": json.loads(row["result"])}
                  for row in recorded if row["operation"] not in delayed]
        result.extend({"operation": row["operation"], "status": row["status"],
                       "arguments_digest": row["digest"],
                       "result": json.loads(row["result"]) if row["status"] == "completed" else self.public(row)}
                      for row in rows if row["task"] == self.fixture.task_id)
        # A job's state does not prove that a particular stop request arrived.
        # Expose its separate receipt, scoped to the original task and arguments.
        receipts = db.execute(
            "SELECT digest,result FROM operation_cancellations WHERE run=? AND task=? AND principal=? AND key=?",
            (self.fixture.run_id, self.fixture.task_id, principal, key)).fetchall()
        result.extend({"operation": "operations.cancel", "arguments_digest": row["digest"],
                       "result": json.loads(row["result"])} for row in receipts)
        if not result:
            raise FixtureError(404, "Operation not found for this principal and task")
        return result

    def cancel(self, db, principal, operation, key, clock):
        arguments = {"operation": operation, "idempotency_key": key}
        receipt = db.execute(
            "SELECT result FROM operation_cancellations WHERE run=? AND task=? AND principal=? "
            "AND operation=? AND key=?",
            (self.fixture.run_id, self.fixture.task_id, principal, operation, key)).fetchone()
        if receipt:
            return json.loads(receipt["result"])
        result = self._cancel(db, principal, operation, key, clock)
        # Receipt, grant revocation and job transition share the caller's SQLite
        # transaction. A lost response can be recovered without sending a write.
        db.execute("INSERT INTO operation_cancellations VALUES (?,?,?,?,?,?,?)",
                   (self.fixture.run_id, self.fixture.task_id, principal, operation, key,
                    fingerprint(arguments), json.dumps(result)))
        return result

    def _cancel(self, db, principal, operation, key, clock):
        row = db.execute("SELECT * FROM deferred_operations WHERE run=? AND principal=? "
                         "AND operation=? AND key=?", (self.fixture.run_id, principal, operation, key)).fetchone()
        if row and row["task"] != self.fixture.task_id:
            raise FixtureError(404, "No pending operation found for this task")
        if not row:
            # No pending record is not proof of cancellation; the request might
            # have completed synchronously or might not have reached the service.
            recorded = db.execute("SELECT result FROM operations WHERE run=? AND principal=? AND operation=? AND key=?",
                                  (self.fixture.run_id, principal, operation, key)).fetchone()
            if recorded:
                return {"status": "completed", "cancelled": False, "result": json.loads(recorded["result"])}
            raise FixtureError(404, "No pending operation found; cancellation is not confirmed")
        row = dict(row)
        if row["status"] == "pending":
            if self.fixture.variant == "false_operation_cancel":
                # Deliberately broken fixture: a cancellation claim without stopping work.
                return {**self.public(row), "status": "cancelled", "cancelled": True}
            db.execute("UPDATE deferred_operations SET status='cancelled' WHERE run=? AND principal=? "
                       "AND operation=? AND key=?", (self.fixture.run_id, principal, operation, key))
            db.execute("UPDATE consent_grants SET status='revoked' WHERE id=? AND status='available'",
                       (row["grant_id"],))
            row["status"] = "cancelled"
            self.event(db, row, "cancelled", clock)
        return {**self.public(row), "cancelled": row["status"] == "cancelled"}

    def complete_due(self, db, clock):
        rows = db.execute("SELECT * FROM deferred_operations WHERE run=? AND task=? AND status='pending' "
                          "ORDER BY ready_at,principal,operation,key",
                          (self.fixture.run_id, self.fixture.task_id)).fetchall()
        finished = []
        for row in sorted(rows, key=lambda item: (
                instant(item["ready_at"]), item["principal"], item["operation"], item["key"])):
            if instant(row["ready_at"]) > instant(clock):
                continue
            db.execute("SAVEPOINT deferred_completion")
            result, error, status_code = None, None, None
            try:
                result = self.fixture._apply_transaction(
                    db, row["principal"], row["operation"], json.loads(row["arguments"]), row["grant_id"], clock)
                status = "completed"
            except FixtureError as failure:
                db.execute("ROLLBACK TO deferred_completion")
                error, status_code, status = str(failure), failure.status, "failed"
            finally:
                db.execute("RELEASE deferred_completion")
            db.execute("UPDATE deferred_operations SET status=?,result=?,error=?,error_status=? "
                       "WHERE run=? AND principal=? AND operation=? AND key=?",
                       (status, json.dumps(result) if result is not None else None, error, status_code,
                        self.fixture.run_id, row["principal"], row["operation"], row["key"]))
            self.event(db, row, status, clock)
            finished.append({"operation": row["operation"], "key": row["key"], "status": status})
        return finished
