"""Muse-like connector permissions: tool classes, approval cards and a scripted user.

Host-owned and never a model tool: the model proposes a call, this engine decides whether the
user is asked, the scenario's scripted user answers the card (first matching rule wins), and the
model only learns that a call was declined or unanswered. Every Muse-specific behavior carries a
basis (CLASS_BASIS, RULE_BASIS): documented rules quote the connector guidelines (sections
3.2-3.4) or the Help Center; everything else is a labeled surrogate assumption. A public-constraint
surrogate, not Muse.
"""
import copy
import hashlib
import hmac
import math
import operator
import re
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .consent import CANONICALIZER, canonical, digest
from .emulation import behavior_bases, target_profile
from .errors import LabError
from .evidence import normalized_key
from .projection import _resolve as local_reference
from .serialization import check_finite

ToolClass = Literal["read", "write", "sensitive-write"]
Basis = Literal["declared", "annotation", "heuristic", "default"]
GrantScope = Literal["once", "task", "site", "always"]
DecisionKind = Literal["allow_once", "allow_task", "allow_site", "always_allow", "deny", "edit", "no_response"]

CLASSES = ("read", "write", "sensitive-write")
KINDS = ("allow_once", "allow_task", "allow_site", "always_allow", "deny", "edit", "no_response")
SCOPES = {"allow_once": "once", "edit": "once", "allow_task": "task", "allow_site": "site", "always_allow": "always"}
# The RULE_BASIS entry behind each final decision kind, recorded on every decision row.
KIND_BASIS = {"allow_once": "allow-once", "allow_task": "allow-task", "allow_site": "allow-site",
              "always_allow": "always-allow", "deny": "deny", "edit": "edit", "no_response": "no-response"}
# Card buttons (guidelines 3.3 table plus the Help Center's task scope). Edit and no response are
# user behaviors, not buttons. Allow for this site is a website scope: only website engines (browser task
# cards, sites=True) offer it, on cards that name their site.
CHOICES = ["allow_once", "allow_task", "always_allow", "deny"]
SITE_CHOICES = ["allow_once", "allow_task", "allow_site", "always_allow", "deny"]
ONCE_ONLY = ["allow_once", "deny"]
# Where each button comes from, recorded on every card and decision: the guidelines' 3.3 table lists
# Allow once, Always allow and Deny for platform connectors; Allow for this task is the Help Center's.
CHOICE_SOURCE = {"allow_once": "guidelines 3.3 table; Help Center",
                 "always_allow": "guidelines 3.3 table (per tool; the Help Center scopes it per Connector)",
                 "deny": "guidelines 3.3 table; Help Center",
                 "allow_task": "Help Center only (not in the guidelines 3.3 table)",
                 "allow_site": "Help Center only (a website scope: browser task cards)"}
# Other spellings of Allow for this site are refused; allow_site itself answers website cards only.
SITE_KINDS = {"allow_for_this_site", "site"}
# The connector name a website engine's cards carry (ext_browser's browser task approvals).
WEBSITE = "browser"
PRIVATE = "[private]"
# Terms guidelines 4.7 lists for tools to return before commitment; scenario disclosure pointers name the keys.
DISCLOSURE_FIELDS = ("seller", "total", "currency", "recurring", "key_terms", "cancellation")
DECLINED = "The user declined this action."
UNANSWERED = "The user has not responded to the approval request; the action was not taken."
ID = r"^[a-zA-Z][a-zA-Z0-9_-]*$"
# Who asked for a call: the model, or an extension acting for the user (a wallet, a background job).
ORIGIN = re.compile(r"model|extension:[a-z][a-z0-9_.:-]{0,63}")

GUIDELINES = "https://platform.muse.ai/docs"
HELP_CENTER = "https://www.meta.com/help/artificial-intelligence/1385290430137537/"
SECURITY_POST = "https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse"
# Fetched 2026-10-15; MCPJam extractText + sha256[0:32] of the guidelines was 60628e5a1c6199b8635a161b7f5a8aaa.
CHECKED = "2026-10-15"


def documented(source, quote, assumed=None):
    return {"basis": "documented", "source": source, "checked": CHECKED, "quote": quote,
            **({"assumed": assumed} if assumed else {})}


def assumed(detail):
    return {"basis": "assumed", "source": None, "detail": detail}


CLASS_BASIS = {
    "read": documented(f"{GUIDELINES} section 3.2",
                       "Read: Retrieves information without changing data, account state, or permissions."),
    "write": documented(f"{GUIDELINES} section 3.2", "Write: Creates, changes, or deletes data, changes "
                        "permissions, or acts on the user’s behalf."),
    "sensitive-write": documented(f"{GUIDELINES} section 3.2", "Sensitive write: Certain actions that produce "
                                  "irreversible or consequential actions (make a purchase, send an email, etc.)"),
}
RULE_BASIS = {
    "review-classifies": documented(
        f"{GUIDELINES} section 3.2", "In the review process we’ll review your documentation and classify each tool",
        "A declared scenario class stands in for that review and wins over annotations and heuristics"),
    "combined-read-write": documented(
        f"{GUIDELINES} section 3.2", "A tool that combines reads and writes must be classified as a write."),
    "hidden-write": documented(
        f"{GUIDELINES} section 3.2", "Never hide a send, share, publish, or payment action in a tool described "
        "as a read or routine edit.", "Detected lexically in the name, titles, description and input property "
        "names of a read-annotated tool; the class follows the strongest signal (fail closed)"),
    "annotations": {"basis": "documented", "source": "mcp==2.2.0 mcp.types.ToolAnnotations",
                    "quote": "Clients should never make tool use decisions based on ToolAnnotations received "
                             "from untrusted servers.",
                    "assumed": "readOnlyHint and destructiveHint stand in for the developer's documented "
                               "class when no class is declared; conflicting hints take the stricter class. "
                               "readOnlyHint false without destructiveHint is read as a plain write: MCP's "
                               "default destructiveHint=true is not applied, because Muse's classes come from "
                               "its review, not from annotations; destructive verbs still make it sensitive"},
    "heuristic": assumed("English verb lexicon over tool names, titles, descriptions and enumerated operation "
                         "values; precision first for hidden writes, never a substitute for a declared class. "
                         "Book and reserve count as sensitive writes (guidelines 2: 'Because this booking "
                         "includes payment, it is a sensitive write'), although 3.2 lists creating a "
                         "reservation as a Write example"),
    "operations": assumed("A schema that enumerates operations (a selector such as operation, action or command, "
                          "also as a word of a longer name such as operation_type or actionName, or any enum "
                          "offering a read and a write verb, as MCPJam reads it) can perform each value, however "
                          "the schema enumerates it (enum or const, through local $ref, anyOf, oneOf, allOf and "
                          "array items, in nested objects too): the strongest value class applies, so a send, "
                          "delete or payment value makes the tool a sensitive write whatever its annotations "
                          "(guidelines 3.2). A write word that also names a computation (add, set, replace, move, "
                          "return, mark, merge, change, run) counts only beside a read operation or in a tool "
                          "not annotated read-only, so a read-only calculator stays a read"),
    "unknown-is-write": assumed("A tool with no declared class, annotation or recognized verb is a write: "
                                "unknown tools stay write-gated"),
    "platform-default": documented(
        f"{GUIDELINES} section 3.3", "Currently reads and writes from platform connectors default to Ask on "
        "first use", "Reads and writes ask on first use, then on each use unless Always allow (or the Help "
        "Center's Allow for this task) was chosen"),
    "sensitive-every-use": documented(
        f"{GUIDELINES} section 3.3", "Sensitive writes cannot be set to Always allow, including through "
        "Settings or a saved permission.", "Sensitive cards offer Allow once and Deny only; a scripted task or "
        "Always allow answer is coerced to Allow once and recorded as scope not offered"),
    "allow-once": documented(f"{GUIDELINES} section 3.4", "Allow once covers only the action and details shown."),
    "changed-details": documented(
        f"{GUIDELINES} section 3.4", "If any details (e.g. the price, recipient, scope) change after a write is "
        "approved, require a new fresh write to be approved.", "Each card binds an HMAC-SHA256, under a random "
        "key this engine holds in memory only, of the RFC 8785 form of the connector, tool, effective arguments "
        "and disclosure; one Allow once covers one call with that binding"),
    "always-allow": documented(
        f"{GUIDELINES} section 3.3", "Always allow applies to future uses of that tool within its approved scope "
        "and can be revoked.", "Held for this engine's lifetime; persisted_always models an earlier choice. "
        "Scoped per tool as the guidelines say; the Help Center scopes it to 'this type of action for this "
        "Connector', which this engine does not model"),
    "allow-task": documented(
        HELP_CENTER, "Allow for this task: Muse can take this type of action for the entire task",
        "The type of action is the tool and the task is this engine (one trial)"),
    "allow-site": documented(
        HELP_CENTER, "Allow for this site: Muse can take this type of action for this website in the future "
        "without asking again", "A website scope: browser task cards that name their site offer it for writes "
        "that are not sensitive, and it covers the same type of action (tool) on the same site (origin) for this "
        "engine's lifetime (one trial; 'in the future' beyond it is not emulated). Connector cards never offer it: "
        "a rule answering allow_site must match browser cards only (connector eq browser), and one a card without "
        "it meets is coerced to Allow once"),
    "deny": documented(HELP_CENTER, "Deny: Muse won’t proceed this one time",
                       "A denial is not remembered: the next call asks again"),
    "ask-for-some": documented(
        HELP_CENTER, "If you select Ask for some actions, your Muse will ask for permission before every write "
        "action and important read actions.", "Which reads are important is not published; the scenario lists "
        "them (important_reads). Writes follow the platform default, as the P1 contract specifies, so an "
        "existing task or Always allow grant still covers a write although the Help Center says 'every write "
        "action'"),
    "always-ask": documented(
        HELP_CENTER, "If you select Always ask, your Muse will ask for permission before any action.",
        "No task or Always allow grant, saved or new, is honored; cards offer Allow once and Deny"),
    "card-outside-conversation": documented(
        SECURITY_POST, "A dialog is presented to the user directly within the client UI — not via their "
        "conversation", "The model gets only a short decline message, never the card"),
    "disclosure": documented(
        f"{GUIDELINES} section 4.7", "Before commitment, your tools should return all relevant information, such "
        "as: the seller, full price and currency, recurring charges, key terms, and cancellation or refund "
        "policy.", "The guideline asks the connector's tools to return these terms; that a sensitive card shows "
        "them is a host assumption, checked only where the scenario configures disclosure or the tool looks "
        "like a transaction (missing_disclosure lists the DISCLOSURE_FIELDS a card lacks)"),
    "withheld-terms": assumed("A term the scenario configures for a card but the host could not show (no "
                              "accepted result the call names, or nothing at its pointer) is null on the card and "
                              "counts as not shown: exists is false for it, and a rule that compares it is "
                              "undecided, so the scripted user answers with the policy default (deny or no "
                              "response), never a later rule. A rule cannot approve on a term the card does not "
                              "show"),
    "edit": assumed("No documented edit control: a user who corrects the shown details and allows that one "
                    "call; host-private values cannot be edited"),
    "no-response": assumed("An unanswered card is not approval: the call is not dispatched"),
    "origin": assumed("A card records who asked for the call when it was not the model (extension:<source>, such "
                      "as a wallet payment or a background job), and scripted rules may match it (field origin), so "
                      "an away user can leave a background card unanswered; how Muse shows such cards is not public"),
}

# Lexical class signals (assumed, English only) standing in for Meta's documentation review.
VERBS = {
    **dict.fromkeys(("get", "list", "search", "find", "read", "check", "quote", "fetch", "lookup", "look",
                     "view", "show", "describe", "query", "retrieve", "browse", "preview", "estimate", "count",
                     "verify", "validate", "calculate", "compare", "track", "inspect"), "read"),
    **dict.fromkeys(("create", "update", "save", "set", "add", "edit", "modify", "change", "upsert", "insert",
                     "write", "upload", "rename", "move", "mark", "hold", "schedule", "register", "enable",
                     "disable", "assign", "archive", "checkin", "signup", "return", "replace", "restore", "merge",
                     "push", "commit", "fork", "close", "reopen", "approve"), "write"),
    # Recurring charges and plan changes are financial commitments (Muse Connector Policies).
    **dict.fromkeys(("pay", "purchase", "buy", "checkout", "placeorder", "order", "book", "reserve", "submit",
                     "send", "email", "message", "notify", "invite", "reply", "forward", "transfer", "withdraw",
                     "charge", "refund", "delete", "remove", "cancel", "publish", "share", "post", "subscribe",
                     "unsubscribe", "renew", "upgrade", "downgrade", "donate", "secure", "erase", "wipe", "purge",
                     "destroy"), "sensitive-write"),
}
# Also common nouns ("order status", "taxes and charges"): a verb only where the position says so.
NOUNS = frozenset({"order", "book", "transfer", "charge", "refund", "purchase", "share", "post", "email",
                   "message", "reply", "forward", "checkout", "checkin", "secure", "update", "change", "set", "hold",
                   "schedule", "move", "mark", "register", "archive", "return", "list", "search", "view", "check",
                   "count", "quote", "estimate", "preview", "query", "track", "lookup", "read", "show"})
# Nouns before a read noun ("order status", "transfer history"); "update status" stays a verb.
NOUN_PHRASE_HEADS = frozenset({"order", "transfer", "refund", "charge", "purchase", "checkout", "return"})
READ_NOUNS = frozenset({"status", "statuses", "history", "details", "detail", "info", "information", "summary",
                        "receipt", "receipts", "policy", "policies", "limits", "limit", "options", "settings",
                        "availability", "preview", "estimate", "metadata", "report", "reports", "count", "list",
                        "lookup", "search", "state", "records", "record"})
MONEY_NOUNS = frozenset({"payment", "payments", "charge", "charges", "transfer", "transfers", "refund", "refunds",
                         "purchase", "purchases", "order", "orders", "checkout"})
# An ambiguous word followed by a copula is a subject ("charges apply") and by a compound head a
# noun ("email address", "order number").
COPULAS = frozenset({"is", "are", "was", "were", "be", "been", "has", "have", "apply", "applies", "may", "can",
                     "will", "must", "should", "include", "includes"})
COMPOUND_HEADS = frozenset({"address", "addresses", "id", "ids", "number", "numbers", "body", "content", "title",
                            "subject", "amount", "amounts", "total", "totals", "date", "dates", "time", "times",
                            "reference", "code", "template", "templates", "thread", "threads", "draft", "drafts",
                            "type", "types", "url", "urls", "page", "pages", "result", "results", "item", "items",
                            "data"})
PREPOSITIONS = frozenset({"for", "of", "in", "on", "at", "to", "from", "with", "by", "about", "between", "under",
                          "over", "within", "without", "per", "across", "after", "before", "during", "including",
                          "like", "as", "than", "that", "which"})
# Words after which the next verb is this tool's own action ("and books the room", "it sends").
CONNECTORS = frozenset({"and", "or", "then", "also", "will", "automatically", "immediately", "instantly", "it",
                        "tool", "endpoint", "function"})
NAME_PAIRS = {("check", "out"): "checkout", ("check", "in"): "checkin", ("look", "up"): "lookup",
              ("sign", "up"): "signup", ("place", "order"): "placeorder"}
TEXT_PAIRS = {("check", "out"): "checkout", ("look", "up"): "lookup", ("sign", "up"): "signup"}  # Not "checks in".
# Clause boundaries; a negation covers the rest of its clause ("does not reserve the room or charge anything").
CLAUSE = re.compile(r"[.;:!?\n()\[\]]+|,\s|\s(?:but|however|although|though|whereas|yet)\s")
NEGATIONS = frozenset({"not", "no", "never", "without", "doesn't", "don't", "won't", "isn't", "aren't", "cannot",
                       "can't", "neither", "nor", "nothing", "none"})
# "book it with create_booking": the action belongs to another tool.
DELEGATION = frozenset({"with", "via", "using", "through", "call", "calling", "use"})
# Using a stored payment instrument is a payment, whatever the verb ("secured with the card on file").
PAYMENT_USE = re.compile(r"\b(?:with|using|uses?|to|from|charges?|charged|charging|bills?|billed|billing)\s+"
                         r"(?:the\s+|your\s+|a\s+|their\s+)?(?:saved\s+|stored\s+|default\s+)?"
                         r"(?:credit\s+card|payment\s+card|payment\s+method|card)s?\s+on[\s-]file\b")
PROPERTY_SIGNALS = {"idempotencykey": "write", "cardnumber": "sensitive-write", "cvc": "sensitive-write",
                    "cvv": "sensitive-write"}
HINTS = (("readOnlyHint", "read_only_hint"), ("destructiveHint", "destructive_hint"),
         ("idempotentHint", "idempotent_hint"), ("openWorldHint", "open_world_hint"))
SEVERITY = {"read": 0, "write": 1, "sensitive-write": 2}
COMBINED = "combines reads and writes: classified as a write (guidelines 3.2)"
# MCPJam tool-shape.ts verb groups: an enumerated operation parameter offering both is a combined tool.
OPERATION_READS = ("get", "list", "read", "search", "query", "fetch", "find")
OPERATION_WRITES = ("create", "update", "delete", "write", "remove", "insert", "upsert", "send", "post", "put",
                    "patch", "execute", "drop")
# Selector parameters whose every enumerated value is an operation of the tool (compared singular).
OPERATION_PARAMETERS = frozenset({"operation", "op", "action", "method", "command", "cmd", "verb", "intent",
                                  "httpmethod", "requestmethod"})
# Selector words that also mark a longer name (operation_type, operationType, action_name, subcommand); not
# method or intent, which qualify data too (payment_method, payment_intent).
SELECTOR_WORDS = frozenset({"operation", "op", "action", "command", "cmd"})
# MCPJam write verbs the name lexicon does not hold, for enumerated operation values only.
OPERATION_VERBS = {"put": "write", "patch": "write", "execute": "write", "run": "write", "drop": "sensitive-write"}
# Write verbs that also name computations or in-memory edits (a calculator's add, a text tool's replace):
# in a read-only tool's selector they count only beside a read operation (RULE_BASIS["operations"]).
GENERIC_WRITES = frozenset({"add", "set", "replace", "move", "return", "mark", "merge", "change", "run"})
# Operations that read stored data: MCPJam's reads plus the lexicon's retrieval verbs, never computations
# such as count, calculate or compare.
READ_OPERATIONS = (*OPERATION_READS, "view", "show", "describe", "retrieve", "browse", "lookup", "inspect")
SCHEMA_DEPTH, SCHEMA_BUDGET = 12, 2000  # Untrusted schemas: bound every walk.


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Classification(Record):
    tool: str = Field(min_length=1)
    tool_class: ToolClass
    basis: Basis
    reasons: list[str]


def stem(word):
    if word in VERBS:
        return word
    for suffix, tail in (("ies", "y"), ("es", ""), ("s", "")):
        if word.endswith(suffix) and word[:-len(suffix)] + tail in VERBS:
            return word[:-len(suffix)] + tail
    return word


def name_signals(name):
    """Class signals of each name segment (search_and_book has two).

    The verb is the segment's first lexicon word (slack_send_message, bookings_create); a word that
    is also a common noun counts first, inside the name or as a read, but not last (address_book).
    With no verb, a trailing read noun suggests a read (booking_status) and a payment noun stays
    sensitive (bank_transfer).
    """
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name))
    tokens = []
    for token in re.split(r"[^a-z0-9]+", spaced.lower()):
        if token and not token.isdigit():
            if tokens and (tokens[-1], token) in NAME_PAIRS:
                tokens[-1] = NAME_PAIRS[tokens[-1], token]
            else:
                tokens.append(token)
    segments = [[]]
    for token in tokens:  # search_and_book, get_or_create: each conjunct is an action
        if token in ("and", "or", "then"):
            segments.append([])
        else:
            segments[-1].append(token)
    found = []
    for segment in filter(None, segments):
        first, last, end, signal = segment[0], segment[-1], len(segment) - 1, None
        verb = next((token for index, token in enumerate(segment) if token in VERBS and (
            index == 0 or index < end or token not in NOUNS or VERBS[token] == "read")), None)
        if first in NOUN_PHRASE_HEADS and end and last in READ_NOUNS:
            signal = "read", f"name noun phrase '{first} {last}'"  # order_status, transfer_history
        elif verb:
            signal = VERBS[verb], f"name verb '{verb}'"
        elif last in READ_NOUNS:
            signal = "read", f"name ends in '{last}'"
        elif money := sorted(MONEY_NOUNS & set(segment)):
            signal = "sensitive-write", f"name names '{money[0]}' without a verb"
        if signal and signal[0] == "write" and MONEY_NOUNS & set(segment):  # create_payment
            signal = "sensitive-write", signal[1] + " on a payment noun"
        if signal:
            found.append((signal[0], f"{signal[1]} ({signal[0]})"))
    return found


def noun_use(base, words, i, copula_after):
    """Whether an ambiguous word at an action position is a noun, not this tool's action.

    Nouns: "Charges apply", "Email address", "Order status", "taxes and charges", "fees and charges
    for the stay", "Charges for late cancellation apply". Verbs, failing closed: "Update status",
    "Share details with", "then posts to", "Book for two adults".
    """
    if base not in NOUNS:
        return False
    following, listed = words[i + 1] if i + 1 < len(words) else None, i > 0 and words[i - 1] in ("and", "or")
    if following in COPULAS or following in COMPOUND_HEADS:
        return True
    if following in READ_NOUNS:
        return base in NOUN_PHRASE_HEADS
    if following is None:
        return listed
    return following in PREPOSITIONS and (listed or copula_after[i + 2])


def text_signals(text, source):
    """Verbs where a tool text states its own action: a clause's first word, or after a connector.

    Precision first: purpose clauses ("to book"), negated clauses ("does not reserve the room or
    charge anything"), modals ("can book"), delegation ("book it with create_booking") and nouns
    ("taxes and charges", "Order status") are not this tool's action. "Return(s)" in prose
    describes output, so it is a read here (return_item names stay writes). Using a card on file
    is a payment whatever the verb.
    """
    found = []
    for clause in CLAUSE.split(text.lower().replace("’", "'")):
        payment = PAYMENT_USE.search(clause)
        if payment and not NEGATIONS & set(re.findall(r"[a-z']+", clause[:payment.start()])):
            found.append(("sensitive-write", f"{source} payment phrase '{payment.group(0)}' (sensitive-write)"))
        words = []
        for word in re.findall(r"[a-z0-9]+(?:[-_'][a-z0-9]+)*", clause):
            word = word.replace("-", "")
            if words and (stem(words[-1]), word) in TEXT_PAIRS:  # "Checks out the cart"
                words[-1] = TEXT_PAIRS[stem(words[-1]), word]
            else:
                words.append(word)
        negated = next((k for k, word in enumerate(words) if word in NEGATIONS), len(words))
        copula_after = [False] * (len(words) + 2)  # Linear in untrusted text: a copula at k or later.
        for k in range(len(words) - 1, -1, -1):
            copula_after[k] = copula_after[k + 1] or words[k] in COPULAS
        delegated = next((k for k in range(len(words) - 1) if words[k] in DELEGATION and "_" in words[k + 1]),
                         len(words))
        for i, word in enumerate(words):
            if i and words[i - 1] not in CONNECTORS or i > negated:
                continue
            base = stem(word)
            tool_class = "read" if base == "return" else VERBS.get(base)
            if tool_class is None or noun_use(base, words, i, copula_after) \
                    or tool_class != "read" and i < delegated < len(words):
                continue
            if tool_class == "write" and MONEY_NOUNS & set(words[i + 1:i + 4]):  # "creates a payment"
                tool_class = "sensitive-write"
            found.append((tool_class, f"{source} verb '{word}' ({tool_class})"))
    return found


def tool_signals(tool, annotations):
    found = name_signals(tool["name"])
    for source, text in (("title", tool.get("title")), ("annotations.title", annotations.get("title")),
                         ("description", tool.get("description"))):
        if isinstance(text, str):
            found += text_signals(text, source)
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    for key in properties if isinstance(properties, dict) else ():
        if tool_class := PROPERTY_SIGNALS.get(re.sub(r"[^a-z0-9]", "", str(key).lower())):
            found.append((tool_class, f"input property '{str(key)[:64]}' ({tool_class})"))  # Untrusted text.
    return list(dict.fromkeys(found))


def strongest(signals):
    return max((row[0] for row in signals), key=SEVERITY.__getitem__)


def infer(tool):
    """(class, basis, reasons) without a declaration; annotations are hints, so signals can override."""
    notes, hints = [], {}
    annotations = tool.get("annotations")
    if annotations is not None and not isinstance(annotations, dict):
        notes.append("annotations ignored: not an object")
    annotations = annotations if isinstance(annotations, dict) else {}
    for name, alias in HINTS:  # Wire names; SDK field names only as a fallback.
        value = annotations.get(name, annotations.get(alias))
        if type(value) is bool:
            hints[name] = value
        elif value is not None:
            notes.append(f"annotations.{name} ignored: not a boolean")
    stated = notes + [f"annotations.{name}={str(value).lower()}" for name, value in hints.items()]
    found = tool_signals(tool, annotations)
    writes = [row for row in found if row[0] != "read"]
    read_only, destructive = hints.get("readOnlyHint"), hints.get("destructiveHint")
    if read_only and destructive:
        return "sensitive-write", "annotation", stated + [
            "annotation-conflict: readOnlyHint and destructiveHint are both true; the stricter class applies"]
    if read_only:
        if writes:  # A read must not hide a send, payment or booking (guidelines 3.2).
            return strongest(writes), "heuristic", stated + [
                f"hidden-write-signal: {reason}" for _, reason in writes] + [COMBINED]
        return "read", "annotation", stated
    if destructive:
        return "sensitive-write", "annotation", stated
    if read_only is False:
        if sensitive := [reason for tool_class, reason in writes if tool_class == "sensitive-write"]:
            return "sensitive-write", "heuristic", stated + sensitive
        return "write", "annotation", stated
    if found:
        tool_class = strongest(found)
        mixed = tool_class != "read" and any(row[0] == "read" for row in found)
        return tool_class, "heuristic", stated + [reason for _, reason in found] + ([COMBINED] if mixed else [])
    return "write", "default", stated + [
        "no declared class, annotation or recognized verb: unknown tools stay write-gated"]


def operation_verb(value: str, verbs) -> bool:
    """MCPJam matchesVerb: "delete", "delete_item", "delete-item" or camelCase "deleteItem"; not "deleted_at"."""
    lowered = value.lower()
    return any(lowered == verb or lowered.startswith((f"{verb}_", f"{verb}-"))
               or lowered.startswith(verb) and value[len(verb):len(verb) + 1].isupper() for verb in verbs)


def _dereferenced(schema, root, seen):
    """`schema` with local $ref chains followed (sibling keywords kept); (None, seen) when a reference is
    remote, missing or recursive."""
    while isinstance(schema, dict) and "$ref" in schema:
        reference = schema["$ref"]
        target = local_reference(root, reference) if reference not in seen else None
        if not isinstance(target, dict):
            return None, seen
        seen = seen | {reference}
        schema = {**target, **{key: value for key, value in schema.items() if key != "$ref"}}
    return (schema if isinstance(schema, dict) else None), seen


def _members(schema):
    """The subschemas a value may also have to match: allOf/anyOf/oneOf members and array items."""
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(schema.get(key), list):
            yield from schema[key]
    items = schema.get("items")
    yield from (items if isinstance(items, list) else [items] if isinstance(items, dict) else [])


def enumerated_values(schema, root, *, seen=frozenset(), depth=0, budget=None) -> list[str]:
    """The strings a property schema enumerates (enum and const), wherever an MCP SDK puts them: through
    local $ref ($defs, a Python Enum), anyOf/oneOf members (Literal | None, zod unions), allOf and array items."""
    budget = [SCHEMA_BUDGET] if budget is None else budget
    schema, seen = _dereferenced(schema, root, seen)
    if schema is None or depth > SCHEMA_DEPTH or budget[0] <= 0:
        return []
    budget[0] -= 1
    found = [value for value in schema.get("enum") if isinstance(value, str)] \
        if isinstance(schema.get("enum"), list) else []
    if isinstance(schema.get("const"), str):
        found.append(schema["const"])
    for member in _members(schema):
        found += enumerated_values(member, root, seen=seen, depth=depth + 1, budget=budget)
    return list(dict.fromkeys(found))


def schema_properties(schema, root=None, *, path="", seen=frozenset(), depth=0, budget=None):
    """(path, name, schema) for each property an input can carry: top level, in allOf/anyOf/oneOf branches
    (a discriminated union), in nested objects and array items ("request.action", "ops[].op"), through
    local $ref; bounded, because the schema is the server's."""
    root = schema if root is None else root
    budget = [SCHEMA_BUDGET] if budget is None else budget
    schema, seen = _dereferenced(schema, root, seen)
    if schema is None or depth > SCHEMA_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1
    properties = schema.get("properties")
    for name, item in properties.items() if isinstance(properties, dict) else ():
        where = f"{path}.{name}" if path else str(name)
        yield where, str(name), item
        yield from schema_properties(item, root, path=where, seen=seen, depth=depth + 1, budget=budget)
    for key in ("allOf", "anyOf", "oneOf"):
        for member in schema.get(key) if isinstance(schema.get(key), list) else ():
            yield from schema_properties(member, root, path=path, seen=seen, depth=depth + 1, budget=budget)
    items = schema.get("items")
    if isinstance(items, dict):
        yield from schema_properties(items, root, path=f"{path}[]", seen=seen, depth=depth + 1, budget=budget)


def operation_parameters(tool: dict) -> dict[str, tuple[str, list[str]]]:
    """path -> (property name, enumerated strings) for every property of the tool's input that enumerates
    values; a property in several branches of a union merges their values."""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    found = {}
    budget = [SCHEMA_BUDGET]
    for path, name, item in schema_properties(schema, budget=budget):
        if values := enumerated_values(item, schema, budget=budget):
            merged = found.get(path, (name, []))[1] + values
            found[path] = (name, list(dict.fromkeys(merged))[:200])
    return found


def selector_name(name: str) -> bool:
    """A parameter named like an operation selector: operation, op, action, method, command, verb or intent
    (plurals too), or a name with an operation, op, action or command word (operation_type, operationType,
    action_name, subcommand)."""
    words = [word for word in normalized_key(name).split("_") if word]
    words = [word[:-1] if word.endswith("s") and (word[:-1] in OPERATION_PARAMETERS or word[:-1] in SELECTOR_WORDS)
             else word for word in words]
    return "".join(words) in OPERATION_PARAMETERS or any(
        word in SELECTOR_WORDS or word.startswith("sub") and word[3:] in SELECTOR_WORDS for word in words)


def combined_operations(tool: dict) -> dict | None:
    """An enumerated operation parameter offering both a read and a write verb (MCPJam tool-shape.ts):
    such a tool combines reads and writes, which guidelines 3.2 classifies as a write. Values are read
    however the schema enumerates them (enumerated_values)."""
    for parameter, (_, values) in operation_parameters(tool).items():
        reads = [value for value in values if operation_verb(value, OPERATION_READS)]
        writes = [value for value in values if operation_verb(value, OPERATION_WRITES)]
        if reads and writes:
            return {"parameter": parameter[:64], "reads": reads, "writes": writes}
    return None


def operation_value_class(value: str) -> ToolClass | None:
    """The class of one enumerated operation value ("list", "send", "delete_room", "createItem"); None
    when it names no action ("deleted_at", "budget")."""
    if signals := name_signals(value):
        return strongest(signals)
    words = re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).lower())
    return OPERATION_VERBS.get(next((word for word in words if word), ""))


def generic_write(value: str) -> bool:
    """An operation value whose verbs all also name computations ("add", "replace", "set_union"), not
    "add_or_update"."""
    words = re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).lower())
    verbs = [word for word in words if word in VERBS or word in OPERATION_VERBS]
    return bool(verbs) and all(verb in GENERIC_WRITES for verb in verbs)


def read_annotated(tool: dict) -> bool:
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    return annotations.get("readOnlyHint", annotations.get("read_only_hint")) is True


def operation_signals(tool: dict) -> dict | None:
    """The operations a schema enumerates, classed: every value of a selector named like an operation,
    or of any enum offering both a read and a write verb (MCPJam tool-shape.ts). The strongest value
    class is the tool's (RULE_BASIS["operations"]): a send or delete value is a sensitive write. In a
    read-annotated tool, a write word that also names a computation (a calculator's add) counts only
    beside a read operation."""
    found, read_only = None, read_annotated(tool)
    for parameter, (name, values) in operation_parameters(tool).items():
        combined = any(operation_verb(value, OPERATION_READS) for value in values) \
            and any(operation_verb(value, OPERATION_WRITES) for value in values)
        if not (selector_name(name) or combined):
            continue
        reads = any(operation_verb(value, READ_OPERATIONS) for value in values)
        classes = {value: tool_class for value in values if (tool_class := operation_value_class(value))
                   and not (tool_class == "write" and read_only and not reads and generic_write(value)
                            and not operation_verb(value, OPERATION_WRITES))}
        if not classes:
            continue
        tool_class = max(classes.values(), key=SEVERITY.__getitem__)
        if found is None or SEVERITY[tool_class] > SEVERITY[found["class"]]:
            found = {"parameter": parameter[:64], "class": tool_class, "combined": combined,
                     "values": [value[:64] for value, cls in classes.items() if cls == tool_class][:10]}
    return found


def classify_tool(tool: dict, declared: ToolClass | None = None, *,
                  important_read: bool = False) -> Classification:
    """Classify one tools/list entry; a declared class (Meta's review stand-in) wins."""
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
        raise LabError("A tools/list entry needs a nonempty string name")
    if declared is not None and declared not in CLASSES:
        raise LabError(f"Declared class for {tool['name']} must be one of {', '.join(CLASSES)}")
    tool_class, basis, reasons = infer(tool)
    operations = operation_signals(tool)
    if operations and SEVERITY[operations["class"]] > SEVERITY[tool_class]:
        # Whatever the annotations say: the schema offers these actions (guidelines 3.2). The sheet uses
        # this classifier too, so the runtime class matches the sheet.
        tool_class, basis = operations["class"], "heuristic"
        reasons = [*reasons, (f"operations: '{operations['parameter']}' offers "
                              f"{', '.join(map(repr, operations['values']))} ({operations['class']})")]
        if operations["combined"]:
            reasons.append(f"combined-operations: '{operations['parameter']}' offers reads and writes; "
                           + COMBINED)
    if declared is not None:
        overridden = [] if declared == tool_class else [f"overrides {basis} class {tool_class}"]
        tool_class, basis, reasons = declared, "declared", [f"declared {declared}", *overridden, *reasons]
    if important_read:
        reasons.append("important read: asks under Ask for some actions (Help Center)" if tool_class == "read"
                       else "important-read flag has no effect: writes always ask")
    return Classification(tool=tool["name"], tool_class=tool_class, basis=basis,
                          reasons=list(dict.fromkeys(reasons)))


def parse_pointer(pointer):
    """RFC 6901 tokens; the root ("") is never a valid target here."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise LabError(f"JSON pointers must start with '/': {pointer!r}")
    tokens = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", token) for token in tokens):
        raise LabError(f"Invalid JSON pointer escape in {pointer!r}")
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def resolve(document, tokens):
    value = document
    for token in tokens:
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token) and int(token) < len(value):
            value = value[int(token)]
        else:
            return False, None
    return True, value


def related(left, right):
    """Equal, ancestor or descendant pointers."""
    size = min(len(left), len(right))
    return left[:size] == right[:size]


def masked(arguments, private):
    result = copy.deepcopy(arguments)
    for tokens in private:
        found, parent = resolve(result, tokens[:-1])
        if found and isinstance(parent, dict) and tokens[-1] in parent:
            parent[tokens[-1]] = PRIVATE
        elif found and isinstance(parent, list) and resolve(parent, tokens[-1:])[0]:
            parent[int(tokens[-1])] = PRIVATE
    return result


def apply_edits(arguments, edits, private, tool):
    """JSON-pointer replacements on model-visible arguments; host-private values are untouchable."""
    result = copy.deepcopy(arguments)
    for path, value in edits.items():
        tokens = parse_pointer(path)
        if any(related(tokens, hidden) for hidden in private):
            raise LabError(f"Scripted edit {path} for {tool} touches a host-private argument; edits apply to "
                           "model-visible arguments only")
        found, parent = resolve(result, tokens[:-1])
        if found and isinstance(parent, dict):
            parent[tokens[-1]] = copy.deepcopy(value)
        elif found and isinstance(parent, list) and resolve(parent, tokens[-1:])[0]:
            parent[int(tokens[-1])] = copy.deepcopy(value)
        else:
            raise LabError(f"Scripted edit {path} does not resolve in the arguments for {tool}; guard the rule "
                           "with an exists predicate")
    return result


def check_edits(edits):
    if not isinstance(edits, dict) or not edits:
        raise ValueError("decision edit needs a nonempty edits object of JSON pointer -> value")
    paths = []
    for path, value in edits.items():
        try:
            paths.append(parse_pointer(path))
            canonical(value)
        except LabError as error:
            raise ValueError(f"edit {path}: {error}") from None
    if any(related(a, b) for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("edits must not overlap (one pointer inside another)")


def json_type(value):
    if value is None:
        return "null"
    for name, kind in (("boolean", bool), ("number", (int, float)), ("string", str), ("array", list),
                       ("object", dict)):
        if isinstance(value, kind):
            return name
    return None


CARD_FIELDS = ("tool", "tool_class", "connector", "origin")
ORDERED = ("lt", "lte", "gt", "gte")
OPERATORS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "lte": operator.le, "gt": operator.gt,
             "gte": operator.ge}


def predicate_field(field):
    """(card attribute, pointer tokens or None) for tool, tool_class, connector, origin, arguments/...,
    disclosure/..."""
    if field in CARD_FIELDS:
        return field, None
    for root in ("arguments", "disclosure"):
        if isinstance(field, str) and field.startswith(root + "/"):
            return root, parse_pointer(field[len(root):])
    raise LabError(f"Unknown predicate field {field!r}; use tool, tool_class, connector, origin, arguments/<pointer> "
                   "or disclosure/<key>")


def validate_predicate(predicate: dict, depth: int = 1):
    """The strict grammar of scenario.validate_predicate, over approval-card fields."""
    if depth > 8:
        raise LabError("Predicate nesting exceeds eight levels")
    if not isinstance(predicate, dict):
        raise LabError("Predicate must be an object")
    if set(predicate) in ({"all"}, {"any"}):
        children = next(iter(predicate.values()))
        if not isinstance(children, list) or not children:
            raise LabError("Composite predicates require a nonempty array")
        for child in children:
            validate_predicate(child, depth + 1)
        return
    if set(predicate) != {"field", "op", "value"}:
        raise LabError("Predicate requires exactly field, op, value or all/any")
    field, op, value = predicate["field"], predicate["op"], predicate["value"]
    predicate_field(field)
    if op not in ("eq", "ne", *ORDERED, "in", "exists"):
        raise LabError(f"Unknown predicate operator {op!r}")
    if op == "exists":
        if type(value) is not bool:
            raise LabError("'exists' takes true or false")
        return
    values = value if op == "in" else [value]
    if not isinstance(values, list) or not values:
        raise LabError("'in' requires a nonempty array")
    types = {json_type(item) for item in values}
    if not types <= {"string", "number", "boolean", "null"} or len(types) != 1:
        raise LabError(f"Predicate values for {field} must be JSON scalars of one type")
    if any(isinstance(item, float) and not math.isfinite(item) for item in values):
        raise LabError("Predicate numbers must be finite")
    if field in CARD_FIELDS and types != {"string"}:
        raise LabError(f"Predicate values for {field} must be strings")
    if field == "tool_class" and any(item not in CLASSES for item in values):
        raise LabError(f"Unknown tool class; use {', '.join(CLASSES)}")
    if field == "origin" and any(not ORIGIN.fullmatch(item) for item in values):
        raise LabError("An origin is model or extension:<source> (extension:wallet, extension:scheduler:<job>)")
    if op in ORDERED and types != {"number"}:
        raise LabError("Ordered comparisons accept numbers only")


def evaluate(predicate: dict, card: "ApprovalCard") -> bool | None:
    """Type-strict: a missing field or a different JSON type never matches (bool is not a number). A
    disclosure term the card holds as null was configured but not shown (RULE_BASIS["withheld-terms"]):
    exists is false for it and a comparison is undecided (None), with Kleene logic through all/any."""
    if "all" in predicate:
        results = [evaluate(child, card) for child in predicate["all"]]
        return False if False in results else None if None in results else True
    if "any" in predicate:
        results = [evaluate(child, card) for child in predicate["any"]]
        return True if True in results else None if None in results else False
    root, tokens = predicate_field(predicate["field"])
    found, actual = (True, getattr(card, root)) if tokens is None else resolve(getattr(card, root), tokens)
    withheld = root == "disclosure" and bool(tokens) and tokens[0] in card.disclosure \
        and card.disclosure[tokens[0]] is None
    op, expected = predicate["op"], predicate["value"]
    if op == "exists":
        return (found and not withheld) is expected
    if withheld:
        return None
    values = expected if op == "in" else [expected]
    if not found or json_type(actual) != json_type(values[0]):
        return False
    return actual in values if op == "in" else OPERATORS[op](actual, expected)


def matches(predicate: dict, card: "ApprovalCard") -> bool:
    """evaluate(), with an undecided comparison as no match."""
    return evaluate(predicate, card) is True


def binding_sha256(connector: str, tool: str, arguments: dict, disclosure: dict, *, key: bytes | None = None) -> str:
    """What one approval covers (guidelines 3.4), in consent.canonical's RFC 8785 form.

    With a key (every engine holds a random one), an HMAC-SHA256: bindings published in cards and
    state compare calls within the trial, but evidence cannot recompute them, so they test no
    guess of a private or redacted value. Without a key, the plain consent.digest."""
    value = {"connector": connector, "tool": tool, "arguments": arguments, "disclosure": disclosure}
    if key is None:
        return digest(value)
    return hmac.new(key, canonical(value).encode("utf-8"), hashlib.sha256).hexdigest()


def card_record(card: "ApprovalCard") -> dict:
    """A card as evidence records it: its origin only when an extension asked (a model's card reads as before),
    its site only on a website card."""
    record = card.model_dump(mode="json")
    if record.get("origin") == "model":
        del record["origin"]
    if record.get("site") is None:
        record.pop("site", None)
    return record


def missing_disclosure(card: "ApprovalCard", required=DISCLOSURE_FIELDS) -> list[str]:
    """Required disclosure keys the card does not show (absent, null or empty), for a 4.7 check."""
    return [key for key in required if card.disclosure.get(key) in (None, "", [], {})]


class ApprovalCard(Record):
    card_id: str = Field(pattern=r"^card-[1-9][0-9]*$")
    connector: str
    tool: str
    tool_class: ToolClass
    arguments: dict
    disclosure: dict
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # keyed per engine (binding_sha256 with key)
    offered: list[DecisionKind]
    offered_basis: dict[str, str] = Field(default_factory=dict)  # CHOICE_SOURCE for each offered button
    origin: str = Field(default="model", pattern=f"^(?:{ORIGIN.pattern})$")  # RULE_BASIS["origin"]
    site: str | None = Field(default=None, pattern=r"^https?://[^/?#\s]+$")  # a website card's site (an origin)

    @model_validator(mode="after")
    def buttons(self):
        if not {"allow_once", "deny"} <= set(self.offered) <= set(SITE_CHOICES if self.site else CHOICES):
            raise ValueError("A card offers Allow once and Deny, and only documented grant choices (Allow for this "
                             "site on a website card that names its site)")
        if self.tool_class == "sensitive-write" and set(self.offered) != set(ONCE_ONLY):
            raise ValueError("Sensitive writes offer only Allow once and Deny (guidelines 3.3)")
        if not set(self.offered_basis) <= set(self.offered):
            raise ValueError("offered_basis names only offered choices")
        return self


class Decision(Record):
    kind: DecisionKind
    edits: dict | None = None
    rule: str | None = None
    coerced_from: DecisionKind | None = None  # The scripted kind this card did not offer.
    # A rule that compares a term this card does not show: the default answered (withheld-terms).
    undecided_rule: str | None = None

    @model_validator(mode="after")
    def shape(self):
        if (self.kind == "edit") != (self.edits is not None):
            raise ValueError("edits accompany exactly the edit decision")
        if self.edits is not None:
            check_edits(self.edits)
        if self.coerced_from is not None and (self.kind != "allow_once" or self.coerced_from not in (
                "allow_task", "allow_site", "always_allow")):
            raise ValueError("Only a task, site or Always allow answer is coerced, and only to allow_once")
        if self.undecided_rule is not None and (self.rule is not None or self.kind not in ("deny", "no_response")):
            raise ValueError("An undecided rule leaves the policy default (deny or no response) to answer")
        return self


class DecisionRule(Strict):
    id: str = Field(pattern=ID)
    when: dict
    decision: DecisionKind
    edits: dict | None = None

    @field_validator("decision", mode="before")
    @classmethod
    def site_scope(cls, value):
        if isinstance(value, str) and value in SITE_KINDS:
            raise ValueError("Allow for this site is a website scope (Muse Help Center); connector cards "
                             "never offer it. Use allow_once, allow_task, always_allow, deny, edit or no_response, "
                             "or allow_site in a rule that matches browser task cards only")
        return value

    @field_validator("when")
    @classmethod
    def predicate(cls, value):
        try:
            validate_predicate(value)
        except LabError as error:
            raise ValueError(str(error)) from None
        return value

    @model_validator(mode="after")
    def shape(self):
        if self.decision == "edit":
            check_edits(self.edits)
        elif self.edits is not None:
            raise ValueError("edits are only valid with decision edit")
        if self.decision == "allow_site" and not website_only(self.when):
            # Connector cards keep refusing the website scope (RULE_BASIS["allow-site"]).
            raise ValueError(f"Allow for this site is a website scope (Muse Help Center); connector cards never "
                             f"offer it. A rule answering allow_site matches browser task cards only: add "
                             f"{{field: connector, op: eq, value: {WEBSITE}}} to its when")
        return self


def website_only(predicate) -> bool:
    """Whether a rule's predicate matches only website cards (connector browser): a connector eq/in browser leaf,
    an all with such a child, or an any whose every child is website-only."""
    if not isinstance(predicate, dict):
        return False
    if "all" in predicate:
        return any(website_only(child) for child in predicate["all"])
    if "any" in predicate:
        return bool(predicate["any"]) and all(website_only(child) for child in predicate["any"])
    value = predicate.get("value")
    return predicate.get("field") == "connector" and (
        predicate.get("op") == "eq" and value == WEBSITE or predicate.get("op") == "in" and value == [WEBSITE])


class PermissionPolicy(Strict):
    mode: Literal["platform-default", "ask-for-some", "always-ask"] = "platform-default"
    important_reads: list[str] = Field(default_factory=list)  # Still ask under ask-for-some.
    rules: list[DecisionRule] = Field(default_factory=list)  # The scripted user; first match wins.
    default: Literal["deny", "no_response"] = "deny"
    persisted_always: list[str] = Field(default_factory=list)  # Saved Always allow (never sensitive).

    @model_validator(mode="after")
    def unique(self):
        for name, items in (("rule ids", [rule.id for rule in self.rules]),
                            ("important_reads", self.important_reads), ("persisted_always", self.persisted_always)):
            if len(set(items)) != len(items) or not all(items):
                raise ValueError(f"{name} must be unique and nonempty")
        return self


class Ticket(Record):
    allowed: bool
    card: ApprovalCard | None = None  # None when no prompt was needed.
    decision: Decision | None = None
    scope: GrantScope | None = None  # The grant that allowed it, existing or new.
    arguments: dict  # Effective arguments to dispatch (after edits); host-private values included.
    model_message: str | None = None  # All the model learns about a refused call.

    @model_validator(mode="after")
    def consistent(self):
        if self.allowed == (self.model_message is not None) or not self.allowed and self.scope is not None:
            raise ValueError("Exactly the refused calls carry a model message, and they carry no grant scope")
        if (self.card is None) != (self.decision is None) or self.decision and self.allowed == (
                self.decision.kind in ("deny", "no_response")):
            raise ValueError("A card comes with its decision, and the decision decides whether the call is allowed")
        return self


class PermissionEngine:
    """One connector's approvals for one task (a trial). Calls are sequential. A website engine (sites=True: the
    browser's task approvals) also offers Allow for this site on cards for a request's site (RULE_BASIS
    ["allow-site"]); a connector engine never does."""

    def __init__(self, policy: PermissionPolicy, classifications: dict[str, Classification], connector: str, *,
                 sites: bool = False, emulation_target: str = "muse"):
        target_profile(emulation_target)
        self.emulation_target = emulation_target
        self.class_bases = behavior_bases(emulation_target, "permission classes", CLASS_BASIS)
        self.rule_bases = behavior_bases(emulation_target, "permissions", RULE_BASIS)
        self.choice_source = dict(CHOICE_SOURCE) if emulation_target == "muse" else {
            kind: "CATT's assumed grant choice for Instinct; the product's approval UI is not published"
            for kind in CHOICE_SOURCE
        }
        self.policy = policy if isinstance(policy, PermissionPolicy) else PermissionPolicy.model_validate(policy)
        if not isinstance(connector, str) or not connector:
            raise LabError("The permission engine needs a connector name")
        self.connector, self.sites = connector, sites is True
        self.classifications = {}
        for name, item in classifications.items():
            item = item if isinstance(item, Classification) else Classification.model_validate(item)
            if item.tool != name:
                raise LabError(f"Classification for {name} names a different tool, {item.tool}")
            self.classifications[name] = item
        for field in ("important_reads", "persisted_always"):
            # A typo would silently change who is asked; fail at setup instead.
            if unknown := [name for name in getattr(self.policy, field) if name not in self.classifications]:
                raise LabError(f"permissions.{field} names {unknown[0]}, which is not a classified tool of "
                               f"connector {connector}")
        for name in self.policy.persisted_always:
            item = self.classifications[name]
            if item.tool_class == "sensitive-write":
                raise LabError(f"permissions.persisted_always lists {name}, a sensitive write ({item.basis}: "
                               f"{'; '.join(item.reasons)}). Sensitive writes cannot be set to Always allow, "
                               + ("including through Settings or a saved permission (Muse connector guidelines 3.3)"
                                  if emulation_target == "muse" else
                                  "including through a saved permission (CATT's conservative Instinct treatment)"))
        self._grants = [{"id": f"grant-{index}", "scope": "always", "tool": name, "source": "persisted",
                         "card_id": None, "binding_sha256": None, "status": "active"}
                        for index, name in enumerate(self.policy.persisted_always, 1)]
        self._cards, self._decisions, self._requests = [], [], []
        # Never recorded: published bindings can then not be recomputed to test guesses of private values.
        self._key = secrets.token_bytes(32)

    def binding(self, tool, arguments, disclosure):
        """This engine's keyed binding of one call (what cards, grants and requests record)."""
        try:
            check_finite(arguments)  # Bounded depth before anything recurses through model output.
            canonical(arguments)
        except LabError as error:
            raise LabError(f"Arguments for {tool} cannot be bound to an approval: {error}",
                           code=1, category="interaction") from None
        try:
            check_finite(disclosure)
            canonical(disclosure)
        except LabError as error:
            raise LabError(f"Approval disclosure for {tool} is not canonical JSON: {error}",
                           code=1, category="tool/API") from None
        return binding_sha256(self.connector, tool, arguments, disclosure, key=self._key)

    def prompt_needed(self, tool, tool_class, site=None):
        """(offered choices or None when nobody is asked, the allowing grant, the RULE_BASIS key). A site grant
        covers only requests for its own site."""
        if tool_class == "sensitive-write":
            return list(ONCE_ONLY), None, "sensitive-every-use"
        if self.policy.mode == "always-ask":
            return list(ONCE_ONLY), None, "always-ask"
        if self.policy.mode == "ask-for-some" and tool_class == "read" and tool not in self.policy.important_reads:
            return None, None, "ask-for-some"
        active = [grant for grant in self._grants if grant["tool"] == tool and grant["status"] == "active"
                  and (grant["scope"] != "site" or site is not None and grant.get("site") == site)]
        if grant := next((grant for grant in active if grant["scope"] == "always"), active[0] if active else None):
            return None, grant, {"always": "always-allow", "site": "allow-site"}.get(grant["scope"], "allow-task")
        return list(SITE_CHOICES if self.sites and site else CHOICES), None, self.policy.mode

    def scripted(self, card):
        """(kind, edits, deciding rule, undecided rule): the first matching rule wins; a rule that
        compares a term the card does not show stops the list, and the default answers."""
        for rule in self.policy.rules:
            verdict = evaluate(rule.when, card)
            if verdict is None:
                return self.policy.default, None, None, rule.id
            if verdict:
                return rule.decision, rule.edits, rule.id, None
        return self.policy.default, None, None, None

    def add_classification(self, name: str, classification) -> Classification:
        """Classify a tool first seen mid-task (an operation an extension discovers). A tool's class never
        changes within a task: the same classification again is a no-op, another one is refused."""
        item = classification if isinstance(classification, Classification) else Classification.model_validate(
            classification)
        if item.tool != name:
            raise LabError(f"Classification for {name} names a different tool, {item.tool}")
        current = self.classifications.get(name)
        if current is not None and current != item:
            raise LabError(f"{name} is already classified {current.tool_class} ({current.basis}); a class never "
                           "changes within a task")
        self.classifications[name] = item
        return item

    def request(self, tool: str, arguments: dict, *, disclosure: dict | None = None,
                private_paths: set[str] = frozenset(), origin: str = "model", site: str | None = None) -> Ticket:
        """Decide one call. `arguments` are the effective arguments (host overlays applied) and
        `private_paths` the JSON pointers of host-private values: bound by value, shown as [private],
        never editable. `origin` is who asked: the model, or extension:<source> for a call an extension
        makes for the user; its card says so and rules may match it. `site` (website engines only) is the
        origin of the website the action is for: its card names it and may offer Allow for this site.
        Allowed tickets carry the arguments to dispatch; refused ones only a message."""
        if not isinstance(origin, str) or not ORIGIN.fullmatch(origin):
            raise LabError(f"A request's origin is model or extension:<source>, not {str(origin)[:64]!r}")
        if site is not None and (not self.sites or not isinstance(site, str)
                                 or not re.fullmatch(r"https?://[^/?#\s]+", site)):
            raise LabError("Only a website engine takes a site, and a site is an origin (scheme, host and port)")
        classification = self.classifications.get(tool)
        if classification is None:
            raise LabError(f"No classification for tool {tool!r}; classify every allowed tool before requests")
        if not isinstance(arguments, dict):
            raise LabError(f"Arguments for {tool} must be a JSON object", code=1, category="interaction")
        disclosure = {} if disclosure is None else disclosure
        if not isinstance(disclosure, dict):
            raise LabError(f"Approval disclosure for {tool} must be an object")
        private = [parse_pointer(path) for path in sorted(private_paths)]
        binding, tool_class = self.binding(tool, arguments, disclosure), classification.tool_class
        arguments, disclosure = copy.deepcopy(arguments), copy.deepcopy(disclosure)
        record = {"request": len(self._requests) + 1, "tool": tool, "tool_class": tool_class,
                  "binding_sha256": binding, "private_paths": sorted(private_paths),
                  **({"site": site} if self.sites else {})}
        offered, grant, basis = self.prompt_needed(tool, tool_class, site)
        record["basis"] = basis
        if offered is None:
            scope = grant["scope"] if grant else None
            self._requests.append({**record, "via": "grant" if grant else "mode",
                                   "grant_id": grant["id"] if grant else None, "scope": scope, "card_id": None,
                                   "allowed": True, "dispatched_sha256": binding, "repeat_of": None})
            return Ticket(allowed=True, scope=scope, arguments=arguments)
        card = ApprovalCard(card_id=f"card-{len(self._cards) + 1}", connector=self.connector, tool=tool,
                            tool_class=tool_class, arguments=masked(arguments, private), disclosure=disclosure,
                            binding_sha256=binding, offered=offered,
                            offered_basis={kind: self.choice_source[kind] for kind in offered}, origin=origin,
                            site=site)
        # Details the user already decided in this task: approval burden the agent caused.
        repeat_of = next((item.card_id for item in self._cards if item.binding_sha256 == binding), None)
        requested, edits, rule, undecided = self.scripted(card)
        kind, note, decided = requested, None, KIND_BASIS[requested]
        if undecided is not None:
            withheld = sorted(key for key, value in card.disclosure.items() if value is None)
            note = (f"undecided: rule {undecided} compares a term this card does not show "
                    f"({', '.join(withheld)}); the default ({requested}) answers (withheld-terms)")
            decided = "withheld-terms"
        if requested in ("allow_task", "allow_site", "always_allow") and requested not in offered:
            kind, decided = "allow_once", basis
            note = "scope not offered: " + (("sensitive writes offer only Allow once and Deny (guidelines 3.3)"
                                            if self.emulation_target == "muse" else
                                            "sensitive writes offer only Allow once and Deny (CATT assumption)")
                                            if basis == "sensitive-every-use"
                                            else "Always ask honors no task or Always allow grant"
                                            if basis == "always-ask" and requested != "allow_site"
                                            else "Always ask honors no site grant either" if basis == "always-ask"
                                            else "Allow for this site is a website scope; this card names no site")
        dispatched = apply_edits(arguments, edits, private, tool) if kind == "edit" else arguments
        allowed, scope = kind not in ("deny", "no_response"), SCOPES.get(kind)
        dispatched_sha256 = None if not allowed else binding if dispatched is arguments else self.binding(
            tool, dispatched, disclosure)
        grant_id = None
        if allowed:
            grant_id = f"grant-{len(self._grants) + 1}"
            self._grants.append({"id": grant_id, "scope": scope, "tool": tool, "source": "decision",
                                 "card_id": card.card_id, "binding_sha256": dispatched_sha256,
                                 "status": "consumed" if scope == "once" else "active",
                                 **({"site": site} if scope == "site" else {})})
        self._cards.append(card)
        self._decisions.append({
            "card_id": card.card_id, "kind": kind, "requested": requested, "rule": rule, "undecided_rule": undecided,
            "edits": copy.deepcopy(edits) if kind == "edit" else None, "note": note, "basis": decided,
            "choice_basis": self.choice_source.get(kind, "assumed user behavior, not a card button"),
            "allowed": allowed, "scope": scope, "dispatched_sha256": dispatched_sha256,
            "dispatched_arguments": masked(dispatched, private) if kind == "edit" else None,
            **({"site": site} if self.sites else {})})
        self._requests.append({**record, "via": "prompt", "grant_id": grant_id, "scope": scope,
                               "card_id": card.card_id, "allowed": allowed, "dispatched_sha256": dispatched_sha256,
                               "repeat_of": repeat_of})
        decision = Decision(kind=kind, edits=copy.deepcopy(edits) if kind == "edit" else None, rule=rule,
                            coerced_from=requested if requested != kind else None, undecided_rule=undecided)
        message = DECLINED if kind == "deny" else UNANSWERED if kind == "no_response" else None
        return Ticket(allowed=allowed, card=card.model_copy(deep=True), decision=decision, scope=scope,
                      arguments=copy.deepcopy(dispatched), model_message=message)

    @property
    def cards(self) -> list[ApprovalCard]:
        """The cards shown so far, in order (copies; private argument values masked)."""
        return [card.model_copy(deep=True) for card in self._cards]

    def state(self) -> dict:
        """Grants, cards, decisions and requests; JSON-serializable, private argument values masked."""
        return {"connector": self.connector, "policy": self.policy.model_dump(mode="json"),
                "honors_grants": self.policy.mode != "always-ask", **({"sites": True} if self.sites else {}),
                "classifications": {name: item.model_dump(mode="json")
                                    for name, item in sorted(self.classifications.items())},
                "binding": {"canonicalizer": CANONICALIZER, "fields": ["connector", "tool", "arguments",
                                                                        "disclosure"],
                            "digest": "HMAC-SHA256 under a random key this engine holds in memory only: "
                                      "bindings compare calls within this trial and cannot be recomputed "
                                      "from evidence",
                            "private_values": f"bound by value, shown as {PRIVATE}"},
                "grants": copy.deepcopy(self._grants), "cards": [card_record(card) for card in self._cards],
                "decisions": copy.deepcopy(self._decisions), "requests": copy.deepcopy(self._requests),
                "basis": {"classes": copy.deepcopy(self.class_bases), "rules": copy.deepcopy(self.rule_bases),
                          "choices": dict(self.choice_source)}}

    def metrics(self) -> dict:
        """Approval burden: cards per class, final decisions per kind, calls allowed without a card
        per class, cards repeating details already decided in this task (redundant_prompts) and
        scripted scopes the card did not offer (coerced_scopes)."""
        prompts, auto = dict.fromkeys(CLASSES, 0), dict.fromkeys(CLASSES, 0)
        decisions = dict.fromkeys(KINDS, 0)
        for card in self._cards:
            prompts[card.tool_class] += 1
        for row in self._decisions:
            decisions[row["kind"]] += 1
        for row in self._requests:
            if row["via"] != "prompt":
                auto[row["tool_class"]] += 1
        return {"prompts": prompts, "decisions": decisions, "auto_allowed": auto,
                "redundant_prompts": sum(row["repeat_of"] is not None for row in self._requests),
                "coerced_scopes": sum(row["requested"] != row["kind"] for row in self._decisions)}
