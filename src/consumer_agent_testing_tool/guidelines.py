"""Muse connector-guidelines readiness: a static check of documented constraints, never an admission.

Meta reviews every connector by hand in three stages, and "Meeting these guidelines does not guarantee
approval." (section 6). This check finds, before submission, what the pinned guidelines (POLICY) state
outright and what a tools/list listing, sampled responses or the developer's readiness manifest can
settle: hidden writes and combined reads and writes (3.2), steering instructions (3.2), exposed
credentials (4.4), money movement and trade orders (4.6), documentation topics (5.4), the test account
and a no-charge checkout path (5.5), declared tool classes (5.6) and the read-only OAuth option (3.1).
Each finding quotes the guideline sentence it rests on and has the basis "documented constraint".
Detection is lexical and English only: a finding is a reason to look, and no finding is no admission.

run_check lists tools and never calls one; behavior on a test account is the connector lane's job. The
classification sheet uses MCPJam's Muse readiness row shape (MCPJam/inspector
sdk/src/muse-readiness/classification.ts at da3b8ac30d2cc914be838637b5422bd66f6dd514) with
permissions.classify_tool's suggestion, so it shows what the connector lane's permission engine does.
"""
import hashlib
import json
import re
import struct
import sys
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import LabError, normalize_error
from .evidence import Redactor, normalized_key
from .serialization import digest, load

try:
    from .permissions import ToolClass
except ImportError:  # permissions.py is a sibling module of this lane; this is the contract's literal.
    ToolClass = Literal["read", "write", "sensitive-write"]

if TYPE_CHECKING:
    from .linking import TargetAuth
    from .mcp_clients import ClientProfile

POLICY_FILE = "data/policy/muse-connector-guidelines-2026-10-15.json"
TEMPLATE_PROFILE = "muse-template-unofficial-2026-10"
LABEL = "documented constraint, not admission"
SUITE = "muse-guidelines"
MCPJAM_SHEET = ("https://github.com/MCPJam/inspector/blob/da3b8ac30d2cc914be838637b5422bd66f6dd514/"
                "sdk/src/muse-readiness/classification.ts")
INTERFACE = {"name": "muse-connector-guidelines-2026-10-15", "surface": "mcp-tools-list", "label": LABEL}
USER_AGENT = "consumer-agent-testing-tool policy drift check (+https://github.com/MCPJam/inspector method)"
MAX_PAGE_BYTES = 5_000_000
WALK_BUDGET, WALK_DEPTH = 5_000, 12  # A listing is attacker-shaped: bound every scan.


def _load_policy():
    try:
        policy = json.loads(files("consumer_agent_testing_tool").joinpath(POLICY_FILE).read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise LabError(f"Pinned Muse policy metadata is unreadable: {error}") from error
    keys = ("source", "fetched", "extract_sha256_32", "method", "terms_effective", "terms_source", "sections")
    if any(key not in policy for key in keys) or not re.fullmatch(r"[0-9a-f]{32}", policy["extract_sha256_32"]):
        raise LabError("Pinned Muse policy metadata is incomplete")
    return policy


POLICY: dict = _load_policy()


# --- MCPJam-compatible policy hashing (scripts/lib/policy-manifest-sync.mjs at 5013212e93) ----------
# A port of MCPJam's extractText (Apache-2.0): visible text by scanning, never by tag regexes, so the
# digest moves with the policy prose and not with markup trivia. JavaScript semantics are kept where
# they differ from Python's: the scan indexes UTF-16 code units, \s and String.prototype.trim use the
# ECMAScript whitespace set, and the raw-text search runs on a lower-cased copy of the whole page whose
# offsets are reused on the original, as the original does ("İ" lower-cases to two units).

JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a" \
           "\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACE_RUN = re.compile(f"[{re.escape(JS_SPACE)}]+")
_DOCTYPE = re.compile(f"<!doctype(?:[{re.escape(JS_SPACE)}]|>)", re.IGNORECASE)
_NAME = re.compile(r"[A-Za-z0-9:-]")
_RAW_TEXT = {"script", "style"}


def _read_tag(html, start):
    index = start + 1
    closing = index < len(html) and html[index] == "/"
    index += closing
    name_start = index
    while index < len(html) and _NAME.match(html[index]):
        index += 1
    if index == name_start:
        return None  # "a < b" is content, not markup.
    name, quote = html[name_start:index].lower(), None
    while index < len(html):
        char = html[index]
        if quote:
            quote = None if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == ">":
            return name, closing, html[index - 1] == "/", index + 1
        index += 1
    return name, closing, False, len(html)  # Unterminated: the rest is markup.


def _skip_raw_text(html, lower, start, name):
    """Past "</name ...>"; positions found in the lower-cased page are used on the page itself."""
    needle, index = f"</{name}", start
    while True:
        at = lower.find(needle, index)
        if at == -1:
            return len(html)
        after = at + len(needle)
        if after >= len(html) or html[after] == ">" or html[after] in JS_SPACE:
            tag = _read_tag(html, at)
            return tag[3] if tag else len(html)
        index = after


def _units(text):
    """One Python character per UTF-16 code unit, as JavaScript indexes a string."""
    data = text.encode("utf-16-le", "surrogatepass")
    return "".join(map(chr, struct.unpack(f"<{len(data) // 2}H", data)))


def _code_points(units):
    """Back from code units; valid pairs join, lone surrogates stay (JavaScript strings may hold them)."""
    return units.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def extract_text(html: str) -> str:
    """Visible text of an HTML page exactly as MCPJam's extractText computes it."""
    return _code_points(_extract(_units(html)))


def _extract(html):
    out, index, lower = [], 0, None
    while index < len(html):
        lt = html.find("<", index)
        if lt == -1:
            out.append(html[index:])
            break
        out.append(html[index:lt])
        if html.startswith("<!--", lt):
            end = html.find("-->", lt + 4)
            out.append(" ")
            index = len(html) if end == -1 else end + 3
            continue
        if _DOCTYPE.match(html[lt:lt + 10]):
            end = html.find(">", lt + 2)
            out.append(" ")
            index = len(html) if end == -1 else end + 1
            continue
        tag = _read_tag(html, lt)
        if tag is None:
            out.append("<")
            index = lt + 1
            continue
        out.append(" ")
        name, closing, self_closing, end = tag
        if not closing and not self_closing and name in _RAW_TEXT:
            lower = html.lower() if lower is None else lower  # MCPJam lower-cases the page per element
            index = _skip_raw_text(html, lower, end, name)
        else:
            index = end
    text = re.sub("&nbsp;", " ", "".join(out), flags=re.IGNORECASE)
    return _SPACE_RUN.sub(" ", text).strip(JS_SPACE)


def _utf16(text):
    """Python code points as JavaScript's UTF-16 string: surrogate pairs join, lone ones become U+FFFD."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def hash_policy_text(text: str) -> str:
    """sha256 of the UTF-8 text, first 32 hex characters (MCPJam hashPolicyText)."""
    return hashlib.sha256(_utf16(text).encode("utf-8")).hexdigest()[:32]


def js_length(text: str) -> int:
    """String.length as MCPJam's sync log reports it (UTF-16 code units)."""
    return len(_utf16(text).encode("utf-16-le")) // 2


# --- Findings and the readiness manifest ------------------------------------------------------------

Severity = Literal["fail", "warn", "info"]
# rule -> (policy section, quote index): every finding quotes the sentence it rests on.
RULES = {
    "3.2-hidden-write": ("3.2", 1),
    "3.2-combined-read-write": ("3.2", 0),
    "3.2-steering": ("3.2", 2),
    "4.4-credential-exposure": ("4.4", 0),
    "4.6-money-movement": ("4.6", 0),
    "5.4-doc-topics": ("5.4", 0),
    "5.5-test-account": ("5.5", 0),
    "5.5-no-charge-checkout": ("5.5", 1),
    "5.6-tool-classes": ("5.6", 0),
    "3.1-read-only-oauth": ("3.1", 0),
}
DOC_TOPICS = ("setup-instructions", "inputs-and-outputs", "read-write-classifications", "sensitive-designations",
              "permissions", "side-effects", "status-handling", "errors", "rate-limits")  # 5.4, MCPJam's keys
DocTopic = Literal["setup-instructions", "inputs-and-outputs", "read-write-classifications",
                   "sensitive-designations", "permissions", "side-effects", "status-handling", "errors",
                   "rate-limits"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuidelineFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule: str
    severity: Severity
    tool: str | None
    evidence: str
    basis: Literal["documented constraint"] = "documented constraint"
    section: str = ""  # "§3.2 How tools are classified"
    source: str = ""  # the pinned guidelines URL
    quote: str = ""  # the guideline sentence the rule rests on, verbatim
    location: str | None = None  # where it was found: "description", "responses[0].content[0].text", ...
    detection: Literal["pattern", "annotation", "schema", "classifier", "manifest", "auth"] = "pattern"
    evaluated: bool = True  # False: the rule could not be evaluated (no manifest); never a pass


class Documentation(Strict):
    url: str | None = None
    covers: list[DocTopic] = []


class Authentication(Strict):
    method: Literal["oauth", "api-key", "none", "other"]
    scopes: list[str] = []
    read_only_scopes: list[str] = []
    read_only_not_possible_reason: str | None = None
    credentials_environment: Literal["test", "production"] | None = None


class TestAccount(Strict):
    """How reviewers reach the test account (5.5). Credentials never belong here: the submission form
    has its own secure channel, and extra fields such as "password" fail validation."""
    __test__ = False  # Not a pytest class.
    sign_in_instructions: str | None = None
    required_permissions: str | None = None
    representative_test_data: str | None = None
    limits: str | None = None
    accessible_throughout_review: bool | None = None


class Transactions(Strict):
    has_transaction_tools: bool
    tools: list[str] = []
    no_charge_test_path: str | None = None  # how reviewers test checkout without real charges


class SampleResponse(Strict):
    tool: str = Field(min_length=1)
    result: dict  # a tools/call result as the server returned it (content, structuredContent, _meta)


class ReadinessManifest(Strict):
    """Developer declarations for the submission (section 5); presence is checked, truth stays with Meta."""
    version: Literal[1] = 1
    kind: Literal["muse-readiness"] = "muse-readiness"
    connector: str | None = None
    documentation: Documentation | None = None
    authentication: Authentication | None = None
    test_account: TestAccount | None = None
    transactions: Transactions | None = None
    classes: dict[str, ToolClass] = {}  # 5.6: the class each tool's documentation declares
    sample_responses: list[SampleResponse] = []  # responses captured on the test account, reviewed for 3.2/4.4


def load_manifest(path: Path) -> ReadinessManifest:
    try:
        return ReadinessManifest.model_validate(load(Path(path)))
    except FileNotFoundError as error:
        raise LabError(f"Readiness manifest not found: {path}") from error
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, row['loc'])) or '(root)'}: {row['msg']}"
                             for row in error.errors(include_input=False))
        raise LabError(f"Invalid readiness manifest {path}: {problems}") from None


def _finding(rule, severity, evidence, *, tool=None, location=None, detection="pattern", evaluated=True):
    key, index = RULES[rule]
    section = POLICY["sections"][key]
    return GuidelineFinding(rule=rule, severity=severity, tool=tool, evidence=evidence[:1200],
                            section=f"§{key} {section['title']}", source=POLICY["source"],
                            quote=section["quotes"][index], location=location, detection=detection,
                            evaluated=evaluated)


# --- Tool signals (English, precision first: a false hidden-write finding is an accusation) ----------

READ_VERBS = {"get", "list", "search", "find", "read", "fetch", "lookup", "check", "view", "show", "query",
              "describe", "retrieve", "browse", "count", "preview", "estimate", "quote", "inspect", "explore",
              "summarize", "summarise", "compare", "validate", "calculate", "track", "status"}
ACTION_VERBS = {"pay", "purchase", "buy", "checkout", "charge", "refund", "transfer", "send", "email", "share",
                "publish", "post", "tweet", "invite", "book", "reserve", "subscribe", "donate", "tip", "place",
                "delete", "remove", "erase", "purge", "destroy", "cancel", "submit", "wire", "withdraw", "notify"}
NAME_PAIRS = {("check", "out"): "checkout", ("check", "in"): "checkin", ("look", "up"): "lookup",
              ("sign", "up"): "signup"}
SENSITIVE_NAME_WORDS = {"pay", "payment", "payments", "purchase", "checkout", "charge", "refund", "transfer",
                        "send", "email", "sms", "share", "publish", "tweet", "invite", "booking", "reservation",
                        "reserve", "subscribe", "subscription", "donate", "wire", "withdraw"}
TRANSACTION_WORDS = {"pay", "payment", "payments", "purchase", "buy", "checkout", "charge", "book", "booking",
                     "reserve", "reservation", "order", "orders", "subscribe", "subscription", "donate", "tip",
                     "refund"}
_OBJ = r"(?:an?\s+|the\s+|your\s+|their\s+|a\s+new\s+)?"
DESCRIPTION_ACTIONS = [(verb, re.compile(pattern, re.IGNORECASE)) for verb, pattern in (
    ("send", (rf"\b(?:sends|(?:will|also|then|automatically|immediately)\s+sends?)\s+{_OBJ}(?:\w+\s+)?"
             r"(?:e-?mails?|messages?|sms|texts?|notifications?|invites?|invitations?|dms?|replies|reply|money"
             r"|funds|payments?)\b")),
    ("pay", (r"\b(?:charges|bills|debits)\s+(?:the\s+|your\s+|a\s+|their\s+)?(?:user'?s?\s+|customer'?s?\s+)?"
            r"(?:card|customer|user|account|payment\s+method|wallet|you)\b")),
    ("pay", (r"\b(?:pays|(?:makes|processes|captures|completes)\s+(?:a\s+|the\s+)?payments?|places\s+(?:an?\s+|the\s+)?"
            r"orders?|purchases|buys|completes\s+(?:an?\s+|the\s+)?(?:purchase|checkout|order)|checks\s+out)\b")),
    ("book", (rf"\b(?:books|reserves|(?:will|also|then|automatically|immediately)\s+(?:book|reserve)s?)\s+{_OBJ}"
             r"(?:\w+\s+)?(?:rooms?|flights?|tables?|seats?|appointments?|tickets?|stays?|reservations?|slots?"
             r"|trips?|hotels?|cars?|it|them)\b")),
    ("book", (rf"\b(?:creates|makes|submits|confirms|finalizes|finalises|commits)\s+{_OBJ}"
             r"(?:bookings?|reservations?|orders?|purchases?|payments?|charges?|transactions?)\b")),
    ("publish", r"\b(?:publishes|posts|tweets)\s+(?:an?\b|the\b|your\b|it\b|them\b|to\b|publicly\b)"),
    ("share", r"\bshares\s+(?:\w+\s+){0,3}?(?:with|publicly)\b"),
    ("delete", (r"\b(?:deletes|erases|purges|wipes|destroys)\s+(?:an?\s+|the\s+|your\s+|all\s+|any\s+|their\s+"
               r"|this\s+|that\s+)?\w+")),
    ("cancel", (rf"\bcancels\s+{_OBJ}(?:\w+\s+)?(?:bookings?|reservations?|orders?|subscriptions?|appointments?"
               r"|payments?|stays?|trips?|flights?|tickets?|it|them)\b")),
    ("transfer", r"\b(?:transfers|moves|wires)\s+(?:the\s+)?(?:funds|money|cash|balance)\b"),
    # "Available rates are secured instantly with the card on file": passive or adverbial payment.
    ("pay", (r"\b(?:with|using|via|through|to|from|on)\s+(?:the\s+|your\s+|a\s+)?(?:saved\s+|stored\s+|default\s+)?"
             r"(?:card|payment\s+method)\s+on\s+file\b")),
    ("pay", (r"\b(?:charged|billed|debited)\s+(?:to\s+)?(?:the\s+|your\s+|a\s+|their\s+)?(?:saved\s+|stored\s+"
             r"|default\s+)?(?:card|account|payment\s+method|wallet)\b")),
    ("book", (r"\b(?:secured|booked|reserved|charged|purchased|paid|confirmed|placed|captured)\s+(?:instantly"
              r"|immediately|automatically|right\s+away)\b|\b(?:instantly|immediately|automatically)\s+(?:secures?"
              r"|secured|books?|booked|reserves?|reserved|charges?|charged|purchases?|purchased|pays?|paid"
              r"|confirms?|confirmed|places?|placed|captures?|captured)\b")),
)]
NEGATION_BEFORE = re.compile(r"\b(?:not|never|no|nothing|none|without|cannot|nor|neither)\b|n't\b", re.IGNORECASE)
# "checks whether it can be booked", "if you approve": not the tool's own unconditional action.
MODAL_BEFORE = re.compile(r"\b(?:can|could|would|may|might|should|whether|if|unless)\b", re.IGNORECASE)


def name_words(name: str) -> list[str]:
    """Lowercase words across snake, kebab and camel case, with "check out" read as "checkout"."""
    words = [word for word in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower())
             if word]
    joined = []
    for word in words:
        if joined and (joined[-1], word) in NAME_PAIRS:
            joined[-1] = NAME_PAIRS[joined[-1], word]
        else:
            joined.append(word)
    return joined


def _annotations(tool):
    value = tool.get("annotations")
    return value if isinstance(value, dict) else {}


def _texts(tool):
    """The tool's own prose: title, annotation title and description."""
    for location, value in (("title", tool.get("title")), ("annotations.title", _annotations(tool).get("title")),
                            ("description", tool.get("description"))):
        if isinstance(value, str) and value.strip():
            yield location, value


class _Budget:
    def __init__(self):
        self.left, self.truncated = WALK_BUDGET, False


def _strings(value, path, budget=None, depth=0):
    """(path, string) leaves of a JSON value, bounded in depth and node count (budget.truncated says so)."""
    budget = budget or _Budget()
    budget.left -= 1
    if budget.left < 0 or depth > WALK_DEPTH:
        budget.truncated = True
        return
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key), budget, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]", budget, depth + 1)


def _truncated(rule, location, tool):
    return _finding(rule, "info", f"Partly not evaluated: the scan of {location} stopped after {WALK_BUDGET} values "
                    f"or {WALK_DEPTH} levels; review the rest by hand.", tool=tool, location=location,
                    evaluated=False)


def _schema_prose(tool, budget=None):
    """Description and title strings inside the tool's schemas: model-visible prose too."""
    for name in ("inputSchema", "outputSchema"):
        for path, value in _strings(tool.get(name), name, budget):
            if path.rsplit(".", 1)[-1] in ("description", "title"):
                yield path, value


def _not_negated(text, match, *, modal=False):
    """No negation (with modal: nor a modal or condition) earlier in the match's clause, within 40 characters."""
    start = max(text.rfind(".", 0, match.start()), text.rfind(";", 0, match.start()), match.start() - 40)
    before = text[max(start, 0):match.start()]
    return not NEGATION_BEFORE.search(before) and not (modal and MODAL_BEFORE.search(before))


def description_actions(tool) -> list[str]:
    """Explicit consequential actions the tool's prose states ("books the room", "charges your card")."""
    found = []
    for location, text in _texts(tool):
        for _, pattern in DESCRIPTION_ACTIONS:
            for match in pattern.finditer(text):
                if _not_negated(text, match, modal=True):
                    found.append(f'{location} says "{match.group(0).strip()}"')
    return list(dict.fromkeys(found))


def _presented_read(tool, declared):
    """Why a tool is presented as a read, or None (3.2: "a tool described as a read")."""
    name, annotations = tool["name"], _annotations(tool)
    if declared.get(name) == "read":
        return "declared Read in the readiness manifest"
    if annotations.get("readOnlyHint") is True:
        return "annotated readOnlyHint: true"
    words = name_words(name)
    if words and words[0] in READ_VERBS and annotations.get("readOnlyHint") is not False \
            and annotations.get("destructiveHint") is not True and declared.get(name) is None:
        return f'named with the read verb "{words[0]}"'
    return None


def _name_actions(tool, *, leading):
    words, found = name_words(tool["name"]), []
    if leading and words and words[0] in ACTION_VERBS:
        found.append(f'the name starts with the action verb "{words[0]}"')
    for index, word in enumerate(words[1:], 1):  # search_and_book: each conjunct is an action
        if words[index - 1] in ("and", "or", "then") and word in ACTION_VERBS:
            found.append(f'the name joins the action verb "{word}"')
    return found


def combined_operations(tool) -> dict | None:
    """An enumerated operation parameter offering both a read and a write verb (MCPJam tool-shape.ts).
    The connector lane's classifier owns the rule, so the sheet and runtime classes agree."""
    return _lane("permissions", "combined_operations")(tool)


_AVOID = (r"(?:use|using|call|calling|recommend|recommending|suggest|suggesting|mention|mentioning|show|showing"
          r"|consider|considering|search|searching|book|booking|compare|comparing|list|listing|offer|offering"
          r"|link|linking|include|including)")
_RIVALS = (r"(?:competitors?'?s?|competing\s+(?:[\w-]+\s+)?[\w-]+|(?:other|alternative|rival)\s+(?:[\w-]+\s+)?"
           r"(?:hotels?|lodging|services?|providers?|sites?|websites?|apps?|tools?|connectors?|companies|brands?"
           r"|vendors?|merchants?|stores?|shops?|platforms?|airlines?|businesses|products?|options?|sellers?"
           r"|retailers?|restaurants?|agencies))\b")
STEERING = [(category, severity, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
            for category, severity, pattern in (
    ("ignore its rules", "fail", (r"\b(?:ignore|disregard|override|bypass|forget)\s+(?:all\s+|any\s+|the\s+|your\s+"
                                 r"|its\s+|these\s+|those\s+|muse'?s\s+)?(?:previous\s+|prior\s+|earlier\s+|other\s+"
                                 r"|system\s+|safety\s+|existing\s+|default\s+)?(?:instructions?|rules?|guidelines?"
                                 r"|polic(?:y|ies)|prompts?|guardrails?|restrictions?)\b")),
    ("ignore its rules", "fail", r"\byou\s+(?:are\s+)?no\s+longer\s+(?:bound|restricted)\b"),
    # "do not search, mention or recommend other hotels": a verb list, then a rival ("other rooms" is none).
    ("steer away from competitors", "fail", (rf"\b(?:do\s+not|don'?t|never|avoid)\s+(?:{_AVOID}(?:\s*,\s*(?:or\s+"
                                            rf"|and\s+)?|\s+or\s+|\s+and\s+))*+{_AVOID}\s+(?:any\s+)?{_RIVALS}")),
    ("steer away from competitors", "fail", (rf"\binstead\s+of\s+(?:any\s+)?{_RIVALS}"
                                            r"|\bavoid\s+(?:all\s+)?competitors?\b")),
    ("steer away from competitors", "fail", (r"\b(?:is|are)\s+the\s+only\s+(?:[\w'-]+\s+){0,3}?(?:service|tool"
                                            r"|connector|provider|app|option|site|source|platform)s?\s+(?:that\s+)?"
                                            r"(?:you|the\s+agent|muse|the\s+assistant)\s+(?:should|may|can|must"
                                            r"|are\s+allowed\s+to)\s+use\b")),
    ("force recommendations", "fail", (r"\b(?:always|only|must)\s+(?:recommend|suggest|prefer|choose|pick|select"
                                      r"|rank)\s+(?:this|our|these|us)\b")),
    ("force recommendations", "fail", r"\b(?:always|must)\s+(?:recommend|promote|upsell)\b"),
    ("force recommendations", "fail", (r"(?:^|[.;:!?]\s*)recommend\s+(?:[\w'-]+\s+){0,6}?(?:first\b|to\s+(?:every"
                                      r"|all|each|any)\b)")),
    ("force recommendations", "fail", (r"\b(?:always|only)\s+(?:use|call)\s+(?:this|our)\s+(?:tool|service|connector"
                                      r"|provider|app)\s+(?:instead|rather|over|regardless|for\s+(?:all|every|any)"
                                      r"|whenever)\b")),
    ("force recommendations", "fail", (r"\b(?:prefer|choose|pick|select)\s+this\s+(?:tool|service|connector|provider"
                                      r"|option)\s+(?:over|above|first|instead)\b")),
    ("force recommendations", "fail", (r"\b(?:rank|list|show|present)\s+(?:our|this|these)\s+\w+(?:\s+\w+)?\s+(?:first"
                                      r"|at\s+the\s+top|above\s+(?:all\s+)?others)\b")),
    ("conceal commercial interests", "fail", (r"\b(?:do\s+not|don'?t|never)\s+(?:tell|mention|disclose|reveal|say"
                                             r"|label|indicate)\b[^.;]{0,60}?\b(?:sponsor(?:ed|ship)?|commissions?"
                                             r"|affiliate|paid\s+placement|advertis(?:ing|ement|ed)|partnership"
                                             r"|kickback|referral\s+fee)")),
    ("conceal commercial interests", "fail", (r"\b(?:hide|conceal|omit)\s+(?:the\s+|any\s+|our\s+)?(?:sponsor(?:ed|ship)?"
                                             r"|commissions?|affiliate|paid\s+placement|advertis(?:ing|ement))\b")),
    ("take unrequested actions", "fail", (r"\b(?:book|buy|purchase|pay|charge|send|submit|subscribe|order|proceed"
                                         r"|complete|confirm|cancel|delete|transfer)\w*\b[^.;]{0,40}?\bwithout\s+"
                                         r"(?:first\s+)?(?:asking|consulting|confirming\s+with|checking\s+with"
                                         r"|notifying|telling|informing|(?:the\s+)?user'?s?\s+(?:approval|consent"
                                         r"|confirmation|permission)|(?:asking\s+for\s+|requesting\s+|getting\s+"
                                         r"|seeking\s+)?(?:approval|confirmation|consent|permission))")),
    ("take unrequested actions", "fail", (r"\b(?:do\s+not|don'?t|never|no\s+need\s+to)\s+(?:ask|tell|inform|notify"
                                         r"|consult|confirm\s+with)\s+(?:the\s+)?user\b")),
    ("take unrequested actions", "fail", (r"\b(?:bypass|skip|disable|circumvent)\s+(?:the\s+|any\s+|all\s+)?"
                                         r"(?:approvals?|confirmations?|permission\s+(?:prompts?|checks?)|consent"
                                         r"|human-in-the-loop|hitl)\b")),
    ("take unrequested actions", "warn", (r"\bwithout\s+(?:first\s+)?(?:asking|consulting|confirming\s+with"
                                         r"|notifying)\s+(?:the\s+)?user(?!\s+to\b)")),
    ("take unrequested actions", "warn", (r"\balways\s+(?:also\s+)?(?:add|include|book|purchase|buy|subscribe|enroll"
                                         r"|upgrade)\b")),
    ("access unrelated data", "warn", (r"\b(?:read|access|collect|upload|send|forward|share|export)\s+(?:all\s+"
                                      r"(?:of\s+)?)?(?:the\s+)?user'?s\s+(?:other\s+)?(?:contacts|address\s+book"
                                      r"|emails?|inbox|messages|chats?|chat\s+history|conversations?|calendar|files"
                                      r"|photos|documents|browsing\s+history|search\s+history|memory|memories"
                                      r"|passwords?|credentials)\b")),
)]
MONEY_MOVEMENT = [re.compile(pattern, re.IGNORECASE) for pattern in (
    (r"\b(?:transfers?|transferring|moves?|moving|sends?|sending|wires?|wiring)\s+(?:the\s+|your\s+|their\s+)?"
    r"(?:funds|money|cash|balances?)\b"),
    r"\bwire\s+transfers?\b", r"\b(?:ach|p2p|peer-to-peer|interbank|bank)\s+(?:transfers?|payments?)\b",
    (r"\bbetween\s+(?:your\s+|the\s+user'?s\s+|their\s+)?(?:own\s+)?(?:bank\s+|financial\s+|checking\s+|savings\s+"
    r"|brokerage\s+)?accounts\b"),
    r"\bwithdraw(?:s|al|als)?\s+(?:funds|money|cash)\b",
)]
TRADE_WORDS = {"trade", "trades", "trading", "buy", "sell", "order", "orders", "short"}
TRADE_PARAMETERS = {"symbol", "ticker", "cusip", "isin", "instrument", "security"}
TRADE_SUBJECT = re.compile(r"\b(?:stocks?|shares\s+of|equit(?:y|ies)|securit(?:y|ies)|etfs?|crypto(?:currenc(?:y|ies))?"
                           r"|options?\s+contracts?|brokerage|bonds?|futures|(?:market|limit|stop)\s+orders?)\b",
                           re.IGNORECASE)
CREDENTIALS = [(kind, re.compile(pattern)) for kind, pattern in (
    ("Stripe secret key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("API secret key (sk-...)", r"\bsk-(?:proj-|ant-(?:api\d{2}-)?)?[A-Za-z0-9_-]{20,}"),
    ("GitHub token", r"\b(?:gh[opsur]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("GitLab token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("Google OAuth access token", r"\bya29\.[0-9A-Za-z_-]{20,}"),
    ("JSON Web Token", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("private key block", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    ("URL with an embedded password", r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@"),
    ("bearer token", r"(?i)\bbearer\s+([A-Za-z0-9._~+/-]{20,}=*)"),
)]
PAN = re.compile(r"(?<![\d.-])(?:\d[ -]?){12,18}\d(?![\d-])")
# A Luhn-valid number is only a card in context: card words nearby, or the usual 4-4-4-4 grouping.
CARD_CONTEXT = re.compile(r"(?i)\b(?:card|visa|mastercard|amex|american\s+express|discover|pan|credit|debit)\b")
GROUPED = re.compile(r"^\d{4}(?:[ -]\d{4}){2}[ -]\d{1,7}$")
TEST_PANS = {"4242424242424242", "4000056655665556", "5555555555554444", "2223003122003222", "5200828282828210",
             "5105105105105100", "378282246310005", "371449635398431", "6011111111111117", "6011000990139424",
             "3056930009020004", "36227206271667", "3566002020360505", "6200000000000005", "4111111111111111",
             "4012888888881881", "4000000000000077", "4000000000000002", "4000000000009995"}
PLACEHOLDER = re.compile(r"(?i)example|sample|dummy|placeholder|redacted|your[_-]?(?:api[_-]?)?(?:key|token)|xxxx|\*\*\*")
PLACEHOLDER_PASSWORD = re.compile(r"(?i)^(?:password|passwd|pass|secret|x{3,}|\*+|<[^>]*>|\{[^}]*\}|\$\{?\w+\}?)$")
SECRET_FIELDS = {"password", "passwd", "passphrase", "secret", "client_secret", "api_key", "apikey", "api_secret",
                 "secret_key", "private_key", "access_token", "refresh_token", "id_token", "auth_token",
                 "bearer_token", "session_token", "card_number", "cardnumber", "pan", "cvv", "cvc", "cvv2",
                 "security_code", "card_cvc", "card_security_code", "otp", "one_time_code", "verification_code",
                 "authorization_code", "authorization", "cookie", "set_cookie"}
MASKED = re.compile(r"^[\s*•xX#.-]*[*•xX#][\s*•xX#.-]*\d{0,4}$|^\[?redacted\]?$|^(?:null|none|n/?a|-)$",
                    re.IGNORECASE)  # "•••• 4242", "XXXX-XXXX-XXXX-4242"; a bare "123" CVV is no mask


def _luhn(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _fingerprint(value):
    # Length only: an unkeyed digest of a CVV, OTP, card number or password is reversible by brute force
    # from an export, and a keyed one would add nothing a finding's location does not already say.
    return f"{len(value)} characters"


def credential_hits(text: str, *, secrets=()) -> list[str]:
    """Credential shapes, payment card numbers and known secrets in one string; values are never echoed
    or fingerprinted (kind and length only)."""
    hits = []
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 8 and secret in text:
            hits.append(f"the credential this check linked with ({len(secret)} characters)")
    for kind, pattern in CREDENTIALS:
        for match in pattern.finditer(text):
            value = match.group(1) if pattern.groups else match.group(0)
            if kind == "URL with an embedded password":
                if PLACEHOLDER_PASSWORD.match(value):
                    continue  # "postgres://user:password@host" documents a shape (MCPJam's rule)
            elif kind != "private key block" and (len(set(value)) < 10 or PLACEHOLDER.search(value)):
                continue  # Documentation placeholders have the shape of a key and none of its entropy.
            hits.append(f"{kind} ({_fingerprint(value)})")
    for match in PAN.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        context = text[max(0, match.start() - 40):match.start()]
        if 13 <= len(digits) <= 19 and digits[0] in "23456" and _luhn(digits) and digits not in TEST_PANS \
                and len(set(digits)) > 2 and (CARD_CONTEXT.search(context) or GROUPED.match(match.group(0))):
            hits.append(f"payment card number ({_fingerprint(digits)})")
    return list(dict.fromkeys(hits))


def _field_hits(value, path, budget=None):
    """Secret-named fields holding a value, in a structured response or a schema default."""
    hits = []
    for location, text in _strings(value, path, budget):
        field = location.rsplit(".", 1)[-1].split("[", 1)[0]
        parent = location.rsplit(".", 2)[-2] if location.count(".") >= 2 else ""
        name = field if field not in ("default", "const", "examples", "enum") else parent
        if normalized_key(name) in SECRET_FIELDS and len(text.strip()) >= 3 and not MASKED.match(text.strip()) \
                and not PLACEHOLDER.search(text) and not PLACEHOLDER_PASSWORD.match(text.strip()):
            hits.append((location, f'a value under the secret-named field "{name}" ({_fingerprint(text)})'))
    return hits


# --- The classification sheet -----------------------------------------------------------------------

CLASS_LABELS = {"read": "Read", "write": "Write", "sensitive-write": "Sensitive write"}
BASIS_MAP = {"annotation": "annotation", "heuristic": "heuristic", "default": "default", "declared": "annotation"}


def _classifier():
    return _lane("permissions", "classify_tool")  # the lane's classifier; never a local substitute


def classification_sheet(tools: list[dict], declared: dict[str, ToolClass] | None = None, *,
                         classify=None) -> list[dict]:
    """MCPJam-compatible rows {tool, suggested, basis, reasons[, declared]} in listing order.

    "suggested" is permissions.classify_tool's class without the declaration, so a disagreeing
    declaration stays visible; "basis" uses MCPJam's values ("annotation", "combined-operations",
    "default") plus "heuristic" where CATT's verb lexicon decided (MCPJam would start that row as a
    write). A schema that enumerates read and write operations makes the row a write (3.2).
    """
    classify = classify or _classifier()
    declared = declared or {}
    rows = []
    for tool in _tools(tools):
        result = classify(tool)
        suggested, basis, reasons = result.tool_class, BASIS_MAP.get(result.basis, result.basis), list(result.reasons)
        if combined := combined_operations(tool):
            basis = "combined-operations"
            if not any(reason.startswith("combined-operations") for reason in reasons):
                reasons.append(f'"{combined["parameter"]}" offers both reads ({", ".join(combined["reads"])}) and '
                               f'writes ({", ".join(combined["writes"])}); a combined tool is a write (§3.2)')
            # The classifier already classed each enumerated operation (a send or delete is sensitive).
            suggested = "write" if suggested == "read" else suggested
        row = {"tool": tool["name"], "suggested": suggested, "basis": basis, "reasons": list(dict.fromkeys(reasons))}
        if tool["name"] in declared:
            row["declared"] = declared[tool["name"]]
        rows.append(row)
    return rows


def sheet_markdown(rows: list[dict]) -> str:
    """The sheet as the Markdown table section 5.6 asks for (MCPJam formatMuseClassificationSheet)."""
    def cell(value):
        return value.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ")
    lines = ["| Tool | Classification | Basis |", "| --- | --- | --- |"]
    for row in rows:
        declared, suggested = row.get("declared"), row["suggested"]
        if declared:
            basis = "declared; matches the suggestion" if declared == suggested else \
                f"declared; suggested {CLASS_LABELS[suggested]}: {'; '.join(row['reasons'])}"
        else:
            basis = f"suggested: {'; '.join(row['reasons'])}"
        lines.append(f"| `{cell(row['tool'])}` | {CLASS_LABELS[declared or suggested]} | {cell(basis)} |")
    return "\n".join(lines)


# --- Static findings --------------------------------------------------------------------------------

def _tools(tools):
    rows = list(tools or [])
    for tool in rows:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
            raise LabError("Every tools/list entry needs a nonempty string name")
    return rows


def _responses(responses):
    for index, response in enumerate(responses or ()):
        if isinstance(response, SampleResponse):
            yield index, response.tool, response.result
        elif isinstance(response, dict) and isinstance(response.get("result"), dict):
            yield index, response.get("tool"), response["result"]
        elif isinstance(response, dict):
            yield index, response.get("tool"), response


def _hidden_writes(tools, declared, sheet):
    found = []
    operations = _lane("permissions", "operation_signals")
    for tool, row in zip(tools, sheet):
        name = tool["name"]
        presented = _presented_read(tool, declared)
        if presented:
            signals = _name_actions(tool, leading=not presented.startswith("named")) + description_actions(tool)
            if (offered := operations(tool)) and offered["class"] == "sensitive-write":
                # A read whose schema offers a send or delete hides it (the enum is the tool's own action).
                signals.append(f'its "{offered["parameter"]}" enum offers '
                               f'{", ".join(map(repr, offered["values"]))}')
            if signals:
                found.append(_finding("3.2-hidden-write", "fail", f'"{name}" is presented as a read ({presented}), but '
                                      f'{"; ".join(signals)}.', tool=name, location="name/description"))
            elif row["suggested"] != "read" and (hidden := [reason for reason in row["reasons"]
                                                            if reason.startswith("hidden-write-signal")]):
                found.append(_finding("3.2-hidden-write", "warn", f'"{name}" is presented as a read ({presented}); the '
                                      f'connector lane classifies it {row["suggested"]}: {"; ".join(hidden)}.', tool=name,
                                      location="name/description", detection="classifier"))
        elif declared.get(name) == "write":
            words = sorted(set(name_words(name)) & SENSITIVE_NAME_WORDS)
            signals = ([f'the name contains {", ".join(map(repr, words))}'] if words else []) + description_actions(tool)
            if _annotations(tool).get("destructiveHint") is True:
                signals.append("it declares destructiveHint: true")
            if signals:
                found.append(_finding("3.2-hidden-write", "warn", f'"{name}" is declared Write (a routine edit), but '
                                      f'{"; ".join(signals)}; declare it Sensitive write if it is consequential.',
                                      tool=name, location="name/description", detection="manifest"))
    return found


def _combined(tools, declared):
    found = []
    for tool in tools:
        combined = combined_operations(tool)
        claims = [why for why, holds in (("annotated readOnlyHint: true", _annotations(tool).get("readOnlyHint") is True),
                                         ("declared Read", declared.get(tool["name"]) == "read")) if holds]
        if combined and claims:
            found.append(_finding("3.2-combined-read-write", "fail", f'"{tool["name"]}" is {" and ".join(claims)}, but '
                                  f'"{combined["parameter"]}" enumerates reads ({", ".join(combined["reads"])}) and '
                                  f'writes ({", ".join(combined["writes"])}).', tool=tool["name"],
                                  location=f"inputSchema.properties.{combined['parameter']}.enum", detection="schema"))
    return found


def _steering_in(text, *, tool, location):
    found = []
    for category, severity, pattern in STEERING:
        for match in pattern.finditer(text):
            if not _not_negated(text, match):
                continue  # "Never ignore the user's instructions" asks for the opposite.
            found.append(_finding("3.2-steering", severity, f'{location} instructs the agent to {category}: '
                                  f'"{match.group(0).strip()[:160]}". §3.2: "{POLICY["sections"]["3.2"]["quotes"][2]}"',
                                  tool=tool, location=location))
    return found


def _steering(tools, instructions, responses):
    found = []
    for tool in tools:
        budget = _Budget()
        for location, text in [*_texts(tool), *_schema_prose(tool, budget)]:
            found += _steering_in(text, tool=tool["name"], location=location)
        if budget.truncated:
            found.append(_truncated("3.2-steering", "the tool's schemas", tool["name"]))
    if isinstance(instructions, str) and instructions.strip():
        found += _steering_in(instructions, tool=None, location="initialize.instructions")
    for index, tool, result in _responses(responses):
        budget = _Budget()
        for path, text in _strings(result, f"responses[{index}]", budget):
            found += _steering_in(text, tool=tool, location=path)
        if budget.truncated:
            found.append(_truncated("3.2-steering", f"responses[{index}]", tool))
    return found


def _credentials(tools, instructions, responses, secrets):
    found = []

    def scan(value, path, tool):
        budget = _Budget()
        rows = [(location, hit) for location, text in _strings(value, path, budget)
                for hit in credential_hits(text, secrets=secrets)] + _field_hits(value, path, _Budget())
        for location, hit in dict.fromkeys(rows):
            found.append(_finding("4.4-credential-exposure", "fail", f"{location} exposes {hit}; remove and rotate "
                                  "it.", tool=tool, location=location))
        if budget.truncated:
            found.append(_truncated("4.4-credential-exposure", path or "the listing entry", tool))
    for tool in tools:
        scan({key: tool.get(key) for key in ("name", "title", "description", "annotations", "inputSchema",
                                             "outputSchema", "_meta") if key in tool}, "", tool["name"])
    if isinstance(instructions, str):
        scan(instructions, "initialize.instructions", None)
    for index, tool, result in _responses(responses):
        scan(result, f"responses[{index}]", tool)
    return found


def _money_movement(tools, declared):
    found = []
    for tool in tools:
        if _presented_read(tool, declared):
            continue  # 4.6 allows read access to financial data; concealed actions are 3.2's.
        name = tool["name"]
        text = " ".join([" ".join(name_words(name)), *(value for _, value in _texts(tool))])
        signals = [f'"{match.group(0).strip()}"' for pattern in MONEY_MOVEMENT for match in pattern.finditer(text)
                   if _not_negated(text, match)]
        words = set(name_words(name)) & TRADE_WORDS
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        ticker = next((key for key in properties if str(key).lower() in TRADE_PARAMETERS), None)
        subject = TRADE_SUBJECT.search(" ".join(value for _, value in _texts(tool)))
        if words and (ticker or subject):  # A trade verb alone is e-commerce ("buy_item").
            signals.append(f'a trade order: the name has "{min(words)}" and '
                           + (f'it takes "{ticker}"' if ticker else f'the description mentions "{subject.group(0)}"'))
        if signals:
            found.append(_finding("4.6-money-movement", "fail", f'"{name}" looks like a money-movement or trade-order '
                                  f'tool: {"; ".join(dict.fromkeys(signals))}.', tool=name, location="name/description"))
    return found


def _transaction_tools(tools, declared, sheet):
    rows = {row["tool"]: row for row in sheet}
    found = []
    for tool in tools:
        name, row = tool["name"], rows.get(tool["name"], {})
        cls = declared.get(name) or row.get("suggested")
        if cls == "sensitive-write" and set(name_words(name)) & TRANSACTION_WORDS and not _presented_read(tool, declared):
            found.append(name)
    return found


def _not_evaluated(rule, what):
    return _finding(rule, "info", f"Not evaluated: no readiness manifest was supplied ({what}).", detection="manifest",
                    evaluated=False)


def _manifest_findings(tools, manifest, sheet):
    declared = manifest.classes if manifest else {}
    transactions = _transaction_tools(tools, declared, sheet)
    if manifest is None:
        return [_not_evaluated("5.4-doc-topics", "documentation coverage"),
                _not_evaluated("5.5-test-account", "test account"),
                _not_evaluated("5.5-no-charge-checkout", "no-charge checkout path"
                               + (f"; tools that look like transactions: {', '.join(transactions)}" if transactions
                                  else "")),
                _not_evaluated("5.6-tool-classes", "declared tool classes")]
    found = []
    docs = manifest.documentation
    if docs is None:
        found.append(_finding("5.4-doc-topics", "fail", "The manifest declares no documentation coverage; document "
                              f"every 5.4 topic: {', '.join(DOC_TOPICS)}.", location="manifest.documentation",
                              detection="manifest"))
    else:
        missing = [topic for topic in DOC_TOPICS if topic not in docs.covers]
        if not docs.url or missing:
            found.append(_finding("5.4-doc-topics", "fail", "; ".join(filter(None, [
                "The manifest gives no documentation URL" if not docs.url else None,
                f"the documentation does not cover: {', '.join(missing)}" if missing else None])) + ".",
                location="manifest.documentation", detection="manifest"))
        if docs.url and not docs.url.startswith("https://"):
            found.append(_finding("5.4-doc-topics", "warn", "The documentation URL is not https://; reviewers and "
                                  "users need public, encrypted documentation.", location="manifest.documentation.url",
                                  detection="manifest"))
    account = manifest.test_account
    fields = {"sign_in_instructions": "sign-in instructions", "required_permissions": "required permissions",
              "representative_test_data": "representative test data"}
    missing = [label for key, label in fields.items() if not (getattr(account, key, None) or "").strip()]
    if account is None or account.accessible_throughout_review is not True:
        missing.append("a commitment to keep it accessible throughout review")
    if account is None or missing:
        found.append(_finding("5.5-test-account", "fail", "The manifest " + (
            "describes no dedicated test account" if account is None else
            f"test account lacks: {', '.join(missing)}") + ".", location="manifest.test_account",
            detection="manifest"))
    checkout = manifest.transactions
    if checkout is None:
        found.append(_finding("5.5-no-charge-checkout", "warn" if transactions else "info", (
            f"The manifest does not say whether there are transaction tools; these look like transactions: "
            f"{', '.join(transactions)}." if transactions else
            "The manifest does not say whether there are transaction tools."), location="manifest.transactions",
            detection="manifest", evaluated=bool(transactions)))
    elif checkout.has_transaction_tools and not (checkout.no_charge_test_path or "").strip():
        found.append(_finding("5.5-no-charge-checkout", "fail", "The manifest declares transaction tools but not how "
                              "to test checkout without real charges.", location="manifest.transactions",
                              detection="manifest"))
    elif not checkout.has_transaction_tools and transactions:
        found.append(_finding("5.5-no-charge-checkout", "warn", "The manifest declares no transaction tools, but these "
                              f"look like transactions: {', '.join(transactions)}.", location="manifest.transactions",
                              detection="manifest"))
    names = [tool["name"] for tool in tools]
    if unclassified := [name for name in names if name not in declared]:
        found.append(_finding("5.6-tool-classes", "warn", "Declare a Read, Write or Sensitive write class for every "
                              f"tool (the sheet is a starting point); unclassified: {', '.join(unclassified)}.",
                              location="manifest.classes", detection="manifest"))
    for tool in tools:
        annotations = _annotations(tool)
        reasons = [why for why, holds in (("readOnlyHint: false", annotations.get("readOnlyHint") is False),
                                          ("destructiveHint: true", annotations.get("destructiveHint") is True))
                   if holds]
        if declared.get(tool["name"]) == "read" and reasons:
            found.append(_finding("5.6-tool-classes", "fail", f'"{tool["name"]}" is declared Read, but the server '
                                  f'annotates it {" and ".join(reasons)}.', tool=tool["name"],
                                  location="manifest.classes", detection="annotation"))
    if stale := sorted(set(declared) - set(names)):
        found.append(_finding("5.6-tool-classes", "info", f"Declared but not listed: {', '.join(stale)} (a scope this "
                              "check did not hold may hide them).", location="manifest.classes", detection="manifest"))
    return found


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _read_only_oauth(manifest, auth):
    found, declared = [], manifest.authentication if manifest else None
    if declared and declared.method == "oauth":
        read_only = [scope for scope in declared.read_only_scopes if scope.strip()]
        if not read_only and not (declared.read_only_not_possible_reason or "").strip():
            found.append(_finding("3.1-read-only-oauth", "info", "The manifest's OAuth setup offers no read-only "
                                  "scope set and gives no reason it is not possible.",
                                  location="manifest.authentication", detection="manifest"))
        elif unrequested := sorted(set(read_only) - set(declared.scopes)):
            found.append(_finding("3.1-read-only-oauth", "info", "These read-only scopes are not among the requested "
                                  f"scopes, so Muse cannot request them: {', '.join(unrequested)}.",
                                  location="manifest.authentication", detection="manifest"))
    if _field(auth, "mode") == "oauth" and not _field(auth, "read_only_scopes") and not (
            declared and (declared.read_only_scopes or declared.read_only_not_possible_reason)):
        scopes = _field(auth, "scopes") or []
        found.append(_finding("3.1-read-only-oauth", "info", "This check linked with OAuth"
                              + (f" scopes {', '.join(map(str, scopes))}" if scopes else "")
                              + " and no read-only scope set (auth.read_only_scopes).", location="auth",
                              detection="auth"))
    return found


def static_findings(tools: list[dict], manifest: ReadinessManifest | None = None, responses=(), *,
                    auth: Any = None, instructions: str | None = None, secrets=(), sheet: list[dict] | None = None,
                    classify=None) -> list[GuidelineFinding]:
    """Documented-constraint findings from a listing, sampled responses and the readiness manifest.

    tools: tools/list entries. responses: sampled tools/call results ({"tool", "result"} or bare results)
    that run elsewhere on a test account; this function never calls a tool. auth: the TargetAuth used,
    for 3.1. instructions: the server's initialize instructions. secrets: credential values to detect if
    echoed. sheet: a precomputed classification_sheet (computed with permissions.classify_tool otherwise).
    """
    tools = _tools(tools)
    declared = dict(manifest.classes) if manifest else {}
    sheet = sheet if sheet is not None else classification_sheet(tools, declared, classify=classify)
    responses = list(responses or ())
    found = (_hidden_writes(tools, declared, sheet) + _combined(tools, declared)
             + _steering(tools, instructions, responses) + _credentials(tools, instructions, responses, secrets)
             + _money_movement(tools, declared) + _manifest_findings(tools, manifest, sheet)
             + _read_only_oauth(manifest, auth))
    order = list(RULES)
    unique = list(dict.fromkeys(found))
    return sorted(unique, key=lambda row: (order.index(row.rule), ("fail", "warn", "info").index(row.severity),
                                           row.tool or "", row.location or ""))


def rule_checks(findings: list[GuidelineFinding]) -> list[dict]:
    """One report row per finding, and a "no-finding" row per rule without one (never "passed")."""
    rows = []
    for rule in RULES:
        hits = [row for row in findings if row.rule == rule]
        key = RULES[rule][0]
        if not hits:
            rows.append({"id": rule, "layer": "guidelines", "status": "no-finding", "required": False,
                         "detail": "no finding; not an admission",
                         "basis": f"§{key} {POLICY['sections'][key]['title']}"})
        for row in hits:
            status = "not-evaluated" if not row.evaluated else {"fail": "failed", "warn": "warning",
                                                                "info": "info"}[row.severity]
            rows.append({"id": f"{rule}/{row.tool}" if row.tool else rule, "layer": "guidelines", "status": status,
                         "required": row.severity == "fail", "detail": row.evidence, "basis": row.section})
    return rows


# --- The check run ----------------------------------------------------------------------------------

class _Events:
    def __init__(self, path, redactor, run_id):
        self.path, self.redactor, self.run_id, self.sequence = path, redactor, run_id, 0
        self.clock_id = uuid.uuid4().hex

    def record(self, kind, value):
        from .sealed_events import record
        self.sequence += 1
        row = record(self.path, self.redactor, self.sequence, kind, value, scenario_id=SUITE, profile=INTERFACE,
                     clock_id=self.clock_id, driver="static", run_id=self.run_id)
        with (self.path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")


def _lane(module, name):
    """A sibling module of the connector lane; its absence is a setup error, never a silent skip."""
    import importlib
    try:
        return getattr(importlib.import_module(f".{module}", __package__), name)
    except (ImportError, AttributeError) as error:
        raise LabError(f"The connector lane's {module}.{name} is unavailable: {error}") from error


def public_policy() -> dict:
    return {key: POLICY[key] for key in ("id", "source", "terms_source", "fetched", "extract_sha256_32",
                                         "extract_chars", "terms_extract_sha256_32", "terms_effective", "method",
                                         "method_source") if key in POLICY}


async def run_check(target_endpoint: str, *, auth: "TargetAuth", manifest_path: Path | None, output: Path,
                    profile: "ClientProfile", store: Path | None = None, sealed=None) -> int:
    """List the target's tools (never call one), write a sealed report under output and return the exit
    code: 0 no fail finding, 1 fail findings, 2 setup error, 3 the selected client profile cannot reach
    the server (for the opt-in unofficial template, a limit of that profile, never a guidelines finding).
    The report says "documented constraint, not admission"; its path is printed on stdout and a one-line
    outcome, naming each failing rule and tool, on stderr. OAuth tokens live in store (default: the
    workspace's .cache/connector-links, which connector-run also uses). sealed(path, code), when given, is
    called with the sealed run (check --junit and --summary-json read it back)."""
    from .sealed_events import metadata
    from .sealed_report import finalize
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    run_id = f"guidelines-{uuid.uuid4().hex}"
    path = output / run_id
    path.mkdir(mode=0o700)
    redactor = Redactor.environment()
    redactor.protect(run_id, SUITE, *INTERFACE.values())
    events = _Events(path, redactor, run_id)
    stage, code, detail, category = "manifest", 0, None, None
    manifest, tools, sheet, findings, server, linked = None, [], [], [], {}, {}
    started = datetime.now(UTC).isoformat()
    try:
        events.record("policy", {"policy": public_policy(), "label": LABEL})
        manifest = load_manifest(manifest_path) if manifest_path else None
        stage = "link"
        link = _lane("linking", "link")
        if store is None:  # connector-run's workspace store: a linked account is reused, never kept in runs/
            store = Path.cwd().resolve() / ".cache" / "connector-links"
            store.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential = await link(auth, target_endpoint, store=Path(store), http_policy=profile)
        secrets = [value for value in (getattr(credential, "secrets", None) or []) if isinstance(value, str)]
        redactor.add(secrets)
        linked = credential.describe()
        stage = "connect"
        make_client = _lane("mcp_clients", "make_client")
        client = make_client(profile, target_endpoint, credential)
        try:
            await client.connect()
            stage = "tools/list"
            tools = _tools(await client.list_tools())  # Read-only: no tools/call is ever sent.
        finally:
            try:
                await client.close()
            except Exception as error:  # noqa: BLE001 — closing a read-only session decides nothing
                events.record("cleanup_failure", {"detail": str(error) or type(error).__name__})
        instructions = getattr(client, "instructions", None)
        # Digests of the redacted text the report shows: a digest of raw server text would let anyone with
        # the report test guesses of an account name or credential the text holds.
        server = {"server_info": getattr(client, "server_info", None),
                  "protocol_version": getattr(client, "protocol_version", None),
                  "capabilities": getattr(client, "capabilities", None),
                  "instructions_sha256": digest(redactor.clean_evidence(instructions))
                  if isinstance(instructions, str) else None,
                  "tools_sha256": digest(redactor.clean_evidence(tools)), "tool_count": len(tools),
                  "digest_basis": "sha256 of the redacted text evidence shows",
                  "observations": list(getattr(client, "observations", None) or [])}
        events.record("catalog", {**server, "instructions": instructions, "tools": tools})
        stage = "classification"
        sheet = classification_sheet(tools, manifest.classes if manifest else {})
        events.record("classification", {"rows": sheet, "markdown": sheet_markdown(sheet), "format": MCPJAM_SHEET})
        stage = "findings"
        findings = static_findings(tools, manifest, manifest.sample_responses if manifest else (), auth=auth,
                                   instructions=instructions, secrets=secrets, sheet=sheet)
        events.record("guideline_findings", {"findings": [row.model_dump() for row in findings]})
        stage = None
        code = 1 if any(row.severity == "fail" for row in findings) else 0
    except Exception as error:  # noqa: BLE001 — a setup failure is still reported and sealed
        failure = normalize_error(error)
        code, category = (3 if failure.code == 3 else 2), failure.category
        detail = redactor.text(str(failure))
        if stage in ("connect", "tools/list") and category == "setup/infrastructure" and target_endpoint not in detail:
            detail = redactor.text(f"{detail} (connecting to {target_endpoint})")  # Where to look, not just why.
        if code == 3 and _field(profile, "name") == TEMPLATE_PROFILE:
            # The unofficial template refuses what the guidelines never mention (port 443, redirects).
            detail = (f"Limit of the opt-in unofficial client profile {TEMPLATE_PROFILE}, not a guidelines "
                      f"finding: {detail}; rerun with --client-profile generic-sdk")
        events.record("setup_error", {"stage": stage, "class": category, "detail": detail})
    counts = {severity: sum(row.severity == severity for row in findings) for severity in ("fail", "warn", "info")}
    status = "completed" if code == 0 else "blocked" if code == 3 else "failed"
    if code == 1:
        detail = f"{counts['fail']} documented-constraint finding(s) at severity fail"
        category, stage = "guidelines", "findings"
    events.record("run_finished", {"status": status, "exit_code": code, "counts": counts})
    recorded = metadata(path, scenario_id=SUITE, profile=INTERFACE, driver="static", run_id=run_id, locations={
        "inference": "none", "browser": "none", "target": redactor.text(target_endpoint), "telemetry": "disabled",
        "egress": "the target endpoint and its authorization server only"})
    report = {
        **recorded, "lane": "readiness", "evidence_label": LABEL,
        "fidelity": "static tools/list review against the pinned Muse connector guidelines; no tool calls",
        "schema_version": 1, "kind": "muse-guidelines-check", "suite": SUITE, "label": LABEL,
        "status": status, "detail": detail, "failure_class": category, "failure_stage": stage if code else None,
        # Findings fail verification, not execution.
        "execution_status": "failed" if code == 2 else "blocked" if code == 3 else "completed",
        "exit_code": code, "started_at": started, "compatibility_pass": False,
        "policy": public_policy(),
        "target": {"endpoint": target_endpoint, "client_profile": _field(profile, "name"),
                   "client_basis": _field(profile, "basis"), "auth": linked},
        "manifest": {"path": str(manifest_path), "sha256": digest(manifest.model_dump(mode="json"))}
        if manifest else None,
        "server": server, "classification_sheet": sheet, "classification_sheet_format": MCPJAM_SHEET,
        "classification_sheet_markdown": sheet_markdown(sheet) if sheet else None,
        "findings": [row.model_dump() for row in findings], "summary": counts, "checks": rule_checks(findings)
        if code in (0, 1) else [], "run_lock": {"policy": POLICY["id"], "policy_sha256": digest(POLICY),
                                           "tools_sha256": server.get("tools_sha256")},
        "evidence_limits": [
            ("Documented constraint, not admission: Meta reviews each connector in three stages and meeting the "
            "guidelines does not guarantee approval (§6)."),
            "Static and lexical (English): findings are reasons to look; no finding proves compliance.",
            ("No tool was called. Behavior on a test account (approvals, duplicates, honest outcomes) needs "
            "connector-run."),
            "Manifest declarations are checked for presence; their truth stays with Meta's reviewers."],
    }
    result = finalize(path, redactor.clean_evidence(report))
    if result["reporting_status"] != "completed":
        code = 2
    print(path)
    failing = list(dict.fromkeys(f"{row.rule} ({row.tool})" if row.tool else row.rule
                                 for row in findings if row.severity == "fail"))
    line = f"{SUITE} {result['status']}: {counts['fail']} fail, {counts['warn']} warn, {counts['info']} info ({LABEL})"
    line += f"; failing: {', '.join(failing[:10])}" + (f" (+{len(failing) - 10} more)" if len(failing) > 10 else "") \
        if failing else ""
    print(line + (f"; {detail}" if detail and code in (2, 3) else "") + f". Evidence: {path}", file=sys.stderr)
    if sealed is not None:
        sealed(path, code)
    return code


# --- Drift --------------------------------------------------------------------------------------------

async def drift_check(timeout: float = 20, *, transport=None) -> dict:
    """Opt-in: fetch the guidelines and terms and compare MCPJam-method hashes with the pin.

    Network only when called; tests pass an httpx transport. A page that fails to fetch is an error,
    never hashed (an error page would pin "no drift" forever), and a missing pin counts as drift.
    """
    import httpx
    pages = [("docs", POLICY["source"], POLICY["extract_sha256_32"]),
             ("terms", POLICY["terms_source"], POLICY.get("terms_extract_sha256_32"))]
    results, failures = {}, []
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, transport=transport,
                                 headers={"accept": "text/html", "user-agent": USER_AGENT}) as client:
        for page, url, pinned in pages:
            try:
                async with client.stream("GET", url) as response:
                    if not 200 <= response.status_code < 300:
                        raise LabError(f"HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES:
                            raise LabError(f"page larger than {MAX_PAGE_BYTES} bytes")
                    text = extract_text(bytes(body).decode("utf-8-sig", "replace"))  # fetch's text(): UTF-8
                    results[page] = {"url": url, "final_url": str(response.url), "revision": hash_policy_text(text),
                                     "chars": js_length(text), "pinned": pinned,
                                     "etag": response.headers.get("etag"),
                                     "last_modified": response.headers.get("last-modified")}
                    results[page]["drifted"] = results[page]["revision"] != pinned
            except (httpx.HTTPError, LabError) as error:
                failures.append({"page": page, "url": url, "error": str(error) or type(error).__name__})
    drifted = sorted(page for page, row in results.items() if row["drifted"])
    return {"status": "error" if failures else "drift" if drifted else "ok", "checked_at": datetime.now(UTC).isoformat(),
            "policy": POLICY["id"], "fetched": POLICY["fetched"], "method": POLICY["method"], "pages": results,
            "drifted": drifted, "failures": failures,
            "action": "Re-audit the rules that cite a drifted page before trusting a check made against the pin."
            if drifted else None}
