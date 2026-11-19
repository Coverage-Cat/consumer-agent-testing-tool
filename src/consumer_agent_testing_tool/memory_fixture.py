"""In-memory claim lifecycle for local surrogate tests with synthetic messages.

The fixture author supplies claim spans and scope. Verification, retrieval and
maintenance are deterministic CATT treatments. Only indexed context is handed
to the connector lane. Source messages and evidence spans stay in the fixture.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .behavior_clock import VirtualClock
from .connector_scenario import IDENTIFIER


@dataclass(frozen=True)
class Message:
    speaker: Literal["user", "assistant", "connector"]
    text: str
    at: datetime
    sequence: int


@dataclass
class Claim:
    id: str
    key: str
    scope: Literal["durable", "today", "hypothetical"]
    message_id: str
    start: int
    end: int
    staged_at: datetime
    status: str = "pending"
    supersedes: str | None = None


class MemoryFixture:
    """Use one instance per account and trial, across fresh connector runs."""

    def __init__(self, account: str, clock: VirtualClock):
        if not re.fullmatch(IDENTIFIER, account):
            raise ValueError("Memory fixture account must be an identifier")
        self.account, self.clock = account, clock
        self.messages: dict[str, Message] = {}
        self.claims: dict[str, Claim] = {}
        self.forgotten: dict[str, int] = {}
        self.retracted_spans: dict[str, set[tuple[int, int]]] = {}
        self.synthesis: list[str] = []
        self.slot = self._boundaries()

    def _boundaries(self):
        now = self.clock.utc()
        hour = now.replace(minute=0, second=0, microsecond=0)
        return {"hourly": hour, "nightly": hour.replace(hour=0)}

    def add_message(self, identifier, speaker, text):
        if identifier in self.messages or not re.fullmatch(IDENTIFIER, identifier):
            raise ValueError("Memory fixture messages need distinct identifiers")
        if speaker not in ("user", "assistant", "connector") or not text.strip():
            raise ValueError("Memory fixture messages need a speaker and text")
        self.messages[identifier] = Message(speaker, text, self.clock.utc(), len(self.messages) + 1)

    def stage(self, identifier, key, message_id, quote, *, scope="durable"):
        message = self.messages[message_id]
        if identifier in self.claims or not re.fullmatch(IDENTIFIER, identifier):
            raise ValueError("Memory fixture claims need distinct identifiers")
        if not key.strip() or scope not in ("durable", "today", "hypothetical"):
            raise ValueError("Memory fixture claims need a key and scope")
        start = message.text.find(quote)
        if not quote.strip() or start < 0 or message.text.find(quote, start + 1) >= 0:
            raise ValueError("A claim needs a unique supporting span from the original message")
        claim = Claim(identifier, key, scope, message_id, start, start + len(quote), self.clock.utc())
        if message.sequence <= self.forgotten.get(key, 0) or self._retracted_span(claim):
            claim.status = "retracted"
        self.claims[identifier] = claim

    def run_due(self):
        """Run each newly due UTC boundary once. Coalesce missed slots per job."""
        receipts = []
        for job, due in sorted(self._boundaries().items(), key=lambda item: item[1]):
            if due <= self.slot[job]:
                continue
            changed = self._verify(due) if job == "hourly" else self._synthesize()
            self.slot[job] = due
            receipts.append({"job": job, "due": due.isoformat(), "clock": self.clock.mode,
                             "outcome": "changed" if changed else "no-op", "claims": changed})
        return receipts

    def _verify(self, due):
        active = {claim.key: claim for claim in self.claims.values() if claim.status == "active"}
        pending = sorted((claim for claim in self.claims.values() if claim.status == "pending"),
                         key=lambda claim: self.messages[claim.message_id].sequence)
        changed = []
        for claim in pending:
            message = self.messages[claim.message_id]
            if claim.staged_at >= due:
                continue
            changed.append(claim.id)
            if message.speaker != "user" or claim.scope == "hypothetical":
                claim.status = "unverified"
            elif claim.scope == "today":
                claim.status = "daily"
            else:
                previous = active.get(claim.key)
                # A candidate staged late from an older message can still be historical.
                if previous and message.sequence < self.messages[previous.message_id].sequence:
                    claim.status = "superseded"
                    continue
                if previous:
                    previous.status = "superseded"
                    claim.supersedes = previous.id
                    changed.append(previous.id)
                claim.status = "active"
                active[claim.key] = claim
        return list(dict.fromkeys(changed))

    def _synthesize(self):
        current = [claim.id for claim in self.claims.values() if claim.status == "active"]
        changed = sorted(set(current) ^ set(self.synthesis))
        self.synthesis = current
        return changed

    def forget(self, key):
        self.forgotten[key] = len(self.messages)
        for claim in self.claims.values():
            if claim.key == key:
                self.retracted_spans.setdefault(claim.message_id, set()).add((claim.start, claim.end))
        retracted = []
        for claim in self.claims.values():
            if claim.status != "retracted" and (claim.key == key or self._retracted_span(claim)):
                claim.status = "retracted"
                retracted.append(claim.id)
        self.synthesis = [identifier for identifier in self.synthesis if identifier not in retracted]
        return retracted

    def _retracted_span(self, claim):
        return any(start < claim.end and claim.start < end
                   for start, end in self.retracted_spans.get(claim.message_id, ()))

    def _visible(self, claim, *, history=False):
        if claim.status == "daily":
            return self.messages[claim.message_id].at.date() == self.clock.utc().date()
        return claim.status == "active" or history and claim.status == "superseded"

    def _quote(self, claim):
        return self.messages[claim.message_id].text[claim.start:claim.end]

    def search(self, query, *, history=False):
        words = set(re.findall(r"\w+", query.casefold()))
        matches = []
        for claim in self.claims.values():
            if self._visible(claim, history=history):
                terms = re.findall(r"\w+", f"{claim.key} {self._quote(claim)}".casefold())
                if score := len(words.intersection(terms)):
                    matches.append((-score, claim.status != "active", claim.id))
        return [identifier for _, _, identifier in sorted(matches)]

    def explain(self, identifier, *, history=False):
        claim = self.claims.get(identifier)
        if claim is None or not self._visible(claim, history=history):
            return None
        return {"claim_id": claim.id, "message_id": claim.message_id,
                "speaker": self.messages[claim.message_id].speaker, "span": [claim.start, claim.end],
                "quote": self._quote(claim), "supersedes": claim.supersedes}

    def ledger(self):
        """Record verification states without source text or derived notes."""
        return {claim.id: {"status": claim.status,
                           "confidence": "confirmed" if claim.status in ("active", "superseded", "daily")
                           else "unverified" if claim.status == "unverified" else None}
                for claim in self.claims.values()}

    def briefing(self):
        """Nightly guidance that is still eligible for use, with evidence references."""
        return [self._record(self.claims[identifier]) for identifier in self.synthesis
                if self.claims[identifier].status == "active"]

    def _record(self, claim):
        scope = "Today only. " if claim.status == "daily" else ""
        return f"{scope}{self._quote(claim)} (source: {claim.message_id})"

    def indexed_context(self):
        """Build a fresh account-scoped snapshot from eligible claims."""
        return {"account": self.account, "sources": {"conversation": {"account": self.account}},
                "records": [{"id": claim.id, "source": "conversation", "text": self._record(claim)}
                            for claim in self.claims.values() if self._visible(claim)]}
