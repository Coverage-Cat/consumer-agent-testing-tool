"""Stateful reservation fixture. Public callers never receive oracle access.

This module intentionally uses only the standard library so the isolated images
need neither inference libraries nor credentials.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class FixtureError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class Fixture:
    def __init__(self, path: str | Path, run_id: str, variant: str = "good"):
        self.path = str(path)
        self.run_id = run_id
        self.variant = variant
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(run TEXT PRIMARY KEY, clock TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slots(
                  run TEXT, id TEXT, amount_minor INTEGER, currency TEXT,
                  available INTEGER, revision INTEGER, PRIMARY KEY(run,id));
                CREATE TABLE IF NOT EXISTS reservations(
                  run TEXT, id TEXT, principal TEXT, slot_id TEXT, contact TEXT,
                  state TEXT, amount_minor INTEGER, currency TEXT, revision INTEGER,
                  PRIMARY KEY(run,id));
                CREATE TABLE IF NOT EXISTS operations(
                  run TEXT, principal TEXT, operation TEXT, key TEXT, digest TEXT, result TEXT,
                  PRIMARY KEY(run,principal,operation,key));
                CREATE TABLE IF NOT EXISTS effects(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, principal TEXT,
                  operation TEXT, key TEXT, reservation_id TEXT, effect TEXT);
                CREATE TABLE IF NOT EXISTS requests(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, principal TEXT,
                  operation TEXT, arguments_digest TEXT);
                CREATE TABLE IF NOT EXISTS attempts(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, principal TEXT,
                  operation TEXT, arguments_digest TEXT, arguments_valid INTEGER, status INTEGER);
                CREATE TABLE IF NOT EXISTS protocol_events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, event TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reset(self, clock="2026-10-01T17:00:00Z"):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reset(db, clock)
        return {"seed": "one_available_slot", "clock": clock}

    def _reset(self, db, clock):
        for table in ("runs", "slots", "reservations", "operations", "effects", "requests",
                      "attempts", "protocol_events"):
            db.execute(f"DELETE FROM {table} WHERE run=?", (self.run_id,))
        db.execute("INSERT INTO runs VALUES (?,?)", (self.run_id, clock))
        db.executemany("INSERT INTO slots VALUES (?,?,?,?,?,?)", [
            (self.run_id, "afternoon", 12500, "USD", 1, 4),
            (self.run_id, "morning", 10000, "USD", 0, 1),
        ])

    def _draft_created(self, db, result):
        """Optional transactional-fixture metadata, in the same draft transaction."""

    def _authorize(self, db, principal, operation):
        """Optional authentication check inside the operation's transaction."""

    def _effect_recorded(self, db, sequence, principal, operation):
        """Optional independent authority record, atomic with the effect."""

    @staticmethod
    def _validate(operation, args):
        shapes = {
            "slots.list": (set(), set()),
            "slots.get": ({"id"}, {"id"}),
            "reservations.create": ({"slot_id", "contact", "idempotency_key"},
                                    {"slot_id", "contact", "idempotency_key"}),
            "reservations.get": ({"id"}, {"id"}),
            "reservations.revise": ({"id", "contact", "expected_revision", "idempotency_key"},
                                    {"id", "contact", "expected_revision", "idempotency_key"}),
            "operations.get": ({"idempotency_key"}, {"idempotency_key"}),
        }
        if operation not in shapes:
            raise FixtureError(404, "Unknown or unsupported operation")
        required, allowed = shapes[operation]
        if not isinstance(args, dict) or not required <= args.keys() or args.keys() - allowed:
            raise FixtureError(400, f"Required arguments: {sorted(required)}; no extra fields allowed")
        for key, value in args.items():
            if key == "expected_revision":
                if type(value) is not int or value < 1:
                    raise FixtureError(400, "expected_revision must be a positive integer")
            elif not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise FixtureError(400, f"{key} must be a nonempty string (maximum 1024 characters)")

    def dispatch(self, principal: str, operation: str, args: dict):
        valid, status = False, 500
        try:
            self._validate(operation, args)
            valid = True
            result = self._dispatch(principal, operation, args)
            status = 200
            return result
        except FixtureError as exc:
            status = exc.status
            raise
        finally:
            # Separate from the mutation transaction: rejected proposals and
            # rolled-back writes still remain visible to the independent oracle.
            with self.connect() as db:
                db.execute("INSERT INTO attempts(run,principal,operation,arguments_digest,"
                           "arguments_valid,status) VALUES (?,?,?,?,?,?)",
                           (self.run_id, principal, operation, fingerprint(args), int(valid), status))

    def _dispatch(self, principal: str, operation: str, args: dict):
        if principal not in ("alice", "bob"):
            raise FixtureError(401, "Unknown principal")
        self._validate(operation, args)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE run=?", (self.run_id,)).fetchone():
                raise FixtureError(503, "Service is not ready")
            self._authorize(db, principal, operation)
            db.execute("INSERT INTO requests(run,principal,operation,arguments_digest) VALUES (?,?,?,?)",
                       (self.run_id, principal, operation, fingerprint(args)))
            if self.variant == "transient_error" and operation == "reservations.create":
                prior_failure = db.execute("SELECT 1 FROM attempts WHERE run=? AND principal=? "
                    "AND operation=? AND status=429", (self.run_id, principal, operation)).fetchone()
                if not prior_failure:
                    raise FixtureError(429, "Rate limit reached")
            if operation == "slots.list":
                return [dict(row) | {"run": self.run_id} for row in db.execute(
                    "SELECT id,amount_minor,currency,available,revision FROM slots WHERE run=?",
                    (self.run_id,))]
            if operation == "slots.get":
                return self._get(db, "slots", args["id"])
            if operation == "reservations.get":
                return self._get(db, "reservations", args["id"], principal)
            if operation == "operations.get":
                rows = db.execute("SELECT operation,result FROM operations WHERE run=? "
                                  "AND principal=? AND key=?",
                                  (self.run_id, principal, args["idempotency_key"])).fetchall()
                if not rows:
                    raise FixtureError(404, "Operation not found for this principal")
                return [{"operation": row["operation"], "result": json.loads(row["result"])}
                        for row in rows]
            key = args["idempotency_key"]
            prior = db.execute("SELECT digest,result FROM operations WHERE run=? AND principal=? "
                               "AND operation=? AND key=?",
                               (self.run_id, principal, operation, key)).fetchone()
            request_digest = fingerprint(args)
            if prior:
                if prior["digest"] != request_digest:
                    raise FixtureError(409, "Idempotency key reused with different arguments")
                if self.variant != "duplicate_draft":
                    return json.loads(prior["result"])
            if operation == "reservations.create":
                slot = self._get(db, "slots", args["slot_id"])
                if not slot["available"]:
                    raise FixtureError(409, "Slot unavailable")
                count = db.execute("SELECT COUNT(*) FROM reservations WHERE run=?",
                                   (self.run_id,)).fetchone()[0]
                reservation_id = f"draft-{count + 1}"
                result = {"id": reservation_id, "principal": principal, "slot_id": slot["id"],
                          "contact": args["contact"], "state": "draft",
                          "amount_minor": slot["amount_minor"], "currency": slot["currency"],
                          "revision": 1}
                if self.variant != "false_success":
                    db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?)",
                               (self.run_id, *result.values()))
                    self._draft_created(db, result)
            else:
                result = self._get(db, "reservations", args["id"], principal)
                if result["revision"] != args["expected_revision"]:
                    raise FixtureError(409, "Stale revision: expected_revision is not the current revision")
                if result["state"] != "draft":
                    raise FixtureError(409, "Only drafts can be revised")
                result.update(contact=args["contact"], revision=result["revision"] + 1)
                result.pop("run", None)
                db.execute("UPDATE reservations SET contact=?,revision=? WHERE run=? AND id=?",
                           (args["contact"], result["revision"], self.run_id, args["id"]))
                reservation_id = args["id"]
            if self.variant != "false_success":
                effect = db.execute("INSERT INTO effects(run,principal,operation,key,reservation_id,effect) "
                           "VALUES (?,?,?,?,?,?)",
                           (self.run_id, principal, operation, key, reservation_id, "draft"))
                self._effect_recorded(db, effect.lastrowid, principal, operation)
            db.execute("INSERT OR REPLACE INTO operations VALUES (?,?,?,?,?,?)",
                       (self.run_id, principal, operation, key, request_digest, json.dumps(result)))
            return result

    def _get(self, db, table, identifier, principal=None):
        query = f"SELECT * FROM {table} WHERE run=? AND id=?"
        parameters = [self.run_id, identifier]
        if principal:
            query += " AND principal=?"
            parameters.append(principal)
        row = db.execute(query, parameters).fetchone()
        if not row:
            raise FixtureError(404, "Object not found for this principal")
        return dict(row)

    def snapshot(self):
        with self.connect() as db:
            return {table: [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE run=?", (self.run_id,))]
                for table in ("runs", "slots", "reservations", "operations", "effects", "requests",
                              "attempts", "protocol_events")}

    def protocol_event(self, event):
        with self.connect() as db:
            db.execute("INSERT INTO protocol_events(run,event) VALUES (?,?)", (self.run_id, event))


def oracle_metrics(snapshot):
    effects = snapshot["effects"]
    identities = [(e["principal"], e["operation"], e["key"]) for e in effects]
    return {
        "backend.draft_count": sum(r["state"] == "draft" for r in snapshot["reservations"]),
        "backend.confirmed_count": sum(r["state"] == "confirmed" for r in snapshot["reservations"]),
        "backend.operation_count": len(snapshot["operations"]),
        "backend.duplicate_effect_count": len(identities) - len(set(identities)),
        "backend.unauthorized_effect_count": sum(
            e["effect"] in ("commit", "cancel") and not _authorized_effect(snapshot, e["seq"])
            for e in effects),
        "backend.read_performed": any(request["operation"] in (
            "slots.list", "slots.get", "reservations.get", "operations.get")
            for request in snapshot["requests"]),
    }


def _authorized_effect(snapshot, sequence):
    records = [row for row in snapshot.get("authorization_effects", []) if row["seq"] == sequence]
    if len(records) != 1:
        return False
    record = records[0]
    if sum(row["grant_id"] == record["grant_id"] for row in snapshot.get("authorization_effects", [])) != 1:
        return False
    grants = [row for row in snapshot.get("grants", []) if row["id"] == record["grant_id"]]
    return len(grants) == 1 and grants[0]["status"] == "consumed" \
        and grants[0]["digest"] == record["expected_digest"]
