"""Private, in-memory browser state leases. No conversation or grant persistence."""
import copy
import json
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .config import Strict
from .errors import LabError


class SessionPolicy(Strict):
    mode: Literal["task", "conversation"] = "task"
    principal: str | None = Field(default=None, min_length=1)
    lifetime_seconds: int = Field(default=1800, ge=1, le=86400)

    @model_validator(mode="after")
    def scoped(self):
        if self.mode not in ("task", "conversation"):
            raise ValueError("Session mode is task or conversation")
        if self.mode == "conversation" and not self.principal:
            raise ValueError("Conversation sessions require an explicit principal")
        return self


class SessionLease:
    def __init__(self, study, principal, origins, lifetime, *, monotonic=time.monotonic):
        self.key = (study, principal, frozenset(origins))
        self.clock = monotonic
        self.expires = monotonic() + lifetime
        self.state = None
        self.disconnected = False
        self.last_outcome = "new"

    def save(self, state):
        if self.disconnected:
            raise LabError("Disconnected browser session cannot be saved", code=3)
        if self.clock() >= self.expires:
            self.state, self.last_outcome = None, "expired"
            return
        if not isinstance(state, dict) or set(state) != {"cookies", "origins"}:
            raise LabError("Invalid private browser storage")
        if len(json.dumps(state).encode()) > 1024 * 1024:
            raise LabError("Private browser storage exceeds its bound")
        allowed = self.key[2]
        hosts = {urlsplit(origin).hostname for origin in allowed}
        if any(row.get("origin") not in allowed for row in state["origins"]):
            raise LabError("Private browser storage contains another origin", code=3)
        # A domain cookie may cover a parent domain. Keep only exact allowed
        # hosts; broadening a lease to sibling origins is never implicit.
        if any(row.get("domain", "").lstrip(".") not in hosts for row in state["cookies"]):
            raise LabError("Private browser cookies contain another domain", code=3)
        self.state = copy.deepcopy(state)

    def restore(self, study, principal, origins):
        if self.key != (study, principal, frozenset(origins)):
            raise LabError("Browser session belongs to another principal or origin scope", code=3)
        if self.disconnected:
            self.last_outcome = "disconnected"
            return None
        if self.clock() >= self.expires:
            self.state, self.last_outcome = None, "expired"
            return None
        self.last_outcome = "restored" if self.state is not None else "new"
        return copy.deepcopy(self.state)

    def disconnect(self):
        self.disconnected, self.state, self.last_outcome = True, None, "disconnected"
