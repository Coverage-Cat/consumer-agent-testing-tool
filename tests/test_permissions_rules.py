"""Scripted-user rules: the strict scenario.py predicate grammar over approval-card fields."""
import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.permissions import (
    RULE_BASIS,
    ApprovalCard,
    DecisionRule,
    PermissionPolicy,
    evaluate,
    matches,
    validate_predicate,
)
from consumer_agent_testing_tool.serialization import loads

ANY = {"field": "tool", "op": "exists", "value": True}


def card(**overrides):
    fields = {"card_id": "card-1", "connector": "cedar-stays", "tool": "create_booking",
              "tool_class": "sensitive-write", "offered": ["allow_once", "deny"], "binding_sha256": "0" * 64,
              "arguments": {"adults": 2, "flexible": True, "guest": {"name": "Taylor"}, "rooms": ["r1", "r2"],
                            "a/b": "slash", "note": None},
              "disclosure": {"total_minor": 41800, "currency": "USD", "fees": [{"minor": 1500}]}}
    return ApprovalCard(**{**fields, **overrides})


def nested(depth):
    predicate = ANY
    for _ in range(depth - 1):
        predicate = {"all": [predicate]}
    return predicate


@pytest.mark.parametrize("predicate,expected", [
    ({"field": "tool", "op": "eq", "value": "create_booking"}, True),
    ({"field": "tool", "op": "ne", "value": "create_booking"}, False),
    ({"field": "tool_class", "op": "in", "value": ["write", "sensitive-write"]}, True),
    ({"field": "connector", "op": "eq", "value": "other-connector"}, False),
    ({"field": "arguments/adults", "op": "eq", "value": 2.0}, True),  # One JSON number type.
    ({"field": "arguments/adults", "op": "gte", "value": 2}, True),
    ({"field": "arguments/adults", "op": "lt", "value": 2}, False),
    ({"field": "arguments/flexible", "op": "eq", "value": 1}, False),  # Booleans are not numbers.
    ({"field": "arguments/flexible", "op": "eq", "value": True}, True),
    ({"field": "arguments/adults", "op": "eq", "value": True}, False),
    ({"field": "arguments/adults", "op": "eq", "value": "2"}, False),
    ({"field": "arguments/guest/name", "op": "in", "value": ["Taylor", "Sam"]}, True),
    ({"field": "arguments/rooms/1", "op": "eq", "value": "r2"}, True),
    ({"field": "arguments/rooms/2", "op": "exists", "value": False}, True),
    ({"field": "arguments/rooms/01", "op": "exists", "value": True}, False),  # RFC 6901 array index.
    ({"field": "arguments/a~1b", "op": "eq", "value": "slash"}, True),
    ({"field": "arguments/note", "op": "eq", "value": None}, True),
    ({"field": "arguments/note", "op": "exists", "value": True}, True),
    ({"field": "arguments/missing", "op": "ne", "value": "x"}, False),  # Missing never matches.
    ({"field": "arguments/guest", "op": "eq", "value": "Taylor"}, False),
    ({"field": "disclosure/total_minor", "op": "lte", "value": 50000}, True),
    ({"field": "disclosure/fees/0/minor", "op": "gt", "value": 1000}, True),
    ({"field": "disclosure/currency", "op": "eq", "value": "usd"}, False),  # Exact strings.
    ({"all": [ANY, {"any": [{"field": "disclosure/currency", "op": "eq", "value": "EUR"},
                            {"field": "disclosure/currency", "op": "eq", "value": "USD"}]}]}, True),
])
def test_predicates_match_card_fields_type_strictly(predicate, expected):
    validate_predicate(predicate)
    assert matches(predicate, card()) is expected


@pytest.mark.parametrize("predicate,message", [
    ({"field": "price", "op": "eq", "value": 1}, "Unknown predicate field"),
    ({"field": "arguments", "op": "eq", "value": 1}, "Unknown predicate field"),
    ({"field": "disclosure.total", "op": "eq", "value": 1}, "Unknown predicate field"),
    ({"field": ["tool"], "op": "eq", "value": 1}, "Unknown predicate field"),
    ({"field": "arguments/a~2", "op": "eq", "value": 1}, "escape"),
    ({"field": "tool", "op": "contains", "value": "x"}, "Unknown predicate operator"),
    ({"field": "tool", "op": "in", "value": []}, "nonempty array"),
    ({"field": "tool", "op": "in", "value": "create_booking"}, "nonempty array"),
    ({"field": "arguments/adults", "op": "in", "value": [1, "1"]}, "one type"),
    ({"field": "arguments/adults", "op": "in", "value": [1, True]}, "one type"),
    ({"field": "arguments/adults", "op": "eq", "value": [1]}, "JSON scalars"),
    ({"field": "arguments/adults", "op": "eq", "value": {"a": 1}}, "JSON scalars"),
    ({"field": "arguments/name", "op": "gt", "value": "m"}, "numbers only"),
    ({"field": "arguments/flexible", "op": "lt", "value": True}, "numbers only"),
    ({"field": "tool", "op": "eq", "value": 1}, "must be strings"),
    ({"field": "tool_class", "op": "eq", "value": "sensitive"}, "Unknown tool class"),
    ({"field": "tool", "op": "exists", "value": "yes"}, "true or false"),
    ({"field": "arguments/total", "op": "lt", "value": float("inf")}, "finite"),
    ({"field": "tool", "op": "eq", "value": "x", "extra": 1}, "exactly field, op, value"),
    ({"field": "tool", "op": "eq"}, "exactly field, op, value"),
    ({"all": []}, "nonempty array"),
    ({"all": [], "any": []}, "exactly field, op, value"),
    ({"not": ANY}, "exactly field, op, value"),
    ({"all": [ANY, "tool"]}, "must be an object"),
])
def test_the_predicate_grammar_is_closed_and_type_strict(predicate, message):
    with pytest.raises(LabError, match=message):
        validate_predicate(predicate)
    with pytest.raises(ValidationError, match=message):
        DecisionRule(id="rule", when=predicate, decision="deny")


def test_predicates_nest_at_most_eight_levels():
    validate_predicate(nested(8))
    assert matches(nested(8), card())
    with pytest.raises(LabError, match="eight levels"):
        validate_predicate(nested(9))


@pytest.mark.parametrize("decision", ["allow_site", "allow_for_this_site", "site"])
def test_allow_for_this_site_is_not_a_connector_choice(decision):
    with pytest.raises(ValidationError, match="website scope"):
        DecisionRule(id="site", when=ANY, decision=decision)


@pytest.mark.parametrize("rule,message", [
    ({"decision": "approve"}, "Input should be"),
    ({"decision": "allow_session"}, "Input should be"),
    ({"decision": "edit"}, "nonempty edits"),
    ({"decision": "edit", "edits": {}}, "nonempty edits"),
    ({"decision": "deny", "edits": {"/adults": 1}}, "only valid with decision edit"),
    ({"decision": "edit", "edits": {"adults": 2}}, "must start with"),
    ({"decision": "edit", "edits": {"": 2}}, "must start with"),
    ({"decision": "edit", "edits": {"/guest": {}, "/guest/name": "Sam"}}, "overlap"),
    ({"decision": "edit", "edits": {"/total": float("nan")}}, "finite"),
    ({"decision": "allow_once", "scope": "task"}, "Extra inputs"),
    ({"decision": "allow_once", "id": "1st"}, "pattern"),
])
def test_rules_are_strict(rule, message):
    with pytest.raises(ValidationError, match=message):
        DecisionRule.model_validate({"id": "rule", "when": ANY, **rule})


@pytest.mark.parametrize("policy,message", [
    ({"mode": "sometimes"}, "Input should be"),
    ({"default": "allow_once"}, "Input should be"),
    ({"rules": [{"id": "same", "when": ANY, "decision": "deny"}] * 2}, "rule ids must be unique"),
    ({"important_reads": ["get_booking", "get_booking"]}, "important_reads must be unique"),
    ({"persisted_always": [""]}, "persisted_always must be unique"),
    ({"persisted_site": ["example.com"]}, "Extra inputs"),
])
def test_policies_are_strict(policy, message):
    with pytest.raises(ValidationError, match=message):
        PermissionPolicy.model_validate(policy)


def test_policies_load_from_scenario_yaml():
    policy = PermissionPolicy.model_validate(loads("""
mode: ask-for-some
important_reads: [get_booking]
default: no_response
rules:
  - id: cheap-bookings
    when: {all: [{field: tool, op: eq, value: create_booking}, {field: disclosure/total_minor, op: lte, value: 50000}]}
    decision: allow_once
  - id: two-adults
    when: {field: arguments/adults, op: gt, value: 2}
    decision: edit
    edits: {"/adults": 2}
"""))
    assert [rule.id for rule in policy.rules] == ["cheap-bookings", "two-adults"]
    assert policy.rules[1].edits == {"/adults": 2} and policy.default == "no_response"
    assert PermissionPolicy().model_dump() == {"mode": "platform-default", "important_reads": [], "rules": [],
                                               "default": "deny", "persisted_always": []}


def test_a_term_the_card_could_not_show_never_satisfies_and_never_decides_a_comparison():
    # The scenario configured total for this card, but no accepted result supplied it: null, not shown.
    shown = card(disclosure={"total": None, "currency": "USD"})
    total = {"field": "disclosure/total", "op": "lte", "value": 50000}
    assert evaluate({"field": "disclosure/total", "op": "exists", "value": True}, shown) is False
    assert evaluate({"field": "disclosure/total", "op": "exists", "value": False}, shown) is True
    assert evaluate(total, shown) is None and not matches(total, shown)
    assert evaluate({"field": "disclosure/total", "op": "gt", "value": 50000}, shown) is None
    assert evaluate({"field": "disclosure/total/amount", "op": "eq", "value": 1}, shown) is None
    # Kleene logic: all() is false once a child is false, any() true once a child is true; else undecided.
    assert evaluate({"all": [{"field": "tool", "op": "eq", "value": "create_booking"}, total]}, shown) is None
    assert evaluate({"all": [{"field": "tool", "op": "eq", "value": "get_quote"}, total]}, shown) is False
    assert evaluate({"any": [{"field": "tool", "op": "eq", "value": "create_booking"}, total]}, shown) is True
    assert evaluate({"any": [{"field": "tool", "op": "eq", "value": "get_quote"}, total]}, shown) is None
    # A term the card never had is plain absence (decided), and a shown term compares as before.
    assert evaluate({"field": "disclosure/seller", "op": "eq", "value": "Cedar Stays"}, shown) is False
    assert evaluate({"field": "disclosure/currency", "op": "eq", "value": "USD"}, shown) is True
    assert RULE_BASIS["withheld-terms"]["basis"] == "assumed"
