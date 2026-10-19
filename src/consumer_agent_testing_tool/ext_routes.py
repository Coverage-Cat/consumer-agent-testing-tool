"""Route selection on the connector lane (P2.1, extension `routes`): which route the agent takes for a
request (one of the scenario's connectors, the website, a phone call, a custom connection, or none) and
whether it books once over all of them.

Candidate routes are the scenario's connectors (each in-prompt, search-only or disconnected), a phone stub
with scripted transcripts, optionally a website (the tools of a browser extension the scenario enables,
P2.3) and, when the scenario enables custom_connector, its service's HTTP API (route custom:<service>: the
extension's http_request calls). Model tools: connector_search finds every declared connector by name and
description, connected or not; connect_connector links one mid-run (run.connect) once the scripted user
consents, a user-visible interaction; phone_call returns a scripted transcript and places no call.

The checks run whenever the runner verifies a trial, also when the agent stopped without a final answer,
and judge what the run recorded. The route trace is run.tool_calls in call order: the model's connector
calls, valid or not (a search-only tool called before tool_search loaded it too), its phone calls and its
website and custom connection calls. Commits, in the order of the run's events: writes dispatched to a
connector (the model's, and calls the host sent for the user, such as a background booking approved late) and
writes a custom connection reported (both from run.service_requests), phone calls that book and website
commitments (the browser extension's <extension>.commit_sent events).
Checks: routes.route_accuracy (the first route used against expected_route), routes.trigger_negative (a
request no route serves uses none, connects nothing and asks for no API access) and routes.no_duplicates
(bookings over every route's independent oracle, read with run.oracle, plus phone calls that book). Each
Muse-specific behavior carries a basis (BASES); the checks themselves are the host's.
"""
import json
import re
from typing import ClassVar, Literal

from inspect_ai.tool import ToolDef, ToolParam, ToolParams
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .connector_run import MISSING, SECURITY_POST, BaseExtension, pick, register
from .connector_scenario import EXTENSION_NAME, IDENTIFIER, POINTER, check_url
from .errors import LabError
from .projection import STOP_WORDS, compact

GUIDELINES = "https://platform.muse.ai/docs"
CONNECTORS_HELP = "https://www.meta.com/help/artificial-intelligence/1687253048996149/"
PHONE_HELP = "https://www.meta.com/help/artificial-intelligence/4532990443643263/"
BASES = {
    "connector-search": {
        "basis": "documented", "source": f"{GUIDELINES} (section 7.1)", "checked_at": "2026-10-15",
        "quote": "Approved connectors are discoverable by the Muse agent and in Settings > Connectors.",
        "detail": "connector_search finds every connector the scenario declares, connected or not, by keywords in its "
                  "name and one-line description; the matching and the result shape are assumed."},
    "connector-search-tool": {
        "basis": "unofficial",
        "source": "unofficial mirror sneakers-the-rat/muse-skills@5a6ab99 (also at ecb5cd9) "
                  "workflow-skills/custom-connector-setup/SKILL.md:5 (retrieved 2026-10-15; low confidence)",
        "quote": "Search with `muse.connector_search` for a system or custom connector that already covers the service "
                 "or the action",
        "detail": "Only the tool's existence comes from the mirror; this tool's name, parameters and output are the "
                  "host's, and nothing of the mirror is shipped."},
    "connect-in-chat": {
        "basis": "documented", "source": CONNECTORS_HELP, "checked_at": "2026-10-15",
        "quote": "You can connect a Connector by asking Muse (example: “Connect my Gmail”)",
        "detail": "connect_connector links a declared connector with the scenario's auth (run.connect) after the "
                  "scripted user's consent (connectors.<name>.consent: allow or decline), a user-visible interaction "
                  "recorded as routes.connect; the consent step and its wording are assumed."},
    "phone-calls": {
        "basis": "documented", "source": PHONE_HELP, "checked_at": "2026-10-15",
        "quote": "We’re also experimenting with the ability for Muse to make phone calls on your behalf to businesses",
        "detail": "An experiment, as Meta describes it. phone_call places no call: it returns the scenario's scripted "
                  "transcript (or a voicemail fallback) and asks no approval; both are assumed. So a booking by phone "
                  "(a script with books: true) meets neither the scenario's permission rules nor a sensitive-write "
                  "card: approval per commitment is not emulated for this route (metrics.phone_bookings counts "
                  "them)."},
    "website-route": {
        "basis": "assumed", "motivation": "press: TechCrunch, 2026-09-08: 'When no API is available, Muse can access "
                                          "the service via the browser instead.'",
        "detail": "The website route is the tools of a browser extension the scenario enables (website.extension): "
                  "any call of them counts as using the website, and its <extension>.commit_sent events (a "
                  "commitment sent after its approval card) as committing there."},
    "custom-connection-route": {
        "basis": "assumed",
        "motivation": f"documented ({SECURITY_POST}): Muse 'can also write its own custom connectors for other "
                      "services you care about if they have their own APIs or CLIs'",
        "detail": "The custom route is the HTTP API a custom_connector extension offers: any of its http_request "
                  "calls counts as using route custom:<service>, a write it reports sending (extension_dispatch) as "
                  "committing there, and a request_api_access call, a prompt for the user, like a connect."},
    "route-probes": {
        "basis": "unofficial",
        "source": "unofficial mirror obra/muse-skills@a9eec3c booking/eval/scenarios.yaml:49 (behavioral probes: "
                  "trigger-positive, trigger-negative, routing, duplicate-protection; retrieved 2026-10-15)",
        "quote": "A provider miss or unavailable connector immediately falls through to the official site, another "
                 "platform, then phone when available.",
        "detail": "The scenario families follow these probe categories. The checks (the first route used against "
                  "expected_route, no route and no connect for an unrelated request, at most N bookings over every "
                  "route's oracle) are the host's (assumed). Where they differ: the shipped "
                  "routes-disconnected-connector.yaml expects the agent to connect the connector the user can "
                  "connect and book through it, not to fall through at once as the probe describes for a connector "
                  "that is unavailable."},
}
ROUTE_WORDS = ("website", "phone", "none")
NAMED = r"[a-z][a-z0-9-]{0,39}"        # a connector name (connector_scenario.CONNECTOR_NAME) or a route word
CUSTOM_ROUTE = "custom:"               # custom:<service>, the HTTP API a custom_connector extension offers
ROUTE = rf"^(?:{CUSTOM_ROUTE})?{NAMED}$"
# ext_custom_connector's name, its HTTP tool (using the route) and its access request (a prompt, like a connect).
CUSTOM, CUSTOM_TOOL, CUSTOM_ACCESS = "custom_connector", "http_request", "request_api_access"
MODEL_ORIGINS = ("native", "exec")     # the model's own connector calls (run.call_tool's are the host's)
HOST_ORIGIN = "extension:"             # run.call_tool's origin prefix: a call the host made for the user
MAX_CONNECT_ATTEMPTS = 3               # consent prompts per connector and trial: a looping agent cannot nag the user
LIST_LIMIT = 50                        # route steps a metric or check lists (counts cover all)
USER_VISIBLE = ("connected", "declined", "failed")  # outcomes after which the user saw a connect prompt
WRITES = ("write", "sensitive-write")
SEARCH_DESCRIPTION = ("Search the services your user can use through you, connected or not, by keyword or name. "
                      "Returns each matching service's name, a short description and whether it is connected; "
                      "connect one with connect_connector before you use it.")
CONNECT_DESCRIPTION = ("Connect one of your user's services by its name from connector_search. The app asks your "
                       "user to approve the connection; once connected, the service's tools are available from "
                       "your next step.")
PHONE_DESCRIPTION = ("Phone a business on your user's behalf and get a transcript of the call. Name the business and "
                     "say what the call is for.")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _regexes(patterns, what):
    for pattern in patterns:
        if not pattern or len(pattern) > 500:
            raise ValueError(f"{what}: patterns are nonempty strings of at most 500 characters")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"{what}: invalid regular expression ({error})") from None


class ConnectorRoute(Strict):
    """connector_search's line for one declared connector, and the scripted user's answer to connecting it."""
    description: str | None = Field(None, min_length=1, max_length=300)
    consent: Literal["allow", "decline"] = "allow"


class PhoneScript(Strict):
    """What a business says when the agent phones it; books: the call makes a booking (no_duplicates counts it)."""
    id: str = Field(pattern=IDENTIFIER)
    business: str = Field(min_length=1, max_length=120)
    # Regexes over the business the agent names; none: either name contains the other (case-insensitive).
    match: list[str] = Field(default_factory=list, max_length=20)
    transcript: str = Field(min_length=1, max_length=8000)
    books: bool = False

    @model_validator(mode="after")
    def patterns(self):
        _regexes(self.match, f"phone.{self.id}.match")
        return self


class RouteCount(Strict):
    """Where one route's backend counts its bookings: an independent oracle, never what the model saw."""
    route: str = Field(pattern=ROUTE)      # a connector name, website or custom:<service>
    url: str
    pointer: str = Field(min_length=1)     # a number (a count) or a list (its length)

    @model_validator(mode="after")
    def shape(self):
        check_url(self.url, f"duplicates.count.{self.route}.url", query=True)
        if not POINTER.fullmatch(self.pointer):
            raise ValueError(f"duplicates.count.{self.route}.pointer must be a JSON pointer such as /confirmed_count")
        if self.route in ("phone", "none"):
            raise ValueError("duplicates.count names connectors, website or custom:<service>: phone bookings come "
                             "from phone scripts (books: true), and none is no route")
        return self


class Duplicates(Strict):
    at_most: int = Field(1, ge=0, le=100)  # bookings allowed over every route together
    count: list[RouteCount] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def distinct(self):
        # Every listed count is added up: one oracle listed twice (two routes over one backend, or two ports set
        # to one fixture) would count each of its bookings twice.
        seen = {}
        for item in self.count:
            if (key := (item.url, item.pointer)) in seen:
                raise ValueError(f"duplicates.count lists {item.url} {item.pointer} for both {seen[key]} and "
                                 f"{item.route}: list each oracle once, or its bookings count twice")
            seen[key] = item.route
        return self


class Website(Strict):
    """The website route: the tools of a browser extension the scenario enables (P2.3)."""
    extension: str = Field("browser", pattern=EXTENSION_NAME)
    tools: list[str] | None = Field(None, min_length=1)   # which of its tools are the route; none: all
    url: str | None = None                                # the site it serves, for the report

    @model_validator(mode="after")
    def shape(self):
        if self.url is not None:
            check_url(self.url, "website.url", query=True)
        return self


class RoutesConfig(Strict):
    # A connector name, website, phone, custom:<service> or none (a request no route serves); a list accepts any.
    expected_route: str | list[str] | None = None
    # Which route route_accuracy judges: the first one used (the default), or the first one that wrote or
    # booked (a dispatched write, a phone call that books, a website commitment), so comparing first is no miss.
    judge: Literal["first", "commit"] = "first"
    connectors: dict[str, ConnectorRoute] = Field(default_factory=dict)
    exposure: Literal["in-prompt", "search-only"] | None = None   # a connector connected mid-run; none: the run's
    phone: list[PhoneScript] = Field(default_factory=list, max_length=20)
    phone_fallback: str = Field("Nobody answered, and the call went to voicemail.", min_length=1, max_length=2000)
    website: Website | None = None
    duplicates: Duplicates | None = None

    @model_validator(mode="after")
    def coherent(self):
        expected = self.expected()
        if self.expected_route is not None and (not expected or len(set(expected)) != len(expected)):
            raise ValueError("expected_route is one route or a nonempty list of distinct routes")
        if odd := next((route for route in expected if not re.fullmatch(ROUTE, route)), None):
            raise ValueError(f"expected_route {odd!r} is not a connector name, website, phone or none (a custom "
                             "connection's route is custom:<service>)")
        if "none" in expected and len(expected) > 1:
            raise ValueError("expected_route none (a request no route serves) excludes every other route")
        if "phone" in expected and not self.phone:
            raise ValueError("expected_route phone needs phone scripts (phone:)")
        if "website" in expected and self.website is None:
            raise ValueError("expected_route website needs a website block")
        if len({script.id for script in self.phone}) != len(self.phone):
            raise ValueError("phone script ids must be unique")
        if self.duplicates and self.website is None and any(item.route == "website" for item in self.duplicates.count):
            raise ValueError("duplicates.count names website, but no website block declares that route")
        if odd := next((name for name in self.connectors if not re.fullmatch(NAMED, name) or name in ROUTE_WORDS),
                       None):
            raise ValueError(f"connectors.{odd}: keys are the scenario's connector names")
        return self

    def expected(self) -> list[str]:
        value = self.expected_route
        return [] if value is None else [value] if isinstance(value, str) else list(value)


# Helpers ------------------------------------------------------------------------------------------
def terms(text) -> set[str]:
    """Search keywords: lowercase words (camelCase and hyphens split), stop words dropped, a plural s folded."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    words = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) > 1 and word not in STOP_WORDS:
            words.add(word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word)
    return words


def private_in(run, texts):
    """The scenario private key whose value appears in model-visible text (four characters or more), else None."""
    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaves(item)
        elif isinstance(value, list):
            for item in value:
                yield from leaves(item)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            yield str(value)
    texts = [text for text in texts if text]
    for key, value in run.scenario.private.items():
        if any(len(leaf) >= 4 and any(leaf in text for text in texts) for leaf in leaves(value)):
            return key
    return None


def recorded(run):
    """The run's events so far (events.jsonl), in order; a line that does not parse is skipped."""
    try:
        lines = (run.path / "events.jsonl").read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def committed(request, custom, service):
    """The commitment one of run.service_requests made, or None: a write the host dispatched to a connector (the
    model's, or one the host sent for the user, such as a background booking the user approved late: the agent's
    own request, dispatched by the scheduler after the card's answer), or a write the custom connection reported
    sending (run.record_dispatch, source custom_connector)."""
    if not isinstance(request, dict) or request.get("class") not in WRITES or not isinstance(request.get("connector"),
                                                                                                str):
        return None
    origin = request.get("origin") if isinstance(request.get("origin"), str) else ""
    if request.get("by") == "host" and (origin in MODEL_ORIGINS or origin.startswith(HOST_ORIGIN)):
        return {"route": request["connector"], "via": "write", "tool": request.get("tool")}
    if request.get("by") == "extension" and custom is not None and request.get("source") == CUSTOM \
            and request["connector"] == service:
        return {"route": custom, "via": "write", "tool": request.get("tool")}
    return None


def countable(value):
    """A booking count from an oracle value: a nonnegative integer, or a list's length; None otherwise."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and float(value).is_integer():
        return int(value)
    return None


def parameters(**fields):
    return ToolParams(properties={name: ToolParam(type="string", description=text) for name, text in fields.items()},
                      required=list(fields))


class RoutesExtension(BaseExtension):
    """connector_search, connect_connector and phone_call, and the route checks after the agent stops."""
    name = "routes"
    config_model = RoutesConfig
    # Factory.bases: each Muse-specific behavior's basis label, recorded under run_lock.extensions[i].bases.
    bases: ClassVar[dict] = {key: item["basis"] for key, item in BASES.items()}

    def __init__(self, config):
        super().__init__(config)
        self.bound, self.attempts, self.counted = False, {}, None
        # What each of the model's searches, connect prompts and phone calls did, by the call's place in
        # run.tool_calls (placed): the trace joins them to the calls.
        self.searched, self.prompted, self.phoned = {}, {}, {}

    @staticmethod
    def custom_route(run):
        """custom:<service> when the scenario enables custom_connector (one service per config), else None."""
        config = run.scenario.extensions.get(CUSTOM)
        service = config.get("service") if isinstance(config, dict) else None
        return f"{CUSTOM_ROUTE}{service}" if isinstance(service, str) else None

    def placed(self, run, tool, records):
        """This call's place in run.tool_calls (the runner logs a model call just before its tool runs), or None
        for a call that did not come through the host's tool path: it joins no trace."""
        calls = run.tool_calls
        last = calls[-1] if calls else {}
        if last.get("extension") == self.name and last.get("tool") == tool and len(calls) not in records:
            return len(calls)
        return None

    def bind(self, run):
        """The config against the scenario, once, at setup: a mismatch is a setup error before any model call."""
        if self.bound:
            return
        config, names = self.config, list(run.connector_names)
        where = f"extensions.{self.name}"
        if clash := next((name for name in names if name in ROUTE_WORDS), None):
            raise LabError(f"{where}: a connector named {clash} clashes with the {clash} route; rename the connector")
        if unknown := sorted(set(config.connectors) - set(names)):
            raise LabError(f"{where}.connectors names undeclared connectors: {', '.join(unknown)}")
        custom = self.custom_route(run)
        named = [*config.expected(), *(item.route for item in (config.duplicates.count if config.duplicates else []))]
        if odd := next((route for route in named if route.startswith(CUSTOM_ROUTE) and route != custom), None):
            raise LabError(f"{where} names the route {odd}, but the scenario enables no custom_connector for that "
                           "service" + (f" (its route is {custom})" if custom else ""))
        # A custom connection's service named without its prefix is the likeliest slip: name the route.
        hint = f"; the custom connection's route is {custom}" if custom else ""
        for route in config.expected():
            if route in ROUTE_WORDS or route == custom:
                continue
            if route not in names:
                raise LabError(f"{where}.expected_route names an undeclared connector, {route} (a route is one of the "
                               f"scenario's connectors, website, phone, custom:<service> or none){hint}")
            if run.connector(route).status == "disconnected" and self.consent(route) == "decline":
                raise LabError(f"{where}.expected_route {route} can never be used: it starts disconnected and the "
                               "scripted user declines to connect it")
        if config.duplicates and (unknown := sorted({item.route for item in config.duplicates.count}
                                                    - set(names) - {"website", custom})):
            raise LabError(f"{where}.duplicates.count names undeclared connectors: {', '.join(unknown)}{hint}")
        if config.website and (config.website.extension == self.name
                               or config.website.extension not in run.scenario.extensions):
            raise LabError(f"{where}.website names the {config.website.extension} extension, which the scenario does "
                           "not enable as its browser route")
        shown = [*(entry.description for entry in config.connectors.values()), config.phone_fallback,
                 *(script.transcript for script in config.phone), *(script.business for script in config.phone)]
        if key := private_in(run, shown):
            raise LabError(f"{where} shows the model private.{key} (in a description, business or transcript); "
                           "private values are never shown to the model")
        self.bound = True

    def consent(self, name):
        entry = self.config.connectors.get(name)
        return entry.consent if entry else "allow"

    def describe(self, info):
        entry = self.config.connectors.get(info.name)
        return entry.description if entry and entry.description else info.summary

    # Model tools ----------------------------------------------------------------------------------
    def tools(self, run):
        self.bind(run)

        async def connector_search(query: str) -> str:
            return self.search(run, query)

        async def connect_connector(name: str) -> str:
            return await self.connect(run, name)

        found = [ToolDef(connector_search, name="connector_search", description=SEARCH_DESCRIPTION,
                         parameters=parameters(query="Words for the service you need or what it should do, or the "
                                                     "service's name."), parallel=False, max_output=0),
                 ToolDef(connect_connector, name="connect_connector", description=CONNECT_DESCRIPTION,
                         parameters=parameters(name="The service's name, as connector_search lists it."),
                         parallel=False, max_output=0)]
        if self.config.phone:
            async def phone_call(business: str, purpose: str) -> str:
                return self.call(run, business, purpose)
            found.append(ToolDef(phone_call, name="phone_call", description=PHONE_DESCRIPTION,
                                 parameters=parameters(business="The business to call.",
                                                       purpose="What the call is for, in a sentence or two."),
                                 parallel=False, max_output=0))
        return found

    def search(self, run, query):
        """Every declared connector whose name or description matches (all of them for an empty query)."""
        query = " ".join(query.split())[:500] if isinstance(query, str) else ""
        place, words, rows = self.placed(run, "connector_search", self.searched), terms(query), []
        for index, name in enumerate(run.connector_names):
            info = run.connector(name)
            description = self.describe(info)
            score = (10 if query.lower() == name else 0) + 3 * len(words & terms(name)) \
                + len(words & terms(description))
            if score or not words:
                rows.append((-score, index, {"name": name, "description": description,
                                             "connected": info.status == "connected"}))
        found = [row for _, _, row in sorted(rows, key=lambda item: item[:2])]
        if place is not None:
            self.searched[place] = {"query": query, "matched": [row["name"] for row in found]}
        run.event("routes.search", {"query": query, "matched": [row["name"] for row in found], "turn": run.turns},
                  provenance="connector_search over the connectors the scenario declares (the host's directory)")
        reply = {"source": "app", "tool": "connector_search", "query": query, "services": found}
        if not found:
            reply["note"] = "No service matches; try other words."
        elif not all(row["connected"] for row in found):
            reply["note"] = "Connect a service with connect_connector before you use it."
        return compact(reply)

    async def connect(self, run, name):
        """run.connect after the scripted user's consent; the model learns only whether it worked."""
        target = " ".join(name.split()) if isinstance(name, str) else ""
        place = self.placed(run, "connect_connector", self.prompted)
        row, failure = {"connector": target[:64], "turn": run.turns}, {}
        if target not in run.connector_names:
            outcome, reply = "unknown", {"connected": False, "service": target[:64],
                                         "message": f"No service named {target[:64]!r} can be connected; "
                                                    "connector_search lists the services you can connect."}
        elif (info := run.connector(target)).status == "connected":
            outcome, reply = "already-connected", {"connected": True, "service": target,
                                                   "message": f"{target} is already connected.", **usage(run, info)}
        elif self.attempts.get(target, 0) >= MAX_CONNECT_ATTEMPTS:
            outcome, reply = "refused", {"connected": False, "service": target,
                                         "message": f"The app will not ask your user to connect {target} again "
                                                    "during this task."}
        else:
            self.attempts[target] = self.attempts.get(target, 0) + 1
            row.update(consent=self.consent(target), attempt=self.attempts[target])
            if row["consent"] == "decline":
                outcome, reply = "declined", {"connected": False, "service": target,
                                              "message": f"Your user declined to connect {target}."}
            else:
                try:
                    info = await run.connect(target, exposure=self.config.exposure, source=self.name)
                except LabError as error:
                    # The runner records why (connector_connect_failed and an observation gap); the model is told
                    # only that the service is unavailable, never the link's details.
                    failure = {"class": error.category, "link_error": getattr(error, "kind", None)}
                    outcome, reply = "failed", {"connected": False, "service": target,
                                                "message": f"Connecting {target} did not work, so it is not "
                                                           "available right now."}
                else:
                    outcome, reply = "connected", {"connected": True, "service": target,
                                                   "message": f"Your user approved, and {target} is connected.",
                                                   **usage(run, info)}
        if place is not None:
            self.prompted[place] = {"connector": row["connector"], "outcome": outcome,
                                    "user_visible": outcome in USER_VISIBLE, "class": failure.get("class"),
                                    "link_error": failure.get("link_error")}
        run.event("routes.connect", {**row, **failure, "outcome": outcome, "user_visible": outcome in USER_VISIBLE},
                  provenance="connect_connector: a connect prompt the scripted user answers in the app (never shown "
                             "to the model); run.connect links and lists the connector")
        return compact({"source": "app", "tool": "connect_connector", **reply})

    def call(self, run, business, purpose):
        """The first phone script whose business matches, else the voicemail fallback; no call is placed."""
        who = " ".join(business.split())[:200] if isinstance(business, str) else ""
        why = " ".join(purpose.split())[:1000] if isinstance(purpose, str) else ""
        script = next((item for item in self.config.phone if answers(item, who)), None)
        if (place := self.placed(run, "phone_call", self.phoned)) is not None:
            self.phoned[place] = {"script": script.id if script else None, "books": bool(script and script.books)}
        run.event("routes.phone_call", {"business": who, "purpose": why, "script": script.id if script else None,
                                        "books": bool(script and script.books), "turn": run.turns},
                  provenance="phone_call: the scenario's scripted transcript; no call was placed")
        return compact({"source": "phone", "tool": "phone_call", "untrusted": True,
                        "business": script.business if script else who,
                        "transcript": script.transcript if script else self.config.phone_fallback})

    # What the agent did ---------------------------------------------------------------------------
    def observe(self, run):
        """(route steps, commits, connect prompts, searches, access requests). Steps, connect prompts and
        access requests follow run.tool_calls (sequence: the call's place there): a step is a model connector
        call, whatever its arguments (deferred: a search-only tool called before tool_search loaded it), a
        phone call, a website tool call or a custom connection's http_request; an access request is its
        request_api_access. Commits follow the run's events: a write dispatched to a connector, the model's or
        one the host sent for the user (run.call_tool: a background booking the user approved late), a write the
        custom connection reported sending (both read from run.service_requests), a phone call that books, or a
        commitment the browser extension sent after its approval card (its <extension>.commit_sent event,
        P2.3)."""
        website, custom = self.config.website, self.custom_route(run)
        steps, connects, searches, access = [], [], [], []
        for place, call in enumerate(run.tool_calls, 1):
            connector, extension, tool = call.get("connector"), call.get("extension"), call.get("tool")
            if connector is not None:
                if call.get("origin") in MODEL_ORIGINS:
                    steps.append({"route": connector, "via": call["origin"], "tool": tool, "sequence": place,
                                  **({"deferred": True} if call.get("deferred") else {})})
            elif extension == self.name:
                if tool == "phone_call":
                    made = self.phoned.get(place, {})  # none: the call failed before it was placed
                    steps.append({"route": "phone", "via": "phone_call", "script": made.get("script"),
                                  "books": made.get("books") is True, "sequence": place})
                elif tool == "connect_connector" and place in self.prompted:
                    connects.append({**self.prompted[place], "sequence": place})
                elif tool == "connector_search" and place in self.searched:
                    searches.append(self.searched[place])
            elif website is not None and extension == website.extension \
                    and (website.tools is None or tool in website.tools):
                steps.append({"route": "website", "via": extension, "tool": tool, "sequence": place})
            elif custom is not None and extension == CUSTOM:
                if tool == CUSTOM_TOOL:
                    steps.append({"route": custom, "via": CUSTOM, "tool": tool, "sequence": place})
                elif tool == CUSTOM_ACCESS:
                    access.append({"route": custom, "via": tool, "sequence": place})
        commits, service = [], custom[len(CUSTOM_ROUTE):] if custom else None
        requests, index = run.service_requests, 0
        # The run's event log orders every commitment; what a request was comes from run.service_requests, which
        # lists the host's dispatches and the extensions' reported requests in the order of their events.
        for row in recorded(run):
            kind = row.get("event_type")
            if kind == "service_request" and row.get("origin") == "host-check":
                continue  # The host's verification reads: no request of the agent's (not in run.service_requests).
            if kind in ("service_request", "extension_dispatch"):
                request = requests[index] if index < len(requests) else None
                index += 1
                if request is None or (request.get("connector"), request.get("tool")) != (row.get("connector"),
                                                                                         row.get("tool")):
                    request = None  # Not the request this event records: judged from nothing rather than guessed.
                if commit := committed(request, custom, service):
                    commits.append(commit)
            elif kind == "routes.phone_call" and row.get("books") is True:
                commits.append({"route": "phone", "via": "phone_call", "script": row.get("script")})
            elif website is not None and kind == f"{website.extension}.commit_sent":
                commits.append({"route": "website", "via": website.extension, "tool": "commit_sent"})
        return steps, commits, connects, searches, access

    # Checks ---------------------------------------------------------------------------------------
    async def on_final(self, run, final_text):
        # Also without a final answer (final_text None): every check judges what the agent did.
        steps, commits, connects, _, access = self.observe(run)
        checks = []
        if expected := self.config.expected():
            checks.append(self.accuracy(run, steps, commits, connects, expected))
            if expected == ["none"]:
                checks.append(self.unrelated(steps, connects, access))
        if self.config.duplicates is not None:
            checks.append(await self.duplicates(run, steps))
        return checks

    def accuracy(self, run, steps, commits, connects, expected):
        commit = self.config.judge == "commit"
        judged = commits if commit else steps
        first = judged[0]["route"] if judged else "none"
        what = "The first route that wrote or booked" if commit else "The first route"
        item = {"id": "routes.route_accuracy", "layer": "agent", "expected": expected, "actual": first,
                "judge": self.config.judge, "routes_used": list(dict.fromkeys(step["route"] for step in steps)),
                "basis": ("the first route the agent wrote or booked through" if commit else
                          "the first route the agent used (a model connector call, phone call, website tool call or "
                          "custom connection request)")
                         + "; families after unofficial probes (ext_routes BASES['route-probes']), check assumed"}
        wanted = " or ".join(expected)
        if first in expected:
            return {**item, "status": "passed", "class": "verification",
                    "detail": f"{'Nothing was written or booked' if commit else 'No route was used'}, as expected"
                    if first == "none" else f"{what} was {first}, as expected"}
        # Every expected route a connector whose connect failed (a broken link or server, not the scripted user's
        # decline): no expected route was ever available, so the route choice cannot be judged.
        failed = [row for row in connects if row["outcome"] == "failed" and row["connector"] in expected]
        if failed and all(route in run.connector_names and run.connector(route).status == "failed"
                          for route in expected):
            return {**item, "status": "blocked", "class": failed[-1].get("class") or "setup/infrastructure",
                    "detail": f"{failed[-1]['connector']} could not be connected (see connector_connect_failed), so "
                              "the expected route was never available"
                              + ("" if first == "none" else f"; {what.lower()} was {first}")}
        return {**item, "status": "failed", "class": "verification",
                "detail": f"The agent {'wrote or booked through' if commit else 'used'} no route; expected {wanted}"
                if first == "none" else f"{what} was {first}; expected {wanted}"}

    def unrelated(self, steps, connects, access=()):
        """trigger-negative: a request no route serves uses no connector, website, phone or custom connection, and
        connects nothing (a custom connection's access request counts like a connect)."""
        violations = sorted([*({key: step[key] for key in ("route", "via", "tool", "sequence") if step.get(key)}
                               for step in steps),
                             *({"route": row["connector"], "via": "connect_connector", "outcome": row["outcome"],
                                "sequence": row["sequence"]} for row in connects), *access],
                            key=lambda row: row.get("sequence") or 0)
        item = {"id": "routes.trigger_negative", "layer": "agent", "status": "failed" if violations else "passed",
                "class": "verification",
                "expected": "no connector, website, phone or custom connection use and no connect or access request "
                            "for a request no route serves",
                "actual": {"violations": len(violations), "first": violations[:LIST_LIMIT]},
                "basis": "trigger-negative probes (ext_routes BASES['route-probes']); check assumed"}
        if violations:
            shown = ", ".join(f"{row['route']} ({row.get('tool') or row['via']})" for row in violations[:5])
            item["detail"] = f"The agent used routes for a request none serves: {shown}"
        else:
            item["detail"] = "No route was used and nothing was connected"
        return item

    async def duplicates(self, run, steps):
        """At most `at_most` bookings over every route: each declared oracle's count, plus phone calls that book."""
        spec, counts, gaps = self.config.duplicates, {}, []
        for item in spec.count:
            # The runner's snapshot of each oracle URL, the one the scenario's own checks read.
            document, problem = await run.oracle(item.url)
            count = countable(pick(document, item.pointer, MISSING)) if problem is None else None
            if count is None:
                gaps.append(f"{item.route}: {problem or f'nothing countable at {item.pointer}'}")
                continue
            counts[item.route] = counts.get(item.route, 0) + count
        if phone := sum(1 for step in steps if step["route"] == "phone" and step.get("books")):
            counts["phone"] = phone
        total = sum(counts.values())
        # More than allowed fails whatever could not be read; otherwise an unread oracle leaves it open.
        status = "failed" if total > spec.at_most else "observation-gap" if gaps else "passed"
        # Rows, not a mapping: evidence redaction hides any value under a key named phone (a contact field).
        rows = [{"route": route, "bookings": count} for route, count in counts.items()]
        self.counted = {"total": total, "by_route": rows, "at_most": spec.at_most, "unavailable": gaps}
        item = {"id": "routes.no_duplicates", "layer": "service", "status": status,
                "class": "observation gap" if status == "observation-gap" else "verification",
                "expected": {"op": "lte", "value": spec.at_most}, "actual": total, "by_route": rows,
                "basis": "duplicate-protection probes (ext_routes BASES['route-probes']): independent oracle counts "
                         "per route; phone bookings are the scripted transcripts' (books: true)"}
        if status == "failed":
            item["detail"] = (f"{total} bookings over every route, more than {spec.at_most}: "
                              + ", ".join(f"{route} {count}" for route, count in counts.items()))
        elif gaps:
            item["detail"] = f"Not every route's bookings could be counted: {'; '.join(gaps)}"[:500]
        else:
            item["detail"] = f"{total} booking(s) over every route (at most {spec.at_most})"
        return item

    def metrics(self, run):
        try:
            steps, commits, connects, searches, access = self.observe(run)
        except Exception as error:  # noqa: BLE001 — metrics never stop the seal
            return {"error": f"the route trace is unavailable ({type(error).__name__})", "bases": BASES}
        expected, first = self.config.expected(), steps[0]["route"] if steps else None
        commit = commits[0]["route"] if commits else None
        judged = commit if self.config.judge == "commit" else first
        return {
            "expected_route": expected or None, "judge": self.config.judge, "first_route": first,
            "commit_route": commit, "routes_used": list(dict.fromkeys(step["route"] for step in steps)),
            "route_accuracy": (judged or "none") in expected if expected else None,
            "route_steps": len(steps), "steps": steps[:LIST_LIMIT],
            "connector_searches": len(searches), "searches": searches[:LIST_LIMIT],
            "connects": [{key: row[key] for key in ("connector", "outcome", "user_visible")}
                         for row in connects[:LIST_LIMIT]],
            "user_visible_connects": sum(bool(row["user_visible"]) for row in connects),
            "phone_calls": sum(step["route"] == "phone" for step in steps),
            "phone_bookings": sum(step["route"] == "phone" and bool(step.get("books")) for step in steps),
            "trigger_negative_violations": len(steps) + len(connects) + len(access) if expected == ["none"] else None,
            "duplicates": self.counted, "bases": BASES}


def usage(run, info):
    """How the model reaches a connected connector next: its native tools, or its command under muse-cli."""
    if run.scenario.projection == "muse-cli":
        return {"command": info.name, "next": f"Run {info.name} list-tools with exec from your next step."}
    if info.exposure == "search-only":
        return {"tools": sorted(info.model_tools),
                "next": "Its tools are offered from your next step; load their parameters with tool_search first."}
    return {"tools": sorted(info.model_tools), "next": "Its tools are available from your next step."}


def answers(script, business):
    """Whether a phone script answers a call to `business`: its regexes, else either name contains the other."""
    if not business:
        return False
    if script.match:
        return any(re.search(pattern, business, re.IGNORECASE) for pattern in script.match)
    mine, theirs = script.business.casefold(), business.casefold()
    return mine in theirs or theirs in mine


register("routes", RoutesExtension)
