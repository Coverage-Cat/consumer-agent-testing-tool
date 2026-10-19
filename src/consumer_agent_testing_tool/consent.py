"""Durable one-use grants. The broker is host-owned, never an agent tool."""
import copy
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime

from jsonschema import Draft202012Validator

from .errors import LabError

CANONICALIZER = "jcs-rfc8785-v1"
# Numbers in a binding: safe integers only (unchanged label and digests), or
# fractional schema/argument numbers too, which an adapter must serialize with
# full RFC 8785 (ECMAScript) number form. Envelope money is always integer
# minor units. The label is bound, so an integer-only adapter fails closed.
NUMERIC_DOMAIN = "safe-integer-json-v1"
FRACTIONAL_DOMAIN = "rfc8785-number-v1"
SAFE_INTEGER = 2**53 - 1
ENVELOPE_FIELDS = {
    "run_id", "task_id", "principal", "target", "destination", "operation",
    "resource_ids", "effect", "state_version", "amount_minor", "currency", "credential_ref",
}


def number(value):
    """RFC 8785 section 3.2.2.3: ECMAScript Number::toString of an IEEE-754 double.

    Python ints must be safe integers: larger ones cannot cross a JSON/JavaScript
    verifier exactly (I-JSON). Floats are already doubles; repr() yields the
    shortest round-trip digits that ECMAScript also requires.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabError("Not a JSON number")
    if isinstance(value, int):
        if abs(value) > SAFE_INTEGER:
            raise LabError("Consent integers must be safe integers (|n| <= 2^53-1)")
        return str(value)
    if not math.isfinite(value):
        raise LabError("Consent numbers must be finite")
    if value == 0:
        return "0"  # Including negative zero.
    if value < 0:
        return "-" + number(-value)
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    scale = int(exponent or 0) - len(fraction)
    significant = digits.rstrip("0")
    scale += len(digits) - len(significant)
    k, n = len(significant), scale + len(significant)  # value = 0.d1..dk * 10^n
    if k <= n <= 21:
        return significant + "0" * (n - k)
    if 0 < n <= 21:
        return significant[:n] + "." + significant[n:]
    if -6 < n <= 0:
        return "0." + "0" * -n + significant
    sign = "+" if n - 1 > 0 else "-"
    head = significant[0] + ("." + significant[1:] if k > 1 else "")
    return f"{head}e{sign}{abs(n - 1)}"


def canonical(value):
    """RFC 8785 JSON canonicalization (UTF-16 key order, ECMAScript numbers).

    No Unicode normalization; unpaired surrogates, NaN, Infinity and unsafe
    integers are rejected. Integer-valued floats and integers share one form,
    including negative zero. Money stays integer minor units in the envelope
    (see binding); other numbers, such as schema bounds, may be fractional.
    """
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return number(value)
    if isinstance(value, str):
        try:
            value.encode("utf-16-be")
        except UnicodeEncodeError as error:
            raise LabError("Unpaired surrogate in consent data") from error
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        names = {key: canonical(key) for key in value}  # Rejects unpaired surrogates before sorting.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(names[key] + ":" + canonical(value[key]) for key in keys) + "}"
    raise LabError("Unsupported consent value")


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fractional(value):
    """Whether a canonicalizable JSON value contains a non-integer number."""
    if isinstance(value, float):
        return not value.is_integer()
    if isinstance(value, dict):
        return any(fractional(item) for item in value.values())
    return isinstance(value, list) and any(fractional(item) for item in value)


def violation(error):
    """Keyword, JSON pointer and offending property names; never instance values."""
    detail = f"{error.validator} at /" + "/".join(str(part) for part in error.absolute_path)
    names = []
    if isinstance(error.instance, dict) and error.validator == "required":
        names = [name for name in error.validator_value if name not in error.instance]
    elif isinstance(error.instance, dict) and error.validator == "additionalProperties":
        names = sorted(key for key in error.instance if key not in error.schema.get("properties", {}))
    return detail + (" (" + ", ".join(map(str, names))[:160] + ")" if names else "")


def effective_arguments(schema, arguments):
    result = copy.deepcopy(arguments)
    def validate_schema(node):
        if isinstance(node, dict):
            if "$ref" in node and not node["$ref"].startswith("#"):
                raise LabError("Consent schema references must be local")
            for value in node.values():
                validate_schema(value)
        elif isinstance(node, list):
            for value in node:
                validate_schema(value)
    validate_schema(schema)
    def defaults(node, value):
        if isinstance(value, dict):
            for key, field in node.get("properties", {}).items():
                if key not in value and "default" in field:
                    value[key] = copy.deepcopy(field["default"])
                if key in value:
                    defaults(field, value[key])
        elif isinstance(value, list):
            for item in value:
                defaults(node.get("items", {}), item)
    defaults(schema, result)
    errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda e: list(map(str, e.path)))
    if errors:
        raise LabError(f"Consent arguments violate schema: {violation(errors[0])}")
    canonical(result)
    return result


def binding(envelope, schema, arguments):
    if set(envelope) != ENVELOPE_FIELDS:
        raise LabError("Consent envelope must include every authorization field and no extras")
    if envelope["effect"] not in ("read", "draft", "commit", "cancel"):
        raise LabError("Unknown consent effect")
    if type(envelope["amount_minor"]) is not int or envelope["amount_minor"] < 0:
        raise LabError("Consent amount must be integer minor units")
    if not isinstance(envelope["resource_ids"], list) or not all(
            isinstance(item, str) and item for item in envelope["resource_ids"]):
        raise LabError("Consent resources must be explicit string identifiers")
    for field in ENVELOPE_FIELDS - {"resource_ids", "amount_minor"}:
        if not isinstance(envelope[field], str) or not envelope[field]:
            raise LabError(f"Missing consent field {field}")
    effective = effective_arguments(schema, arguments)
    schema_sha256, arguments_sha256 = digest(schema), digest(effective)  # Rejects non-finite values first.
    return {**envelope, "canonicalizer_version": CANONICALIZER,
            "numeric_domain": FRACTIONAL_DOMAIN if fractional(schema) or fractional(effective) else NUMERIC_DOMAIN,
            "schema_sha256": schema_sha256, "arguments_sha256": arguments_sha256}


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise LabError("Consent clocks must include a timezone")
    return result


class ConsentBroker:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS consent_grants(
                id TEXT PRIMARY KEY, binding TEXT NOT NULL, digest TEXT NOT NULL,
                expires_at TEXT NOT NULL, status TEXT NOT NULL)""")

    def issue(self, envelope, schema, arguments, *, decision, now, expires_at, transaction=None):
        if decision != "approve_once":
            raise LabError("Consent denied")
        if instant(expires_at) <= instant(now):
            raise LabError("Grant expiration must be in the future")
        bound = binding(envelope, schema, arguments)
        identifier = str(uuid.uuid4())
        owned = transaction is None
        db = transaction or sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("INSERT INTO consent_grants VALUES (?,?,?,?,?)", (
                identifier, canonical(bound), digest(bound), expires_at, "available"))
            if owned:
                db.commit()
        except BaseException:
            if owned:
                db.rollback()
            raise
        finally:
            if owned:
                db.close()
        return {"id": identifier, "binding": bound, "expires_at": expires_at}

    def revoke(self, identifier):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE consent_grants SET status='revoked' WHERE id=? AND status='available'",
                       (identifier,))

    def check(self, identifier, envelope, schema, arguments, *, now, transaction):
        """Validate a grant within the caller's transaction, without consuming it."""
        bound = binding(envelope, schema, arguments)
        row = transaction.execute("SELECT digest,expires_at,status FROM consent_grants WHERE id=?",
                                  (identifier,)).fetchone()
        if not row or row[2] != "available":
            raise LabError("Grant absent, revoked, or already consumed")
        if instant(row[1]) <= instant(now):
            raise LabError("Grant expired")
        if row[0] != digest(bound):
            raise LabError("Grant binding changed; a new decision is required")
        return bound

    def consume(self, identifier, envelope, schema, arguments, *, now, transaction=None):
        owned = transaction is None
        db = transaction or sqlite3.connect(self.path, timeout=10)
        try:
            if owned:
                db.execute("BEGIN IMMEDIATE")
            bound = self.check(identifier, envelope, schema, arguments, now=now, transaction=db)
            db.execute("UPDATE consent_grants SET status='consumed' WHERE id=?", (identifier,))
            if owned:
                db.commit()
        except BaseException:
            if owned:
                db.rollback()
            raise
        finally:
            if owned:
                db.close()
        return bound
