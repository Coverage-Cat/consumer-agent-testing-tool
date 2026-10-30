import json

import pytest
from test_connector_support import Hotel, events, run_connector, scenario

from consumer_agent_testing_tool.ext_behavior import BehaviorExtension
from consumer_agent_testing_tool.replay import playback


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_connector_journal_is_sealed_and_does_not_infer_effects(tmp_path, target):
    document = scenario(emulation_target=target, extensions={"behavior": {"principal": "synthetic-owner"}})
    run, code, hotel = await run_connector(tmp_path, document)
    assert code == 0, run.summary()
    assert len(hotel.bookings) == 1
    [record] = events(run, "behavior.journal")
    stages = [row["kind"] for row in record["events"]]
    assert stages == ["grant", "proposal", "authorized-dispatch", "transport-response"]
    assert "effect" not in stages
    assert playback(run.path)["integrity"] == "verified"


@pytest.mark.parametrize("target", ["muse", "instinct"])
@pytest.mark.parametrize("guard,bookings", [(True, 0), (False, 1)])
async def test_revocation_between_approval_and_dispatch(tmp_path, monkeypatch, guard, bookings, target):
    from consumer_agent_testing_tool.connector_run import EXTENSIONS
    class Revoker(BehaviorExtension):
        async def after_approval(self, run, connector, tool, arguments, decision):
            await super().after_approval(run, connector, tool, arguments, decision)
            if tool == "create_booking":
                self.revoke(connector, tool)
    monkeypatch.setitem(EXTENSIONS, "behavior", Revoker)
    document = scenario(emulation_target=target, extensions={"behavior": {"principal": "synthetic-owner", "dispatch_guard": guard}})
    run, code, hotel = await run_connector(tmp_path, document, registry={"behavior": Revoker})
    assert len(hotel.bookings) == bookings
    assert code == (1 if guard else 0), run.summary()  # Independent expected-booking check fails after veto.
    assert "acknowledged" not in json.dumps(events(run, "behavior.journal"))
    assert run.terminal is None
    assert run.final_text is not None


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_uncertain_connector_records_independent_reconciliation(tmp_path, target):
    document = scenario(emulation_target=target, extensions={"behavior": {"principal": "synthetic-owner"}})
    document["connectors"][0]["tools"]["reconcile"] = {
        "create_booking": {key: value for key, value in document["checks"][0].items() if key != "id"}}
    hotel = Hotel(uncertain_booking=True)
    run, _, _ = await run_connector(tmp_path, document, hotel=hotel)
    assert len(hotel.bookings) == 1
    [record] = events(run, "behavior.journal")
    assert any(row["kind"] == "reconciliation" for row in record["events"])
    effects = [row for row in record["events"] if row["kind"] == "effect"]
    assert len(effects) == 1
    assert effects[0]["data"]["identity_kind"] == "predicate"
    assert effects[0]["data"]["effect_id"] is None
