import bisect
import functools
import html
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from . import __version__
from .serialization import MAX_DOCUMENT_DEPTH, canonical_bytes, digest, file_digest

_CREDENTIAL_VARIABLES = r"authorization|cookie|password|secret|token|api.?key|access.?key|storage.state"
SECRET_KEYS = re.compile(rf"({_CREDENTIAL_VARIABLES}|contact|email|phone)", re.IGNORECASE)
# Contact-named variables (EMAIL, GIT_AUTHOR_EMAIL, PHONE) hold the developer's own details:
# evidence hides their values, but they are no credentials (PersonalValue).
CREDENTIAL_VARIABLES = re.compile(rf"({_CREDENTIAL_VARIABLES})", re.IGNORECASE)
# Exact lowercase names that other lanes use to filter headers and to register
# values as secrets for the model credential guard. Kept as-is: widening it would
# turn personal example data into "credentials" the guard refuses to send.
PRIVATE_VALUE_KEYS = {
    "authorization", "cookie", "set-cookie", "password", "secret", "token",
    "access_token", "refresh_token", "api_key", "secret_key", "contact", "email", "phone",
    "x-amz-security-token", "x-api-key", "x-goog-api-key", "x-auth-token",
    "secret_access_key", "access_key_id", "session_token",
    "intake_access_token", "selection_token", "client_secret",
    "polling_token", "resume_token", "review_token",
    "card_number", "cardnumber", "cvc", "cvv", "security_code",
    "body_base64",  # Recovery receipts may encode personal data that text redaction cannot inspect.
}
# Redaction-only rules (compared after camelCase/kebab-case normalization).
# Credentials are hidden everywhere, including model-facing views.
CREDENTIAL_VALUE_KEYS = (PRIVATE_VALUE_KEYS - {"contact", "email", "phone"}) | {
    "proxy-authorization", "cookies", "id_token", "apikey", "session_id", "sessionid",
    "otp", "one_time_code", "verification_code", "auth_code", "authorization_code",
}
# ``clean`` hides only these personal fields; model-facing views (``mask_credentials``)
# hide none of them: the model must see the user's data (P0.5).
CONTACT_VALUE_KEYS = {"contact", "email", "phone"}
# Evidence written to disk and every export (``clean_evidence``) also hides these.
PERSONAL_VALUE_KEYS = CONTACT_VALUE_KEYS | {
    "e-mail", "email_address", "phone_number", "telephone", "mobile",
    "full_name", "first_name", "last_name", "given_name", "family_name",
    "birthday", "date_of_birth", "dob",
    "license_number", "drivers_license", "ssn", "social_security_number", "passport_number", "tax_id",
    "address", "street", "street_address", "address_line1", "address_line2",
    "billing_address", "shipping_address", "home_address", "mailing_address",
    "postal_code", "zip", "zip_code",
}
REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 4
# Configuration words and token types, not credentials; registering them would
# rewrite ordinary JSON ("additionalProperties": false) and trip the model guard.
NON_SECRET_VALUES = frozenset({
    "true", "false", "yes", "no", "on", "off", "null", "none", "nil", "undefined",
    "bearer", "basic", "digest", "dpop", "negotiate", "[redacted]", "redacted", "[email]",
})
# Exact harness vocabulary: statuses and event-envelope constants. Never admitted as
# secrets and never rewritten, so a learned "completed" or a fragment of "developer"
# cannot corrupt a status, an envelope or a seal.
PUBLIC_WORDS = frozenset({
    "passed", "failed", "completed", "blocked", "pending", "running", "paused", "interrupted",
    "observation-gap", "not-exercised", "unavailable", "observed", "skipped", "captured", "excluded",
    "model", "developer", "evaluated-agent", "scripted-host", "redacted",
})
ENVIRONMENT_MINIMUM = 8
# Inside machine values only exact or long secrets are replaced: a shorter match in a
# random hex digest or a timestamp is a coincidence, not a disclosure.
MACHINE_SECRET_MINIMUM = 16
# Values the harness computes: digests, generated run/clock/event IDs, evidence-relative
# references and ISO timestamps. Seals, locks and links compare them exactly.
MACHINE_VALUE = re.compile(
    r"[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}"
    r"|(?:agent/)?(?:r|surface|browser|worker|connector|guidelines)-[0-9a-f]{32}(?::[1-9][0-9]*)?"
    r"|(?:[a-z0-9_-]+/)*artifacts/[0-9a-f]{64}\.(?:json|zip)"
    r"|(?:agent/r-[0-9a-f]{32}/)?inspect/trial-[0-9]+\.eval"
    r"|browser(?:-attempt-[0-9]+)?"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
MACHINE_LIMIT = 200
# A run's public identity as the harness registers it: run, scenario, profile and clock
# names. No "@", "/", "?" or "=", so no contact detail, path or URL can be registered.
IDENTITY_VALUE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._:+-]{0,198}[A-Za-z0-9])?")
BEARER_VALUE = re.compile(r"(?i)(bearer\s+)[^\s\"'<>]+")
# Deeper content is replaced, not recursed into: no RecursionError, and every
# redacted document stays within the nesting limit that sealing reloads with.
MAX_REDACTION_DEPTH = MAX_DOCUMENT_DEPTH
NESTING_MARKER = f"[REDACTED: nested deeper than {MAX_REDACTION_DEPTH} levels]"
# Exactly the markers this redactor writes; look-alikes in service text get no protection.
MARKERS = re.compile("|".join(re.escape(marker) for marker in (NESTING_MARKER, REDACTED, "[EMAIL]")))
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
# Harness-authored numbers at the top level of a record (run locks, metrics, timings, sizes and envelope
# counters): never the private value they may equal by chance, so numeric redaction (Redactor.numbers)
# keeps them and leak scans skip them. Hiding a known default (the 16384 result bound) would itself
# reveal an equal private value.
HARNESS_NUMBERS = frozenset({"run_lock", "metrics", "timing", "started_clock", "finished_clock", "trial",
                             "elapsed_ms", "characters", "bytes", "monotonic_ns", "sequence", "event_schema_version",
                             "schema_version", "artifacts", "exit_code"})
# JSON Schema and OpenAPI maps keyed by property names, not by data fields.
SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "schemas"})
# A child of a schema map is a schema only if it is a boolean, empty, or uses schema keywords;
# a data map that happens to be called "properties" still gets the private-key rules.
SCHEMA_KEYWORDS = frozenset({
    "type", "$ref", "properties", "items", "prefixItems", "enum", "const", "anyOf", "oneOf", "allOf",
    "not", "format", "description", "title", "default", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "minItems",
    "maxItems", "uniqueItems", "required", "additionalProperties", "patternProperties", "propertyNames",
    "examples", "nullable", "readOnly", "writeOnly", "deprecated", "$schema", "$id", "$defs",
    "contentMediaType", "contentEncoding", "if", "then", "else", "dependentRequired", "contains",
})
URL_CREDENTIAL_PARAMETERS = frozenset({
    "code", "auth_code", "key", "sig", "signature", "credential", "x_amz_signature", "x_amz_credential",
    "x_goog_signature", "x_goog_credential",
})
QUERY_PARAMETER = re.compile(r"""([?&#])([^=\s?&#"'<>]+)=([^&#\s"'<>]+)""")
EMAIL_ADDRESS = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@functools.lru_cache(maxsize=8192)
def normalized_key(key):
    """Compare camelCase, kebab-case and spaced names as snake_case."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


_CREDENTIAL_NAMES = frozenset(normalized_key(key) for key in CREDENTIAL_VALUE_KEYS)
_PERSONAL_NAMES = frozenset(normalized_key(key) for key in PERSONAL_VALUE_KEYS)
_CONTACT_NAMES = frozenset(normalized_key(key) for key in CONTACT_VALUE_KEYS)
_CREDENTIAL_WORDS = frozenset({"token", "secret", "secrets", "password", "passwd", "passphrase",
                               "credential", "credentials"})
_CREDENTIAL_VALUE_WORDS = frozenset({"value", "confirmation", "hash", "digest", "raw", "plain", "text", "string"})
_CREDENTIAL_SUFFIXES = ("api_key", "apikey", "access_key", "secret_key", "private_key",
                        "authorization", "cookie", "session_id")


@functools.lru_cache(maxsize=8192)
def credential_key(key):
    """Credential-bearing names; counts and types such as max_tokens or token_type are not."""
    name = normalized_key(key)
    words = name.split("_")
    return (name in _CREDENTIAL_NAMES or name.endswith(_CREDENTIAL_SUFFIXES)
            or words[-1] in _CREDENTIAL_WORDS
            or (words[-1] in _CREDENTIAL_VALUE_WORDS and not _CREDENTIAL_WORDS.isdisjoint(words)))


# Service responses carry credentials under any name that mentions one ("password_reset_url",
# "secret_answer", "csrftoken"); SurfaceRun has always learned them. Names that describe a
# credential instead of holding one are not learned: counts, types, limits, expiry and status.
_LEARNED_WORDS = ("token", "secret", "password", "passwd", "passphrase", "credential")
_DESCRIPTOR_WORDS = frozenset({
    "type", "types", "kind", "scheme", "format", "algorithm", "count", "counts", "used", "remaining",
    "max", "min", "limit", "limits", "length", "expires", "expiry", "expiration", "expired", "ttl",
    "lifetime", "status"})


@functools.lru_cache(maxsize=8192)
def learned_key(key):
    """Names whose string values ``Redactor.learn`` registers from a service response: every
    credential name (``status_token`` too), and any other name that mentions a credential
    unless it describes one (max_tokens, token_type, tokens_used, token_expires_at, token_ttl)."""
    name = normalized_key(key)
    return credential_key(key) or (_DESCRIPTOR_WORDS.isdisjoint(name.split("_"))
                                   and any(word in name for word in _LEARNED_WORDS))


def _value_shaped(key):
    """A field that holds the credential of the container it is in: {"token": {"value": ...}}."""
    return normalized_key(key).split("_")[-1] in _CREDENTIAL_VALUE_WORDS


def private_key(key, personal=True):
    """Credential names, plus every personal-data field (``personal``), only contact details
    (False), or nothing else (None: model-facing views)."""
    if personal is None:
        return credential_key(key)
    return credential_key(key) or normalized_key(key) in (_PERSONAL_NAMES if personal else _CONTACT_NAMES)


def _strings(value):
    """Flatten declarations to their strings; mappings contribute their values."""
    return (item for item in _leaves(value) if isinstance(item, str))


def _leaves(value):
    """Flatten nested declarations; mappings contribute their values, never their keys."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(reversed(list(item.values())))
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(reversed(list(item)))
        else:
            yield item


def admissible_secret(value):
    """The stored form of a secret, or None for empty, short, ordinary or harness values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)  # A numeric credential (a PIN, a captured ID) appears as its decimal text.
    elif isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str):
        raise TypeError("Secrets must be strings or numbers")
    value = value.strip()
    folded = value.lower()
    if len(value) < MIN_SECRET_LENGTH or folded in NON_SECRET_VALUES or folded in PUBLIC_WORDS \
            or MARKERS.fullmatch(value):
        return None
    return value


class PersonalValue(str):
    """A contact detail from the environment, such as the developer's own email address.

    Evidence hides it like any registered secret, but it is no credential: it may be the
    user's own data (P0.5), so model views show it and the provider-boundary credential
    guard ignores it (``Redactor.credentials``). Copies of a secret list keep the marker.
    """
    __slots__ = ()


# Lanes register secrets from several threads; admission is a check followed by an insert.
_SECRET_LOCK = threading.RLock()


def _secret_order(value):
    return -len(value), value


class SecretList(list):
    """Registered secrets: unique, longest first, never empty, trivially short or public.

    Many lanes append to ``Redactor.secrets`` directly, so every list mutator applies
    the admission rules of ``Redactor.add``. A shorter secret can then never be replaced
    first and leave part of a longer one visible. ``public`` holds the run's registered
    identity (``Redactor.protect``), which is never a secret.
    """

    def __init__(self, values=(), *, public=None):
        super().__init__()
        self.public = set() if public is None else public
        self.extend(values)

    def _admit(self, values):
        public = self.__dict__.get("public", ())  # Not yet restored while unpickling.
        with _SECRET_LOCK:
            for item in values:
                for leaf in _leaves(item):
                    secret = admissible_secret(leaf)
                    if secret is None or secret in public:
                        continue
                    if isinstance(leaf, PersonalValue):
                        secret = PersonalValue(secret)
                        self.__dict__["personal"] = True  # See Redactor._replace_secrets.
                    if list.__contains__(self, secret):
                        # Fail closed: the same text registered as a credential stays a credential.
                        index = list.index(self, secret)
                        if isinstance(list.__getitem__(self, index), PersonalValue) \
                                and not isinstance(secret, PersonalValue):
                            list.__setitem__(self, index, secret)
                        continue
                    position = bisect.bisect_left(self, _secret_order(secret), key=_secret_order)
                    list.insert(self, position, secret)
                    if MARKERS.search(secret):
                        self.__dict__["spanning"] = True  # See Redactor._replace_secrets.

    def append(self, value):
        self._admit([value])

    def add(self, value):
        self._admit([value])

    def insert(self, index, value):  # Position is not meaningful; order is by length.
        self._admit([value])

    def extend(self, values):
        # A bare string is one secret, and a mapping contributes its values, never its keys.
        self._admit([values] if isinstance(values, (str, dict)) else list(values))

    def discard(self, value):
        with _SECRET_LOCK:
            while list.__contains__(self, value):
                list.remove(self, value)

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        if count <= 0:
            self.clear()
        return self

    def __setitem__(self, index, value):
        with _SECRET_LOCK:
            items = list(self)
            items[index] = value
            list.clear(self)
            self._admit(items)

    def sort(self, *args, **kwargs):
        """Already ordered longest first; kept for callers that re-sort."""

    def reverse(self):
        """Reversal would let a shorter secret run first; the order is fixed."""

    def __contains__(self, value):
        return super().__contains__(value) or (
            isinstance(value, str) and super().__contains__(value.strip()))


def _environment_value(value):
    return (len(value) >= ENVIRONMENT_MINIMUM and value.lower() not in NON_SECRET_VALUES
            and not NUMBER.fullmatch(value) and len(set(value)) >= 3)


def _opaque(segment):
    """A token-like path segment: an unbroken alphanumeric run of 16+ characters that mixes
    digits with letters or lower with upper case, or of 24+ characters. Names joined by
    separators ("test_settings_0", "storage-state.json") are not."""
    for run in re.findall(r"[A-Za-z0-9]{16,}", segment):
        digits, lower, upper = (bool(re.search(pattern, run)) for pattern in (r"\d", "[a-z]", "[A-Z]"))
        if (digits and (lower or upper)) or (lower and upper) or len(run) >= 24:
            return True
    return False


def credential_values(value):
    """Credential candidates in one secret-named environment value.

    Booleans, numbers, short values, plain local paths and URLs without embedded
    credentials are configuration, not secrets. Opaque path segments (webhook and
    bot tokens, base64 blobs) and URL user info or credential parameters are.
    """
    value = value.strip() if isinstance(value, str) else ""
    if not _environment_value(value):
        return []
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", value):
        try:
            parts = urlsplit(value)
            embedded = [parts.username, parts.password, *(
                item for key, item in parse_qsl(f"{parts.query}&{parts.fragment}", keep_blank_values=True)
                if private_key(key) or normalized_key(key) in URL_CREDENTIAL_PARAMETERS)]
        except ValueError:
            return [value]
        embedded = [unquote(item) for item in embedded if item]
        embedded += [segment for segment in map(unquote, parts.path.split("/")) if _opaque(segment)]
        return [value, *(item for item in embedded if _environment_value(item))] if embedded else []
    if re.match(r"(?:~|\.{1,2})?[\\/]|[A-Za-z]:[\\/]", value) \
            and not any(_opaque(segment) for segment in re.split(r"[\\/]", value)):
        return []
    scheme = re.fullmatch(r"(?i)(?:bearer|token)\s+(\S+)", value)
    return [value, *([scheme[1]] if scheme and _environment_value(scheme[1]) else [])]


def environment_secrets(environment=None):
    """Values of secret-named variables that can plausibly be credentials. Values of
    contact-named variables without a credential word (EMAIL, GIT_AUTHOR_EMAIL) are
    PersonalValue: hidden in evidence, never a credential."""
    environment = os.environ if environment is None else environment
    return [item if CREDENTIAL_VARIABLES.search(key) else PersonalValue(item)
            for key, value in environment.items() if SECRET_KEYS.search(key)
            for item in credential_values(value)]


@functools.lru_cache(maxsize=64)
def _numbers(secrets):
    found = set()
    for secret in secrets:
        text = secret.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            found.add(int(text))
        elif NUMBER.fullmatch(text) and math.isfinite(number := float(text)):
            found.add(number)
    return frozenset(found)


def numeric_values(secrets) -> frozenset:
    """The registered values that are numbers in text ("94103", "4815.1623"), as numbers: a service may
    echo one as a JSON number (94103 or 94103.0), which text redaction never sees."""
    return _numbers(tuple(secret for secret in secrets if isinstance(secret, str)))


def _redact_private(item):
    # Booleans and null carry no secret; numbers and whole subtrees can.
    if item is None or isinstance(item, bool):
        return item
    return REDACTED if isinstance(item, (str, int, float, dict, list, tuple)) else item


def _schema(value):
    return isinstance(value, bool) or (isinstance(value, dict) and (
        not value or not SCHEMA_KEYWORDS.isdisjoint(value)))


# Brackets that can begin a JSON container; snapshot markers such as [ref=e12] cannot.
JSON_START = re.compile(r"""\{(?=\s*["}])|\[(?=\s*[-0-9"\[{\]tfn])""")


def _bracket_ends(text):
    """Where each balanced array or object in `text` ends, by its start: one linear pass, strings respected."""
    ends, opened, index, length = {}, [], 0, len(text)
    while index < length:
        char = text[index]
        if char == '"':
            index += 1
            while index < length and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
        elif char in "[{":
            opened.append(index)
        elif char in "]}" and opened:
            ends[opened.pop()] = index + 1
        index += 1
    return ends
# Python reprs and invalid JSON fragments that the embedded-JSON scan cannot parse.
_PAIR = re.compile(r"""(["'])([^"'\\\n]{1,64})\1(\s*:\s*)("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|-?\d[\d.eE+-]*)""")


def _redact_pairs(value, personal=True):
    def replace(match):
        if not private_key(match[2], personal):
            return match[0]
        quote = match[4][0] if match[4][0] in "\"'" else '"'
        return f"{match[1]}{match[2]}{match[1]}{match[3]}{quote}{REDACTED}{quote}"
    return _PAIR.sub(replace, value)


@functools.lru_cache(maxsize=8)
def _home(environment):
    """The home directory that evidence shows as "~", for the current HOME, with the pattern
    that matches it as a whole path prefix; None for a root such as "/", which would turn
    every path and URL into "~" text."""
    home = Path(environment) if environment else Path.home()
    if home.parent == home:
        return None
    # Never inside a longer name: HOME=/Users/gab keeps /Users/gabriel and /Users/gab.bak.
    return str(home), re.compile(re.escape(str(home)) + r"(?![\w-]|\.\w)")


class Redactor:
    def __init__(self, secrets=(), *, numbers=False):
        self._public = set()
        self._secrets = SecretList(public=self._public)
        # Evidence views also hide numbers equal to a registered value (numeric_values), except harness
        # numbers at a record's top level (HARNESS_NUMBERS); opt-in per lane.
        self.numbers = numbers
        self.add(secrets)

    @property
    def secrets(self):
        return self._secrets

    @secrets.setter
    def secrets(self, values):
        # Always rebuilt, never shared, so admission and this run's identity still apply.
        self._secrets = SecretList(values, public=self._public)

    @property
    def credentials(self):
        """Registered secrets except contact details from the environment (PersonalValue): what
        the provider-boundary guard refuses and model-facing views hide."""
        return [secret for secret in self.secrets if not isinstance(secret, PersonalValue)]

    @property
    def _walk(self):
        # Traversal state is per thread; one redactor may serve several writers.
        state = self.__dict__.get("_walk_state")
        if state is None:
            state = self.__dict__.setdefault("_walk_state", threading.local())
        return state

    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop("_walk_state", None)
        return state

    def add(self, secrets):
        self.secrets.extend([secrets] if isinstance(secrets, (str, dict)) else secrets)

    def protect(self, *values):
        """Register the run's own identity (run, scenario, profile and clock names) as public.

        These are generated by the harness or declared by the scenario author, never
        captured from a target. Equal secrets are dropped and the exact values are never
        rewritten, so a token that happens to equal the scenario ID or the profile name
        cannot make a redacted result disagree with the envelopes it is sealed with.
        """
        for value in values:
            if isinstance(value, str) and value not in self._public and IDENTITY_VALUE.fullmatch(value) \
                    and not BEARER_VALUE.match(value):
                with _SECRET_LOCK:  # Atomic with admission: no concurrent append can re-add it.
                    self._public.add(value)
                    self._secrets.discard(value)

    def learn(self, value):
        """Register strings that a service response holds under credential names, iteratively.

        A string under a learned name (``learned_key``) or a list of strings directly under one
        is a credential. A credential-named container is searched, never flattened: only its
        value-shaped fields ({"token": {"value": ...}}) and credential-named fields are, so
        {"credentials": {"kind": "host"}} registers nothing that could rewrite evidence.
        """
        found, pending = [], [(value, False)]
        while pending:
            item, container = pending.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in SCHEMA_MAPS and isinstance(child, dict):
                        # Keyed by property names, as in clean(): a "token" schema holds no token
                        # ("string" is no secret). Schemas are scanned; data entries keep the rules.
                        pending.extend((schema, False) for schema in child.values()
                                       if isinstance(schema, dict) and _schema(schema))
                        pending.append(({name: entry for name, entry in child.items() if not _schema(entry)},
                                        container))
                        continue
                    learned = learned_key(key)
                    if isinstance(child, str):
                        if learned or container and _value_shaped(key):
                            found.append(child)
                    elif isinstance(child, (dict, list, tuple)):
                        if learned and isinstance(child, (list, tuple)):
                            found.extend(entry for entry in child if isinstance(entry, str))
                        pending.append((child, container or learned))
            elif isinstance(item, (list, tuple)):
                pending.extend((child, container) for child in item if isinstance(child, (dict, list, tuple)))
        self.add(found)
        return [item for item in found if item.strip() in self.secrets]

    @classmethod
    def environment(cls):
        return cls(environment_secrets())

    def _replace_secrets(self, value, minimum=0):
        """Replace every occurrence, merging overlaps, without rewriting earlier markers.

        Model-facing views (``mask_credentials``) replace credentials only."""
        secrets = self.credentials if self.secrets.__dict__.get("personal") \
            and getattr(self._walk, "personal", False) is None else self.secrets
        if minimum:
            secrets = [secret for secret in secrets if len(secret) >= minimum]
        if not secrets or not value:
            return value
        if self.secrets.__dict__.get("spanning") and (
                spanning := [secret for secret in secrets if MARKERS.search(secret)]):
            # A secret that contains a marker can only match literally, across that marker.
            for secret in spanning:
                value = value.replace(secret, REDACTED)
            secrets = [secret for secret in secrets if secret not in spanning]
        pieces, cursor = [], 0
        for marker in [*MARKERS.finditer(value), None]:
            end = marker.start() if marker else len(value)
            segment, spans = value[cursor:end], []
            length = len(segment)
            for secret in secrets:
                if len(secret) > length:
                    continue
                position = segment.find(secret)
                while position >= 0:
                    spans.append((position, position + len(secret)))
                    position = segment.find(secret, position + 1)
            if spans:
                spans.sort()
                merged = [list(spans[0])]
                for start, stop in spans[1:]:
                    if start < merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], stop)
                    else:
                        merged.append([start, stop])
                last, parts = 0, []
                for start, stop in merged:
                    parts.extend((segment[last:start], REDACTED))
                    last = stop
                segment = "".join(parts) + segment[last:]
            pieces.append(segment)
            if marker:
                pieces.append(marker[0])
                cursor = marker.end()
        return "".join(pieces)

    def _protected(self, value):
        """Public words, this run's registered identity and machine values; None for other text."""
        if value in PUBLIC_WORDS or value in self._public:
            return value
        if len(value) <= MACHINE_LIMIT and MACHINE_VALUE.fullmatch(value):
            return REDACTED if value in self._secrets else self._replace_secrets(value, MACHINE_SECRET_MINIMUM)
        return None

    @staticmethod
    def _patterns(value, personal=True):
        # Consumer login/review links may carry newly minted credentials in
        # query strings, including inside a text or JSON tool response.
        def parameter(match):
            name = unquote(match[2])
            # A model-facing view (personal None) hides credential names only: a generic "code"
            # or "key" may be the user's own coupon or booking reference.
            if private_key(name, personal) or personal is not None \
                    and normalized_key(name) in URL_CREDENTIAL_PARAMETERS:
                return f"{match[1]}{match[2]}={REDACTED}"
            return match[0]
        if "=" in value:
            value = QUERY_PARAMETER.sub(parameter, value)
        value = BEARER_VALUE.sub(rf"\1{REDACTED}", value)
        # A model-facing view (personal None) keeps the user's own addresses.
        return EMAIL_ADDRESS.sub("[EMAIL]", value) if "@" in value and personal is not None else value

    def _embedded(self, value):
        """Clean JSON embedded in text; return the text with its unparsed remainder."""
        decoder, walk = json.JSONDecoder(), self._walk
        pieces, cursor = [], 0
        # A failed parse costs a scan to the error plus a line count over the text
        # before it, so unterminated nesting is quadratic. Past this budget the
        # remainder is plain text (secrets, pairs and patterns still apply).
        budget = 32 * len(value) + 1_000_000
        ends = None
        for match in JSON_START.finditer(value):
            start = match.start()
            if start < cursor:
                continue
            try:
                parsed, end = decoder.raw_decode(value, start)
            except json.JSONDecodeError as error:
                budget -= error.pos + 1
            except RecursionError:
                # Deeper than this interpreter's parser goes (before Python 3.14, about 1,000 levels), so far
                # deeper than MAX_REDACTION_DEPTH: a balanced value is replaced whole, as its deep part would be.
                ends = _bracket_ends(value) if ends is None else ends
                if (end := ends.get(start)) is not None:
                    pieces.extend((value[cursor:start], json.dumps(NESTING_MARKER)))
                    cursor = end
                    continue
                budget -= 2 * len(value)
            else:
                if isinstance(parsed, (dict, list)):
                    walk.depth = getattr(walk, "depth", 0) + 1
                    try:
                        cleaned = self.clean(parsed)
                    finally:
                        walk.depth -= 1
                    pieces.extend((value[cursor:start], json.dumps(cleaned, ensure_ascii=False)))
                    cursor = end
                continue
            if budget < 0:
                break
        return "".join(pieces) + value[cursor:] if pieces else value

    def text(self, value: str):
        personal = getattr(self._walk, "personal", False)
        # Evidence shows the home directory as "~"; a model-facing view keeps local paths as they are.
        if personal is not None and (home := _home(os.environ.get("HOME"))) and home[0] in value:
            value = home[1].sub("~", value)
        # Tool responses and shell arguments often embed JSON inside a string.
        # Scrub its typed fields too; replacing environment secrets alone misses
        # newly returned contact details and session credentials.
        if "{" in value or "[" in value:
            value = self._embedded(value)
        if '"' in value or "'" in value:
            value = _redact_pairs(value, personal)
        return self._patterns(self._replace_secrets(value), personal)

    def _mapping(self, value):
        walk = self._walk
        personal, result = getattr(walk, "personal", False), {}
        top = getattr(walk, "depth", 0) == 1  # A record itself, not data nested in it or embedded in text.
        for key, item in value.items():
            if key in SCHEMA_MAPS and isinstance(item, dict):
                # Property definitions are schemas, not values; data under them is still checked.
                result[key] = {name: self.clean(schema) if _schema(schema)
                               else _redact_private(schema) if private_key(name, personal) else self.clean(schema)
                               for name, schema in item.items()}
            elif private_key(key, personal):
                result[key] = _redact_private(item)
            elif top and key in HARNESS_NUMBERS:
                previous, walk.harness = getattr(walk, "harness", False), True
                try:
                    result[key] = self.clean(item)
                finally:
                    walk.harness = previous
            else:
                result[key] = self.clean(item)
        return result

    def clean(self, value):
        """Credentials, registered secrets, contact details and credential patterns.

        Model-facing views use ``mask_credentials`` instead. Records written to
        disk use ``clean_evidence``, which also hides other personal data.
        """
        walk = self._walk
        depth = getattr(walk, "depth", 0)
        if isinstance(value, (dict, list, tuple)) and depth >= MAX_REDACTION_DEPTH:
            return NESTING_MARKER  # Redact the remainder instead of exhausting the stack.
        walk.depth = depth + 1
        try:
            if isinstance(value, dict):
                return self._mapping(value)
            if isinstance(value, (tuple, list)):
                return [self.clean(item) for item in value]
            if isinstance(value, str):
                protected = self._protected(value)
                return self.text(value) if protected is None else protected
            if isinstance(value, (int, float)) and not isinstance(value, bool) and self.__dict__.get("numbers") \
                    and getattr(walk, "personal", False) is not None and not getattr(walk, "harness", False) \
                    and value in numeric_values(self.secrets):
                return REDACTED  # A registered value echoed as a JSON number (evidence views only).
            return value
        finally:
            walk.depth = depth

    def clean_evidence(self, value):
        """Redaction for records written to disk and for exports.

        ``clean`` plus personal-data fields (names, birth dates, identity documents,
        addresses), which other lanes' model-facing ``clean`` calls must not hide.
        """
        walk = self._walk
        previous, walk.personal = getattr(walk, "personal", False), True
        try:
            return self.clean(value)
        finally:
            walk.personal = previous

    def mask_credentials(self, value):
        """A model-facing copy: hide credentials, keep the user's own data visible.

        Registered credentials, credential fields, bearer tokens and credential-named URL
        parameters, also inside embedded JSON, exactly as ``clean`` finds them. No personal-data,
        contact or email rules, no environment contact details (PersonalValue) and no "~" for
        the home directory. Evidence written to disk must still go through ``clean_evidence``.
        """
        walk = self._walk
        previous, walk.personal = getattr(walk, "personal", False), None
        try:
            return self.clean(value)
        finally:
            walk.personal = previous


def dependency_inventory():
    return sorted(
        [{"name": dist.metadata["Name"], "version": dist.version,
          "license": dist.metadata.get("License-Expression") or dist.metadata.get("License")
          or "unknown"} for dist in importlib.metadata.distributions()],
        key=lambda item: item["name"].lower(),
    )


def run_lock(*, profile: dict, model=None, target=None, scenario=None):
    root = Path(__file__).resolve().parents[2]
    package_root = Path(__file__).resolve().parent
    dependency_lock = root / "uv.lock"
    if not dependency_lock.exists():
        dependency_lock = package_root / "data/locks/uv.lock"
    source_hashes = {str(p.relative_to(package_root)): file_digest(p)
                     for p in sorted(package_root.rglob("*"))
                     if p.is_file() and "__pycache__" not in str(p)}
    return {
        "schema_version": 1, "harness": __version__, "platform": platform.platform(),
        "python": platform.python_version(), "dependencies": dependency_inventory(),
        "dependency_lock_sha256": file_digest(dependency_lock) if dependency_lock.exists() else None,
        "source_hashes": source_hashes, "interface_profile": profile,
        "evidence_registry": json.loads((package_root / "data/evidence-registry.json").read_text()),
        "evidence_registry_sha256": file_digest(package_root / "data/evidence-registry.json"),
        "protocol_revision": profile.get("protocol_revision"),
        "model": model, "target": target, "scenario_sha256": digest(scenario) if scenario else None,
        "browser": None, "effective_policy": {"external_agent_egress": "deny",
            "commits": "unsupported", "hosted_judge": False, "fallback": "disabled"},
        "provider_transformations": "captured where upstream ModelCall is available; otherwise unknown",
    }


def write_artifact(path, redactor, value, media_type="application/json", *, redacted=False):
    """Write a content-addressed artifact; ``redacted`` values were already cleaned by the caller."""
    from .replay import evidence_path
    cleaned = value if redacted else redactor.clean_evidence(value)
    data = canonical_bytes(cleaned)
    key = digest(cleaned)
    directory = evidence_path(path, "artifacts")
    directory.mkdir(exist_ok=True, mode=0o700)
    destination = evidence_path(path, f"artifacts/{key}.json")
    if not destination.exists():
        destination.write_bytes(data)
        destination.chmod(0o600)
    elif destination.read_bytes() != data:
        from .errors import LabError
        raise LabError("Existing content-addressed artifact has changed")
    return {"sha256": key, "path": f"artifacts/{key}.json", "media_type": media_type,
            "bytes": len(data), "characters": len(data.decode()),
            "token_estimate": None, "tokenizer": "unavailable",
            "redaction": "default text redaction"}


def event_envelope(run_id, sequence, scenario_id, profile, kind, artifacts, *,
                   audience="developer", parent_id=None, provenance="coordinator",
                   agent_id="evaluated-agent"):
    from .measurements import CLOCK_ID
    return {
        "run_id": run_id, "scenario_id": scenario_id, "event_id": f"{run_id}:{sequence}",
        "parent_id": parent_id, "agent_id": agent_id, "task_id": scenario_id,
        "wall_timestamp": datetime.now(UTC).isoformat(), "monotonic_ns": time.monotonic_ns(),
        "clock_id": CLOCK_ID,
        "event_type": kind, "surface": profile["surface"], "profile": profile["name"],
        "visibility": audience, "provenance": provenance, "redaction_status": "redacted",
        "artifacts": artifacts,
    }


NOTICE_STORE = Path(".cache/notices")
_RUN_NAME = re.compile(r"(?:r|surface|browser|worker|connector|guidelines)-[0-9a-f]{32}")
_RUN_FILES = ("manifest.json", "result.json", "events.jsonl", "checkpoint.private.json")


def _run_directory(directory):
    return bool(_RUN_NAME.fullmatch(directory.name)) or any((directory / name).is_file() for name in _RUN_FILES)


def notice_store(path):
    """The workspace cache beside the runs directory of the outermost run holding ``path``.

    Sub-runs (``<run>/agent/<id>``), browser segments (``<run>/browser``) and connector browser
    tasks (``<run>/browser/<task>``) belong to their outermost run, so one verified copy serves every run of a workspace
    (``<workspace>/runs/<run>`` uses ``<workspace>/.cache/notices``). It is never inside
    the runs directory, which worker exports require to hold only runs.
    """
    top = Path(path).absolute()
    while True:
        container = top.parent.parent if top.parent.name in ("agent", "browser") else top.parent
        if container == top or container.parent == container or not _run_directory(container):
            break
        top = container
    runs = top.parent
    # Runs written directly into a workspace (--output .) keep the store in that workspace.
    workspace = runs if (runs / ".cache").is_dir() or (runs / "consumer-agent-testing-tool.yaml").is_file() \
        else runs.parent
    return workspace / NOTICE_STORE


def stored_public_file(source, store):
    """A read-only, content-addressed copy of ``source`` in a harness-owned store; returns (path, sha256).

    Evidence never links to the distribution archive itself: tools that regenerate it
    write in place (``shutil.copyfile``, ``cp``), which would silently rewrite every
    linked evidence copy and invalidate its seal.
    """
    key = file_digest(source)
    store = Path(store)
    target = store / f"{key}.zip"
    if target.is_file() and not target.is_symlink() and file_digest(target) == key:
        return target, key
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = store / f".{key}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copyfile(source, temporary)
        # Name the entry by what was copied, even if the source changed meanwhile.
        key = file_digest(temporary)
        target = store / f"{key}.zip"
        temporary.chmod(0o444)
        temporary.replace(target)  # Replaces a damaged or linked entry; never writes through it.
    finally:
        temporary.unlink(missing_ok=True)
    return target, key


def share_public_file(source, destination, *, link=True):
    """Place an unmodified public file in evidence: a hard link when allowed and possible, else a copy.

    An existing destination is replaced, never opened for writing, so writing
    evidence can never modify a file it shares an inode with. Copies are read-only.
    """
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        if link and not destination.is_symlink() and os.path.samefile(source, destination):
            return
        destination.unlink()
    try:
        if not link:
            raise OSError("copy requested")
        os.link(source, destination)
    except OSError:  # Another filesystem, or links unsupported or exhausted.
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(0o444)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def place_notice(source, directory, store):
    """Put notices in ``directory`` as ``<sha256>.zip``; returns the digest of what was placed.

    A hard link to the verified store copy on the same filesystem, else an independent
    read-only copy; never a link to the distribution archive itself.
    """
    try:
        shared, key = stored_public_file(source, store)
    except OSError:  # A read-only or foreign workspace: copy instead.
        shared, key = Path(source), None
    staged = Path(directory) / f".notices.{uuid.uuid4().hex}.tmp"
    try:
        share_public_file(shared, staged, link=key is not None)
        key = key or file_digest(staged)
        staged.replace(Path(directory) / f"{key}.zip")
    finally:
        staged.unlink(missing_ok=True)  # Left behind when the destination already was this file.
    return key


def notices_source():
    """The distribution notices archive: the checkout's copy, else the installed package data."""
    root = Path(__file__).resolve().parents[2]
    notices = root / "third_party/notices.zip"
    return notices if notices.is_file() else Path(__file__).parent / "data/third_party/notices.zip"


def dependency_notice(path):
    """Reference the distribution notices without treating them as trace data.

    The 3.3 MB archive is not copied into every evidence directory: each one hard-links
    a verified, read-only copy in the workspace cache (``notice_store``) when on the same
    filesystem. Its digest and path are unchanged.
    """
    from .replay import evidence_path
    notices = notices_source()
    if not notices.is_file():
        return None
    directory = evidence_path(path, "artifacts")
    directory.mkdir(exist_ok=True, mode=0o700)
    key = place_notice(notices, directory, notice_store(path))
    return {"sha256": key, "path": f"artifacts/{key}.zip",
            "kind": "original public dependency notices",
            "redaction": "unmodified public license texts; not a target trace"}


def export_notice(source, reference, destination):
    """Place a verified run's notices in an export, sharing the source workspace's store."""
    from .errors import LabError
    from .sealed_events import reference_path
    original = reference_path(source, reference, suffix="zip")
    directory = Path(destination) / "artifacts"
    directory.mkdir(exist_ok=True, mode=0o700)
    if place_notice(original, directory, notice_store(source)) != reference["sha256"]:
        raise LabError("Dependency notices changed during export")


class Evidence:
    def __init__(self, root: Path, *, lane: str, label: str, profile: dict,
                 lock: dict, scenario_id: str | None = None, locations=None, redactor=None):
        self.run_id = f"r-{uuid.uuid4().hex}"
        self.path = root / self.run_id
        self.path.mkdir(parents=True, mode=0o700)
        (self.path / "artifacts").mkdir(mode=0o700)
        self.redactor = redactor or Redactor.environment()
        self.sequence = 0
        self.profile = profile
        self.scenario_id = scenario_id
        from .measurements import clock_reading
        self.manifest = {
            "schema_version": 1, "run_id": self.run_id, "scenario_id": scenario_id,
            "lane": lane, "evidence_label": label, "fidelity": profile["label"],
            "execution_status": "running", "reporting_status": "pending",
            "locations": locations or {"inference": "none", "browser": "none",
                "target": "local", "telemetry": "disabled", "egress": "not exercised"},
            "run_lock": lock, "started_at": datetime.now(UTC).isoformat(),
            "started_clock": clock_reading(),
            "observation_gaps": [], "checks": [],
        }
        if notice := dependency_notice(self.path):
            self.manifest["dependency_notices"] = notice
        else:
            self.manifest["observation_gaps"].append("Distribution dependency notices unavailable")
        self.save()

    def protect_identity(self):
        # Lanes may replace the redactor after construction, so this runs before every write.
        self.redactor.protect(self.run_id, self.scenario_id, self.manifest["lane"], self.manifest["evidence_label"],
                              *(self.profile.get(key) for key in ("name", "surface", "label")))

    def artifact(self, value, media_type="application/json"):
        self.protect_identity()
        return write_artifact(self.path, self.redactor, value, media_type)

    def event(self, kind, payload, *, audience="developer", parent_id=None, provenance="coordinator"):
        self.sequence += 1
        event = event_envelope(self.run_id, self.sequence, self.scenario_id, self.profile,
            kind, [self.artifact(payload)], audience=audience, parent_id=parent_id, provenance=provenance)
        with (self.path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        return event["event_id"]

    def save(self):
        self.protect_identity()
        temporary = self.path / "manifest.tmp"
        temporary.write_text(json.dumps(self.redactor.clean_evidence(self.manifest), indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path / "manifest.json")

    def finish(self, status, checks=None):
        from .measurements import clock_reading
        self.manifest.update(execution_status=status, finished_at=datetime.now(UTC).isoformat(),
                             finished_clock=clock_reading())
        if checks is not None:
            self.manifest["checks"] = checks
        self.event("run_finished", {"status": status})
        self.save()
        try:
            if self.scenario_id:
                from .metrics import summarize_run
                self.manifest["metrics"] = summarize_run(self.path, self.manifest)
            self.manifest["reporting_status"] = "completed"
            self.save()
            write_report(self.path)
        except Exception:
            self.manifest["reporting_status"] = "failed"
            self.save()
            raise
        self.save()


def wilson(successes: int, total: int, z=1.959963984540054):
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("Invalid success count/denominator")
    if total == 0:
        return None
    rate = successes / total
    denominator = 1 + z*z / total
    center = (rate + z*z / (2*total)) / denominator
    half = z * math.sqrt(rate*(1-rate)/total + z*z/(4*total*total)) / denominator
    return [max(0, center-half), min(1, center+half)]


REPORT_ARTIFACT_LIMIT = 2 * 1024 * 1024


def _report_payload(path, reference):
    """A recorded artifact whose digest still matches, for inline display only."""
    from .sealed_report import evidence_file
    name = reference.get("path") if isinstance(reference, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r"artifacts/[0-9a-f]{64}\.json", name):
        return None
    artifact = evidence_file(path, name)
    if artifact is None or artifact.stat().st_size > REPORT_ARTIFACT_LIMIT \
            or file_digest(artifact) != reference.get("sha256"):
        return None
    return json.loads(artifact.read_text())


def _report_rows(path):
    """Core events as report rows: each payload is its digest-verified recorded artifact."""
    from .sealed_report import event_row, read_events
    rows = []
    for event in read_events(path):
        artifacts = event.get("artifacts") if isinstance(event.get("artifacts"), list) else []
        reference = artifacts[0] if artifacts and isinstance(artifacts[0], dict) else None
        payload = _report_payload(path, reference) if reference else None
        rows.append(event_row(sequence=str(event.get("event_id", "")).rpartition(":")[2] or None,
                              time=event.get("wall_timestamp"), kind=event.get("event_type"),
                              audience=event.get("visibility"), payload=payload, reference=reference,
                              provenance=event.get("provenance"),
                              unavailable=reference is not None and payload is None))
    return rows


def sealing_run(path):
    """The sealed run whose seal lists this core run's files (an agent sub-run, ``<run>/agent/<id>``), else None."""
    path = Path(os.path.abspath(path))  # Lexically: `report .` inside the sub-run is the same run.
    parent, seal = path.parent.parent, path.parent.parent / "evidence.sha256.json"
    if path.parent.name != "agent" or not seal.is_file():
        return None
    try:
        names = json.loads(seal.read_text())
    except (OSError, ValueError):
        return parent  # Unreadable: the parent's playback refuses it (fail closed).
    return parent if not isinstance(names, dict) or any(
        isinstance(name, str) and name.startswith(f"agent/{path.name}/") for name in names) else None


def write_report(path: Path):
    """Core run report, in the sealed report's order: verdict and failing step, checks, the per-step timeline,
    the published, exposed, observed and diagnostic views, metrics, then Inspect View and linked evidence.

    A sub-run its parent has sealed keeps its sealed report: rendering it again (a newer layout) would break
    the parent's seal, so it is returned once the parent's playback verifies it, as `report` does for runs."""
    if (parent := sealing_run(path)) is not None:
        from .errors import LabError
        from .replay import playback
        playback(parent)  # Every sealed file matches; an unsealed report.html would have failed it.
        if not (report := Path(path) / "report.html").is_file():
            raise LabError(f"This run is sealed in {parent.name} without a report; export {parent.name} for a "
                           "verified rendering", category="reporting")
        return report
    from .sealed_report import (
        DISCLAIMER,
        checks_table,
        compact,
        escape,
        failing_step,
        link,
        metrics_section,
        page,
        pretty,
        status_badge,
        step_groups,
        steps_section,
        table,
        verdict,
        views_section,
    )
    manifest = json.loads((path / "manifest.json").read_text())
    status = manifest.get("execution_status")
    failure = manifest.get("execution_failure") if isinstance(manifest.get("execution_failure"), dict) else {}
    rows = [("Execution", status_badge(status)), ("Reporting", status_badge(manifest.get("reporting_status")))]
    if failure or status not in ("completed", "running"):
        rows += [("Failure class", escape(failure["class"]) if failure.get("class") else
                  "<span class=muted>not recorded</span>"),
                 ("Reason", escape(failure["detail"]) if failure.get("detail") is not None else
                  "<span class=muted>not recorded</span>")]
    groups = step_groups(_report_rows(path))
    if found := failing_step(groups, status):
        rows.append(found)
    linked = [f"<li>{log_link}</li>" for name in manifest.get("inspect_logs") or []
              if (log_link := link(path, name, f"Inspect log {name}"))]
    if linked:
        linked.append("<li>Inspect View: <code>inspect view --log-dir &lt;run&gt;/inspect</code></li>")
    from .browser_segments import references, verify
    for reference in references(manifest):
        from .errors import LabError
        from .replay import evidence_path
        from .sealed_report import view
        child = verify(path, reference)
        child_seal = evidence_path(path, reference["path"] + "/evidence.sha256.json")
        report = view(child)
        if file_digest(child_seal) != reference["seal_sha256"]:
            raise LabError("Browser evidence changed during report generation")
        relative = report.relative_to(path).as_posix()
        linked.append(f'<li><a href="{html.escape(relative, quote=True)}">Browser interaction evidence</a> '
                      f"<span class=muted>({escape(reference['path'])})</span></li>")
    labels = " · ".join(escape(manifest[key]) for key in ("lane", "evidence_label", "fidelity")
                        if isinstance(manifest.get(key), str))
    body = [f"<h1>Consumer Agent Testing Tool</h1><p>run <code>{escape(manifest.get('run_id', path.name))}</code>"
            + (f" · scenario <code>{escape(manifest['scenario_id'])}</code>" if manifest.get("scenario_id") else "")
            + "</p><p>Test your integration against documented Muse interface constraints and an assistant "
            f'surrogate.</p><p class="label">{labels}</p>', verdict(rows, status),
            f"<p class=muted>{html.escape(DISCLAIMER)}</p>", "<h2>Checks</h2>",
            checks_table(manifest.get("checks")), "<h2>Timeline</h2>", steps_section(path, groups),
            views_section(path, groups, manifest), metrics_section(manifest.get("metrics"))]
    if linked:
        body += ["<h2>Viewers and linked evidence</h2>", f"<ul>{''.join(linked)}</ul>"]
    if gaps := manifest.get("observation_gaps"):
        body.append("<h2>Observation gaps</h2>" + table(["Gap"], [[compact(item, 2000)] for item in gaps]))
    body.append(f"<details><summary>Run lock</summary>{pretty(manifest.get('run_lock'), None)}</details>")
    rest = {key: value for key, value in manifest.items() if key != "run_lock"}
    body.append(f"<details><summary>Run manifest (JSON)</summary>{pretty(rest, None)}</details>")
    (path / "report.html").write_text(page(
        f"Consumer Agent Testing Tool — {manifest.get('run_id', path.name)}", "".join(body)))
    return path / "report.html"


PRIVATE_STATE_FILES = ("checkpoint.private.json", "consumer-checkpoint.json")
PRIVATE_STATE_LIMIT = 64 * 1024 * 1024


def private_secrets(root):
    """Captured values, --secrets-file values and credentials a run kept privately.

    Everything under a ``secrets`` mapping counts (strings and numbers), plus string
    values under credential keys. Dict values are scanned, not collected: private state
    also holds mappings and JSON Schemas whose property names look like credentials.
    Export and report re-redaction apply these values; the files are never exported.
    Private state that is linked, oversized or unreadable refuses the export (fail closed).
    """
    from .errors import LabError
    root = Path(root)
    private = root / "private"
    if private.is_symlink():
        raise LabError("Private run state cannot be a link")
    candidates = [root / name for name in PRIVATE_STATE_FILES]
    if private.is_dir():
        candidates += sorted(private.glob("*.private.json"))
    values = []
    for path in candidates:
        if path.is_symlink():
            raise LabError("Private run state cannot be a link")
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > PRIVATE_STATE_LIMIT:
            raise LabError(f"Private run state {path.name} cannot be applied to export redaction")
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as error:
            raise LabError(f"Private run state {path.name} is unreadable; export refused") from error
        try:
            pending = [json.loads(text)]
        except (ValueError, RecursionError):
            values.append(text)  # Unstructured private state is itself private.
            continue
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if normalized_key(key) == "secrets":
                        values.extend(leaf for leaf in _leaves(child)
                                      if isinstance(leaf, (str, int, float)) and not isinstance(leaf, bool))
                    elif credential_key(key) and (isinstance(child, str) or (
                            isinstance(child, list) and all(isinstance(entry, str) for entry in child))):
                        values.extend(_strings(child))
                    elif isinstance(child, (dict, list)):
                        pending.append(child)
            elif isinstance(item, list):
                pending.extend(child for child in item if isinstance(child, (dict, list)))
    return values


IDENTITY_KEYS = ("run_id", "scenario_id", "task_id", "profile", "surface", "lane", "evidence_label",
                 "fidelity", "clock_id")


def record_identity(*records):
    """The identity values verified harness records carry (results, manifests, envelopes)."""
    values = []
    for record in records:
        if isinstance(record, dict):
            values += [record.get(key) for key in IDENTITY_KEYS]
            if isinstance(profile := record.get("interface_profile"), dict):
                values += [profile.get(key) for key in ("name", "surface", "label")]
    return values


def export_redactor(source, secrets=(), identity=()):
    """Environment credentials, the run's own private values and any parent run's values.

    The verified run's identity stays legible unless it is itself one of those secrets.
    """
    redactor = Redactor.environment()
    redactor.add(private_secrets(source))
    redactor.add(list(secrets))
    redactor.protect(*(value for value in identity if value not in redactor.secrets))
    return redactor


def export_redacted(source: Path, destination: Path, *, secrets=()):
    """Rebuild an allowlisted export, revalidating and redacting Inspect logs."""
    import tempfile

    from .errors import LabError
    if destination.exists():
        raise LabError("Export destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a fully validated export. A failure leaves the original run
    # and destination untouched, and the user can retry the same destination.
    with tempfile.TemporaryDirectory(prefix=".surface-export-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "export"
        _export_redacted_into(source, staged, secrets)
        # Last line of defense, as for sealed exports: a private value that survived redaction anywhere in
        # the staged files (Inspect log members included) refuses the whole export.
        from .replay import exported_private_values
        if leaked := exported_private_values(staged, [*private_secrets(source), *secrets]):
            raise LabError(f"Export still contains {len(leaked)} of the run's private values; nothing was "
                           "exported", category="reporting")
        staged.rename(destination)
    return destination


def _export_redacted_into(source: Path, destination: Path, secrets=()):
    from .errors import LabError
    from .sealed_events import reference_path

    original_manifest = json.loads((source / "manifest.json").read_text())
    rows = [json.loads(line) for line in (source / "events.jsonl").read_text().splitlines()]
    redactor = export_redactor(source, secrets, record_identity(original_manifest, *rows))
    manifest = redactor.clean_evidence(original_manifest)
    destination.mkdir(parents=True, mode=0o700)
    (destination / "artifacts").mkdir()
    from .browser_segments import references, verify
    segments = []
    for reference in references(original_manifest):
        from .replay import export_public_run
        original = verify(source, reference)
        target = destination / reference["path"]
        export_public_run(original, target, source_seal_sha256=reference["seal_sha256"],
                          secrets=list(redactor.secrets))
        segments.append({"path": reference["path"], "seal_sha256": file_digest(target / "evidence.sha256.json")})
    if segments:
        manifest.update(browser_evidence=segments[-1], browser_segments=segments)
    if reference := original_manifest.get("dependency_notices"):
        export_notice(source, reference, destination)
        manifest["dependency_notices"].update(path=reference["path"], sha256=reference["sha256"])
    events = []
    for original_event in rows:
        event = redactor.clean_evidence(original_event)
        for original_reference, reference in zip(original_event["artifacts"], event["artifacts"], strict=True):
            artifact_path = reference_path(source, original_reference, suffix="json")
            data = redactor.clean_evidence(json.loads(artifact_path.read_text()))
            key = digest(data)
            encoded = canonical_bytes(data)
            (destination / "artifacts" / f"{key}.json").write_bytes(encoded)
            reference.update(sha256=key, path=f"artifacts/{key}.json",
                             bytes=len(encoded), characters=len(encoded.decode()))
        events.append(event)
    if log_artifacts := original_manifest.get("inspect_log_artifacts"):
        from inspect_ai.log import EvalLog, read_eval_log, write_eval_log
        (destination / "inspect").mkdir(mode=0o700)
        exported_logs = []
        for index, reference in enumerate(log_artifacts):
            original = source / reference["path"]
            if (original.is_symlink() or original.resolve().parent != (source / "inspect").resolve()
                    or file_digest(original) != reference["sha256"]):
                raise LabError("Inspect log path or digest mismatch")
            parsed = read_eval_log(str(original))
            cleaned = EvalLog.model_validate(redactor.clean_evidence(parsed.model_dump(mode="json")))
            target = destination / "inspect" / f"trial-{index}.eval"
            write_eval_log(cleaned, str(target))
            exported_logs.append({"path": str(target.relative_to(destination)),
                                  "sha256": file_digest(target),
                                  "redaction": "revalidated structured/text redaction on export"})
        manifest["inspect_log_artifacts"] = exported_logs
        manifest["inspect_logs"] = [reference["path"] for reference in exported_logs]
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (destination / "events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
    write_report(destination)
    return destination
