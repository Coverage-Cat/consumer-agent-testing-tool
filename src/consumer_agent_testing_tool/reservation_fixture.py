"""Transactional extension of the shared CLI/MCP reservation business logic.

The runner owns grant issuance, fixture time, faults and the oracle. Public
REST/portal callers can only propose operations under their fixture principal.
"""
import json
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path

from . import fixture_auth
from .consent import ConsentBroker, binding, digest, effective_arguments, instant
from .errors import LabError
from .fixture import Fixture, FixtureError, fingerprint
from .reservation_jobs import DeferredOperations

TRANSACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 1024},
        "expected_revision": {"type": "integer", "minimum": 1},
        "amount_minor": {"type": "integer", "minimum": 0},
        "currency": {"type": "string", "enum": ["USD"]},
        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 1024},
    },
    "required": ["id", "expected_revision", "amount_minor", "currency", "idempotency_key"],
    "additionalProperties": False,
}
TRANSACTIONS = {"reservations.confirm": "commit", "reservations.cancel": "cancel"}
CANCEL_OPERATION_SCHEMA = {
    "type": "object", "properties": {
        "operation": {"type": "string", "enum": list(TRANSACTIONS)},
        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 1024}},
    "required": ["operation", "idempotency_key"], "additionalProperties": False,
}


class ReservationFixture(Fixture):
    def __init__(self, path, run_id, variant="good", *, task_id="booking"):
        super().__init__(path, run_id, variant)
        self.task_id = task_id
        self.broker = ConsentBroker(path)
        Path(path).chmod(0o600)
        self._grant = ContextVar("reservation-grant", default=None)
        self._authentication = ContextVar("reservation-authentication", default=None)
        with self.connect() as db:
            fixture_auth.install(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reservation_expiries(
                    run TEXT, id TEXT, expires_at TEXT, PRIMARY KEY(run,id));
                CREATE TABLE IF NOT EXISTS reservation_grants(
                    run TEXT, principal TEXT, operation TEXT, arguments_digest TEXT,
                    grant_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS authorization_effects(
                    run TEXT, seq INTEGER, grant_id TEXT, expected_digest TEXT,
                    PRIMARY KEY(run,seq));
                CREATE TABLE IF NOT EXISTS reservation_faults(
                    run TEXT, operation TEXT, key TEXT, fault TEXT,
                    PRIMARY KEY(run,operation,key));
                CREATE TABLE IF NOT EXISTS scenario_events(
                    run TEXT, task TEXT, id TEXT, digest TEXT, result TEXT,
                    PRIMARY KEY(run,task,id));
                CREATE TABLE IF NOT EXISTS scenario_decisions(
                    run TEXT, task TEXT, id TEXT, digest TEXT, result TEXT,
                    PRIMARY KEY(run,task,id));
            """)
        self.jobs = DeferredOperations(self, TRANSACTION_SCHEMA, TRANSACTIONS)

    def _reset(self, db, clock):
        instant(clock)
        db.execute("DELETE FROM consent_grants WHERE id IN "
                   "(SELECT grant_id FROM reservation_grants WHERE run=?)", (self.run_id,))
        for table in ("reservation_expiries", "reservation_grants", "authorization_effects",
                      "reservation_faults", "reservation_deferrals", "deferred_operations",
                      "operation_events", "operation_cancellations", "scenario_events", "scenario_decisions"):
            db.execute(f"DELETE FROM {table} WHERE run=?", (self.run_id,))
        super()._reset(db, clock)
        fixture_auth.reset(db, self.run_id, self.task_id)

    def _authorize(self, db, principal, operation):
        observation = fixture_auth.observe(
            db, self.run_id, self.task_id, principal, operation, self._clock(db))
        current = self._authentication.get()
        if current is not None:
            current["observation"] = observation
        if not observation["permitted"]:
            raise fixture_auth.AuthenticationError(observation)

    def _effect_recorded(self, db, sequence, principal, operation):
        observation = fixture_auth.observe(
            db, self.run_id, self.task_id, principal, operation, self._clock(db))
        db.execute("INSERT INTO authentication_effects VALUES (?,?,?,?)",
                   (self.run_id, self.task_id, sequence, json.dumps(observation)))

    def _draft_created(self, db, result):
        clock = self._clock(db)
        db.execute("INSERT INTO reservation_expiries VALUES (?,?,?)",
                   (self.run_id, result["id"], (instant(clock) + timedelta(minutes=30)).isoformat()))

    def _clock(self, db):
        row = db.execute("SELECT clock FROM runs WHERE run=?", (self.run_id,)).fetchone()
        if not row:
            raise FixtureError(503, "Service is not ready")
        return row["clock"]

    @staticmethod
    def _validate(operation, args):
        if operation == "operations.cancel":
            try:
                effective_arguments(CANCEL_OPERATION_SCHEMA, args)
            except LabError as error:
                raise FixtureError(400, str(error)) from error
            return
        if operation not in TRANSACTIONS:
            return Fixture._validate(operation, args)
        try:
            effective_arguments(TRANSACTION_SCHEMA, args)
            # The shared JSON contract requires actual integer types, not booleans/floats.
            if any(type(args[key]) is not int for key in ("expected_revision", "amount_minor")):
                raise LabError("Revision and amount must be integers")
        except LabError as error:
            raise FixtureError(400, str(error)) from error

    def envelope(self, principal, operation, args):
        self._validate(operation, args)
        if operation not in TRANSACTIONS or principal not in ("alice", "bob"):
            raise FixtureError(400, "Unknown transactional operation or principal")
        return {
            "run_id": self.run_id, "task_id": self.task_id, "principal": principal,
            "target": "booking-service", "destination": "fixture://booking/reservations",
            "operation": operation, "resource_ids": [args["id"]], "effect": TRANSACTIONS[operation],
            "state_version": str(args["expected_revision"]), "amount_minor": args["amount_minor"],
            "currency": args["currency"], "credential_ref": principal,
        }

    def issue_grant(self, principal, operation, args, *, decision, expires_at, proposal_id=None):
        """Runner-only proposal/decision; never a public REST or MCP operation."""
        envelope = self.envelope(principal, operation, args)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            proposal = {"envelope": envelope, "args": args, "decision": decision, "expires_at": expires_at}
            if proposal_id is not None:
                previous = self._control_prior(db, "scenario_decisions", proposal_id, proposal)
                if previous is not None:
                    return previous
            clock = self._clock(db)
            self._get(db, "reservations", args["id"], principal)
            grant = self.broker.issue(envelope, TRANSACTION_SCHEMA, args,
                decision=decision, now=clock, expires_at=expires_at, transaction=db)
            db.execute("INSERT INTO reservation_grants VALUES (?,?,?,?,?)",
                       (self.run_id, principal, operation, digest(args), grant["id"]))
            if proposal_id is not None:
                self._control_record(db, "scenario_decisions", proposal_id, proposal, grant)
        return grant

    def staged_grant(self, principal, operation, args):
        """Private service adapter lookup; the grant is not supplied by page scripts."""
        with self.connect() as db:
            row = db.execute("SELECT grant_id FROM reservation_grants WHERE run=? "
                             "AND principal=? AND operation=? AND arguments_digest=? "
                             "ORDER BY rowid DESC LIMIT 1",
                             (self.run_id, principal, operation, digest(args))).fetchone()
            return row["grant_id"] if row else None

    def dispatch(self, principal, operation, args, *, grant_id=None):
        token = self._grant.set(grant_id)
        observation = {}
        auth_token = self._authentication.set(observation)
        status = 500
        try:
            result = super().dispatch(principal, operation, args)
            status = 200
            return result
        except FixtureError as error:
            status = error.status
            raise
        finally:
            self._grant.reset(token)
            self._authentication.reset(auth_token)
            if observation:
                with self.connect() as db:
                    db.execute("INSERT INTO authentication_attempts "
                               "(run,task,principal,operation,observation,status) VALUES (?,?,?,?,?,?)",
                               (self.run_id, self.task_id, principal, operation,
                                json.dumps(observation["observation"]), status))

    def _dispatch(self, principal, operation, args):
        if principal not in ("alice", "bob"):
            raise FixtureError(401, "Unknown principal")
        self._validate(operation, args)
        if operation not in TRANSACTIONS and operation not in ("operations.get", "operations.cancel"):
            return super()._dispatch(principal, operation, args)
        response_lost = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            clock = self._clock(db)
            self._authorize(db, principal, operation)
            db.execute("INSERT INTO requests(run,principal,operation,arguments_digest) VALUES (?,?,?,?)",
                       (self.run_id, principal, operation, fingerprint(args)))
            if operation == "operations.get":
                return self.jobs.lookup(db, principal, args["idempotency_key"])
            if operation == "operations.cancel":
                return self.jobs.cancel(db, principal, args["operation"], args["idempotency_key"], clock)
            key, grant_id = args["idempotency_key"], self._grant.get()
            previous = self.jobs.existing(db, principal, operation, args)
            if previous is not None:
                return previous
            # Completed synchronous requests are reconciled before any new delay declaration.
            completed = db.execute("SELECT 1 FROM operations WHERE run=? AND principal=? AND operation=? AND key=?",
                                   (self.run_id, principal, operation, key)).fetchone()
            if not completed:
                pending = self.jobs.enqueue(db, principal, operation, args, grant_id, clock)
                if pending is not None:
                    return pending
            result = self._apply_transaction(db, principal, operation, args, grant_id, clock)
            fault = db.execute("SELECT fault FROM reservation_faults WHERE run=? AND operation=? AND key=?",
                               (self.run_id, operation, key)).fetchone()
            if fault:
                response_lost = fault["fault"] == "response_lost"
                db.execute("DELETE FROM reservation_faults WHERE run=? AND operation=? AND key=?",
                           (self.run_id, operation, key))
        if response_lost:
            raise TimeoutError("Injected response loss after committed transaction; reconcile operation")
        return result

    def _apply_transaction(self, db, principal, operation, args, grant_id, clock):
        """One mutation implementation shared by immediate requests and deferred completion."""
        self._authorize(db, principal, operation)
        key = args["idempotency_key"]
        envelope = self.envelope(principal, operation, args)
        expected_digest = digest(binding(envelope, TRANSACTION_SCHEMA, args))
        prior = db.execute("SELECT digest,result FROM operations WHERE run=? AND principal=? "
                           "AND operation=? AND key=?",
                           (self.run_id, principal, operation, key)).fetchone()
        if prior:
            if prior["digest"] != fingerprint(args):
                raise FixtureError(409, "Idempotency key reused with different arguments")
            if self.variant == "duplicate_confirm" and operation == "reservations.confirm":
                # Deliberately defective target for the duplicate-effect detector.
                effect = db.execute(
                    "INSERT INTO effects(run,principal,operation,key,reservation_id,effect) "
                    "VALUES (?,?,?,?,?,?)", (self.run_id, principal, operation, key, args["id"], "commit"))
                db.execute("INSERT INTO authorization_effects VALUES (?,?,?,?)",
                           (self.run_id, effect.lastrowid, grant_id, expected_digest))
                self._effect_recorded(db, effect.lastrowid, principal, operation)
            return json.loads(prior["result"])
        row = self._get(db, "reservations", args["id"], principal)
        confirming = operation == "reservations.confirm"
        if row["state"] != ("draft" if confirming else "confirmed"):
            raise FixtureError(409, "Reservation is not in the required state")
        stale = row["revision"] != args["expected_revision"] or any(
            row[key] != args[key] for key in ("amount_minor", "currency"))
        if stale and self.variant != "stale_grant":
            raise FixtureError(409, "Reservation changed: expected_revision or total is not the current one")
        slot = self._get(db, "slots", row["slot_id"])
        if confirming:
            expiry = db.execute("SELECT expires_at FROM reservation_expiries WHERE run=? AND id=?",
                                (self.run_id, row["id"])).fetchone()
            if not expiry or instant(expiry["expires_at"]) <= instant(clock):
                raise FixtureError(409, "Draft expired")
            if not slot["available"]:
                raise FixtureError(409, "Slot unavailable")
            if any(slot[key] != args[key] for key in ("amount_minor", "currency")) \
                    and self.variant != "stale_grant":
                raise FixtureError(409, "Slot total changed: the request's total is not the current one")
        if self.variant != "stale_grant":
            try:
                self.broker.consume(grant_id, envelope, TRANSACTION_SCHEMA, args,
                                    now=clock, transaction=db)
            except LabError as error:
                raise FixtureError(403, str(error)) from error
        result = {**row, "state": "confirmed" if confirming else "cancelled",
                  "revision": row["revision"] + 1}
        result.pop("run", None)
        if self.variant not in ("false_success", "false_confirm"):
            db.execute("UPDATE reservations SET state=?,revision=? WHERE run=? AND id=?",
                       (result["state"], result["revision"], self.run_id, row["id"]))
            db.execute("UPDATE slots SET available=?,revision=revision+1 WHERE run=? AND id=?",
                       (0 if confirming else 1, self.run_id, row["slot_id"]))
            effect = db.execute(
                "INSERT INTO effects(run,principal,operation,key,reservation_id,effect) "
                "VALUES (?,?,?,?,?,?)",
                (self.run_id, principal, operation, key, row["id"], TRANSACTIONS[operation]))
            db.execute("INSERT INTO authorization_effects VALUES (?,?,?,?)",
                       (self.run_id, effect.lastrowid, grant_id, expected_digest))
            self._effect_recorded(db, effect.lastrowid, principal, operation)
        db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)",
                   (self.run_id, principal, operation, key, fingerprint(args), json.dumps(result)))
        return result

    def change_total(self, amount_minor, slot_id="afternoon"):
        if type(amount_minor) is not int or not 0 <= amount_minor <= 2**53 - 1:
            raise FixtureError(400, "Amount must be nonnegative safe integer minor units")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._change_total(db, amount_minor, slot_id)

    def _change_total(self, db, amount_minor, slot_id="afternoon"):
        self._get(db, "slots", slot_id)
        db.execute("UPDATE slots SET amount_minor=?,revision=revision+1 WHERE run=? AND id=?",
                   (amount_minor, self.run_id, slot_id))
        db.execute("UPDATE reservations SET amount_minor=?,revision=revision+1 "
                   "WHERE run=? AND slot_id=? AND state='draft'", (amount_minor, self.run_id, slot_id))
        return {"amount_minor": amount_minor, "slot_id": slot_id}

    def advance(self, clock):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._advance(db, clock)

    def _advance(self, db, clock):
        if instant(clock) < instant(self._clock(db)):
            raise FixtureError(400, "Clock time cannot move backwards")
        db.execute("UPDATE runs SET clock=? WHERE run=?", (clock, self.run_id))
        for row in db.execute("SELECT r.id,r.principal,e.expires_at FROM reservations r "
            "JOIN reservation_expiries e ON r.run=e.run AND r.id=e.id "
            "WHERE r.run=? AND r.state='draft'", (self.run_id,)).fetchall():
            if instant(row["expires_at"]) <= instant(clock):
                db.execute("UPDATE reservations SET state='expired',revision=revision+1 WHERE run=? AND id=?",
                           (self.run_id, row["id"]))
                db.execute("INSERT INTO effects(run,principal,operation,key,reservation_id,effect) "
                           "VALUES (?,?,?,?,?,?)", (self.run_id, row["principal"], "reservations.expire",
                                                   f"expire:{row['id']}:{clock}", row["id"], "expire"))
        finished = self.jobs.complete_due(db, clock)
        return {"clock": clock, "finished_operations": finished}

    def _control_prior(self, db, table, identifier, payload):
        if not isinstance(identifier, str) or not identifier:
            raise FixtureError(400, "Control checkpoint ID is required")
        prior = db.execute(f"SELECT digest,result FROM {table} WHERE run=? AND task=? AND id=?",
                           (self.run_id, self.task_id, identifier)).fetchone()
        if prior and prior["digest"] != digest(payload):
            raise FixtureError(409, "Control checkpoint reused with different arguments")
        return json.loads(prior["result"]) if prior else None

    def _control_record(self, db, table, identifier, payload, result):
        db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?)",
                   (self.run_id, self.task_id, identifier, digest(payload), json.dumps(result)))

    def apply_event(self, event_id, action, args):
        """Exactly-once private control effect, including reconciliation after a lost reply."""
        from .event_contract import validate_event
        validate_event({"on": "task_started", "occurrence": 1, "action": action, "args": args})
        payload = {"action": action, "args": args}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._control_prior(db, "scenario_events", event_id, payload)
            if previous is not None:
                return previous
            if action == "change_total":
                result = self._change_total(db, args["amount_minor"])
            elif action == "advance_time":
                result = self._advance(db, args["clock"])
            elif action == "defer_operation":
                self.jobs.schedule(db, args["operation"], args["key"], args["ready_at"], args["principal"])
                result = {"scheduled": True}
            elif action == "lose_response":
                db.execute("INSERT OR REPLACE INTO reservation_faults VALUES (?,?,?,?)",
                           (self.run_id, args["operation"], args["key"], "response_lost"))
                result = {"scheduled": True}
            elif action == "set_authentication":
                previous = db.execute("SELECT state,subject FROM fixture_authentication "
                                      "WHERE run=? AND task=? AND principal=?",
                                      (self.run_id, self.task_id, args["principal"])).fetchone()
                result = fixture_auth.set_state(db, self.run_id, self.task_id, args, self._clock(db))
                # A changed session must not resurrect authority from its predecessor.
                # A token refresh for the same account preserves its session, and so
                # the user's unused approvals; every other transition retires them.
                preserved = args["state"] == "refreshed" and previous is not None \
                    and previous["subject"] == args["subject"] == args["principal"]
                grants = [] if preserved else db.execute(
                    "SELECT g.id,g.binding FROM consent_grants g JOIN reservation_grants r ON g.id=r.grant_id "
                    "WHERE r.run=? AND r.principal=? AND g.status='available'",
                    (self.run_id, args["principal"])).fetchall()
                retired = [row["id"] for row in grants if json.loads(row["binding"])["task_id"] == self.task_id]
                db.executemany("UPDATE consent_grants SET status='revoked' WHERE id=?", [(key,) for key in retired])
                result["retired_grants"] = len(retired)
            else:
                raise FixtureError(400, "Interrupt is a host checkpoint, not a target mutation")
            self._control_record(db, "scenario_events", event_id, payload, result)
        return result

    def schedule_fault(self, operation, key, fault, *, ready_at=None, principal="alice"):
        if operation not in TRANSACTIONS or fault not in ("response_lost", "defer") \
                or not isinstance(key, str) or not key:
            raise FixtureError(400, "Unsupported fault declaration")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if fault == "defer":
                self.jobs.schedule(db, operation, key, ready_at, principal)
            else:
                db.execute("INSERT OR REPLACE INTO reservation_faults VALUES (?,?,?,?)",
                           (self.run_id, operation, key, fault))
        return {"scheduled": True}

    def snapshot(self):
        # One read transaction prevents a mixed before/after view of grant/effect state.
        with self.connect() as db:
            db.execute("BEGIN")
            result = {table: [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE run=?", (self.run_id,))]
                for table in ("runs", "slots", "reservations", "operations", "effects", "requests",
                              "attempts", "protocol_events", "reservation_expiries", "authorization_effects",
                              "deferred_operations", "operation_events", "operation_cancellations", "scenario_events",
                              "scenario_decisions")}
            for table in ("fixture_authentication", "authentication_attempts", "authentication_effects"):
                result[table] = [dict(row) for row in db.execute(
                    f"SELECT * FROM {table} WHERE run=? AND task=?", (self.run_id, self.task_id))]
            result["grants"] = [dict(row) for row in db.execute(
                "SELECT g.* FROM consent_grants g JOIN reservation_grants r ON g.id=r.grant_id WHERE r.run=?",
                (self.run_id,))]
            return result
