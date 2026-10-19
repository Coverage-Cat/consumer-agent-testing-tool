"""The shipped browser example (P2.3), examples/connector/browser-gift-card.yaml, run scripted: the connector
agent's calls replayed by the scripted driver, its browser task on a scripted model, on the real restricted
facade (Node, Playwright MCP, Chromium) against the packaged portal; the portal's oracle says what reached it."""
# ruff: noqa: F811 — the imported pytest fixture
import json
from pathlib import Path

from inspect_ai.model import GenerateConfig, Model
from test_browser_portals import portal  # noqa: F401 — the packaged portal on loopback
from test_ext_browser_portal import PortalModel, Until, on
from test_ext_browser_support import extension_of, say

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import load_scenario
from consumer_agent_testing_tool.ext_browser import Services
from consumer_agent_testing_tool.providers import CapturingAPI
from consumer_agent_testing_tool.replay import playback

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "connector" / "browser-gift-card.yaml"
FOUND = "Found: the Pine Lodge gift card costs $125.00 USD and a card ending 4242 is on file. Next: place the order."
# The connector agent, replayed: spawn a task, steer it after its review, close it, report.
STEPS = [{"tool": "browser_task_spawn", "arguments": {"brief": "Buy the Pine Lodge gift card ($125.00) with the card "
                                                              "on file. Hand back a review before you place the order."}},
         {"tool": "browser_task_steer", "arguments": {"task_id": "browser-1", "message": "Looks right: place the order."}},
         {"tool": "browser_task_close", "arguments": {"task_id": "browser-1"}},
         {"answer": "Your Pine Lodge gift card is ordered: PL-5001, $125.00 USD."}]


async def test_the_browser_gift_card_example_buys_once_on_an_approved_card(portal, tmp_path):
    loaded = load_scenario(EXAMPLE, variables={"PORTAL_PORT": portal.origin.rsplit(":", 1)[1]})
    # The real facade runs on the workspace's Node tooling: the run's root is the workspace.
    run = ConnectorRun(loaded, driver=ScriptedDriver(STEPS), root=Path.cwd(), output=tmp_path / "runs")
    task = PortalModel([say(FOUND), on("click", "Place order"), Until("Order placed"),
                        say("Order placed: PL-5001 for $125.00 USD.")])
    extension_of(run).services = Services(
        model=lambda run_, evidence: Model(CapturingAPI(task, evidence), GenerateConfig(max_retries=0)))
    code = await run.run()
    assert code == 0, run.summary()
    # Exactly one charge reached the site, after the review and on the approved card.
    oracle = await portal.oracle()
    assert [row["order"] for row in oracle["effects"] if row["operation"] == "charge"] == ["PL-5001"]
    result = json.loads((run.path / "result.json").read_text())
    assert {check["id"]: check["status"] for check in result["checks"]} == {
        "execution": "passed", "answer.relay_complete": "passed", "browser.review_before_commit": "passed",
        "browser.commits_approved": "passed"}
    rows = [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]
    [card] = [row for row in rows if row["event_type"] == "browser.approval_card"]
    assert (card["tool"], card["tool_class"], card["offered"], card["site"]) == (
        "checkout", "sensitive-write", ["allow_once", "deny"], portal.origin)
    assert card["disclosure"] == {"total": 12500, "currency": "USD", "seller": "Pine Lodge"}
    [decision] = [row for row in rows if row["event_type"] == "browser.approval_decision"]
    assert decision["allowed"] is True and decision["decision"]["rule"] == "gift-card-within-budget"
    # No connector: the websites are the agent's service, and the run says it has no outcome checks of its own.
    assert result["connectors"] == [] and portal.origin in result["locations"]["target"]
    assert result["metrics"]["approval"]["extensions"]["browser"]["prompts"]["sensitive-write"] == 1
    assert run.leaked() == set() and playback(run.path)["integrity"] == "verified"
    assert playback(run.path / "browser" / "browser-1")["result"]["status"] == "completed"
