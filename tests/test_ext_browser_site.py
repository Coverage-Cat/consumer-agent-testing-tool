"""Allow for this site on browser task cards (F2): a write card names the site its request goes to and offers the
Help Center's website scope; a site grant covers that type of action on that site, never another site's."""
import json

from test_connector_support import agent, answer, events, result_of
from test_ext_browser_support import (
    PAGES,
    SITE,
    FakeSite,
    act,
    browser_config,
    browser_scenario,
    main_call,
    on_latest,
    page,
    run_browser,
    say,
)

OTHER = "http://127.0.0.1:8791"
SITE_RULE = {"id": "messages-for-this-site", "decision": "allow_site", "when": {"all": [
    {"field": "connector", "op": "eq", "value": "browser"}, {"field": "tool", "op": "eq", "value": "submit"}]}}


def two_sites():
    """The contact page posts to this site; the partner page, on this site too, posts to another one."""
    pages = {**PAGES, f"{SITE}/case/partner": page(
        "Partner desk", ("textbox", "Message", "e1", ""), ("button", "Send to partner", "e2", None),
        ("paragraph", "", "e3", "Not sent"),
        clicks={"e2": ("request", "partner", "POST", f"{OTHER}/contact", "Message sent", "Message not sent")})}
    config = browser_config()
    config["allowed_origins"].append(OTHER)
    config["rules"] += [{"id": "case-partner", "url": f"{SITE}/case/partner", "methods": ["GET"], "effect": "read"},
                        {"id": "partner", "url": f"{OTHER}/contact", "methods": ["POST"], "effect": "write"}]
    document = browser_scenario(config, checks=[], answer=None, expected={"tools": []})
    document["permissions"]["rules"].append(SITE_RULE)
    return document, FakeSite(pages)


async def test_an_allow_for_this_site_covers_that_site_s_writes_and_asks_again_elsewhere(tmp_path):
    document, site = two_sites()
    main = [main_call("browser_task_spawn", brief="Send Pine Lodge two notes, then one to their partner desk.",
                      url=f"{SITE}/case/contact"),
            main_call("browser_task_steer", task_id="browser-1", message="Send all three."), answer("All three sent.")]
    sub = [say("Found: the contact form. Next: send the notes."),
           on_latest("type", ref="e1", text="Late arrival, around 10 pm"), on_latest("click", ref="e2"),
           on_latest("type", ref="e1", text="Thank you"), on_latest("click", ref="e2"),
           act("navigate", url=f"{SITE}/case/partner"),
           on_latest("type", ref="e1", text="Please hold the shuttle"), on_latest("click", ref="e2"),
           say("Sent both notes and the partner message.")]
    driver, _api = agent(main)
    run, code, site, _sub_api = await run_browser(tmp_path, document, driver=driver, site=site, sub=sub)
    assert code == 0, run.summary()
    assert [(row["url"], row["sent"]) for row in site.requests] == [
        (f"{SITE}/contact", True), (f"{SITE}/contact", True), (f"{OTHER}/contact", True)]
    # One card per site, each naming its site and offering Allow for this site; the first site's grant covered
    # the second note, and the other site asked again.
    cards = events(run, "browser.approval_card")
    assert [(card["tool"], card["site"]) for card in cards] == [("submit", SITE), ("submit", OTHER)]
    assert all("allow_site" in card["offered"] for card in cards)
    [granted] = events(run, "browser.approval_granted")
    assert (granted["scope"], granted["site"], granted["card_id"]) == ("site", SITE, cards[0]["card_id"])
    assert [row["via"] for row in events(run, "browser.commit_sent")] == [
        "its own card", "an earlier Allow for this site", "its own card"]
    decisions = [row["decision"] for row in events(run, "browser.approval_decision")]
    assert [row["kind"] for row in decisions] == ["allow_site", "allow_site"]
    result = result_of(run)
    check = next(check for check in result["checks"] if check["id"] == "browser.commits_approved")
    assert check["status"] == "passed" and check["actual"]["approved_cards"] == 2
    browser = result["metrics"]["extensions"]["browser"]["approval"]
    assert browser["decisions"]["allow_site"] == 2 and browser["auto_allowed"]["write"] == 1
    # The browser's own approval engine also lands beside the connectors' under metrics.approval.extensions.
    assert result["metrics"]["approval"]["extensions"]["browser"] == browser
    approvals = [row for row in events(run, "browser.approvals")]
    assert [(grant["scope"], grant.get("site")) for grant in approvals[0]["grants"]] == [("site", SITE), ("site", OTHER)]
    assert json.dumps(cards).count(SITE) >= 1
