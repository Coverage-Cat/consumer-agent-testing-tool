"""Convenience fixture API backed by the shared reservation service.

There is one reservation/inventory/idempotency/grant implementation. This module
retains the small seed/execute helpers used by deterministic consent tests.
"""
from .reservation_fixture import TRANSACTION_SCHEMA as SCHEMA  # noqa: F401 — public schema alias
from .reservation_fixture import ReservationFixture


class TransactionFixture(ReservationFixture):
    def seed(self, clock="2026-10-01T17:00:00Z"):
        self.reset(clock)
        return self.dispatch("alice", "reservations.create", {
            "slot_id": "afternoon", "contact": "Synthetic shopper", "idempotency_key": "seed-draft"})

    def execute(self, operation, arguments, grant, *, now, principal="alice", lose_response=False):
        self.advance(now)
        name = {"commit": "reservations.confirm", "cancel": "reservations.cancel"}[operation]
        if lose_response:
            self.schedule_fault(name, arguments["idempotency_key"], "response_lost")
        return self.dispatch(principal, name, arguments, grant_id=grant)
