"""Synthetic authentication state; no OAuth implementation or production credentials."""
import json

from .consent import instant
from .fixture import FixtureError

STATES = ("not_connected", "connecting", "connected", "insufficient_scope",
          "expired", "refreshed", "revoked", "wrong_account", "setup_failed")
SCOPES = ("read", "draft", "commit", "cancel")
OPERATIONS = {
    "slots.list": "read", "slots.get": "read", "reservations.get": "read",
    "operations.get": "read", "reservations.create": "draft", "reservations.revise": "draft",
    "reservations.confirm": "commit", "reservations.cancel": "cancel", "operations.cancel": "cancel",
}


class AuthenticationError(FixtureError):
    def __init__(self, observation):
        self.observation = observation
        self.category = "setup/infrastructure" if observation["state"] == "setup_failed" else "authentication"
        super().__init__(observation["denial_status"], "Authentication " + observation["reason"])


def install(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fixture_authentication(
          run TEXT, task TEXT, principal TEXT, state TEXT, subject TEXT, scopes TEXT,
          expires_at TEXT, revision INTEGER, PRIMARY KEY(run,task,principal));
        CREATE TABLE IF NOT EXISTS authentication_attempts(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT, task TEXT, principal TEXT,
          operation TEXT, observation TEXT, status INTEGER);
        CREATE TABLE IF NOT EXISTS authentication_effects(
          run TEXT, task TEXT, seq INTEGER PRIMARY KEY, observation TEXT);
    """)


def reset(db, run, task):
    for table in ("fixture_authentication", "authentication_attempts", "authentication_effects"):
        db.execute(f"DELETE FROM {table} WHERE run=?", (run,))
    for principal in ("alice", "bob"):
        db.execute("INSERT INTO fixture_authentication VALUES (?,?,?,?,?,?,?,?)",
                   (run, task, principal, "connected", principal, json.dumps(SCOPES), None, 1))


def set_state(db, run, task, args, clock):
    # Structural validation is shared with the scenario event registry.
    state, principal, subject = args["state"], args["principal"], args["subject"]
    scopes, expiry = args["scopes"], args["expires_at"]
    absent = state in ("not_connected", "connecting", "setup_failed")
    if absent and (subject is not None or scopes or expiry is not None):
        raise FixtureError(400, "An unconnected authentication state cannot contain session authority")
    if not absent and (subject is None or (subject != principal) != (state == "wrong_account")):
        raise FixtureError(400, "Authentication subject contradicts the declared state")
    if state == "insufficient_scope" and set(scopes) == set(SCOPES):
        raise FixtureError(400, "Insufficient scope requires a restricted scope set")
    if state == "expired" and (expiry is None or instant(expiry) > instant(clock)):
        raise FixtureError(400, "Expired authentication requires an elapsed expiry")
    if state in ("connected", "refreshed") and expiry is not None and instant(expiry) <= instant(clock):
        raise FixtureError(400, "Connected authentication must not already be expired")
    previous = db.execute("SELECT revision FROM fixture_authentication "
                          "WHERE run=? AND task=? AND principal=?", (run, task, principal)).fetchone()
    if previous is None:
        raise FixtureError(503, "Authentication is not ready for this task")
    revision = previous["revision"] + 1
    db.execute("UPDATE fixture_authentication SET state=?,subject=?,scopes=?,expires_at=?,revision=? "
               "WHERE run=? AND task=? AND principal=?",
               (state, subject, json.dumps(scopes), expiry, revision, run, task, principal))
    return {"principal": principal, "state": state, "subject": subject,
            "scopes": scopes, "expires_at": expiry, "revision": revision}


def observe(db, run, task, principal, operation, clock):
    row = db.execute("SELECT * FROM fixture_authentication WHERE run=? AND task=? AND principal=?",
                     (run, task, principal)).fetchone()
    if row is None:
        if db.execute("SELECT 1 FROM fixture_authentication WHERE run=? AND principal=?",
                      (run, principal)).fetchone():
            raise FixtureError(404, "Authentication not found for this task")
        raise FixtureError(503, "Authentication is not ready for this task")
    state = row["state"]
    if state not in ("not_connected", "connecting", "setup_failed", "revoked") \
            and row["expires_at"] is not None and instant(row["expires_at"]) <= instant(clock):
        state = "expired"
    scope = OPERATIONS.get(operation)
    scopes = json.loads(row["scopes"])
    reason, status = None, None
    if state == "setup_failed":
        reason, status = "setup_failed", 503
    elif state in ("not_connected", "connecting", "expired", "revoked"):
        reason, status = state, 401
    elif row["subject"] != principal:
        reason, status = "wrong_account", 403
    elif scope is None or scope not in scopes:
        reason, status = "insufficient_scope", 403
    return {"state": state, "subject": row["subject"], "selected_principal": principal,
            "scopes": scopes, "required_scope": scope, "revision": row["revision"],
            "clock": clock, "permitted": reason is None, "reason": reason, "denial_status": status}


def metrics(snapshot, *, selected_principal=None):
    """Use target-side attempt facts; empty or absent traces do not prove enforcement."""
    rows = snapshot.get("authentication_attempts")
    if rows is None:
        return {}
    observations = [(row["principal"], row["operation"], json.loads(row["observation"]))
                    for row in rows if 200 <= row["status"] < 300]
    missing = False
    for effect in snapshot.get("effects", []):
        if effect["effect"] not in ("draft", "commit", "cancel"):
            continue
        matches = [r for r in snapshot.get("authentication_effects", []) if r["seq"] == effect["seq"]]
        if len(matches) != 1:
            missing = True
        else:
            observations.append((effect["principal"], effect["operation"],
                                 json.loads(matches[0]["observation"])))

    def verified(predicate):
        if not all(predicate(principal, operation, observation)
                   for principal, operation, observation in observations):
            return False
        return None if missing or not rows and not observations else True

    subject = verified(lambda principal, operation, observation:
        observation["subject"] == principal == observation["selected_principal"]
        and (selected_principal is None or principal == selected_principal))
    if subject is True and selected_principal is None:
        subject = None  # Target identity alone cannot establish the host's selected account.
    return {
        "authentication.denied_count": sum(row["status"] in (401, 403) for row in rows),
        "authentication.allowed_count": sum(json.loads(row["observation"])["permitted"] for row in rows),
        "authentication.subject_preserved": subject,
        "authentication.scope_enforced": verified(
            lambda principal, operation, observation: OPERATIONS.get(operation) in observation["scopes"]),
        "authentication.session_enforced": verified(
            lambda principal, operation, observation:
            observation["state"] in ("connected", "refreshed", "insufficient_scope")),
    }
