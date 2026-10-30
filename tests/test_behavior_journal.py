import pytest

from consumer_agent_testing_tool.errors import LabError


def journal(path):
    from consumer_agent_testing_tool.behavior_journal import ActionJournal
    return ActionJournal(path)


def grant(db):
    return db.grant("grant", principal="owner", service="fixture", action="book",
                    terms={"total": 120})


def propose(db, action_id="intent", route="web"):
    return db.propose(action_id, grant_id="grant", principal="owner", service="fixture",
                      action="book", terms={"total": 120}, route=route)


def test_lost_response_reconciles_one_effect_without_retry(tmp_path):
    with journal(tmp_path / "state.sqlite") as db:
        grant(db)
        propose(db)
        db.dispatch("intent")
        db.response("intent", received=False)
        db.reconcile("intent", "committed", evidence="ledger-read", effect_id="booking-1")
        with pytest.raises(LabError):
            db.dispatch("intent", recovery=True)
        assert len(db.events("intent", "effect")) == 1


def test_recovery_needs_noncommitment_and_current_grant(tmp_path):
    with journal(tmp_path / "state.sqlite") as db:
        grant(db)
        propose(db)
        db.dispatch("intent")
        db.response("intent", received=False)
        with pytest.raises(LabError):
            db.dispatch("intent", recovery=True)
        db.reconcile("intent", "not-committed", evidence="ledger-read")
        with pytest.raises(LabError):
            db.dispatch("intent")
        db.dispatch("intent", recovery=True)
        db.reconcile("intent", "not-committed", evidence="second-read")
        db.revoke("grant")
        with pytest.raises(LabError, match="revoked"):
            db.dispatch("intent", recovery=True)


def test_identical_arguments_are_separate_intents_but_replay_is_not(tmp_path):
    with journal(tmp_path / "state.sqlite") as db:
        grant(db)
        propose(db, "web")
        propose(db, "contact", "contact")
        for action_id in ("web", "contact"):
            db.dispatch(action_id)
            db.reconcile(action_id, "committed", evidence="ledger", effect_id=action_id)
        assert len(db.events(kind="effect")) == 2
        propose(db, "web")
        with pytest.raises(LabError):
            db.dispatch("web")


def test_revocation_survives_restart_and_route_change(tmp_path):
    path = tmp_path / "state.sqlite"
    with journal(path) as db:
        grant(db)
        propose(db)
        db.revoke("grant")
    with journal(path) as db:
        with pytest.raises(LabError, match="revoked"):
            db.dispatch("intent")
        with pytest.raises(LabError):
            grant(db)
        with pytest.raises(LabError):
            propose(db, route="contact")


def test_out_of_order_delivery_is_not_read_or_acknowledged(tmp_path):
    with journal(tmp_path / "state.sqlite") as db:
        db.message("stop", task_id="task", reply_to="question", stage="served",
                   source="room-fetch", timestamp="2026-10-30T12:00:00Z", precision="second")
        db.message("stop", task_id="task", reply_to="question", stage="accepted",
                   source="room-fetch", timestamp="2026-10-30T11:59:00Z", precision="second")
        assert {event["data"]["stage"] for event in db.events("stop")} == {"served", "accepted"}
        with pytest.raises(LabError, match="association"):
            db.message("stop", task_id="other", reply_to="question", stage="acknowledged",
                       source="operator-copy", timestamp="2026-10-30T12:01:00Z", precision="minute")


@pytest.mark.parametrize("committed,attempts", [(True, 1), (False, 2)])
async def test_recovery_adapter_reads_before_optional_retry(tmp_path, committed, attempts):
    from consumer_agent_testing_tool.behavior_journal import perform_action
    calls = []
    async def send():
        calls.append("send")
        if len(calls) == 1:
            raise TimeoutError
        return {"received": True}
    async def read():
        calls.append("read")
        return {"outcome": "committed" if committed else "not-committed",
                "evidence": "independent-ledger", **({"effect_id": "booking-1"} if committed else {})}
    with journal(tmp_path / "state.sqlite") as db:
        grant(db)
        propose(db)
        result = await perform_action(db, "intent", send, read, allow_recovery=True)
        assert calls[:2] == ["send", "read"]
        assert calls.count("send") == attempts
        assert result["effect"] == ("committed" if committed else "unknown")
