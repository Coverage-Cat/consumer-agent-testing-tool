"""What the model sees of extension and relayed results, the hooks around approval and dispatch, and
on_final for trials that end without a final answer."""
import json
from typing import Any, ClassVar

import pytest
from inspect_ai.tool import ToolDef, ToolParams
from test_connector_multi_support import STAY, NoConfig, ready, tool_messages
from test_connector_support import (
    API_KEY,
    Hotel,
    agent,
    answer,
    booking_responses,
    call,
    events,
    result_of,
    run_connector,
    scenario,
)

from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.projection import render_result

EMAIL = "taylor.private@example.com"
PAGE = {"next_page_token": "pg-ss-0001-10", "quote_token": "qt-0042-abc", "search_session_id": "ss-7731",
        "access_token": "tok-9f8e7d6c5b4a", "contact": EMAIL, "rooms": [{"id": "garden-2"}]}


def tool(function, name):
    return ToolDef(function, name=name, description=f"{name}.", parameters=ToolParams(), parallel=False, max_output=0)


class Pager(BaseExtension):
    """A service of its own: a JSON page with functional tokens, and a text answer naming secrets."""
    config_model = NoConfig

    def tools(self, run):
        async def list_page(**kwargs: Any):
            return dict(PAGE)

        async def say_text(**kwargs: Any):
            return f"Signed in with api_key={API_KEY} for {EMAIL}."
        return [tool(list_page, "list_page"), tool(say_text, "say_text")]


async def test_extension_json_output_keeps_functional_tokens_and_hides_credentials(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-pager", Pager)
    driver, api = agent([call("list_page"), call("say_text"), answer("Listed.")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-pager": {}}, checks=[], answer=None),
                                       driver=driver)
    assert code == 0, run.summary()
    page, text = (message["content"] for message in tool_messages(api))
    # Masked like a connector result: what the model needs to continue stays, credentials and private values go.
    assert json.loads(page) == {**PAGE, "access_token": "[REDACTED]", "contact": "[REDACTED]"}
    assert API_KEY not in text and EMAIL not in text and text.count("[REDACTED]") == 2
    assert events(run, "tool_result")[0]["characters"] == len(page)


class Keyed(BaseExtension):
    """Returns JSON text whose keys, nesting and repeated keys carry the private email."""
    config_model = NoConfig

    def tools(self, run):
        async def by_contact(**kwargs: Any):
            return json.dumps({EMAIL: {"rooms": 2}})

        async def repeated(**kwargs: Any):
            return f'{{"contact": "{EMAIL}", "contact": "on file"}}'

        async def deep(**kwargs: Any):
            return json.dumps(json.loads("[" * 70 + json.dumps(EMAIL) + "]" * 70))
        return [tool(by_contact, "by_contact"), tool(repeated, "repeated"), tool(deep, "deep")]


async def test_extension_json_output_hides_private_values_in_keys_repeated_keys_and_deep_nesting(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-keyed", Keyed)
    driver, api = agent([call("by_contact"), call("repeated"), call("deep"), answer("Listed.")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-keyed": {}}, checks=[], answer=None),
                                       driver=driver)
    assert code == 0, run.summary()
    keyed, repeated, deep = (message["content"] for message in tool_messages(api))
    assert EMAIL not in json.dumps(api.inputs)
    assert json.loads(keyed) == {"[REDACTED]": {"rooms": 2}}
    assert json.loads(repeated) == {"contact": "on file"}  # As a parser reads it, never the earlier value.
    assert "[REDACTED: nested deeper than 64 levels]" in deep


class Labeler(BaseExtension):
    """Labels other extensions' results (a trust stage), and offers a tool of its own."""
    config_model = NoConfig
    seen: ClassVar[list] = []

    def tools(self, run):
        async def own_tool(**kwargs: Any):
            return "plain text"
        return [tool(own_tool, "own_tool")]

    async def on_extension_result(self, run, extension, tool, result):
        Labeler.seen.append((extension, tool, json.dumps(result)))
        if result["output"] == "break":
            return ["not", "an", "object"]
        return {**result, "provenance": {"source": extension, "untrusted": True}}


async def test_other_extensions_see_extension_results_before_masking(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-pager", Pager)
    monkeypatch.setitem(EXTENSIONS, "probe-labeler", Labeler)
    Labeler.seen = []
    driver, api = agent([call("list_page"), call("own_tool"), answer("Listed.")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-pager": {}, "probe-labeler": {}},
                                                          checks=[], answer=None), driver=driver)
    assert code == 0, run.summary()
    page, own = (message["content"] for message in tool_messages(api))
    assert json.loads(page) == {"source": "probe-pager", "tool": "list_page",
                                "output": {**PAGE, "access_token": "[REDACTED]", "contact": "[REDACTED]"},
                                "provenance": {"source": "probe-pager", "untrusted": True}}
    assert own == "plain text"  # An extension's own hook never sees its own output.
    assert [(row[0], row[1]) for row in Labeler.seen] == [("probe-pager", "list_page")]
    assert EMAIL in Labeler.seen[0][2] and EMAIL not in json.dumps(api.inputs)  # Masked after the hooks.
    monkeypatch.setattr(Pager, "tools", lambda self, run: [tool(lambda **kwargs: _async("break"), "list_page")])
    driver, api = agent([call("list_page"), answer("unused")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-pager": {}, "probe-labeler": {}},
                                                          checks=[], answer=None), driver=driver)
    assert code == 2 and "not an object" in result_of(run)["detail"] and len(api.inputs) == 1


async def _async(value):
    return value


class Tagger(BaseExtension):
    """Tags every connector result the model sees and records the call it learned about."""
    config_model = NoConfig
    calls: ClassVar[list] = []

    async def on_tool_result(self, run, connector, tool, result, call=None):
        Tagger.calls.append((tool, call))
        return {**result, "tagged": True}


async def test_model_view_renders_relayed_outcomes_through_every_result_hook(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-tagger", Tagger)
    Tagger.calls = []
    run = await ready(tmp_path, scenario(extensions={"probe-tagger": {}}))
    outcome = await run.call_tool("cedar-stays", "get_quote", {"room_id": "garden-2", **STAY}, source="job")
    shown = json.loads(await run.model_view("cedar-stays", "get_quote", outcome))
    assert shown["tagged"] is True and shown["source"] == "cedar-stays" and shown["untrusted"] is True
    assert shown["result"]["structuredContent"]["quote_id"] == "q-1-garden-2"
    assert Tagger.calls == [("get_quote", {"dispatched": True, "kind": "result", "origin": "relayed", "card_id": None,
                                           "decision": None})]
    assert len(await run.model_view("cedar-stays", "get_quote", outcome, limit=200)) <= 200
    declined = json.loads(await run.model_view("cedar-stays", "create_booking", {
        "kind": "declined", "message": "The user declined this action."}))
    assert declined == {"source": "app", "tool": "create_booking", "message": "The user declined this action.",
                        "tagged": True}
    # The host's own form (an envelope it rendered) still works, without a call for the hooks.
    legacy = json.loads(await run.model_view("cedar-stays", "get_quote", render_result(
        "cedar-stays", "get_quote", outcome["result"]), "result"))
    assert legacy["tagged"] is True and Tagger.calls[-1] == ("get_quote", None)
    for args, options, message in ((("cedar-stays", "get_quote", {"result": {}}), {}, "takes a call outcome"),
                                   (("elsewhere", "get_quote", outcome), {}, "No connector named"),
                                   (("cedar-stays", "get_quote", outcome), {"limit": 0}, "positive number")):
        with pytest.raises(LabError, match=message):
            await run.model_view(*args, **options)


class Gate(BaseExtension):
    """Sees every approval decision before dispatch and vetoes approved bookings (a wallet out of cards)."""
    config_model = NoConfig
    decisions: ClassVar[list] = []

    async def after_approval(self, run, connector, tool, arguments, decision):
        Gate.decisions.append((tool, decision["allowed"], decision["kind"], decision["origin"],
                               arguments.get("guest_email")))
        if tool == "create_booking":
            return {"error": "The app's wallet could not issue a card. Nothing was sent to the service."}
        return {"gate": "open"} if tool == "get_quote" else None


async def test_after_approval_sees_every_decision_and_may_still_veto(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-gate", Gate)
    for hotel, allowed, kind, told in ((Hotel(), True, "allow_once", "could not issue a card"),
                                       (Hotel(total_minor=61000), False, "deny", "The user declined this action.")):
        Gate.decisions = []
        driver, api = agent(booking_responses())
        run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-gate": {}}), driver=driver,
                                           hotel=hotel)
        assert code == 1 and [name for name, _ in hotel.calls] == ["search_rooms", "get_quote"]
        assert Gate.decisions == [("search_rooms", True, "allow_task", "model", None),
                                  ("get_quote", True, "allow_task", "model", None),
                                  ("create_booking", allowed, kind, "model", EMAIL)]  # Host-private values too.
        assert told in json.loads(tool_messages(api)[2]["content"])["message"]
        vetoed = events(run, "dispatch_vetoed")
        assert [(row["stage"], row["card_id"]) for row in vetoed] == ([("after_approval", "card-3")] if allowed else [])
        assert [row["annotations"] for row in events(run, "dispatch_annotated")] == [{"gate": "open"}]
        checks = {check["id"]: check for check in result_of(run)["checks"]}
        assert checks["approval.dispatches_match"]["actual"]["dispatched"] == 2


async def test_on_tool_result_learns_whether_the_call_was_dispatched(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-tagger", Tagger)
    Tagger.calls = []
    driver, _ = agent(booking_responses())
    _, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-tagger": {}}), driver=driver,
                                     hotel=Hotel(total_minor=61000))
    assert code == 1
    assert Tagger.calls == [
        ("search_rooms", {"dispatched": True, "kind": "result", "origin": "native", "card_id": "card-1",
                          "decision": "allow_task"}),
        ("get_quote", {"dispatched": True, "kind": "result", "origin": "native", "card_id": "card-2",
                       "decision": "allow_task"}),
        ("create_booking", {"dispatched": False, "kind": "declined", "origin": "native", "card_id": "card-3",
                            "decision": "deny"})]


class Final(BaseExtension):
    """Reports a check from on_final, whatever the agent said."""
    config_model = NoConfig
    texts: ClassVar[list] = []

    def tools(self, run):
        async def broken(**kwargs: Any):
            raise RuntimeError("extension bug")
        return [tool(broken, "broken")]

    async def on_final(self, run, final_text):
        Final.texts.append(final_text)
        return [{"id": "probe.final", "status": "passed"}]


async def test_on_final_runs_without_a_final_answer(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-final", Final)
    Final.texts = []
    limits = {"model_turns": 2, "tool_calls": 12, "wall_seconds": 120}
    driver, _ = agent([call("search_rooms", **STAY), call("search_rooms", **STAY), answer("unreached")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-final": {}}, limits=limits),
                                       driver=driver)
    assert code == 1 and result_of(run)["answer"]["final_answer"] is None
    assert Final.texts == [None] and "probe.final" in {check["id"] for check in result_of(run)["checks"]}
    # A setup-class failure before any dispatch still concludes nothing, extension checks included.
    driver, _ = agent([call("broken"), answer("unused")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-final": {}}), driver=driver)
    assert code == 2 and Final.texts == [None] and [check["id"] for check in result_of(run)["checks"]] == [
        "execution"]


async def test_answer_format_checks_need_a_final_answer(tmp_path):
    limits = {"model_turns": 2, "tool_calls": 12, "wall_seconds": 120}
    document = scenario(extensions={"presentation": {"channel": "whatsapp", "max_characters": 500}}, limits=limits)
    driver, _ = agent([call("search_rooms", **STAY), call("search_rooms", **STAY), answer("unreached")])
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 1
    result = result_of(run)
    # No answer to judge for format or length; the widget-field gap judges results and is still reported.
    assert [check["id"] for check in result["checks"] if check["id"].startswith("presentation.")] == [
        "presentation.widget_fields"]
    assert result["metrics"]["extensions"]["presentation"]["answer"] is None
    driver, _ = agent([answer("| a | b |\n|---|---|\n| 1 | 2 |")])
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"presentation": {"channel": "whatsapp"}},
                                                          checks=[], answer=None), driver=driver)
    checks = {check["id"]: check for check in result_of(run)["checks"]}
    assert checks["presentation.whatsapp_format"]["findings"] == ["a markdown table"]  # Judged when there is one.
