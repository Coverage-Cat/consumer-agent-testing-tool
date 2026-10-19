"""Host credential fill for browser tasks: the task names a saved credential, the host types it while the
task waits, and the value never reaches a model view or public evidence."""
import hashlib
import json
import re

import pytest
from inspect_ai.tool import ToolDef, ToolParams
from test_connector_multi_support import NoConfig
from test_connector_support import agent, answer, events, result_of
from test_ext_browser_support import (
    MEMBER_NUMBER,
    MEMBER_PASSWORD,
    SITE,
    browser_config,
    browser_results,
    browser_scenario,
    extension_of,
    fill,
    main_call,
    on_latest,
    run_browser,
    say,
    tool_replies,
)

from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension
from consumer_agent_testing_tool.errors import LabError

LOGIN = f"{SITE}/case/member-login"
OTHER = "http://127.0.0.1:8791"
CARD = "4000001234567899"


def sign_in(config=None):
    return browser_scenario(config, checks=[], answer=None, expected={"tools": []})


async def test_ext_browser_credential_fill_pauses_the_task_and_never_shows_the_value(tmp_path):
    main = [main_call("browser_task_spawn", brief="Sign in to Pine Lodge with my saved member credentials.", url=LOGIN),
            main_call("browser_task_close", task_id="browser-1"), answer("You are signed in at Pine Lodge.")]
    sub = [fill("e1", "member-number"), fill("e2", "member-password"), on_latest("click", ref="e3"),
           say("Found: signed in. Next: nothing.")]
    driver, api = agent(main)
    run, code, site, sub_api = await run_browser(tmp_path, sign_in(), driver=driver, sub=sub)
    assert code == 0, run.summary()
    # The host typed both values into the referenced fields; the sign-in is a session write (no card, no review).
    assert site.typed == [(LOGIN, "e1", MEMBER_NUMBER), (LOGIN, "e2", MEMBER_PASSWORD)]
    assert [(row["rule"], row["sent"]) for row in site.requests] == [("sign-in", True)]
    assert not events(run, "browser.approval_card") and browser_results(api)[0]["status"] == "review"
    # The task saw the fields filled, never the values: masked by the host, and by the facade for passwords.
    member, password = tool_replies(sub_api, 1)[-1], tool_replies(sub_api, 2)[-1]
    assert 'textbox "Member number" [ref=e1]: [REDACTED]' in member["text"]
    assert 'textbox "Password" [ref=e2]: [password masked]' in password["text"]
    for value in (MEMBER_NUMBER, MEMBER_PASSWORD):
        assert value not in json.dumps(sub_api.inputs) and value not in json.dumps(api.inputs)
    fill_tool = next(tool for tool in sub_api.inputs[0]["tools"] if tool["name"] == "request_credential_fill")
    assert fill_tool["parameters"]["properties"]["credential"]["enum"] == ["member-number", "member-password"]
    assert "Pine Lodge member password" in fill_tool["description"]
    # The task was paused for each fill, and evidence records it without the value.
    paused, resumed = events(run, "browser.agent_paused"), events(run, "browser.agent_resumed")
    assert [(row["credential_name"], row["agent"], row["reason"]) for row in paused] == [
        ("member-number", "paused", "credential fill"), ("member-password", "paused", "credential fill")]
    assert [row["outcome"] for row in resumed] == ["filled", "filled"]
    assert run.leaked() == set() and result_of(run)["reporting_status"] == "completed"
    task_events = (run.path / "browser" / "browser-1" / "events.jsonl").read_text()
    assert '"credential_fill"' in task_events and MEMBER_PASSWORD not in task_events and MEMBER_NUMBER not in task_events
    assert result_of(run)["metrics"]["extensions"]["browser"]["credential_fills"] == 2
    # An environment credential is a host-private value of the run: hidden from views and evidence, kept for export.
    assert events(run, "private_registered")[0]["name"] == "browser-credential-member-password"
    assert MEMBER_PASSWORD in (run.path / "private" / "connector.private.json").read_text()
    # The posted body held both values: evidence keeps a keyed digest of it, never a plain one a guess could test.
    plain = hashlib.sha256(site.requests[0]["body"].encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", events(run, "browser.request_allowed")[0]["body_digest"])
    assert not [path for path in run.path.rglob("*") if path.is_file() and "private" not in path.parts
                and plain in path.read_text(errors="replace")]


async def test_ext_browser_a_credential_fills_only_on_its_own_sites(tmp_path):
    config = browser_config(allowed_origins=[SITE, OTHER], credentials={
        "other-pin": {"env": "OTHER_PIN", "allowed_origins": [OTHER]}})
    main = [main_call("browser_task_spawn", brief="Sign in.", url=LOGIN), answer("Not signed in.")]
    sub = [fill("e2", "other-pin"), say("Found: the saved credential is not for this site. Next: ask the user.")]
    driver, _api = agent(main)
    run, code, site, sub_api = await run_browser(tmp_path, sign_in(config), driver=driver, sub=sub,
                                                 environ={"OTHER_PIN": "other-pin-55120"})
    assert code == 0, run.summary()
    assert tool_replies(sub_api)[-1] == {"status": "denied", "generation": 1,
                                         "message": "The app does not fill other-pin on this site; nothing was filled."}
    assert site.typed == [] and events(run, "browser.agent_paused") == []


class CardIssuer(BaseExtension):
    """A stand-in for a wallet: issue_card() registers a single-use card number as a run-time private value."""
    config_model = NoConfig
    private_names = ("probe_card",)

    def tools(self, run):
        async def issue_card():
            run.register_private("probe_card", {"number": CARD, "expiry": "12/29"})
            return "A single-use card is ready in your user's wallet."
        return [ToolDef(issue_card, name="issue_card", description="Issue a single-use card.",
                        parameters=ToolParams(), parallel=False, max_output=0)]


async def test_ext_browser_a_private_credential_is_read_when_it_is_filled(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-issuer", CardIssuer)
    config = browser_config(credentials={"single-use-card": {"private": "probe_card", "pointer": "/number",
                                                             "allowed_origins": [SITE]}})
    document = sign_in(config)
    document["extensions"]["probe-issuer"] = {}
    main = [main_call("browser_task_spawn", brief="Enter the card for the $40 payment.", url=f"{SITE}/case/card-entry"),
            main_call("issue_card"), main_call("browser_task_steer", task_id="browser-1", message="The card is ready."),
            answer("The card was entered.")]
    sub = [fill("e2", "single-use-card"), say("Found: no card yet. Next: wait for the card."),
           fill("e2", "single-use-card"), say("Found: the card is entered. Next: pay.")]
    driver, api = agent(main)
    run, code, site, sub_api = await run_browser(tmp_path, document, driver=driver, sub=sub)
    assert code == 0, run.summary()
    assert tool_replies(sub_api, 1)[-1] == {"status": "unavailable", "generation": 1,
                                            "message": "single-use-card is not available yet; nothing was filled."}
    # Registered after the task started, the card is still hidden from the task's view and from evidence.
    assert site.typed == [(f"{SITE}/case/card-entry", "e2", CARD)]
    assert 'textbox "Card number" [ref=e2]: [REDACTED]' in tool_replies(sub_api)[-1]["text"]
    assert CARD not in json.dumps(sub_api.inputs) and CARD not in json.dumps(api.inputs) and run.leaked() == set()
    assert [row["outcome"] for row in events(run, "browser.agent_resumed")] == ["unavailable", "filled"]


def test_ext_browser_a_private_credential_must_be_declared_or_provided(tmp_path):
    from consumer_agent_testing_tool.connector_run import ConnectorRun
    from consumer_agent_testing_tool.connector_scenario import parse_scenario
    config = browser_config(credentials={"card": {"private": "nobody_registers", "allowed_origins": [SITE]}})
    loaded = parse_scenario(sign_in(config), base=tmp_path)
    run = ConnectorRun(loaded, driver=agent([answer("x")])[0], root=tmp_path, output=tmp_path / "runs")
    with pytest.raises(LabError, match="private.nobody_registers is neither declared nor registered"):
        next(extension for extension in run.extensions if extension.name == "browser").prepare(run)


async def test_ext_browser_a_credential_fills_only_the_page_s_own_fields(tmp_path):
    login = f"{SITE}/case/frame-login"
    main = [main_call("browser_task_spawn", brief="Sign in with my saved password.", url=login), answer("Signed in.")]
    sub = [fill("e2", "member-password"), fill("f1e3", "member-number"), fill("e9", "member-password"),
           fill("e4", "member-password"), say("Found: the password is entered. Next: sign in.")]
    driver, _api = agent(main)
    run, code, site, sub_api = await run_browser(tmp_path, sign_in(), driver=driver, sub=sub)
    assert code == 0, run.summary()
    # A field nested in an embedded frame (by its ancestor or its frame reference) is another document.
    framed = "only into this page's own fields, not into an embedded frame; nothing was filled."
    assert tool_replies(sub_api, 1)[-1] == {"status": "denied", "generation": 1,
                                            "message": f"The app fills member-password {framed}"}
    assert tool_replies(sub_api, 2)[-1]["message"] == f"The app fills member-number {framed}"
    assert tool_replies(sub_api, 3)[-1] == {"status": "stale-reference", "generation": 1,
                                            "message": "That reference is not in the latest snapshot; nothing was filled."}
    # Only the page's own field was filled; the refused requests never paused the task.
    assert site.typed == [(login, "e4", MEMBER_PASSWORD)]
    assert [row["credential_name"] for row in events(run, "browser.agent_paused")] == ["member-password"]


async def test_ext_browser_a_filled_number_stays_masked_when_the_page_formats_it(tmp_path):
    config = browser_config(credentials={"card": {"private": "card", "allowed_origins": [SITE]}})
    document = sign_in(config)
    document["private"]["card"] = CARD
    entry = f"{SITE}/case/card-format"
    main = [main_call("browser_task_spawn", brief="Enter the saved card for the $40 payment.", url=entry),
            answer("The card is entered.")]
    sub = [fill("e2", "card"), say("Found: the card is entered. Next: pay.")]
    driver, api = agent(main)
    run, code, site, sub_api = await run_browser(tmp_path, document, driver=driver, sub=sub)
    assert code == 0, run.summary()
    assert site.typed == [(entry, "e2", CARD)]
    # The page shows the number in groups of four: that form is masked too, in model views and evidence.
    grouped = " ".join(CARD[index:index + 4] for index in range(0, len(CARD), 4))
    assert 'textbox "Card number" [ref=e2]: [REDACTED]' in tool_replies(sub_api)[-1]["text"]
    for form in (CARD, grouped):
        assert form not in json.dumps(sub_api.inputs) and form not in json.dumps(api.inputs)
    assert grouped not in (run.path / "browser" / "browser-1" / "events.jsonl").read_text()
    assert run.leaked() == set()


async def test_ext_browser_the_wallet_s_single_use_card_is_no_credential_a_task_fills(tmp_path):
    from test_ext_wallet_support import Checkout, wallet_scenario, wallet_services

    from consumer_agent_testing_tool.connector_run import ConnectorRun
    from consumer_agent_testing_tool.connector_scenario import parse_scenario
    from consumer_agent_testing_tool.ext_browser import Services
    # The wallet binds a card only to a connector purchase the user approved; a website checkout is approved on
    # the browser's own card, so filling the wallet's number there would pay with a card nothing bound.
    document = wallet_scenario()
    document["extensions"]["browser"] = browser_config(credentials={
        "wallet": {"private": "wallet_card", "allowed_origins": [SITE], "description": "your user's wallet card"}})
    driver, _api = agent([main_call("browser_task_spawn", brief="Pay the $40 invoice with my wallet card.",
                                    url=f"{SITE}/case/card-entry"), answer("Paid.")])
    merchant, sites = Checkout(), []
    run = ConnectorRun(parse_scenario(document, base=tmp_path), driver=driver, root=tmp_path, output=tmp_path / "runs",
                       services=wallet_services(merchant))
    extension_of(run).services = Services(host=lambda root: sites.append(root))
    code = await run.run()
    result = result_of(run)
    assert code == 2 and result["failure_stage"] == "project", run.summary()  # Setup: before any model turn.
    assert "private.wallet_card is the wallet extension's single-use card" in result["detail"]
    # Refused before anything ran: no browser opened, no card issued or registered with the merchant.
    assert sites == [] and merchant.numbers() == [] and not events(run, "wallet.card_issued")
