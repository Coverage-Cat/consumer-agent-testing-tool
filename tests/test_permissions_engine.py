"""Approval engine: Muse guidelines 3.3/3.4 prompts and grants, Help Center modes, scripted user."""
import json

import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.consent import digest
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.permissions import (
    CHOICE_SOURCE,
    CHOICES,
    DECLINED,
    HELP_CENTER,
    KINDS,
    RULE_BASIS,
    UNANSWERED,
    ApprovalCard,
    Decision,
    PermissionEngine,
    PermissionPolicy,
    Ticket,
    binding_sha256,
    classify_tool,
    missing_disclosure,
)

ONCE_ONLY = ["allow_once", "deny"]
TOOLS = [
    {"name": "search_rooms", "annotations": {"readOnlyHint": True}},
    {"name": "get_booking", "annotations": {"readOnlyHint": True}},
    {"name": "save_favorite", "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "create_booking", "annotations": {"destructiveHint": True}},
]
CLASSIFIED = {tool["name"]: classify_tool(tool) for tool in TOOLS}
ZERO = {"read": 0, "write": 0, "sensitive-write": 0}


def everyone(decision):
    """A scripted user who gives the same answer on every card."""
    return {"id": f"user-{decision}", "when": {"field": "tool", "op": "exists", "value": True}, "decision": decision}


def engine(mode="platform-default", rules=(), classifications=None, **policy):
    return PermissionEngine(PermissionPolicy(mode=mode, rules=list(rules), **policy),
                            classifications or CLASSIFIED, "cedar-stays")


@pytest.mark.parametrize("answer,prompts", [
    ("allow_once", {"read": 3, "write": 3, "sensitive-write": 3}),
    ("always_allow", {"read": 1, "write": 1, "sensitive-write": 3}),
    ("allow_task", {"read": 1, "write": 1, "sensitive-write": 3}),
])
def test_platform_default_prompt_counts_follow_the_guidelines_table(answer, prompts):
    # 3.3: reads and writes ask on first use, then on each use unless Always allow; sensitive: every use.
    permissions = engine(rules=[everyone(answer)])
    for tool in ("get_booking", "save_favorite", "create_booking"):
        for call in range(3):
            assert permissions.request(tool, {"id": f"b{call}"}).allowed
    metrics = permissions.metrics()
    assert metrics["prompts"] == prompts
    assert metrics["auto_allowed"] == {name: 3 - count for name, count in prompts.items()}
    assert metrics["coerced_scopes"] == (0 if answer == "allow_once" else 3)
    assert metrics["redundant_prompts"] == 0


@pytest.mark.parametrize("scripted", ["allow_task", "always_allow"])
def test_sensitive_writes_ask_every_use_and_never_take_a_wider_scope(scripted):
    permissions = engine(rules=[everyone(scripted)])
    for _ in range(3):
        ticket = permissions.request("create_booking", {"quote_id": "q1"})
        assert ticket.allowed and ticket.scope == "once"
        assert ticket.card.offered == ONCE_ONLY
        assert ticket.decision == Decision(kind="allow_once", rule=f"user-{scripted}", coerced_from=scripted)
    state = permissions.state()
    assert [row["note"] for row in state["decisions"]] == [
        "scope not offered: sensitive writes offer only Allow once and Deny (guidelines 3.3)"] * 3
    assert {row["basis"] for row in state["decisions"]} == {"sensitive-every-use"}
    assert [(grant["scope"], grant["status"]) for grant in state["grants"]] == [("once", "consumed")] * 3
    assert permissions.metrics()["coerced_scopes"] == 3
    assert permissions.metrics()["redundant_prompts"] == 2


def test_a_saved_always_allow_for_a_sensitive_write_is_a_setup_error():
    with pytest.raises(LabError, match="cannot be set to Always allow") as caught:
        engine(persisted_always=["create_booking"])
    assert caught.value.code == 2


def test_a_saved_always_allow_skips_the_prompt_for_that_tool_only():
    permissions = engine(persisted_always=["save_favorite"])
    ticket = permissions.request("save_favorite", {"room": "r1"})
    assert ticket.allowed and ticket.card is None and ticket.decision is None and ticket.scope == "always"
    row = permissions.state()["requests"][0]
    assert (row["via"], row["grant_id"], row["basis"]) == ("grant", "grant-1", "always-allow")
    assert permissions.request("get_booking", {"id": "b1"}).card.offered == CHOICES


def test_ask_for_some_asks_before_writes_and_important_reads_only():
    permissions = engine("ask-for-some", [everyone("allow_once")], important_reads=["get_booking"])
    silent = permissions.request("search_rooms", {"adults": 2})
    assert silent.allowed and (silent.card, silent.decision, silent.scope) == (None, None, None)
    assert permissions.request("get_booking", {"id": "b1"}).card.offered == CHOICES
    assert permissions.request("save_favorite", {"room": "r1"}).card.offered == CHOICES
    assert permissions.request("create_booking", {"quote_id": "q1"}).card.offered == ONCE_ONLY
    metrics = permissions.metrics()
    assert metrics["prompts"] == {"read": 1, "write": 1, "sensitive-write": 1}
    assert metrics["auto_allowed"] == {**ZERO, "read": 1}
    requests = permissions.state()["requests"]
    assert [(row["via"], row["basis"]) for row in requests] == [
        ("mode", "ask-for-some"), ("prompt", "ask-for-some"), ("prompt", "ask-for-some"),
        ("prompt", "sensitive-every-use")]


def test_important_reads_change_nothing_where_reads_already_ask():
    permissions = engine(rules=[everyone("allow_once")], important_reads=["get_booking"])
    assert permissions.request("search_rooms", {}).card is not None
    assert permissions.request("get_booking", {}).card is not None


def test_always_ask_asks_before_every_action_and_honors_no_grant():
    permissions = engine("always-ask", [everyone("always_allow")], persisted_always=["save_favorite"])
    for tool in ("search_rooms", "search_rooms", "save_favorite", "save_favorite", "create_booking"):
        ticket = permissions.request(tool, {})
        assert ticket.card.offered == ONCE_ONLY and ticket.scope == "once"
        assert (ticket.decision.kind, ticket.decision.coerced_from) == ("allow_once", "always_allow")
    metrics = permissions.metrics()
    assert metrics["prompts"] == {"read": 2, "write": 2, "sensitive-write": 1}
    assert metrics["auto_allowed"] == ZERO and metrics["coerced_scopes"] == 5
    state = permissions.state()
    assert state["honors_grants"] is False
    assert {row["note"] for row in state["decisions"]} == {
        "scope not offered: Always ask honors no task or Always allow grant",
        "scope not offered: sensitive writes offer only Allow once and Deny (guidelines 3.3)"}


def test_task_and_always_grants_cover_later_calls_with_other_details():
    permissions = engine(rules=[
        {"id": "favorites", "when": {"field": "tool", "op": "eq", "value": "save_favorite"}, "decision": "allow_task"},
        {"id": "lookups", "when": {"field": "tool", "op": "eq", "value": "get_booking"}, "decision": "always_allow"}])
    first = permissions.request("save_favorite", {"room": "r1"})
    assert first.scope == "task" and first.card.offered == CHOICES and first.decision.rule == "favorites"
    later = permissions.request("save_favorite", {"room": "r2"})
    assert later.allowed and later.card is None and later.scope == "task"
    assert permissions.request("get_booking", {"id": "b1"}).scope == "always"
    assert permissions.request("get_booking", {"id": "b2"}).card is None
    grants = permissions.state()["grants"]
    assert [(row["tool"], row["scope"], row["status"], row["card_id"]) for row in grants] == [
        ("save_favorite", "task", "active", "card-1"), ("get_booking", "always", "active", "card-2")]
    assert permissions.metrics()["auto_allowed"] == {**ZERO, "read": 1, "write": 1}


@pytest.mark.parametrize("tool", ["save_favorite", "create_booking"])
def test_allow_once_covers_one_call_and_changed_details_need_a_fresh_card(tool):
    permissions = engine(rules=[everyone("allow_once")])
    arguments, quote = {"quote_id": "q1", "guest_name": "Taylor"}, {"total_minor": 41800, "currency": "USD"}
    tickets = [permissions.request(tool, arguments, disclosure=quote),
               permissions.request(tool, arguments, disclosure=quote),
               permissions.request(tool, {**arguments, "quote_id": "q2"}, disclosure=quote),
               permissions.request(tool, arguments, disclosure={**quote, "total_minor": 46800})]
    cards = [ticket.card for ticket in tickets]
    assert [card.card_id for card in cards] == ["card-1", "card-2", "card-3", "card-4"]
    assert cards[0].binding_sha256 == cards[1].binding_sha256 == permissions.binding(tool, arguments, quote)
    assert len({card.binding_sha256 for card in cards}) == 3
    state = permissions.state()
    assert [row["repeat_of"] for row in state["requests"]] == [None, "card-1", None, None]
    assert all((grant["scope"], grant["status"]) == ("once", "consumed") for grant in state["grants"])
    assert [grant["binding_sha256"] for grant in state["grants"]] == [card.binding_sha256 for card in cards]
    assert permissions.metrics()["redundant_prompts"] == 1


def test_the_binding_is_the_consent_digest_of_connector_tool_arguments_and_disclosure():
    arguments = {"quote_id": "q1", "guest_email": "taylor@example.com", "adults": 2.5}
    permissions = engine()
    ticket = permissions.request("create_booking", arguments, disclosure={"total_minor": 41800},
                                 private_paths={"/guest_email"})
    covered = {"connector": "cedar-stays", "tool": "create_booking", "arguments": arguments,
               "disclosure": {"total_minor": 41800}}
    # Keyed: the RFC 8785 form under the engine's in-memory key, never the plain digest evidence could test.
    assert ticket.card.binding_sha256 == binding_sha256(**covered, key=permissions._key)
    assert ticket.card.binding_sha256 != digest(covered) == binding_sha256(**covered)
    assert permissions.state()["binding"]["digest"].startswith("HMAC-SHA256")
    other = permissions.request("create_booking", {**arguments, "guest_email": "sam@example.com"},
                                disclosure={"total_minor": 41800}, private_paths={"/guest_email"})
    # Both cards show [private]; the binding still covers the real value.
    assert other.card.arguments == ticket.card.arguments
    assert other.card.binding_sha256 != ticket.card.binding_sha256
    # Another engine (another trial) binds the same call differently: nothing to compare across trials.
    assert engine().request("create_booking", arguments, disclosure={"total_minor": 41800},
                            private_paths={"/guest_email"}).card.binding_sha256 != ticket.card.binding_sha256


def test_edits_dispatch_changed_model_visible_arguments_and_record_both():
    rule = {"id": "fewer-adults", "when": {"field": "arguments/adults", "op": "gt", "value": 2},
            "decision": "edit", "edits": {"/adults": 2, "/room/view": "garden", "/note": "late arrival"}}
    permissions = engine(rules=[rule])
    proposed = {"quote_id": "q1", "adults": 3, "room": {"view": "street", "floor": 1},
                "guest_email": "taylor@example.com"}
    ticket = permissions.request("create_booking", proposed, private_paths={"/guest_email"})
    assert ticket.allowed and ticket.scope == "once"
    assert ticket.decision == Decision(kind="edit", edits=rule["edits"], rule="fewer-adults")
    assert ticket.arguments == {**proposed, "adults": 2, "room": {"view": "garden", "floor": 1},
                                "note": "late arrival"}
    assert ticket.card.arguments == {**proposed, "guest_email": "[private]"}  # What the user was shown.
    decision = permissions.state()["decisions"][0]
    assert decision["edits"] == rule["edits"] and decision["basis"] == "edit"
    assert decision["dispatched_arguments"] == {**ticket.arguments, "guest_email": "[private]"}
    assert decision["dispatched_sha256"] == permissions.binding("create_booking", ticket.arguments, {})
    assert decision["dispatched_sha256"] != ticket.card.binding_sha256
    assert permissions.state()["grants"][0]["binding_sha256"] == decision["dispatched_sha256"]
    assert permissions.metrics()["decisions"]["edit"] == 1


@pytest.mark.parametrize("path", ["/guest/email", "/guest", "/guest/email/domain"])
def test_edits_never_reach_host_private_arguments(path):
    rule = {"id": "edit", "when": {"field": "tool", "op": "exists", "value": True}, "decision": "edit",
            "edits": {path: "attacker@example.com"}}
    permissions = engine(rules=[rule])
    with pytest.raises(LabError, match="host-private argument"):
        permissions.request("create_booking", {"guest": {"email": "taylor@example.com", "name": "Taylor"}},
                            private_paths={"/guest/email"})
    assert permissions.state()["cards"] == [] and permissions.metrics()["prompts"] == ZERO


@pytest.mark.parametrize("edits", [{"/room/view": "garden"}, {"/rooms/3": "r9"}])
def test_an_edit_outside_the_proposed_arguments_fails_with_guidance(edits):
    permissions = engine(rules=[{"id": "edit", "when": {"field": "tool", "op": "exists", "value": True},
                                 "decision": "edit", "edits": edits}])
    with pytest.raises(LabError, match="exists predicate"):
        permissions.request("create_booking", {"quote_id": "q1", "rooms": ["r1"]})


def test_cards_show_host_private_arguments_as_private_and_rules_see_the_card():
    rule = {"id": "masked", "decision": "allow_once", "when": {"all": [
        {"field": "arguments/guest_email", "op": "eq", "value": "[private]"},
        {"field": "arguments/travellers/1/passport", "op": "eq", "value": "[private]"}]}}
    permissions = engine(rules=[rule])
    arguments = {"quote_id": "q1", "guest_email": "taylor@example.com", "payment": {"token": "tok_live_1234567"},
                 "travellers": [{"name": "A"}, {"name": "B", "passport": "X1234567"}]}
    ticket = permissions.request("create_booking", arguments, private_paths={
        "/guest_email", "/payment/token", "/travellers/1/passport", "/missing/value"})
    assert ticket.allowed and ticket.decision.rule == "masked"
    assert ticket.card.arguments == {"quote_id": "q1", "guest_email": "[private]", "payment": {"token": "[private]"},
                                     "travellers": [{"name": "A"}, {"name": "B", "passport": "[private]"}]}
    assert ticket.arguments == arguments  # Dispatch gets the real values.
    recorded = json.dumps(permissions.state())
    for value in ("taylor@example.com", "tok_live_1234567", "X1234567"):
        assert value not in recorded
    assert permissions.state()["requests"][0]["private_paths"] == [
        "/guest_email", "/missing/value", "/payment/token", "/travellers/1/passport"]


def test_declined_and_unanswered_calls_are_not_dispatched_and_the_model_learns_only_that():
    permissions = engine(rules=[{"id": "no-bookings", "when": {"field": "tool", "op": "eq",
                                                               "value": "create_booking"}, "decision": "deny"}],
                         default="no_response")
    denied = permissions.request("create_booking", {"quote_id": "q1"}, disclosure={"total_minor": 41800})
    assert (denied.allowed, denied.scope, denied.model_message) == (False, None, DECLINED)
    assert DECLINED == "The user declined this action."
    unanswered = permissions.request("save_favorite", {"room": "r1"})
    assert not unanswered.allowed and unanswered.decision == Decision(kind="no_response")
    assert unanswered.model_message == UNANSWERED and "not taken" in UNANSWERED
    for ticket in (denied, unanswered):
        assert not any(value in ticket.model_message for value in ("q1", "41800", "r1", "card-"))
    state = permissions.state()
    assert state["grants"] == []
    assert [(row["allowed"], row["dispatched_sha256"]) for row in state["requests"]] == [(False, None)] * 2
    assert [row["basis"] for row in state["decisions"]] == ["deny", "no-response"]
    assert permissions.metrics()["decisions"] == {**dict.fromkeys(KINDS, 0), "deny": 1, "no_response": 1}


def test_a_denial_is_not_remembered():
    permissions = engine(rules=[everyone("deny")])
    permissions.request("get_booking", {"id": "b1"})
    again = permissions.request("get_booking", {"id": "b1"})
    assert again.card.card_id == "card-2" and not again.allowed
    assert permissions.metrics()["redundant_prompts"] == 1


def test_the_first_matching_rule_answers_and_the_default_covers_the_rest():
    permissions = engine(rules=[
        {"id": "expensive", "when": {"field": "disclosure/total_minor", "op": "gt", "value": 50000},
         "decision": "deny"},
        {"id": "bookings", "when": {"field": "tool", "op": "eq", "value": "create_booking"},
         "decision": "allow_once"}])
    assert permissions.request("create_booking", {}, disclosure={"total_minor": 60000}).decision.rule == "expensive"
    assert permissions.request("create_booking", {}, disclosure={"total_minor": 40000}).decision.rule == "bookings"
    fallback = permissions.request("save_favorite", {})
    assert fallback.decision == Decision(kind="deny") and fallback.model_message == DECLINED


def test_hidden_writes_and_unknown_tools_are_asked_even_when_reads_are_not():
    tools = [{"name": "search_rooms", "annotations": {"readOnlyHint": True}},
             {"name": "get_quote", "description": "Gets a quote and books the room.",
              "annotations": {"readOnlyHint": True}},
             {"name": "weather"}]
    permissions = engine("ask-for-some", [everyone("always_allow")],
                         {tool["name"]: classify_tool(tool) for tool in tools})
    assert permissions.request("search_rooms", {}).card is None
    hidden = permissions.request("get_quote", {"room_id": "r1"})
    assert hidden.card.tool_class == "sensitive-write" and hidden.card.offered == ONCE_ONLY
    assert hidden.decision.coerced_from == "always_allow"
    unknown = permissions.request("weather", {"city": "Seattle"})
    assert unknown.card.tool_class == "write" and unknown.scope == "always"


def test_state_records_every_card_decision_and_request_as_json():
    permissions = engine("ask-for-some", [everyone("allow_once")])
    permissions.request("search_rooms", {})
    permissions.request("save_favorite", {"room": "r1"})
    state = json.loads(json.dumps(permissions.state()))
    assert set(state) == {"connector", "policy", "honors_grants", "classifications", "binding", "grants", "cards",
                          "decisions", "requests", "basis"}
    assert (len(state["cards"]), len(state["decisions"]), len(state["requests"])) == (1, 1, 2)
    assert state["cards"][0] == {
        "card_id": "card-1", "connector": "cedar-stays", "tool": "save_favorite", "tool_class": "write",
        "arguments": {"room": "r1"}, "disclosure": {}, "offered": CHOICES,
        "binding_sha256": permissions.binding("save_favorite", {"room": "r1"}, {}),
        "offered_basis": {kind: CHOICE_SOURCE[kind] for kind in CHOICES}}
    assert state["cards"][0]["offered_basis"]["allow_task"].startswith("Help Center only")
    assert state["decisions"][0]["choice_basis"] == CHOICE_SOURCE["allow_once"]
    assert state["basis"]["choices"] == CHOICE_SOURCE
    assert state["binding"]["canonicalizer"] == "jcs-rfc8785-v1"
    assert state["policy"]["mode"] == "ask-for-some" and set(state["classifications"]) == set(CLASSIFIED)
    assert state["basis"]["rules"]["ask-for-some"]["source"] == HELP_CENTER
    keys = {row["basis"] for row in state["requests"] + state["decisions"]}
    assert keys and keys <= set(RULE_BASIS)


def test_metrics_have_a_stable_shape():
    assert engine().metrics() == {"prompts": ZERO, "decisions": dict.fromkeys(KINDS, 0), "auto_allowed": ZERO,
                                  "redundant_prompts": 0, "coerced_scopes": 0}


def test_unclassified_tools_and_unbindable_values_fail_closed():
    permissions = engine(rules=[everyone("allow_once")])
    with pytest.raises(LabError, match="No classification"):
        permissions.request("transfer_funds", {})
    with pytest.raises(LabError) as caught:
        permissions.request("save_favorite", {"amount": float("nan")})
    assert (caught.value.code, caught.value.category) == (1, "interaction")
    with pytest.raises(LabError) as caught:
        permissions.request("save_favorite", {}, disclosure={"total": 2**60})
    assert (caught.value.code, caught.value.category) == (1, "tool/API")
    with pytest.raises(LabError, match="JSON object"):
        permissions.request("save_favorite", ["r1"])
    with pytest.raises(LabError, match="must start with"):
        permissions.request("save_favorite", {}, private_paths={"guest_email"})
    deep = {}
    for _ in range(100):
        deep = {"nested": deep}
    with pytest.raises(LabError, match="nesting exceeds") as caught:
        permissions.request("save_favorite", deep)
    assert (caught.value.code, caught.value.category) == (1, "interaction")
    assert permissions.state()["requests"] == []


def test_engine_setup_rejects_policies_naming_unknown_tools():
    with pytest.raises(LabError, match="important_reads names get_bookng"):
        engine(important_reads=["get_bookng"])
    with pytest.raises(LabError, match="persisted_always names send_email"):
        engine(persisted_always=["send_email"])
    with pytest.raises(LabError, match="different tool"):
        PermissionEngine(PermissionPolicy(), {"other": CLASSIFIED["get_booking"]}, "cedar-stays")
    with pytest.raises(LabError, match="connector name"):
        PermissionEngine(PermissionPolicy(), CLASSIFIED, "")
    loaded = PermissionEngine({"mode": "always-ask"}, {name: item.model_dump() for name, item in CLASSIFIED.items()},
                              "cedar-stays")
    assert loaded.request("search_rooms", {}).card.offered == ONCE_ONLY


def test_tickets_cannot_change_what_the_engine_recorded():
    permissions = engine(rules=[everyone("allow_once")])
    ticket = permissions.request("save_favorite", {"room": {"id": "r1"}})
    ticket.arguments["room"]["id"] = "r2"
    ticket.card.arguments["room"]["id"] = "r3"
    assert permissions.state()["cards"][0]["arguments"] == {"room": {"id": "r1"}}
    with pytest.raises(ValidationError):
        ticket.card.tool = "create_booking"


def test_a_sensitive_card_cannot_offer_wider_scopes():
    fields = {"card_id": "card-1", "connector": "cedar-stays", "tool": "create_booking", "arguments": {},
              "disclosure": {}, "binding_sha256": "0" * 64}
    with pytest.raises(ValidationError, match="only Allow once and Deny"):
        ApprovalCard(**fields, tool_class="sensitive-write", offered=["allow_once", "always_allow", "deny"])
    for offered in (["allow_once"], [*CHOICES, "edit"]):
        with pytest.raises(ValidationError):
            ApprovalCard(**fields, tool_class="read", offered=offered)
    assert ApprovalCard(**fields, tool_class="read", offered=CHOICES).offered == CHOICES


def test_tickets_carry_a_model_message_exactly_when_refused():
    with pytest.raises(ValidationError):
        Ticket(allowed=True, arguments={}, model_message=DECLINED)
    with pytest.raises(ValidationError):
        Ticket(allowed=False, arguments={})
    with pytest.raises(ValidationError):
        Ticket(allowed=False, arguments={}, model_message=DECLINED, scope="once")
    assert Ticket(allowed=False, arguments={}, model_message=DECLINED).card is None
    card = engine(rules=[everyone("allow_once")]).request("save_favorite", {}).card
    with pytest.raises(ValidationError, match="comes with its decision"):
        Ticket(allowed=True, card=card, arguments={}, scope="once")
    with pytest.raises(ValidationError, match="comes with its decision"):
        Ticket(allowed=True, card=card, decision=Decision(kind="deny"), arguments={}, scope="once")


@pytest.mark.parametrize("fields", [{"kind": "deny", "coerced_from": "always_allow"},
                                    {"kind": "allow_once", "coerced_from": "deny"},
                                    {"kind": "allow_once", "edits": {"/adults": 2}},
                                    {"kind": "edit"}])
def test_decisions_are_internally_consistent(fields):
    with pytest.raises(ValidationError):
        Decision(**fields)


def test_missing_disclosure_lists_the_commitment_terms_a_card_does_not_show():
    card = engine().request("create_booking", {"quote_id": "q1"}, disclosure={
        "seller": "Cedar Stays", "total": 41800, "currency": "USD", "recurring": None, "cancellation": ""}).card
    assert missing_disclosure(card) == ["recurring", "key_terms", "cancellation"]
    assert missing_disclosure(card, ("total", "taxes")) == ["taxes"]
    assert RULE_BASIS["disclosure"]["source"].endswith("section 4.7")
    # Verbatim, not an excerpt that turns the guideline's examples into required card fields.
    assert RULE_BASIS["disclosure"]["quote"].startswith("Before commitment, your tools should return all relevant "
                                                        "information, such as: the seller, full price")
    assert "host assumption" in RULE_BASIS["disclosure"]["assumed"]


@pytest.mark.parametrize("default", ["deny", "no_response"])
def test_a_rule_on_a_term_the_card_does_not_show_leaves_the_answer_to_the_default(default):
    # "Deny over budget, else allow": with no total on the card, the later allow rule must not answer.
    rules = [{"id": "over-budget", "decision": "deny", "when": {"field": "disclosure/total", "op": "gt",
                                                                 "value": 50000}},
             {"id": "book", "decision": "allow_once", "when": {"field": "tool", "op": "eq",
                                                               "value": "create_booking"}}]
    permissions = engine(rules=rules, default=default)
    withheld = permissions.request("create_booking", {"quote_id": "q-0001"},
                                   disclosure={"total": None, "currency": "USD"})
    assert not withheld.allowed and withheld.decision.kind == default and withheld.decision.rule is None
    assert withheld.decision.undecided_rule == "over-budget"
    row = permissions.state()["decisions"][0]
    assert row["basis"] == "withheld-terms" and row["undecided_rule"] == "over-budget" and "(total)" in row["note"]
    # With the term shown, the list works as written.
    expensive = permissions.request("create_booking", {"quote_id": "q-0002"}, disclosure={"total": 89636})
    cheap = permissions.request("create_booking", {"quote_id": "q-0003"}, disclosure={"total": 44836})
    assert (expensive.allowed, expensive.decision.rule) == (False, "over-budget")
    assert (cheap.allowed, cheap.decision.rule, cheap.decision.undecided_rule) == (True, "book", None)
    # "Terms shown" (exists) cannot approve a card without them either.
    shown = engine(rules=[{"id": "terms-shown", "decision": "allow_once",
                           "when": {"field": "disclosure/total", "op": "exists", "value": True}}])
    assert not shown.request("create_booking", {"quote_id": "q-0001"}, disclosure={"total": None}).allowed
    with pytest.raises(ValidationError, match="leaves the policy default"):
        Decision(kind="allow_once", undecided_rule="over-budget")
