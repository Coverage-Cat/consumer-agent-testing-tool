"""Opt-in action journal and revocation guard for local connector/browser trials."""
from typing import ClassVar

from pydantic import Field, model_validator

from .behavior_journal import ActionJournal
from .config import Strict
from .connector_run import BaseExtension, register
from .errors import DispatchVeto, LabError
from .serialization import digest


class ScopeRoute(Strict):
    service: str = Field(min_length=1)
    action: str = Field(min_length=1)


class BehaviorConfig(Strict):
    principal: str = Field(min_length=1)
    dispatch_guard: bool = True
    scopes: dict[str, list[ScopeRoute]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unambiguous_scopes(self):
        routes = [(row.service, row.action) for rows in self.scopes.values() for row in rows]
        if any(not name or not rows for name, rows in self.scopes.items()) or len(routes) != len(set(routes)):
            raise ValueError("Named scopes need distinct routes and nonempty names")
        return self


class BehaviorExtension(BaseExtension):
    name = "behavior"
    config_model = BehaviorConfig
    bases: ClassVar[dict] = {"journal": "assumed", "revocation": "assumed"}

    def __init__(self, config):
        super().__init__(config)
        self.journal = None
        self.pending = {}
        self.revoked = set()
        self.grants = {}
        self.count = 0

    def prepare(self, run):
        if self.journal is not None:
            return
        self.journal = ActionJournal(run.path / "private" / "actions.sqlite")
        run.event("behavior.configured", {"dispatch_guard": self.config.dispatch_guard},
                  provenance="explicit local surrogate treatment")

    def tools(self, run):
        self.prepare(run)
        return []

    def revoke(self, service, action):
        """Host-only scope change; never exposed as a model tool."""
        target = self.scope(service, action)
        self.revoked.add(target)
        for identifier, scope in self.grants.items():
            if self.config.dispatch_guard and scope == target:
                self.journal.revoke(identifier)

    def scope(self, service, action):
        for name, routes in self.config.scopes.items():
            if any((row.service, row.action) == (service, action) for row in routes):
                return ("named-scope", name)
        return (service, action)

    def propose(self, service, action, terms, *, route):
        self.count += 1
        identifier = f"action-{self.count}"
        service, action = self.scope(service, action)
        if self.config.dispatch_guard and (service, action) in self.revoked:
            raise DispatchVeto("Scoped authorization was revoked")
        # Each separately authorized invocation is a distinct intent.
        self.journal.grant(identifier, principal=self.config.principal, service=service,
                           action=action, terms=terms)
        self.grants[identifier] = (service, action)
        self.journal.propose(identifier, grant_id=identifier, principal=self.config.principal,
                             service=service, action=action, terms=terms, route=route)
        return identifier

    async def after_approval(self, run, connector, tool, arguments, decision):
        if decision["allowed"] and run.connections[connector].tool_class(tool) != "read":
            identifier = self.propose(connector, tool, arguments, route="connector")
            self.pending[(connector, decision["request"])] = (identifier, digest(arguments))

    def dispatch_gate(self, run, connector, tool, arguments, *, request):
        if run.connections[connector].tool_class(tool) == "read":
            return None
        pending = self.pending.pop((connector, request), None)
        if pending is None:
            raise LabError("No pending journal proposal for this request", code=3)
        identifier, bound = pending
        if bound != digest(arguments):
            raise LabError("Authorized action changed before dispatch", code=3)
        self.journal.dispatch(identifier)
        run.event("behavior.authorized_dispatch", {"action_id": identifier, "service": connector,
                                                   "action": tool, "dispatch_guard": self.config.dispatch_guard})
        return identifier

    def browser_dispatch(self, run, service, action, arguments):
        identifier = self.propose(service, action, arguments, route="browser")
        self.journal.dispatch(identifier)
        run.event("behavior.authorized_dispatch", {"action_id": identifier, "route": "browser",
                                                   "dispatch_guard": self.config.dispatch_guard})
        return identifier

    def transport_response(self, identifier, received):
        self.journal.response(identifier, received=received)

    def reconciliation(self, identifier, observation):
        outcome = "committed" if observation.get("effect_observed") is True else (
            "not-committed" if observation.get("effect_observed") is False else "uncertain")
        self.journal.reconcile(identifier, outcome, evidence=observation["id"], effect_identity="predicate")

    def cleanup(self, run):
        if self.journal is not None:
            self.journal.abandon_proposals()
            run.event("behavior.journal", {"events": self.journal.events()},
                      provenance="local surrogate journal; transport receipts do not prove effects")
            self.journal.db.close()
            self.journal = None


register("behavior", BehaviorExtension)
