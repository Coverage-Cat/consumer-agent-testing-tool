"""Who asked for a call (the model, or an extension acting for the user) and tools classified mid-task."""
import pytest
from test_connector_multi_support import STAY, ready
from test_connector_support import Hotel, events, scenario, services
from test_permissions_engine import CLASSIFIED, engine, everyone

from consumer_agent_testing_tool import connector_scenario, permissions
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.permissions import (
    UNANSWERED,
    Classification,
    card_record,
    classify_tool,
)

AWAY = {"id": "away-user", "when": {"field": "origin", "op": "ne", "value": "model"}, "decision": "no_response"}


def test_cards_record_who_asked_and_rules_can_match_the_origin():
    gate = engine(rules=[AWAY, everyone("allow_once")])
    own = gate.request("save_favorite", {"room": "r1"})
    background = gate.request("save_favorite", {"room": "r2"}, origin="extension:scheduler:price-watch")
    assert own.allowed and own.card.origin == "model" and "origin" not in card_record(own.card)
    assert not background.allowed and background.model_message == UNANSWERED
    assert background.card.origin == "extension:scheduler:price-watch"
    assert card_record(background.card)["origin"] == "extension:scheduler:price-watch"
    assert [card.get("origin") for card in gate.state()["cards"]] == [None, "extension:scheduler:price-watch"]
    for origin in ("extension:", "Extension:wallet", "user", 3):
        with pytest.raises(LabError, match="origin is model or extension"):
            gate.request("save_favorite", {"room": "r3"}, origin=origin)
    for value in ("wallet", "extension:Wallet"):
        with pytest.raises(LabError, match="origin is model or extension"):
            permissions.validate_predicate({"field": "origin", "op": "eq", "value": value})
    permissions.validate_predicate({"field": "origin", "op": "in", "value": ["model", "extension:wallet"]})
    # The scenario grammar mirrors the engine's.
    assert connector_scenario.ORIGIN.pattern == permissions.ORIGIN.pattern
    assert connector_scenario.CARD_FIELDS == permissions.CARD_FIELDS


def test_scenario_rules_validate_the_origin_field(tmp_path):
    for value, ok in (("extension:wallet", True), ("model", True), ("wallet", False)):
        document = scenario(permissions={"rules": [{**AWAY, "when": {"field": "origin", "op": "eq", "value": value}}]})
        if ok:
            parse_scenario(document, base=tmp_path)
        else:
            with pytest.raises(LabError, match="origin is model or extension:<source>"):
                parse_scenario(document, base=tmp_path)


async def test_extension_calls_reach_the_scripted_user_with_their_origin(tmp_path):
    document = scenario()
    document["permissions"]["rules"].insert(0, AWAY)
    run = await ready(tmp_path, document, services(Hotel()))
    outcome = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="scheduler")
    assert outcome == {"kind": "declined", "message": UNANSWERED}  # The away user leaves background cards open.
    card = events(run, "approval_card")[0]
    assert card["origin"] == "extension:scheduler" and events(run, "approval_decision")[0]["allowed"] is False


def test_add_classification_adds_a_tool_first_seen_mid_task():
    gate = engine(rules=[everyone("allow_once")])
    operation = Classification(tool="POST /v1/bookings", tool_class="sensitive-write", basis="heuristic",
                               reasons=["booking path"])
    assert gate.add_classification("POST /v1/bookings", operation) == operation
    assert gate.add_classification("POST /v1/bookings", operation.model_dump()) == operation  # The same: a no-op.
    assert gate.request("POST /v1/bookings", {"quote": "q1"}).card.offered == ["allow_once", "deny"]
    with pytest.raises(LabError, match="already classified sensitive-write"):
        gate.add_classification("POST /v1/bookings", {**operation.model_dump(), "tool_class": "read"})
    with pytest.raises(LabError, match="names a different tool"):
        gate.add_classification("GET /v1/rooms", operation)
    assert set(gate.classifications) == {*CLASSIFIED, "POST /v1/bookings"}
    assert classify_tool({"name": "x"}).tool_class == "write"
