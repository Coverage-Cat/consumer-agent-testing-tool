"""Allow for this site (F2): a website scope the Help Center lists. Website engines (browser task approvals) offer
it on cards that name their site and grant it per tool and site; connector cards never offer it, and a rule that
answers it must match browser cards only."""
import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.permissions import (
    CHOICE_SOURCE,
    CHOICES,
    KINDS,
    RULE_BASIS,
    SITE_CHOICES,
    ApprovalCard,
    Classification,
    DecisionRule,
    PermissionEngine,
    PermissionPolicy,
    card_record,
    website_only,
)

SITE, OTHER = "https://shop.pine.example", "https://pay.other.example"
BROWSER = {"field": "connector", "op": "eq", "value": "browser"}
CLASSES = {"submit": Classification(tool="submit", tool_class="write", basis="declared", reasons=["declared"]),
           "checkout": Classification(tool="checkout", tool_class="sensitive-write", basis="declared",
                                      reasons=["declared"])}


def site_rule(**when):
    return {"id": "site-submits", "decision": "allow_site",
            "when": {"all": [BROWSER, {"field": "tool", "op": "eq", "value": "submit"}]} if not when else when}


def website(*rules, mode="platform-default"):
    return PermissionEngine(PermissionPolicy(mode=mode, rules=list(rules)), CLASSES, "browser", sites=True)


def test_a_site_grant_covers_the_same_action_on_the_same_site_only():
    permissions = website(site_rule())
    first = permissions.request("submit", {"message": "Late arrival"}, site=SITE)
    assert first.allowed and first.scope == "site"
    assert first.card.site == SITE and first.card.offered == SITE_CHOICES
    assert first.card.offered_basis["allow_site"] == CHOICE_SOURCE["allow_site"]
    # The same type of action on the same site: no card; another site asks again.
    again = permissions.request("submit", {"message": "Thank you"}, site=SITE)
    assert again.allowed and again.card is None and again.scope == "site"
    elsewhere = permissions.request("submit", {"message": "Hello"}, site=OTHER)
    assert elsewhere.card is not None and elsewhere.card.site == OTHER
    state = permissions.state()
    assert [(grant["scope"], grant["site"], grant["status"]) for grant in state["grants"]] == [
        ("site", SITE, "active"), ("site", OTHER, "active")]
    assert [(row["via"], row["basis"], row["site"]) for row in state["requests"]] == [
        ("prompt", "platform-default", SITE), ("grant", "allow-site", SITE), ("prompt", "platform-default", OTHER)]
    assert [row["site"] for row in state["decisions"]] == [SITE, OTHER] and state["sites"] is True
    assert permissions.metrics()["decisions"]["allow_site"] == 2
    # Cards record their site; a connector card's record names none.
    assert card_record(first.card)["site"] == SITE and RULE_BASIS["allow-site"]["basis"] == "documented"


def test_sensitive_and_always_ask_cards_never_take_the_site_scope():
    permissions = website({**site_rule(), "when": BROWSER})
    checkout = permissions.request("checkout", {"total": 12500}, site=SITE)
    assert checkout.card.offered == ["allow_once", "deny"] and checkout.scope == "once"
    assert checkout.decision.coerced_from == "allow_site"
    assert permissions.request("checkout", {"total": 12500}, site=SITE).card is not None  # Every use.
    strict = website({**site_rule(), "when": BROWSER}, mode="always-ask")
    asked = strict.request("submit", {"message": "x"}, site=SITE)
    assert asked.card.offered == ["allow_once", "deny"] and asked.decision.coerced_from == "allow_site"
    assert strict.state()["decisions"][0]["note"] == "scope not offered: Always ask honors no site grant either"


def test_connector_engines_never_offer_the_site_scope():
    # Only a connector named like the website cards could meet such a rule; its card offers no site scope.
    permissions = PermissionEngine(PermissionPolicy(rules=[{**site_rule(), "when": BROWSER}]), CLASSES, "browser")
    ticket = permissions.request("submit", {"message": "x"})
    assert ticket.card.offered == CHOICES and ticket.card.site is None and "site" not in card_record(ticket.card)
    assert (ticket.decision.kind, ticket.decision.coerced_from, ticket.scope) == ("allow_once", "allow_site", "once")
    assert permissions.state()["decisions"][0]["note"] == ("scope not offered: Allow for this site is a website "
                                                           "scope; this card names no site")
    assert "site" not in permissions.state()["requests"][0] and "sites" not in permissions.state()
    with pytest.raises(LabError, match="Only a website engine takes a site"):
        permissions.request("submit", {"message": "x"}, site=SITE)
    with pytest.raises(LabError, match="a site is an origin"):
        website().request("submit", {"message": "x"}, site=f"{SITE}/path")


@pytest.mark.parametrize("when, valid", [
    ({"all": [BROWSER, {"field": "tool", "op": "eq", "value": "submit"}]}, True),
    (BROWSER, True), ({"field": "connector", "op": "in", "value": ["browser"]}, True),
    ({"any": [BROWSER, {"all": [BROWSER, {"field": "tool", "op": "eq", "value": "submit"}]}]}, True),
    ({"field": "tool", "op": "exists", "value": True}, False),
    ({"field": "connector", "op": "in", "value": ["browser", "cedar-stays"]}, False),
    ({"field": "connector", "op": "ne", "value": "cedar-stays"}, False),
    ({"any": [BROWSER, {"field": "tool", "op": "eq", "value": "submit"}]}, False)])
def test_a_rule_answering_allow_site_must_match_browser_cards_only(when, valid):
    assert website_only(when) is valid
    if valid:
        assert DecisionRule(id="site", when=when, decision="allow_site").decision == "allow_site"
    else:
        with pytest.raises(ValidationError, match="connector cards never offer it"):
            DecisionRule(id="site", when=when, decision="allow_site")


def test_the_site_kind_joins_the_decision_kinds_and_cards_validate_their_buttons():
    assert "allow_site" in KINDS and "allow_site" not in CHOICES
    with pytest.raises(ValidationError, match="names its site"):
        ApprovalCard(card_id="card-1", connector="browser", tool="submit", tool_class="write", arguments={},
                     disclosure={}, binding_sha256="0" * 64, offered=SITE_CHOICES)
    card = ApprovalCard(card_id="card-1", connector="browser", tool="submit", tool_class="write", arguments={},
                        disclosure={}, binding_sha256="0" * 64, offered=SITE_CHOICES, site=SITE)
    assert card.site == SITE


def scenario_with(rule, connector_name="cedar-stays"):
    from test_connector_support import scenario
    document = scenario()
    document["connectors"][0]["name"] = connector_name
    document["permissions"]["rules"].append(rule)
    return document


def test_scenarios_take_allow_site_for_browser_cards_and_refuse_it_for_connectors():
    loaded = parse_scenario(scenario_with(site_rule()))
    assert loaded.scenario.permissions.rules[-1].decision == "allow_site"
    with pytest.raises(LabError, match="answers browser task cards only"):
        parse_scenario(scenario_with(site_rule(field="tool", op="exists", value=True)))
    with pytest.raises(LabError, match="a connector named browser would share their name"):
        parse_scenario(scenario_with(site_rule(), connector_name="browser"))
