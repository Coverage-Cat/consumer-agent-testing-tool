"""Scheduled background work on the fixture clock (P2.5): the connector lane's `scheduler` extension.

Muse "continues to work on a schedule and in response to relevant events" (introducing.muse.ai), and
the Help Center lists reminders and scheduled tasks under Upcoming. A scenario declares jobs ({id,
every, jitter, message, first, until, max_runs}). Whenever the agent stops, on_idle moves the clock
extension's fixture clock (and each test service's clock, through its clock endpoint) to the next due
event and resumes the agent with a background-context message. While background work runs the user is
away: a call the connector's permission engine would show a card for (run.approval_needed) is held as a
pending approval, answered after a scripted delay (late: the host then sends it through the same approval
path and shows the agent the outcome as a model call's result is shown, run.model_view, so the other
extensions' result hooks see it) or never. An ask_user question waits for the away user the same way,
whether or not a scripted reply matches it (questions: answered late with that reply or the scenario's
fallback, or never). Only connector calls are held: the cards of other extensions' own engines (a custom
connection's http_request, a browser task's checkout) are answered by the scripted user at once (not
emulated; BASIS["background-approvals"]). Metrics
come from what the model received: polls, conditional reads and not-modified answers, wasted polls (an
unchanged full re-read), Retry-After compliance, expired-quote handling and what the agent said when
each background resume ended. Third-party push is unknown (no documented webhook API), so this polls.
Every Muse-specific behavior carries a basis in BASIS (the run lock records the labels, Factory.bases);
nothing here claims equivalence to Muse. A scripted replay runs the jobs at its {"idle": true} steps.
"""
import copy
import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inspect_ai.tool import ToolDef, ToolError, ToolParam, ToolParams
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .connector_run import RESUME_LIMIT, BaseExtension, pick, register, same
from .connector_scenario import POINTER, check_url
from .errors import LabError
from .serialization import digest, recordable

INTRODUCING = "https://introducing.muse.ai/"
DATA_ARTICLE = "https://www.meta.com/help/artificial-intelligence/2225571704857152/"
UPCOMING_ARTICLE = "https://www.meta.com/help/artificial-intelligence/1484325780075655/"
PERMISSIONS_ARTICLE = "https://www.meta.com/help/artificial-intelligence/1385290430137537/"
CONNECTORS_ARTICLE = "https://www.meta.com/help/artificial-intelligence/1687253048996149/"
SECURITY_POST = "https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse"
GUIDELINES = "https://platform.muse.ai/docs"
CHECKED = "2026-10-15"
BASIS = {
    "scheduled-work": {
        "basis": "documented", "source": INTRODUCING, "checked_at": CHECKED,
        "quote": "Once you’ve set a goal or a task, Muse continues to work on a schedule and in response to relevant "
                 "events, taking the next step and following up, alongside whatever you ask it in conversation.",
        "detail": "Jobs come from the scenario. At each run the stopped agent resumes with a background-context "
                  "message (the time, the task and its request); that wording is assumed."},
    "upcoming": {
        "basis": "documented", "source": f"{UPCOMING_ARTICLE}; {DATA_ARTICLE}", "checked_at": CHECKED,
        "quote": "tap your Assistant Icon at the top of the app, go to the Upcoming tab, and view all scheduled "
                 "reminders and tasks. / You can ask your Muse in chat to update or delete a reminder or scheduled "
                 "task.",
        "detail": "The standing context lists the jobs as Upcoming would, and cancel_scheduled_task stops one; the "
                  "wording, the tool's name and shape, and cancelling without an approval card are assumed. "
                  "Creating or editing tasks is not emulated."},
    "background-approvals": {
        "basis": "assumed",
        "motivation": f"{PERMISSIONS_ARTICLE}: 'If you select Artifacts and scheduled tasks, you can manage your "
                      f"approvals for each scheduled task and artifacts.' {SECURITY_POST}: 'A dialog is presented "
                      "to the user directly within the client UI — not via their conversation with Muse'",
        "detail": "During background runs the user is away. A call the connector's permission engine would show a "
                  "card for is held and never sent; the model is told it waits for the user. approvals.answer "
                  "late: after the scripted delay the host sends the held call through the same path (validation, "
                  "host overlays, card, the scripted user's rules, dispatch) and resumes the agent with the outcome, "
                  "rendered as a model call's result is (every extension's result hooks, masking, the result "
                  "bound). never: it stays pending. Calls a grant or the mode allows go through. A repeated "
                  "identical request is counted, not asked again. Questions: background-questions. Only connector "
                  "calls are held: cards other extensions show on their own permission engines (a custom "
                  "connection's http_request, a browser task's checkout) are answered by the scripted user at once "
                  "during background runs too (not emulated)."},
    "background-questions": {
        "basis": "assumed",
        "motivation": f"{INTRODUCING}: Muse 'continues to work on a schedule ... alongside whatever you ask it in "
                      "conversation', so background work runs while its user may be away, as for "
                      "background-approvals; how Muse asks an away user a question is not public",
        "detail": "During background runs every ask_user question reaches the away user (the scheduler's "
                  "on_ask_user hook, which the runner asks first also about questions a user.replies rule matches; "
                  "label away user): the model is told they have not seen it. questions.answer late: after the "
                  "scripted delay the agent resumes with what the scripted user says to it, the matched reply or "
                  "the scenario's user.fallback; never: it stays unanswered. A repeated identical question is "
                  "counted, not asked again."},
    "fixture-time": {
        "basis": "assumed",
        "detail": "Time moves only between runs: on_idle moves the fixture clock to the next run or late answer, "
                  "and each advance URL moves the service's clock with it (the connector fixture's clock control: "
                  "{\"set\": <instant>}, or the seconds since the last move for a clock that only moves forward), "
                  "so the service judges expiry on the same clock; its reply's clock must keep one offset from the "
                  "fixture clock (none for set). No time passes during a model turn."},
    "jitter": {"basis": "assumed", "detail": "Run k is due at first + k * every plus a deterministic offset in "
                                             "[0, jitter] seeded by seed, the job id and k; Muse's timing is not "
                                             "public."},
    "missed-runs": {"basis": "assumed", "detail": "Slots that pass while the agent works or waits for an answer run "
                                                  "once, late, and count as missed (cron-like coalescing)."},
    "background-notifications": {
        "basis": "assumed",
        "detail": "What the agent says when it stops after a background resume is taken as what its user is told "
                  "then; whether and how Muse notifies a user is not public. metrics.notifications records each "
                  "resume's closing message length from the run's stops (the text stays in agent_stopped events); "
                  "only the final answer is scored."},
    "third-party-push": {
        "basis": "unknown", "source": CONNECTORS_ARTICLE, "checked_at": CHECKED,
        "quote": "Some Connectors can also share information proactively with Muse. For example, calendar Connectors "
                 "can update Muse automatically when there are changes in your schedule.",
        "detail": "The example is a calendar connector; whether platform (third-party developer) connectors can "
                  "push is undocumented. No webhook or push API for them is documented, so this emulation polls and "
                  "never wakes the agent on a service event."},
    "conditional-reads": {
        "basis": "assumed", "motivation": "RFC 9110 sections 13.1.2 (If-None-Match) and 15.4.5 (304 Not Modified); "
                                          "MCP has no conditional request",
        "detail": "polling.conditional names the connector's own token argument and result fields: an ETag analog "
                  "or a since-cursor. A poll is conditional when it sends the token it last received, not modified "
                  "when the result says so (or lists no changes), and wasted when a full re-read returns what the "
                  "previous read of the same call returned. A call's first read counts as first, never as changed "
                  "or wasted: no earlier read of it exists to compare with (a write's result is not one, even when "
                  "the read shows that write's booking newly confirmed)."},
    "retry-after": {
        "basis": "assumed",
        "motivation": f"RFC 9110 section 10.2.3 (Retry-After); {GUIDELINES} section 5.4: 'In your API or MCP "
                      "documentation, include ... status handling, errors, and rate limits.'",
        "detail": "rate_limit.retry_after reads the delay from a tool error result. A model call in its scope before "
                  "the delay has passed on the fixture clock violates it."},
    "expired-quotes": {
        "basis": "assumed",
        "motivation": f"{GUIDELINES} section 4.7: 'Distinguish quoted, pending, confirmed, failed, and cancelled "
                      "actions. Report uncertain outcomes honestly and check their status before retrying.'",
        "detail": "An expiry rule names the error a write returns for an expired quote or hold, the argument that "
                  "names it and the tools that renew it. renewed counts a renew call after the expiry; stale retries "
                  "count the same write naming the expired reference again."},
}
# The runner resumes a stopped agent at most 20 times (connector_run.MAX_IDLE_RESUMES).
MAX_RUNS = 20
MAX_SPAN = 30 * 86400        # longest interval, offset or delay (a bound of this emulation)
PLAN_CAP = 1000              # schedule slots counted per job; more is reported as capped
JOB_ID = r"^[a-z][a-z0-9-]{0,39}$"
DURATION = re.compile(r"(?:(\d{1,4})d)?(?:(\d{1,5})h)?(?:(\d{1,6})m)?(?:(\d{1,8})s)?")
TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u2028\u2029]")
AWAY = "Your user is not in the conversation right now."
PENDING = ("Your user has been asked to approve this and has not answered yet, so nothing was sent. If they "
           "approve it, the app sends it and tells you the result.")
REPEATED = "Your user has not answered the earlier approval request for this same action yet, so nothing was sent."
UNSEEN = ("Your user is not in the conversation right now, so they have not seen this question yet. If they answer, "
          "the app tells you.")
UNSEEN_AGAIN = "Your user has not answered your earlier question yet."
AWAY_LABEL = "away user"     # how the runner labels the scheduler's ask_user replies (answered_by: scheduler)
QUESTION_EXCERPT = 300       # characters of a question quoted back with its late answer
PROVENANCE = "scheduler extension (P2.5): background work on the fixture clock"
# Extensions that show cards on their own permission engines, which the away user's hold does not cover.
OWN_ENGINES = ("browser", "custom_connector")
MISSING_CHANGES = object()


def seconds(value, what) -> int:
    """Whole seconds from an integer or a duration such as 90s, 15m, 1h30m or 2d."""
    if isinstance(value, int) and not isinstance(value, bool):
        total = value
    elif isinstance(value, str) and (match := DURATION.fullmatch(value.strip())) and any(match.groups()):
        days, hours, minutes, rest = (int(part or 0) for part in match.groups())
        total = ((days * 24 + hours) * 60 + minutes) * 60 + rest
    else:
        raise ValueError(f"{what} is whole seconds or a duration such as 90s, 15m, 1h30m or 2d")
    if not 0 <= total <= MAX_SPAN:
        raise ValueError(f"{what} is between 0 seconds and 30 days")
    return total


def instant(value, what) -> datetime | None:
    """An aware datetime from ISO 8601 text with a UTC offset, or None when `value` is a duration."""
    if isinstance(value, str) and "T" in value:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{what} is an ISO 8601 date and time with a UTC offset, or a duration") from None
        if moment.tzinfo is None:
            raise ValueError(f"{what} needs a UTC offset")
        if moment.microsecond:
            raise ValueError(f"{what} is in whole seconds")  # Services report their clocks in seconds.
        return moment
    seconds(value, what)
    return None


def moment_of(value, start, what) -> datetime:
    """`value` as an instant: itself, or a duration after `start`."""
    found = instant(value, what)
    return found if found is not None else start + timedelta(seconds=seconds(value, what))


def pointer(value, what):
    if not isinstance(value, str) or not value or not POINTER.fullmatch(value):
        raise ValueError(f"{what} is a JSON pointer such as /structuredContent/etag")
    return value


def span(total) -> str:
    """A duration for the model: 1 h 30 min, 15 min, 90 s."""
    parts, rest = [], int(total)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        if rest >= size:
            parts.append(f"{rest // size} {unit}")
            rest %= size
    return " ".join(parts) or "0 s"


def iso(moment):
    return moment.isoformat() if moment is not None else None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Job(Strict):
    """One recurring task: due at first, first + every, ... (each plus its jitter) until it ends."""
    id: str = Field(pattern=JOB_ID)
    every: int | str
    jitter: int | str = 0
    message: str = Field(min_length=1, max_length=2000)  # what the task does; the model sees it at each run
    first: int | str | None = None   # an instant, or a duration after the clock's start (default: every)
    until: int | str | None = None   # the last moment a run may start: an instant, or a duration after the start
    max_runs: int | None = Field(None, ge=1, le=MAX_RUNS)

    @model_validator(mode="after")
    def shape(self):
        where = f"jobs.{self.id}"
        every, jitter = seconds(self.every, f"{where}.every"), seconds(self.jitter, f"{where}.jitter")
        if every < 1:
            raise ValueError(f"{where}.every is at least 1 second")
        if jitter >= every:
            raise ValueError(f"{where}.jitter must be shorter than every, so runs keep their order")
        for name in ("first", "until"):
            if getattr(self, name) is not None:
                instant(getattr(self, name), f"{where}.{name}")
        if self.until is None and self.max_runs is None:
            raise ValueError(f"{where} needs until or max_runs; without an end it would stop only at the runner's "
                             f"{MAX_RUNS} resumes")
        if not self.message.strip() or TEXT_CONTROL.search(self.message):
            raise ValueError(f"{where}.message is text without control characters")
        return self


class ClockEndpoint(Strict):
    """A test service's clock control, answering {"clock": <ISO instant>}: POST {"set": <ISO instant>} (set, the
    connector fixture's /__oracle/clock), or the whole seconds since the last move, as {"advance_seconds": n} or
    {"seconds": n} (a clock that only moves forward; the first POST sends 0 to read it)."""
    url: str
    send: Literal["set", "advance_seconds", "seconds"] = "set"

    @model_validator(mode="after")
    def shape(self):
        check_url(self.url, "advance.url")
        return self


class Approvals(Strict):
    """How the away user answers cards shown during background work (BASIS["background-approvals"])."""
    what: ClassVar[str] = "approvals"
    answer: Literal["late", "never"] = "never"
    after: int | str | list[int | str] | None = None   # late: delay per pending card, in order; the last repeats

    @model_validator(mode="after")
    def shape(self):
        if self.answer == "never":
            if self.after is not None:
                raise ValueError(f"{self.what}.after applies to answer: late only")
            return self
        if self.after is None:
            raise ValueError(f"{self.what}.answer late needs after: how long the user takes to answer")
        delays = self.after if isinstance(self.after, list) else [self.after]
        if not 1 <= len(delays) <= MAX_RUNS:
            raise ValueError(f"{self.what}.after lists 1 to {MAX_RUNS} delays")
        if any(seconds(delay, f"{self.what}.after") < 1 for delay in delays):
            raise ValueError(f"{self.what}.after delays are at least 1 second")
        return self

    def delay(self, index) -> int:
        delays = self.after if isinstance(self.after, list) else [self.after]
        return seconds(delays[min(index, len(delays) - 1)], f"{self.what}.after")


class Questions(Approvals):
    """How the away user answers ask_user questions no scripted reply matches during background work
    (BASIS["background-questions"]): late, with the scenario's user.fallback, or never; delays per question."""
    what: ClassVar[str] = "questions"


class Conditional(Strict):
    """The connector's own conditional read: an ETag analog (if_none_match -> not_modified) or a since-cursor
    (since -> only the changes since then). The token argument a call sends and the result fields it reads."""
    argument: str        # pointer into the call's arguments (if_none_match, since)
    etag: str            # pointer into the result holding the token for the next read (etag, next cursor)
    not_modified: str | None = None   # pointer into the result that is true for a not-modified answer
    changes: str | None = None        # pointer into the result to the list of changes; empty: nothing changed

    @model_validator(mode="after")
    def shape(self):
        pointer(self.argument, "polling.conditional.argument")
        pointer(self.etag, "polling.conditional.etag")
        for name in ("not_modified", "changes"):
            if getattr(self, name) is not None:
                pointer(getattr(self, name), f"polling.conditional.{name}")
        return self

    def unchanged(self, result) -> bool:
        """A conditional answer that says nothing changed: not modified, or an empty list of changes."""
        return (self.not_modified is not None and pick(result, self.not_modified) is True) or (
            self.changes is not None and pick(result, self.changes, MISSING_CHANGES) == [])


class Polling(Strict):
    conditional: Conditional | None = None
    volatile: list[str] = Field(default_factory=list, max_length=20)  # result fields that change on every read

    @model_validator(mode="after")
    def shape(self):
        for item in self.volatile:
            pointer(item, "polling.volatile")
        return self


class RateLimit(Strict):
    """Where a rate-limited tool error result carries its Retry-After delay, in seconds, and what it limits: the
    connector, one tool, or one call (the same tool and arguments, such as one booking's status)."""
    retry_after: str
    scope: Literal["connector", "tool", "call"] = "connector"

    @model_validator(mode="after")
    def shape(self):
        pointer(self.retry_after, "rate_limit.retry_after")
        return self


class Signal(Strict):
    pointer: str
    value: str | int | bool

    @model_validator(mode="after")
    def shape(self):
        pointer(self.pointer, "expiry.error.pointer")
        return self


class Expiry(Strict):
    """A write refused because its quote or hold expired, and the tools that obtain a new one."""
    connector: str | None = None   # none: the tool of any connector
    tool: str = Field(min_length=1)
    error: Signal
    reference: str                 # pointer into the write's arguments naming the quote or hold (/quote_id)
    renew: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def shape(self):
        pointer(self.reference, "expiry.reference")
        return self


class SchedulerConfig(Strict):
    jobs: list[Job] = Field(min_length=1, max_length=8)
    advance: list[ClockEndpoint] = Field(default_factory=list, max_length=8)
    approvals: Approvals = Field(default_factory=Approvals)
    questions: Questions = Field(default_factory=Questions)
    polling: Polling = Field(default_factory=Polling)
    rate_limit: RateLimit | None = None
    expiry: list[Expiry] = Field(default_factory=list, max_length=8)
    standing_context: bool = True     # list the jobs in the standing context, as Upcoming shows them
    cancel_tool: bool = True          # offer cancel_scheduled_task
    seed: int = Field(0, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def unique(self):
        names = [job.id for job in self.jobs]
        if len(set(names)) != len(names):
            raise ValueError("jobs ids must be unique")
        urls = [endpoint.url for endpoint in self.advance]
        if len(set(urls)) != len(urls):
            raise ValueError("advance lists each clock URL once")
        return self


@dataclass
class Schedule:
    """One job's slots on the fixture clock and what became of them."""
    job: Job
    every: int
    jitter: int
    first: datetime
    until: datetime | None
    seed: int
    index: int = 0                # the next slot not yet run or missed
    runs: int = 0
    missed: int = 0
    times: list = field(default_factory=list)
    cancelled: datetime | None = None

    def slot(self, index) -> datetime | None:
        if index < 0 or self.job.max_runs is not None and index >= self.job.max_runs:
            return None
        offset = random.Random(f"{self.seed}:{self.job.id}:{index}").randint(0, self.jitter) if self.jitter else 0
        moment = self.first + timedelta(seconds=index * self.every + offset)
        return None if self.until is not None and moment > self.until else moment

    def upto(self, moment) -> int:
        """How many slots fall at or before `moment`. Jitter stays below every, so only the last candidate can
        fall after it."""
        if moment < self.first:
            return 0
        last = int((moment - self.first).total_seconds() // self.every)
        if self.until is not None:
            last = min(last, int((self.until - self.first).total_seconds() // self.every))
        if self.job.max_runs is not None:
            last = min(last, self.job.max_runs - 1)
        while last >= 0 and ((found := self.slot(last)) is None or found > moment):
            last -= 1
        return last + 1

    @property
    def planned(self) -> int:
        if self.until is None:
            return self.job.max_runs
        return min(self.upto(self.until), PLAN_CAP)

    def due(self):
        return None if self.cancelled is not None else self.slot(self.index)


@dataclass
class Pending:
    """An approval card shown while the user was away: held, and sent only once they answer."""
    id: str
    job: str
    run: int
    connector: str
    tool: str
    tool_class: str | None
    arguments: dict               # what the model sent; host-owned values are filled again at dispatch
    key: str                      # the same connector, tool and arguments: a repeated request
    requested_at: datetime
    due: datetime | None          # when the user answers (late); None: never
    answered_at: datetime | None = None
    outcome: str | None = None
    repeats: int = 0


@dataclass
class Question:
    """An ask_user question asked while the user was away: answered late (user.fallback) or never."""
    id: str
    job: str
    run: int
    question: str                 # as the hooks receive it: masked by the runner
    key: str                      # the same question again (whitespace and case aside): a repeat
    requested_at: datetime
    due: datetime | None          # when the user answers (late); None: never
    answered_at: datetime | None = None
    repeats: int = 0
    scripted: str | None = None   # the scripted reply a user.replies rule gives it; None: user.fallback


def call_result(envelope, connector):
    """The CallToolResult a model-visible envelope shows (native or muse-cli), or None for host messages and
    service errors; native text blocks become text content again, so pointers read /content/0/text/..."""
    if not isinstance(envelope, dict) or envelope.get("source") != connector:
        return None
    body = envelope.get("result")
    if isinstance(body, dict):
        content = body.get("content") if isinstance(body.get("content"), list) else []
        return {**body, "content": [{"type": "text", "text": block} if isinstance(block, str) else block
                                    for block in content]}
    stdout = envelope.get("stdout")
    return stdout if isinstance(stdout, dict) else None


def without(document, path):
    """A copy of `document` without the member at JSON pointer `path`, descending into strings that hold JSON
    as connector_run.lookup does (/content/0/text/checked_at); a pointer that finds nothing changes nothing."""
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]

    def drop(value, rest):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            return json.dumps(drop(parsed, rest), sort_keys=True) if isinstance(parsed, (dict, list)) else value
        head = rest[0]
        if isinstance(value, dict) and head in value:
            return {key: item for key, item in value.items() if key != head} if len(rest) == 1 else \
                {**value, head: drop(value[head], rest[1:])}
        if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", head) and int(head) < len(value):
            index = int(head)
            if len(rest) == 1:
                return value[:index] + value[index + 1:]
            return [*value[:index], drop(value[index], rest[1:]), *value[index + 1:]]
        return value
    return drop(copy.deepcopy(document), parts)


def asks(run, connector, tool, tool_class) -> bool:
    """Whether the connector's permission engine would show a card for this call now (run.approval_needed: no
    grant or mode allows it)."""
    if tool_class is None:
        return True  # Fail closed: hold an unclassified call for the away user rather than let it through unasked.
    return run.approval_needed(connector, tool)


class SchedulerExtension(BaseExtension):
    """Runs the scenario's jobs whenever the agent stops; see the module docstring and BASIS."""
    name = "scheduler"
    config_model = SchedulerConfig
    # Factory.bases: each Muse-specific behavior's label (and source), under run_lock.extensions[i].bases.
    bases: ClassVar[dict] = {key: {"basis": item["basis"], **({"source": item["source"]} if "source" in item else {})}
                             for key, item in BASIS.items()}

    def __init__(self, config):
        super().__init__(config)
        self.schedules = None              # job id -> Schedule, once the run's clock start is known
        self.background = None             # {"job", "run"} once background work has started
        self.resumes = []                  # each background resume: {"kind", "job", "run", "turn"}
        self.pending, self.questions, self.answering, self.call = [], [], None, None
        self.synced, self.syncs, self.classes, self.clocks = None, 0, {}, {}
        self.polls, self.last, self.etags = Counter(), {}, {}
        self.limits, self.limit_counts = {}, Counter()
        self.expired, self.expiry_counts = [], Counter()
        self.counts = Counter()

    # Clock and plan -----------------------------------------------------------------------------
    def clock(self, run):
        clock = run.fixture_clock
        if clock is None:
            raise LabError("The scheduler runs on the fixture clock: set extensions.clock.now (ISO 8601 with a UTC "
                           "offset)")
        return clock

    def plan(self, run):
        """Each job's schedule from the clock's start, recorded once as a scheduler.plan event."""
        if self.schedules is not None:
            return self.schedules
        start = self.clock(run).start
        schedules = {}
        for job in self.config.jobs:
            every = seconds(job.every, "every")
            first = moment_of(job.first, start, f"jobs.{job.id}.first") if job.first is not None \
                else start + timedelta(seconds=every)
            until = moment_of(job.until, start, f"jobs.{job.id}.until") if job.until is not None else None
            if first < start:
                raise LabError(f"extensions.scheduler.jobs.{job.id}.first is before the clock's start "
                               f"{start.isoformat()}")
            if until is not None and until < first:
                raise LabError(f"extensions.scheduler.jobs.{job.id}.until ({until.isoformat()}) is before its first "
                               f"run ({first.isoformat()}); the task would never run")
            schedules[job.id] = Schedule(job, every, seconds(job.jitter, "jitter"), first, until, self.config.seed)
        self.schedules = schedules
        run.event("scheduler.plan", {
            "clock": self.clock(run).mode,
            "start": start.isoformat(),
            "jobs": [{"job": item.job.id, "every_seconds": item.every, "jitter_seconds": item.jitter,
                      "first": iso(item.first), "until": iso(item.until), "max_runs": item.job.max_runs,
                      "planned": item.planned, "slots": [iso(item.slot(index)) for index in range(min(item.planned,
                                                                                                    MAX_RUNS))]}
                     for item in schedules.values()],
            "advance": [endpoint.url for endpoint in self.config.advance],
            "approvals": self.config.approvals.model_dump(mode="json"),
            "questions": self.config.questions.model_dump(mode="json"), "basis": self.behavior_bases(BASIS)},
            provenance=PROVENANCE)
        return schedules

    def zone(self, run):
        name = (run.scenario.extensions.get("clock") or {}).get("timezone") \
            or run.scenario.profile.identity.get("timezone")
        try:
            return ZoneInfo(name) if isinstance(name, str) and name else None
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def when(self, run, moment):
        """A moment as the clock extension's standing-context line words it."""
        zone = self.zone(run)
        local = moment.astimezone(zone) if zone else moment
        offset = local.strftime("%z")
        label = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
        return f"{local.strftime('%A, %Y-%m-%d %H:%M')} ({f'{zone.key}, {label}' if zone else label})"

    async def sync(self, run, moment):
        """Move every declared service clock with the fixture clock to `moment` (BASIS["fixture-time"]). Its reply
        must keep one offset from the fixture clock (none for set); anything else stops the run."""
        for index, endpoint in enumerate(self.config.advance):
            state = self.clocks.setdefault(index, {"at": None, "offset": None})
            if endpoint.send == "set":
                body = {"set": moment.isoformat()}
            else:  # The seconds since the last move; the first POST reads the service's clock.
                body = {endpoint.send: 0 if state["at"] is None else max(0, round((moment - state["at"]).total_seconds()))}
            try:
                async with run.services.http() as client:
                    response = await client.post(endpoint.url, json=body)
                    status, raw = response.status_code, response.content[:4096]
            except Exception as error:  # noqa: BLE001 — name the URL; the run cannot continue on a stale clock
                raise LabError(f"The scheduler could not move the service clock at {endpoint.url} "
                               f"({type(error).__name__})") from None
            reported = None
            try:
                reported = datetime.fromisoformat(json.loads(raw)["clock"])
                if reported.tzinfo is None:
                    reported = None
            except (ValueError, KeyError, TypeError):
                pass
            offset = (reported - moment).total_seconds() if reported is not None else None
            expected = 0 if endpoint.send == "set" else state["offset"]
            self.event(run, "scheduler.clock_set", {"url": endpoint.url, "sent": body, "status": status,
                                                    "fixture_clock": moment.isoformat(), "reported": iso(reported),
                                                    "offset_seconds": offset})
            # Services report whole seconds (the connector fixture does).
            if not 200 <= status < 300 or offset is None or expected is not None and abs(offset - expected) >= 1:
                raise LabError(f"The service clock at {endpoint.url} did not follow the fixture clock to "
                               f"{moment.isoformat()} (HTTP {status}, clock {iso(reported)}); the scheduler keeps "
                               "service time in step with fixture time")
            state.update(at=moment, offset=offset if state["offset"] is None else state["offset"])
        self.synced, self.syncs = moment, self.syncs + 1

    def event(self, run, kind, payload, audience="developer"):
        return run.event(kind, payload, audience, provenance=PROVENANCE)

    # Hooks ------------------------------------------------------------------------------------------
    def system_context(self, run):
        schedules = self.plan(run)
        if not self.config.standing_context:
            return []
        lines = ["Scheduled tasks (Upcoming); each runs in the background at its time:"]
        for item in schedules.values():
            due = item.due()
            ends = (f"until {self.when(run, item.until)}" if item.until is not None
                    else f"{item.job.max_runs} run{'s' if item.job.max_runs != 1 else ''}")
            first = f"next {self.when(run, due)}" if due else "no runs left"
            lines.append(f"- {item.job.id}: every {span(item.every)}, {first}, {ends}. {item.job.message}")
        return lines

    def tools(self, run):
        if not self.config.cancel_tool:
            return []

        async def cancel_scheduled_task(**kwargs: Any):
            task = kwargs.get("task_id")
            schedules = self.plan(run)
            if set(kwargs) != {"task_id"} or not isinstance(task, str) or task not in schedules:
                raise ToolError(f"No scheduled task named {str(task)[:64]!r}; scheduled tasks: "
                                f"{', '.join(schedules)}")
            item = schedules[task]
            if item.cancelled is None:
                item.cancelled = self.clock(run).now()
                self.event(run, "scheduler.job_cancelled", {"job": task, "at": iso(item.cancelled),
                                                            "runs": item.runs, "turn": run.turns})
            return f"The scheduled task {task} is cancelled; it will not run again."
        return [ToolDef(cancel_scheduled_task, name="cancel_scheduled_task",
                        description="Stop one of your user's scheduled tasks (listed under Upcoming) from running "
                                    "again.",
                        parameters=ToolParams(properties={"task_id": ToolParam(
                            type="string", description="The scheduled task's name, as listed.")}, required=["task_id"]),
                        parallel=False, max_output=0)]

    async def before_dispatch(self, run, connector, tool, arguments):
        now = self.clock(run).now()
        if self.config.advance and self.synced != now:
            await self.sync(run, now)  # Before the first call: a service clock left by an earlier trial is wrong.
        host = self.answering
        self.call = None if host else {"connector": connector, "tool": tool, "arguments": copy.deepcopy(arguments)}
        if host:
            return {"scheduled_task": host.job, "approval": host.id}
        self.track(run, connector, tool, arguments, now)
        if self.background is None:
            return None
        tool_class = self.tool_class(run, connector, tool)
        if asks(run, connector, tool, tool_class):
            return self.hold(run, connector, tool, tool_class, arguments, now)
        return {"scheduled_task": self.background["job"], "run": self.background["run"]}

    async def on_tool_result(self, run, connector, tool, result):
        call, self.call = self.call, None
        found = call_result(result, connector)
        if call is not None and (call["connector"], call["tool"]) == (connector, tool) and found is not None:
            self.observe(run, connector, tool, call["arguments"], found, model=True)
        return result

    async def on_ask_user(self, run, question, scripted=None):
        """A question (masked; hooks run in name order), with the scripted reply a user.replies rule gives it
        (scripted), or None when no rule matches. In the foreground the user is present: None leaves it to that
        reply, the next hook (a simulated user) or user.fallback. During background work the away user has not
        seen it, matched or not (BASIS["background-questions"]); a late answer is that reply."""
        if self.background is None:
            return None
        text = " ".join(question.split()) if isinstance(question, str) else ""
        waiting = next((item for item in self.questions if item.key == text.casefold() and item.answered_at is None),
                       None)
        if waiting is not None:
            waiting.repeats += 1
            self.counts["repeated_questions"] += 1
            self.event(run, "scheduler.question_repeated", {"pending": waiting.id, "repeats": waiting.repeats,
                                                            "job": self.background["job"],
                                                            "run": self.background["run"]})
            return {"reply": UNSEEN_AGAIN, "label": AWAY_LABEL}
        now, questions = self.clock(run).now(), self.config.questions
        due = now + timedelta(seconds=questions.delay(len(self.questions))) if questions.answer == "late" else None
        item = Question(f"question-{len(self.questions) + 1}", self.background["job"], self.background["run"], text,
                        text.casefold(), now, due, scripted=scripted)
        self.questions.append(item)
        self.event(run, "scheduler.question_pending", {
            "pending": item.id, "job": item.job, "run": item.run, "question": text, "requested_at": iso(now),
            "answer": questions.answer, "due": iso(due), "scripted_reply": scripted is not None,
            "basis": self.behavior_bases(BASIS)["background-questions"]["basis"]})
        return {"reply": UNSEEN, "label": AWAY_LABEL}

    async def on_idle(self, run):
        clock = self.clock(run)
        schedules, now = self.plan(run), clock.now()
        if run.turns >= run.scenario.limits.model_turns:
            return None  # No model turn left to resume: nothing more runs, and no late answer is sent unheard.
        # At one instant: approval answers first, then question answers, then job runs.
        answers = [(item.due, 0 if isinstance(item, Pending) else 1, index, item)
                   for index, item in enumerate([*self.pending, *self.questions])
                   if item.due is not None and item.answered_at is None]
        runs = [(due, 2, index, item) for index, item in enumerate(schedules.values()) if (due := item.due())]
        if not answers and not runs:
            return None
        due, kind, _, item = min(answers + runs, key=lambda row: row[:3])
        at = max(due, now)
        if at > now:
            label = f"{item.job.id} run {item.runs + 1}" if kind == 2 else f"{item.id} answered"
            clock.set(at, reason=f"scheduler: {label}")
        if self.config.advance and self.synced != at:
            await self.sync(run, at)
        if kind == 0:
            return await self.answer(run, item, at)
        if kind == 1:
            return self.reply(run, item, at)
        return self.start(run, item, due, at)

    async def on_final(self, run, final_text):
        for item in self.pending:
            if item.answered_at is None:
                self.event(run, "scheduler.approval_unanswered", {
                    "pending": item.id, "job": item.job, "connector": item.connector, "tool": item.tool,
                    "requested_at": iso(item.requested_at), "answer": self.config.approvals.answer,
                    "due": iso(item.due), "repeats": item.repeats})
        for item in self.questions:
            if item.answered_at is None:
                self.event(run, "scheduler.question_unanswered", {
                    "pending": item.id, "job": item.job, "requested_at": iso(item.requested_at),
                    "answer": self.config.questions.answer, "due": iso(item.due), "repeats": item.repeats})
        checks = [self.runs_check()]
        if self.config.rate_limit is not None and self.limit_counts["limited"]:
            checks.append(self.retry_check())
        if self.resumes and (unheld := sorted(name for name in run.scenario.extensions if name in OWN_ENGINES)):
            checks.append({"id": "scheduler.away_user_scope", "status": "observation-gap", "class": "observation gap",
                           "layer": "host", "required": False,
                           "basis": self.behavior_bases(BASIS)["background-approvals"]["basis"],
                           "detail": f"During background runs only connector calls wait for the away user: cards "
                                     f"{', '.join(unheld)} showed on {'their' if len(unheld) > 1 else 'its'} own "
                                     "permission engine were answered by the scripted user at once"})
        clock = run.fixture_clock
        if clock is not None and clock.elapsed and not self.config.advance:
            checks.append({"id": "scheduler.service_clock", "status": "observation-gap", "class": "observation gap",
                           "layer": "host", "required": False,
                           "basis": self.behavior_bases(BASIS)["fixture-time"]["basis"],
                           "detail": f"The fixture clock moved {span(clock.elapsed)}, but no advance URL is "
                                     "declared: the service judged expiry and status on its own clock"})
        return checks

    def metrics(self, run):
        schedules = self.schedules or {}
        clock = run.fixture_clock
        answered = [item for item in self.pending if item.answered_at is not None]
        replied = [item for item in self.questions if item.answered_at is not None]
        polls = self.polls
        notifications = self.notifications(run)
        return {
            "jobs": {name: {"runs": item.runs, "planned": item.planned, "planned_capped": item.planned == PLAN_CAP,
                            "missed": item.missed, "times": list(item.times), "cancelled": iso(item.cancelled)}
                     for name, item in schedules.items()},
            "background_runs": sum(item.runs for item in schedules.values()),
            "approvals": {"answer": self.config.approvals.answer, "pending": len(self.pending),
                          "answered_late": len(answered), "unanswered": len(self.pending) - len(answered),
                          "repeated_requests": self.counts["repeated_requests"],
                          "outcomes": dict(Counter(item.outcome for item in answered)),
                          "late_seconds": [int((item.answered_at - item.requested_at).total_seconds())
                                           for item in answered]},
            "questions": {"answer": self.config.questions.answer, "asked": len(self.questions),
                          "answered_late": len(replied), "unanswered": len(self.questions) - len(replied),
                          "repeated_questions": self.counts["repeated_questions"],
                          "late_seconds": [int((item.answered_at - item.requested_at).total_seconds())
                                           for item in replied]},
            "notifications": notifications,
            "silent_resumes": sum(row["characters"] == 0 for row in notifications),
            "polls": {"total": polls["total"], "by_tool": dict(sorted(
                (key[5:], count) for key, count in polls.items() if key.startswith("tool:"))),
                      **{key: polls[key] for key in ("first", "changed", "wasted", "not_modified", "errors",
                                                     "conditional", "missed_conditional")},
                      "wasted_rate": round(polls["wasted"] / polls["total"], 4) if polls["total"] else None,
                      "conditional_rate": round(polls["conditional"] / polls["total"], 4) if polls["total"] else None,
                      "conditional_configured": self.config.polling.conditional is not None},
            "rate_limits": None if self.config.rate_limit is None else {
                **{key: self.limit_counts[key] for key in ("limited", "honored", "violations")},
                # Retry-After values beyond the emulation's bound (30 days): reported, never a window.
                **({"malformed": self.limit_counts["malformed"]} if self.limit_counts["malformed"] else {})},
            "expiry": None if not self.config.expiry else {
                key: self.expiry_counts[key] for key in ("expired", "renewed", "stale_retries")},
            "clock": {"start": iso(clock.start) if clock else None, "end": iso(clock.now()) if clock else None,
                      "advanced_seconds": clock.elapsed if clock else None,
                      "service_clocks": len(self.config.advance), "service_clock_sets": self.syncs},
            "push": self.behavior_bases(BASIS)["third-party-push"]["basis"],
            "basis": {key: item["basis"] for key, item in self.behavior_bases(BASIS).items()},
        }

    def notifications(self, run):
        """What the agent said when it stopped after each background resume (run.stops; BASIS
        ["background-notifications"]): characters of that message, None when the trial ended before the agent
        stopped again (a limit, or a scripted replay's final answer step)."""
        stops, rows = run.stops, []
        for resume in self.resumes:
            end = next((stop for stop in stops if stop["turn"] > resume["turn"]), None)
            said = (end["text"] or "").strip() if end is not None else None
            rows.append({**resume, "characters": len(said) if said is not None else None})
        return rows

    # Background runs and approvals ------------------------------------------------------------------
    def resumed(self, run, kind, job, number):
        self.background = {"job": job, "run": number}
        self.resumes.append({"kind": kind, "job": job, "run": number, "turn": run.turns})

    def start(self, run, item, due, at):
        count = max(item.upto(at), item.index + 1)
        missed = count - item.index - 1
        item.index, item.runs, item.missed = count, item.runs + 1, item.missed + missed
        item.times.append(at.isoformat())
        self.resumed(run, "run", item.job.id, item.runs)
        self.event(run, "scheduler.job_run", {"job": item.job.id, "run": item.runs, "scheduled": iso(due),
                                              "at": iso(at), "missed": missed, "turn": run.turns,
                                              "clock": self.clock(run).mode, "cleanup_verified": False})
        return f"Scheduled task {item.job.id} (run {item.runs}), {self.when(run, at)}. {AWAY}\n\n{item.job.message}"

    def hold(self, run, connector, tool, tool_class, arguments, now):
        """Keep a call that needs the away user's card pending; the model learns only that it waits."""
        spec = next(item for item in run.scenario.connectors if item.name == connector)
        owned = set(spec.tools.host_arguments.get(tool, {}))
        visible = {key: copy.deepcopy(value) for key, value in arguments.items() if key not in owned}
        key = digest(recordable({"connector": connector, "tool": tool, "arguments": visible}))
        waiting = next((item for item in self.pending if item.key == key and item.answered_at is None), None)
        if waiting is not None:
            waiting.repeats += 1
            self.counts["repeated_requests"] += 1
            self.event(run, "scheduler.approval_repeated", {"pending": waiting.id, "repeats": waiting.repeats,
                                                            "job": self.background["job"],
                                                            "run": self.background["run"]})
            return {"error": REPEATED}
        approvals = self.config.approvals
        due = now + timedelta(seconds=approvals.delay(len(self.pending))) if approvals.answer == "late" else None
        item = Pending(f"pending-{len(self.pending) + 1}", self.background["job"], self.background["run"], connector,
                       tool, tool_class, visible, key, now, due)
        self.pending.append(item)
        self.event(run, "scheduler.approval_pending", {
            "pending": item.id, "job": item.job, "run": item.run, "connector": connector, "tool": tool,
            "tool_class": tool_class, "arguments": visible, "requested_at": iso(now), "answer": approvals.answer,
            "due": iso(due), "basis": self.behavior_bases(BASIS)["background-approvals"]["basis"]})
        return {"error": PENDING}

    async def answer(self, run, item, at):
        """The away user answers a pending card: the host sends the held call through the approval path."""
        self.answering = item
        try:
            outcome = await run.call_tool(item.connector, item.tool, copy.deepcopy(item.arguments),
                                          source=f"scheduler:{item.job}")
        finally:
            self.answering, self.call = None, None
        item.answered_at, item.outcome = at, outcome["kind"]
        if outcome["kind"] == "result" and isinstance(outcome.get("result"), dict):
            self.observe(run, item.connector, item.tool, item.arguments, outcome["result"], model=False)
        self.event(run, "scheduler.approval_answered", {
            "pending": item.id, "job": item.job, "connector": item.connector, "tool": item.tool,
            "requested_at": iso(item.requested_at), "answered_at": iso(at),
            "delay_seconds": int((at - item.requested_at).total_seconds()), "outcome": outcome["kind"]})
        self.resumed(run, "approval", item.job, item.run)
        head = f"Approval update, {self.when(run, at)}, scheduled task {item.job}:"
        asked = f"the {self.shown_tool(run, item.connector, item.tool)} request you made at {self.short(run, item)}"
        kind = outcome["kind"]
        if kind == "result":
            lead = f"{head} your user approved {asked}, and the app sent it. Result: "
            return lead + await self.relay(run, item, outcome, RESUME_LIMIT - len(lead) - 16)
        if kind == "service-error":
            lead = f"{head} your user approved {asked}, and the app sent it; {item.connector} returned an error: "
            return lead + await self.relay(run, item, outcome, RESUME_LIMIT - len(lead) - 16)
        if kind == "declined":
            return f"{head} about {asked}: {outcome.get('message')}"
        if kind == "uncertain":
            return f"{head} your user approved {asked}, and the app sent it. {outcome.get('message')}"
        return f"{head} your user approved {asked}, but the app could not send it: {outcome.get('message')}"

    def reply(self, run, item, at):
        """The away user answers a question late, with what the scripted user says to it: the matched reply, or
        user.fallback for a question no reply matches (BASIS["background-questions"])."""
        item.answered_at = at
        self.event(run, "scheduler.question_answered", {
            "pending": item.id, "job": item.job, "requested_at": iso(item.requested_at), "answered_at": iso(at),
            "delay_seconds": int((at - item.requested_at).total_seconds())})
        self.resumed(run, "question", item.job, item.run)
        asked = item.question if len(item.question) <= QUESTION_EXCERPT else item.question[:QUESTION_EXCERPT - 1] + "…"
        said = item.scripted if item.scripted is not None else run.scenario.user.fallback
        return (f"Question answered, {self.when(run, at)}, scheduled task {item.job}: your user answered the question "
                f"you asked at {self.short(run, item)} (\"{asked}\"): {said}")

    def short(self, run, item):
        zone = self.zone(run)
        return (item.requested_at.astimezone(zone) if zone else item.requested_at).strftime("%Y-%m-%d %H:%M")

    async def relay(self, run, item, outcome, room):
        """A late result for the model, shown as a model call's result is (run.model_view: every extension's
        on_tool_result, content trust included, then masking and the result bound), within the `room` a resume
        message has left, so the JSON is never cut."""
        return await run.model_view(item.connector, item.tool, outcome, limit=max(room, 1))

    def scope(self, connector, tool, arguments):
        """What a Retry-After delay covers (rate_limit.scope)."""
        scope = self.config.rate_limit.scope
        return (connector,) if scope == "connector" else (connector, tool) if scope == "tool" else (
            connector, tool, digest(recordable(arguments)))

    def shown_tool(self, run, connector, tool):
        return f"{connector}/{tool}" if len(run.connector_names) > 1 else tool

    def tool_class(self, run, connector, tool):
        if not self.classes.get(connector):
            self.classes[connector] = dict(run.connector(connector).classes)
        return self.classes[connector].get(tool)

    # What the model's calls and results show ------------------------------------------------------
    def track(self, run, connector, tool, arguments, now):
        """A model call: Retry-After windows it falls in, expired references it repeats, renewals."""
        if self.config.rate_limit is not None:
            scope = self.scope(connector, tool, arguments)
            limit = self.limits.get(scope)
            if limit is not None:
                early = now < limit["until"]
                self.limit_counts["violations" if early else "honored"] += 1
                self.event(run, "scheduler.retry_after", {
                    "connector": connector, "tool": tool, "until": iso(limit["until"]), "at": iso(now),
                    "honored": not early, "retry_after_seconds": limit["seconds"]})
                if not early:
                    del self.limits[scope]
        for episode in self.expired:
            rule = episode["rule"]
            if episode["connector"] != connector:
                continue
            if tool == rule.tool and episode["reference"] is not None \
                    and same(pick(arguments, rule.reference), episode["reference"]):
                self.expiry_counts["stale_retries"] += 1
                episode["stale"] += 1
            elif tool in rule.renew and not episode["renewed"]:
                episode["renewed"] = True
                self.expiry_counts["renewed"] += 1

    def observe(self, run, connector, tool, arguments, result, *, model):
        """A result the model received (model) or the host's late dispatch received: rate limits, expiries, polls."""
        clock = self.clock(run)
        error = bool(result.get("isError"))
        rate = self.config.rate_limit
        if error and rate is not None and model:
            delay = pick(result, rate.retry_after)
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay >= 0:
                until = None
                if delay <= MAX_SPAN:
                    try:
                        until = clock.now() + timedelta(seconds=delay)
                    except OverflowError:  # Past the calendar: as malformed as a delay beyond the bound.
                        until = None
                if until is None:
                    # A service finding, never a host failure: no window is kept for a delay this emulation bounds.
                    self.limit_counts["malformed"] += 1
                    self.event(run, "scheduler.rate_limit_malformed", {
                        "connector": connector, "tool": tool, "retry_after_seconds": delay,
                        "detail": "Retry-After is longer than 30 days, this emulation's bound"})
                else:
                    scope = self.scope(connector, tool, arguments)
                    self.limits[scope] = {"until": until, "seconds": delay}
                    self.limit_counts["limited"] += 1
                    self.event(run, "scheduler.rate_limited", {"connector": connector, "tool": tool,
                                                               "retry_after_seconds": delay, "until": iso(until)})
        for rule in self.config.expiry:
            if error and tool == rule.tool and rule.connector in (None, connector) \
                    and same(pick(result, rule.error.pointer), rule.error.value):
                reference = pick(arguments, rule.reference)
                self.expired.append({"rule": rule, "connector": connector, "reference": reference, "renewed": False,
                                     "stale": 0})
                self.expiry_counts["expired"] += 1
                self.event(run, "scheduler.expired", {"connector": connector, "tool": tool, "at": iso(clock.now()),
                                                      "reference": reference, "host_dispatch": not model})
        if model and self.tool_class(run, connector, tool) == "read":
            self.read(run, connector, tool, arguments, result, error)

    def read(self, run, connector, tool, arguments, result, error):
        """A read the model received. Every read updates what the model already knows (the latest data and token
        per call); a background read is a poll: conditional or not, and whether it told the agent anything new."""
        conditional = self.config.polling.conditional
        token = pick(arguments, conditional.argument) if conditional else None
        sent = isinstance(token, str) and bool(token)
        target_arguments = without(arguments, conditional.argument) if conditional else arguments
        target = digest(recordable({"connector": connector, "tool": tool, "arguments": target_arguments}))
        polls = self.polls if self.background is not None else Counter()  # Foreground reads are no polls.
        polls["total"] += 1
        polls[f"tool:{self.shown_tool(run, connector, tool)}"] += 1
        if conditional:
            polls["conditional"] += sent
            if not sent and self.etags.get(target):
                polls["missed_conditional"] += 1  # The agent held a token for this read and did not send it.
        if error:
            polls["errors"] += 1
            return
        if conditional and isinstance(etag := pick(result, conditional.etag), str) and etag:
            self.etags[target] = etag  # A since-cursor moves on even when nothing changed.
        if conditional and conditional.unchanged(result):
            polls["not_modified"] += 1
            return
        for path in self.config.polling.volatile:
            result = without(result, path)
        # The data a read shows: structuredContent when the server sends it, else its content (held in memory
        # only: a digest of raw data in evidence would let an export test guesses of a private value).
        shown = result.get("structuredContent") if "structuredContent" in result else result.get("content")
        fingerprint = digest(recordable(shown))
        previous = self.last.get(target)
        polls["first" if previous is None else "wasted" if previous == fingerprint else "changed"] += 1
        self.last[target] = fingerprint

    # Checks ------------------------------------------------------------------------------------------
    def runs_check(self):
        schedules = (self.schedules or {}).values()
        ran = sum(item.runs for item in schedules)
        reached = sum(item.runs + item.missed for item in schedules)
        planned = sum(item.planned if item.cancelled is None else item.runs + item.missed for item in schedules)
        item = {"id": "scheduler.background_runs", "layer": "host", "expected": {"planned_slots": planned},
                "actual": {"runs": ran, "missed": reached - ran, "not_reached": max(planned - reached, 0)},
                "basis": self.behavior_bases(BASIS)["scheduled-work"]["basis"]}
        if not planned:
            return {**item, "status": "passed", "class": "verification", "required": True,
                    "detail": "Every scheduled task was cancelled before it ran"}
        if not ran:
            return {**item, "status": "unsupported", "class": "unsupported", "required": True,
                    "detail": "No scheduled task ran: the agent never stopped where the scheduler could resume it "
                              "(a scripted replay stops only at {\"idle\": true} steps)"}
        if reached < planned:
            return {**item, "status": "observation-gap", "class": "observation gap", "required": False,
                    "detail": f"{planned - reached} of {planned} scheduled slots never came: the run ended first (the "
                              f"agent resumes at most {MAX_RUNS} times and within its model turns)"}
        return {**item, "status": "passed", "class": "verification", "required": True,
                "detail": f"{ran} background run(s) over {planned} slot(s)" + (
                    f", {reached - ran} missed while the agent was busy" if reached > ran else "")}

    def retry_check(self):
        counts = self.limit_counts
        failed = counts["violations"] > 0
        return {"id": "scheduler.retry_after_respected", "layer": "agent", "status": "failed" if failed else "passed",
                "class": "interaction" if failed else "verification", "required": True,
                "expected": "no call to a rate-limited connector before its Retry-After delay passed",
                "actual": {key: counts[key] for key in ("limited", "honored", "violations")},
                "basis": self.behavior_bases(BASIS)["retry-after"]["basis"],
                "detail": (f"{counts['violations']} call(s) came before Retry-After passed on the fixture clock "
                           f"({counts['limited']} rate-limited response(s))") if failed else
                          f"{counts['limited']} rate-limited response(s); no call came before Retry-After passed"}


register("scheduler", SchedulerExtension)
