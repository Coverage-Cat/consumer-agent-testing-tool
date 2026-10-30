"""Durable intent, authority, delivery and independently observed effect records.

The dispatch transaction is the authority boundary. An effect already in flight
cannot be undone by a later revocation. The journal never infers an effect from
transport success and never sends a request itself.
"""
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .errors import DispatchVeto, LabError
from .serialization import canonical_bytes


class ActionJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), binding_key BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, revision INTEGER NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, scope TEXT NOT NULL,
                revision INTEGER NOT NULL, route TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, reply TEXT);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, id TEXT NOT NULL, kind TEXT NOT NULL,
                recorded_at TEXT NOT NULL, data TEXT NOT NULL);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES(1,?)", (os.urandom(32),))
        self.key = self.db.execute("SELECT binding_key FROM metadata WHERE id=1").fetchone()[0]
        self.path.chmod(0o600)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def _event(self, identifier, kind, **data):
        self.db.execute("INSERT INTO events(id,kind,recorded_at,data) VALUES(?,?,?,?)",
                        (identifier, kind, datetime.now(UTC).isoformat(), json.dumps(data)))

    def scope(self, principal, service, action, terms):
        # Only a digest enters the journal; callers keep raw terms in private evidence.
        return hmac.new(self.key, canonical_bytes({"principal": principal, "service": service,
                                                 "action": action, "terms": terms}), hashlib.sha256).hexdigest()

    def grant(self, identifier, *, principal, service, action, terms):
        scope = self.scope(principal, service, action, terms)
        with self.db:
            prior = self.db.execute("SELECT * FROM grants WHERE id=?", (identifier,)).fetchone()
            if prior:
                if prior["scope"] != scope or not prior["active"]:
                    raise LabError("A grant identity cannot be rebound or restore revoked authority", code=3)
                return prior["revision"]
            self.db.execute("INSERT INTO grants VALUES(?,?,1,1)", (identifier, scope))
            self._event(identifier, "grant", revision=1, scope=scope)
        return 1

    def revoke(self, identifier):
        with self.db:
            cursor = self.db.execute("UPDATE grants SET revision=revision+1,active=0 WHERE id=?", (identifier,))
            if not cursor.rowcount:
                raise LabError("Unknown authorization grant", code=3)
            self._event(identifier, "revoked")

    def propose(self, identifier, *, grant_id, principal, service, action, terms, route):
        scope = self.scope(principal, service, action, terms)
        with self.db:
            prior = self.db.execute("SELECT * FROM actions WHERE id=?", (identifier,)).fetchone()
            if prior:
                if (prior["scope"], prior["grant_id"], prior["route"]) != (scope, grant_id, route):
                    raise LabError("An action identity cannot be rebound", code=3)
                return identifier
            grant = self.db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
            if not grant or grant["scope"] != scope or not grant["active"]:
                raise LabError("Action has no matching active grant", code=3)
            self.db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?)",
                            (identifier, grant_id, scope, grant["revision"], route, "proposed"))
            self._event(identifier, "proposal", scope=scope, route=route, revision=grant["revision"])
        return identifier

    def dispatch(self, identifier, *, recovery=False):
        """Atomically consume a proposal or explicitly reconciled recovery."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT a.*,g.active,g.revision AS current_revision FROM actions a "
                "JOIN grants g ON a.grant_id=g.id WHERE a.id=?", (identifier,)).fetchone()
            if not row:
                raise LabError("Unknown action at dispatch", code=3)
            if not row["active"] or row["revision"] != row["current_revision"]:
                raise DispatchVeto("Authorization was revoked or changed before dispatch")
            if row["state"] != "proposed" and not (recovery and row["state"] == "not-committed"):
                raise LabError("Dispatch blocked: reconcile uncertainty before an explicit recovery", code=3)
            self.db.execute("UPDATE actions SET state='uncertain' WHERE id=?", (identifier,))
            self._event(identifier, "authorized-dispatch", revision=row["revision"], recovery=recovery)

    def response(self, identifier, *, received, detail=None):
        with self.db:
            self._action(identifier)
            self._event(identifier, "transport-response", received=received, detail=detail)

    def _action(self, identifier):
        row = self.db.execute("SELECT * FROM actions WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LabError("Unknown action", code=3)
        return row

    def reconcile(self, identifier, outcome, *, evidence, effect_id=None, effect_identity="service"):
        if outcome not in ("committed", "not-committed", "uncertain") or not evidence:
            raise LabError("Reconciliation needs an independent read and an explicit outcome", code=3)
        if effect_identity not in ("service", "predicate"):
            raise LabError("Unknown effect identity kind")
        if outcome == "committed" and not effect_id and effect_identity != "predicate":
            raise LabError("A committed effect needs a service effect identity", code=3)
        with self.db:
            row = self._action(identifier)
            if row["state"] == "proposed":
                raise LabError("Cannot reconcile an action that was never dispatched", code=3)
            if row["state"] == "committed" and outcome != "committed":
                raise LabError("An observed effect cannot be erased by reconciliation", code=3)
            self._event(identifier, "reconciliation", outcome=outcome, evidence=evidence)
            if outcome == "committed":
                effects = self.events(identifier, "effect")
                if effects and effects[-1]["data"]["effect_id"] != effect_id:
                    raise LabError("Conflicting effect identity for one intent", code=3)
                if not effects:
                    self._event(identifier, "effect", effect_id=effect_id, identity_kind=effect_identity,
                                evidence=evidence)
            self.db.execute("UPDATE actions SET state=? WHERE id=?", (outcome, identifier))

    def abandon_proposals(self):
        with self.db:
            for row in self.db.execute("SELECT id FROM actions WHERE state='proposed'").fetchall():
                self._event(row["id"], "abandoned", reason="not-dispatched")
            self.db.execute("UPDATE actions SET state='abandoned' WHERE state='proposed'")

    def message(self, identifier, *, task_id, reply_to=None, stage, source, timestamp, precision):
        if stage not in ("submitted", "accepted", "served", "acknowledged"):
            raise LabError("Unknown delivery stage")
        if source not in ("native-transcript", "room-fetch", "operator-copy", "adapter"):
            raise LabError("Unknown delivery source")
        if precision not in ("millisecond", "second", "minute", "unknown") or (
                timestamp is not None and datetime.fromisoformat(timestamp).tzinfo is None):
            raise LabError("Delivery timestamps require an offset and explicit precision")
        if timestamp is None and precision != "unknown":
            raise LabError("Missing timestamp cannot have known precision")
        with self.db:
            row = self.db.execute("SELECT * FROM messages WHERE id=?", (identifier,)).fetchone()
            if row and (row["task"], row["reply"]) != (task_id, reply_to):
                raise LabError("Message association cannot change", code=3)
            self.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?)", (identifier, task_id, reply_to))
            data = {"task_id": task_id, "reply_to": reply_to, "stage": stage, "source": source,
                    "timestamp": timestamp, "precision": precision}
            if not any(event["data"] == data for event in self.events(identifier, "delivery")):
                self._event(identifier, "delivery", **data)

    def events(self, identifier=None, kind=None):
        rows = self.db.execute(
            "SELECT * FROM events WHERE (? IS NULL OR id=?) AND (? IS NULL OR kind=?) ORDER BY seq",
            (identifier, identifier, kind, kind)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]


async def perform_action(journal, identifier, send, read, *, allow_recovery=False):
    """Dispatch once, reconcile lost responses, and optionally recover once.

    `read` returns outcome/evidence/effect_id fields from an independent read.
    A received transport response is returned to the adapter without asserting
    commitment. The same intent and current grant govern the optional retry.
    """
    for attempt in range(2 if allow_recovery else 1):
        journal.dispatch(identifier, recovery=attempt > 0)
        try:
            response = await send()
        except (TimeoutError, ConnectionError, OSError):
            response = None
        journal.response(identifier, received=response is not None)
        if response is not None:
            return {"transport": response, "effect": "unknown"}
        observation = await read()
        journal.reconcile(identifier, **observation)
        if observation["outcome"] == "committed":
            return {"transport": None, "effect": "committed", "effect_id": observation["effect_id"]}
        if observation["outcome"] != "not-committed":
            break
    raise LabError("Action outcome unresolved or recovery not authorized", code=3)
