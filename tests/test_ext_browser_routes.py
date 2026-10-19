"""The browser extension's <name>.commit_sent event as the contract the routes extension reads: a website
commitment a browser task sent after its approval card is the website route's commit (judge: commit)."""
from inspect_ai.model import GenerateConfig, Model
from test_connector_support import Hotel, agent, answer, events, result_of, services
from test_ext_browser_support import FakeSite, Scripted, browser_scenario, main_call, on_latest, say

from consumer_agent_testing_tool.connector_run import EXTENSIONS, ConnectorRun
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.ext_browser import BrowserExtension, Services
from consumer_agent_testing_tool.providers import CapturingAPI

BRIEF = "Buy one Pine Lodge gift card ($125) for Taylor with the card on file; do not buy anything else."
FOUND = "Found: the gift card costs $125.00 USD and a card ending 4242 is on file. Next: place the order."
# The fields routes and reports rely on; a change here is a contract change.
COMMIT_FIELDS = {"browser_task", "url", "method", "rule", "body_digest", "card_id", "via"}


def website_order(name):
    """A gift card bought on the website through the browser extension registered as `name`, judged by routes."""
    document = browser_scenario(checks=[], answer=None, expected={"tools": []})
    document["extensions"][name] = document["extensions"].pop("browser")
    document["extensions"]["routes"] = {"expected_route": "website", "judge": "commit", "website": {"extension": name}}
    return document


async def ordered(tmp_path, name):
    driver, _ = agent([main_call("browser_task_spawn", brief=BRIEF),
                       main_call("browser_task_steer", task_id="browser-1", message="Go ahead and place the order."),
                       answer("Your Pine Lodge gift card is ordered: $125.00, order PL-5001.")])
    sub = Scripted([on_latest("click", ref="e3"), say(FOUND), on_latest("click", ref="e4"),
                    say("Order placed: PL-5001.")])
    run = ConnectorRun(parse_scenario(website_order(name), base=tmp_path), driver=driver, root=tmp_path,
                       output=tmp_path / "runs", services=services(Hotel()))
    extension = next(item for item in run.extensions if item.name == name)
    extension.services = Services(host=lambda root: FakeSite(), environ={"PINE_MEMBER_PASSWORD": "pine-member-pass-7731"},
                                  model=lambda run_, evidence: Model(CapturingAPI(sub, evidence),
                                                                     GenerateConfig(max_retries=0)))
    code = await run.run()
    return run, code, {check["id"]: check for check in result_of(run)["checks"]}


async def test_a_browser_task_s_approved_commit_is_the_website_route_for_routes(tmp_path):
    run, code, checks = await ordered(tmp_path, "browser")
    assert code == 0, run.summary()
    [sent] = events(run, "browser.commit_sent")
    assert COMMIT_FIELDS <= set(sent) and sent["rule"] == "pay" and sent["browser_task"] == "browser-1"
    assert sent["card_id"] == events(run, "browser.approval_card")[0]["card_id"]
    accuracy = checks["routes.route_accuracy"]
    assert (accuracy["status"], accuracy["actual"]) == ("passed", "website")


async def test_the_commit_event_follows_the_name_the_browser_extension_runs_under(tmp_path, monkeypatch):
    # routes reads <website.extension>.commit_sent: a browser extension under another name still counts.
    monkeypatch.setitem(EXTENSIONS, "shop-browser", BrowserExtension)
    run, code, checks = await ordered(tmp_path, "shop-browser")
    assert code == 0, run.summary()
    assert [row["rule"] for row in events(run, "shop-browser.commit_sent")] == ["pay"]
    assert events(run, "browser.commit_sent") == [] and events(run, "shop-browser.task_spawned")
    assert checks["routes.route_accuracy"]["actual"] == "website"
