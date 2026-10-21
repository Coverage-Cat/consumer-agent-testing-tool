"""Presentation profiles on the connector lane (P2.8, extension `presentation`): the user's channel and what
Muse's own widgets need from a connector's results.

Muse renders its own cards and widgets from what its agent has, and documents no third-party UI: the
projection already replaces MCP Apps UI resources with a note (counted here). This extension:
- names the channel (app, whatsapp or voice) in the standing context (announce) and checks the final answer
  against it. WhatsApp gets plain text: no markdown tables, HTML, headings or markdown links and images
  (links as bare URLs), within max_characters (default 4096): presentation.whatsapp_format and
  presentation.length. Voice gets a short reply (default 500 characters) without URLs, identifiers from
  the results the model received (unless speakable_ids names their fields), machine identifiers or
  markdown: presentation.length and presentation.voice_speakable. The app channel has no format checks;
  how the app renders replies is not public. enforce: false reports a finding as a non-required
  observation gap instead of a failure.
- reports, as evidence, the fields a list widget shows in every list result the model received (title,
  price with currency, status, ID and link), found by field name: metrics.extensions.presentation.widgets
  and, when a configured field is missing, a non-required presentation.widget_fields observation gap.

Limits: widget fields are read from connector results only (on_tool_result); other extensions' tool output
(a custom connection's HTTP response, a browser task's review) is not reviewed for widgets.
"""
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from .connector_run import BaseExtension, register
from .errors import LabError
from .evidence import normalized_key
from .projection import APP
from .serialization import loads

LAUNCH_POST = "https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/"
BASES = {
    "text": {
        "basis": "assumed",
        "detail": "Text messages use plain text and bare URLs. CATT checks for table, HTML, heading, "
                  "Markdown link and image syntax; it imposes no default character limit."},
    "channels": {
        "basis": "documented", "source": LAUNCH_POST, "checked_at": "2026-10-15",
        "quote": "talking to it works just like messaging another person, in the Muse app or directly in WhatsApp.",
        "detail": "The app and WhatsApp channels are documented; how either renders a reply is not public."},
    "whatsapp-text": {
        "basis": "assumed",
        "detail": "A WhatsApp reply is plain text with WhatsApp's own light formatting (bold, italic, strikethrough, "
                  "monospace, lists and quotes); markdown tables, HTML, headings and markdown links or images show "
                  "as raw markup, and a link works as a bare URL. The 4096-character default follows the WhatsApp "
                  "Business Platform's limit for a text message body, not a documented Muse limit."},
    "voice": {
        "basis": "assumed", "source": LAUNCH_POST, "checked_at": "2026-10-15", "quote": "coming soon to AI glasses",
        "detail": "No Muse voice channel is documented. A spoken reply is assumed to be short (500 characters by "
                  "default, about half a minute), with no URLs, no identifiers from results unless speakable_ids "
                  "names their fields, no machine identifiers and no markdown."},
    "widgets": {
        "basis": "unofficial",
        "source": "unlicensed mirrors, cited and not copied: RomanSlack/Meta_Muse_Agent_System_Prompt_Tools "
                  "system-prompt-master.md:754 ('widget — Transient in-chat widget UI.'); sneakers-the-rat/muse-skills "
                  "workflow-skills/place-discovery/references/map-presentation.md:5 (widget.create_map_widget); "
                  "widget.create_list_widget in an hourly mirror (docs/muse-emulation-review-2026-10-15.md, section 2.2)",
        "checked_at": "2026-10-15",
        "detail": "Muse renders its own widgets from what its agent has. The fields a list widget needs (title, price "
                  "with currency, status, ID and link) and finding them by field name are assumed."},
    "channel-context": {
        "basis": "assumed",
        "detail": "The standing context names the user's channel (announce: true); whether and how Muse tells its "
                  "model is not public."},
}
ROLES = ("title", "price", "status", "id", "link")
LIMITS = {"whatsapp": 4096, "voice": 500}
ANNOUNCEMENTS = {"whatsapp": "Your user is messaging you in WhatsApp; your replies reach them as WhatsApp messages.",
                 "voice": "Your user is talking to you by voice; your replies are read aloud to them.",
                 "text": "Your user receives your replies as text messages. Lead with the result, use plain text "
                         "and write links as bare URLs."}
CHECK_BASES = {"app": "assumed (ext_presentation BASES['channels'])",
               "text": "assumed (ext_presentation BASES['text'])",
               "whatsapp": "documented channel, assumed limits (ext_presentation BASES['whatsapp-text'])",
               "voice": "assumed (ext_presentation BASES['voice'])"}
LIST_LIMIT = 50      # list results kept for evidence (the latest of each tool and path)
ID_LIMIT = 2000      # identifier values remembered for the voice check
MAX_DEPTH = 32

# What WhatsApp shows as raw markup.
TABLE_RULE = re.compile(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
TABLE_ROW = re.compile(r"(?m)^\s*\|[^\n]*\|\s*$")
HTML = re.compile(r"(?i)</?(?:a|b|i|u|s|em|strong|br|p|div|span|table|thead|tbody|tr|td|th|ul|ol|li|h[1-6]|img"
                  r"|code|pre|hr|font|sup|sub|blockquote|section|article|details|summary)\b[^<>]{0,200}>")
HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]\n]{0,200}\]\([^)\s]{1,2000}\)")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]\n]{1,200}\]\(\s*<?(?:https?://|mailto:|www\.)[^)\s]{0,2000}\)"
                           r"|<(?:https?://|mailto:)[^>\s]{1,2000}>")
# What a voice reply cannot say well.
URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
BULLET = re.compile(r"(?m)^\s*(?:[-*•+]|\d{1,3}[.)])\s+\S")
EMPHASIS = re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|```")
MACHINE_ID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
                        r"|(?<![\w-])(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[A-Za-z0-9_-]{16,}(?![\w-])")
# Field roles, by the words of a field's name.
TITLE_WORDS = frozenset({"name", "title", "label", "headline", "display"})
PRICE_WORDS = frozenset({"price", "prices", "amount", "total", "cost", "rate", "fee", "fees", "subtotal", "nightly",
                         "minor", "cents", "fare"})
STATUS_WORDS = frozenset({"status", "state", "availability", "available", "stock"})
LINK_WORDS = frozenset({"url", "link", "href", "uri", "website", "permalink"})
CURRENCY_CODE = re.compile(r"[A-Z]{3}")
PRICED_TEXT = re.compile(r"(?:[$€£¥]|\b[A-Z]{3}\b)\s?\d|\d(?:[.,]\d+)?\s?(?:[$€£¥]|\b[A-Z]{3}\b)")
LINK_VALUE = re.compile(r"(?i)^(?:https?://|mailto:)\S+$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PresentationConfig(Strict):
    channel: Literal["app", "text", "whatsapp", "voice"] = "app"
    announce: bool = True                 # name the channel in the standing context
    enforce: bool = True                  # a channel finding fails the trial (else a non-required observation gap)
    max_characters: int | None = Field(None, ge=20, le=65536)      # default: whatsapp 4096, voice 500
    speakable_ids: list[str] = Field(default_factory=list, max_length=20)  # id fields a voice reply may read out
    widget_fields: list[Literal["title", "price", "status", "id", "link"]] = Field(
        default_factory=lambda: list(ROLES), max_length=5)


def result_data(envelope):
    """(data, its pointer, body) of a model-visible result: structuredContent, else the first JSON text block
    (pointed to as a tool check would: /content/<n>/text); data is None for errors and results without data,
    body is None for host messages."""
    body = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope.get("stdout")
    if not isinstance(body, dict):
        return None, None, None
    if body.get("isError") is True:
        return None, None, body
    if isinstance(body.get("structuredContent"), (dict, list)):
        return body["structuredContent"], "/structuredContent", body
    for index, block in enumerate(body.get("content") or []):
        text = block if isinstance(block, str) else block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text.lstrip().startswith(("{", "[")):
            try:
                data = loads(text, json_only=True)
            except LabError:
                continue
            if isinstance(data, (dict, list)):
                return data, f"/content/{index}/text", body
    return None, None, body


def words(key):
    return set(normalized_key(key).split("_")) if isinstance(key, str) else set()


def is_id(key):
    name = normalized_key(key) if isinstance(key, str) else ""
    return name == "id" or name.endswith("_id") or name in ("uuid", "sku", "slug")


def currency_of(record):
    """An ISO 4217 code a record states for its prices ({"currency": "USD"}), or None."""
    for key, value in record.items():
        if "currency" in words(key) and isinstance(value, str) and CURRENCY_CODE.fullmatch(value):
            return value
    return None


def roles(record, currency):
    """The widget roles one record fills; price counts only with a currency (its own, or its list's)."""
    found, priced, currency = set(), False, currency or currency_of(record)
    fields = list(record.items()) + [(key, value) for item in record.values() if isinstance(item, dict)
                                     for key, value in item.items()]
    for key, value in fields:
        named, empty = words(key), value in (None, "", [], {})
        if is_id(key) and not empty and not isinstance(value, (dict, list, bool)):
            found.add("id")
        elif named & TITLE_WORDS and isinstance(value, str) and value.strip():
            found.add("title")
        if named & STATUS_WORDS and not empty:
            found.add("status")
        if isinstance(value, str) and (named & LINK_WORDS and value.strip() or LINK_VALUE.match(value)):
            found.add("link")
        if named & PRICE_WORDS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                priced = True
            elif isinstance(value, str) and PRICED_TEXT.search(value):
                priced, currency = True, currency or "in text"
            elif isinstance(value, dict) and value.keys() & {"amount", "value", "minor", "amount_minor", "cents"}:
                priced, currency = True, currency or currency_of(value)
    if priced and currency:
        found.add("price")
    return found, priced and not currency


def lists(value, path="", currency=None, depth=0):
    """(path, records, currency) of every list of objects in a result, outermost first; the nearest
    enclosing currency field applies to its records."""
    if depth > MAX_DEPTH:
        return
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            yield path, value, currency
            return
        for index, item in enumerate(value):
            yield from lists(item, f"{path}/{index}", currency, depth + 1)
    elif isinstance(value, dict):
        currency = currency_of(value) or currency
        for key, item in value.items():
            yield from lists(item, f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}", currency, depth + 1)


def identifiers(value, skip, found, depth=0):
    """Identifier values (id fields) a voice reply should not read out, keyed by value."""
    if depth > MAX_DEPTH or len(found) >= ID_LIMIT:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if is_id(key) and normalized_key(key) not in skip and isinstance(item, (str, int)) \
                    and not isinstance(item, bool):
                text = str(item)
                # A bare word ("garden") or a short number reads as speech; a machine id has a digit or separator.
                if len(text) >= 4 and (re.search(r"[\d_.:-]", text) if isinstance(item, str) else len(text) >= 6):
                    found.setdefault(text, str(key))
            else:
                identifiers(item, skip, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            identifiers(item, skip, found, depth + 1)


class Presentation(BaseExtension):
    """Channel checks on the final answer; widget-field evidence on list results."""
    name = "presentation"
    config_model = PresentationConfig
    # Factory.bases: each Muse-specific behavior's basis label, recorded under run_lock.extensions[i].bases.
    bases: ClassVar[dict] = {key: item["basis"] for key, item in BASES.items()}

    def __init__(self, config):
        super().__init__(config)
        self.results, self.ui_hidden, self.lists, self.ids = 0, 0, {}, {}
        self.answer = None
        self.skip = {normalized_key(name) for name in config.speakable_ids}

    def label(self, run, connector, tool):
        return f"{connector}/{tool}" if len(run.connector_names) > 1 else tool

    @property
    def limit(self):
        return self.config.max_characters or LIMITS.get(self.config.channel)

    # Hooks ------------------------------------------------------------------------------------------
    def system_context(self, run):
        line = ANNOUNCEMENTS.get(self.config.channel) if self.config.announce else None
        return [line] if line else []

    async def on_tool_result(self, run, connector, tool, result):
        if result.get("source") == APP:
            return result
        data, base, body = result_data(result)
        if body is None:
            return result
        self.results += 1
        self.ui_hidden += sum(isinstance(block, dict) and (block.get("note") == "UI resource not shown" or isinstance(
            block.get("resource"), dict) and block["resource"].get("note") == "UI resource not shown")
            for block in body.get("content") or [])
        if data is None:
            return result
        identifiers(data, self.skip, self.ids)
        for path, records, currency in lists(data, base):
            rows = [roles(record, currency) for record in records]
            key = (self.label(run, connector, tool), path)
            if key not in self.lists and len(self.lists) >= LIST_LIMIT:
                continue
            seen = self.lists.get(key, {}).get("seen", 0)
            self.lists[key] = {"tool": key[0], "path": path, "records": len(records), "seen": seen + 1,
                               "fields": {role: sum(role in found for found, _ in rows) for role in ROLES},
                               "price_without_currency": sum(bare for _, bare in rows)}
        return result

    async def on_final(self, run, final_text):
        text = final_text if isinstance(final_text, str) else ""
        channel, checks = self.config.channel, []
        # No final answer (the runner calls on_final then too): nothing to judge for format or length.
        answered = final_text is not None
        if answered and channel in ("whatsapp", "text"):
            checks.append(self.check(f"presentation.{channel}_format", self.whatsapp(text),
                                     "plain text: no markdown tables, HTML, headings, or markdown links and images"))
        if answered and (channel in LIMITS or self.config.max_characters):
            over = [f"{len(text)} characters, over the {self.limit}-character limit"] if len(text) > self.limit else []
            checks.append(self.check("presentation.length", over, f"at most {self.limit} characters",
                                     {"characters": len(text)}))
        if answered and channel == "voice":
            checks.append(self.check("presentation.voice_speakable", self.voice(text),
                                     "speakable: no URLs, identifiers from results, machine identifiers or markdown"))
        if answered:
            self.answer = {"characters": len(text), "findings": [finding for check in checks
                                                                 for finding in check.get("findings", [])]}
        if gaps := self.widget_gaps():
            checks.append({"id": "presentation.widget_fields", "layer": "host", "status": "observation-gap",
                           "required": False, "class": "observation gap", "expected": list(self.config.widget_fields),
                           "actual": gaps, "basis": "unofficial (ext_presentation BASES['widgets'])"
                           if self.emulation_target == "muse" else "assumed CATT presentation check",
                           "detail": ("List results lack fields a list widget shows: " + "; ".join(
                               f"{where}: {', '.join(missing)}" for where, missing in gaps.items()))[:500]})
        return checks

    # Judgments --------------------------------------------------------------------------------------
    def whatsapp(self, text):
        found = []
        if TABLE_RULE.search(text) or len(TABLE_ROW.findall(text)) >= 2:
            found.append("a markdown table")
        if HTML.search(text):
            found.append("HTML markup")
        if HEADING.search(text):
            found.append("a markdown heading")
        if MARKDOWN_IMAGE.search(text):
            found.append("a markdown image")
        if MARKDOWN_LINK.search(text):
            found.append("a markdown link (WhatsApp shows links as bare URLs)" if self.config.channel == "whatsapp"
                         else "a markdown link (this text treatment uses bare URLs)")
        return found

    def voice(self, text):
        found = []
        if urls := URL.findall(text):
            found.append(f"{len(urls)} URL(s)")
        said = sorted({key for value, key in self.ids.items()
                       if re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text, re.IGNORECASE)})
        if said:
            found.append(f"identifiers from results ({', '.join(said[:10])})")
        if MACHINE_ID.search(URL.sub(" ", text)):
            found.append("a machine identifier")
        if TABLE_RULE.search(text) or len(TABLE_ROW.findall(text)) >= 2 or HEADING.search(text) \
                or BULLET.search(text) or EMPHASIS.search(text) or HTML.search(text):
            found.append("markdown or markup a voice cannot read")
        return found

    def check(self, identifier, findings, expected, actual=None):
        if not findings:
            status, kind = "passed", "verification"
        else:
            status, kind = ("failed", "presentation") if self.config.enforce else ("observation-gap", "observation gap")
        item = {"id": identifier, "layer": "agent", "status": status, "class": kind, "channel": self.config.channel,
                "expected": expected, "actual": actual if actual is not None else {"findings": findings},
                "findings": findings, "basis": CHECK_BASES[self.config.channel] if self.emulation_target == "muse"
                else "assumed CATT channel treatment for Instinct (see the sealed emulation_policy)"}
        if not self.config.enforce:
            item["required"] = False
        if findings:
            item["detail"] = f"The final answer for the {self.config.channel} channel has {'; '.join(findings)}"[:500]
        return item

    def widget_gaps(self):
        gaps = {}
        for (tool, path), row in self.lists.items():
            missing = [role for role in self.config.widget_fields if row["fields"][role] < row["records"]]
            if missing:
                gaps[f"{tool} {path}"] = missing
        return gaps

    def metrics(self, run):
        try:
            return {"channel": self.config.channel, "limit": self.limit, "answer": self.answer,
                    "results": self.results, "ui_resources_hidden": self.ui_hidden,
                    "widgets": {"fields": list(self.config.widget_fields), "lists": list(self.lists.values()),
                                "incomplete": self.widget_gaps()},
                    "bases": self.behavior_bases(BASES)}
        except Exception as error:  # noqa: BLE001 — metrics never stop the seal
            return {"error": f"presentation metrics are unavailable ({type(error).__name__})",
                    "bases": self.behavior_bases(BASES)}


register("presentation", Presentation)
