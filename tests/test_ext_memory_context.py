"""The memory extension's standing context and config: Memory.md, Soul.md and Identity.md-like files in the
system prompt, stale facts as ordinary memory lines, strict validation and binding to the scenario."""
import re

import pytest
from test_connector_multi_support import NoConfig, multi_services, ready, two_connectors
from test_connector_support import ORIGIN, agent, answer, result_of, run_connector, scenario

from consumer_agent_testing_tool.connector_run import EXTENSIONS, BaseExtension
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_memory import (
    BASES,
    covered,
    fields,
    personal_field,
    read,
    segments,
)

QUOTE_FACT = {"id": "saved-quote", "text": "Taylor's saved Cedar Stays quote is q-9-garden-2.", "value": "q-9-garden-2",
              "live": [{"tool": "get_quote", "pointer": "/structuredContent/quote_id"}]}


async def test_memory_files_are_standing_context_after_the_date_line(tmp_path):
    memory = {"memory": "- Taylor prefers ground-floor rooms.\n- Taylor travels with a partner.",
              "soul": "Warm, brief and practical.", "identity": "Name: Juniper. A calm owl.",
              "stale": [QUOTE_FACT, {**QUOTE_FACT, "id": "repeat", "text": "Taylor prefers ground-floor rooms."}]}
    run = await ready(tmp_path, scenario(extensions={"clock": {"now": "2026-11-30T09:00:00-08:00"},
                                                     "memory": memory}))
    prompt = run.system_prompt()
    tail = prompt[prompt.index("Current date and time"):]
    remembered = ["- Taylor prefers ground-floor rooms.", "- Taylor travels with a partner.",
                  "- Taylor's saved Cedar Stays quote is q-9-garden-2."]  # A stale fact reads like any other line.
    assert tail.splitlines()[1:] == ["", "Memory.md:", *remembered, "", "Soul.md:", "Warm, brief and practical.", "",
                                     "Identity.md:", "Name: Juniper. A calm owl."]
    # The scenario's own profile memory stays where P1 put it; nothing names a fact as stale.
    assert "What you remember about your user:\n- Prefers ground-floor rooms." in prompt
    assert "stale" not in prompt.lower() and "re-read" not in prompt.lower()
    metrics = run.extensions[[extension.name for extension in run.extensions].index("memory")].metrics(run)
    assert metrics["files"] == {"Memory.md": {"lines": 3, "characters": len("\n".join(remembered))},
                                "Soul.md": {"lines": 1, "characters": 26}, "Identity.md": {"lines": 1, "characters": 26}}
    assert {entry["basis"] for entry in metrics["bases"].values()} == {"documented", "assumed"}
    assert set(metrics["bases"]) == set(BASES)


@pytest.mark.parametrize(("memory", "message"), [
    ({}, "configure memory, soul, identity, stale or needs"),
    ({"memory": ""}, "at least 1 character"),
    ({"stale": [QUOTE_FACT, QUOTE_FACT]}, "stale fact ids must be unique"),
    ({"stale": [{**QUOTE_FACT, "value": "  "}]}, "stale.saved-quote.value must not be blank"),
    ({"stale": [{**QUOTE_FACT, "value": True}]}, "Input should be a valid"),
    ({"stale": [{**QUOTE_FACT, "live": []}]}, "at least 1 item"),
    ({"stale": [{**QUOTE_FACT, "live": [{"tool": "get_quote", "pointer": "quote_id"}]}]}, "must be a JSON pointer"),
    ({"stale": [{**QUOTE_FACT, "answer": ["("]}]}, "stale.saved-quote.answer: invalid regular expression"),
    ({"needs": {"get_quote": []}}, "needs.get_quote: name a tool and list 1 to 100 pointers"),
    ({"needs": {"get_quote": ["total"]}}, "needs.get_quote must be a JSON pointer"),
    ({"needs": {"get_quote": [""]}}, "pointers name fields, not the whole result"),
    ({"memory": "x", "require_minimal": True}, "require_minimal needs needs"),
    ({"memory": "x", "notes": "y"}, "Extra inputs are not permitted"),
])
def test_memory_config_is_strict_and_fails_closed(memory, message):
    with pytest.raises(LabError, match=re.escape(message)):
        parse_scenario(scenario(extensions={"memory": memory}))


@pytest.mark.parametrize(("document", "message"), [
    (two_connectors(extensions={"memory": {"stale": [QUOTE_FACT]}}),
     "stale.saved-quote.live: with several connectors, name the connector"),
    (scenario(extensions={"memory": {"stale": [{**QUOTE_FACT, "live": [
        {"connector": "birch-inn", "tool": "get_quote", "pointer": "/structuredContent/quote_id"}]}]}}),
     "stale.saved-quote.live names an undeclared connector, birch-inn"),
    (scenario(extensions={"memory": {"stale": [{**QUOTE_FACT, "live": [
        {"tool": "cancel_booking", "pointer": "/structuredContent/status"}]}]}}),
     "names cancel_booking, which cedar-stays does not offer the model"),
    (two_connectors(extensions={"memory": {"needs": {"get_quote": ["/structuredContent/total_minor"]}}}),
     "needs.get_quote: with several connectors, name each tool as <connector>/<tool>"),
    (scenario(extensions={"memory": {"memory": "Taylor's email is taylor.private@example.com."}}),
     "shows the model private.email in a memory file"),
])
async def test_memory_config_binds_to_the_scenario_at_setup(tmp_path, document, message):
    with pytest.raises(LabError, match=re.escape(message)):
        await ready(tmp_path, document, multi_services())


class Surface(BaseExtension):
    """A service of the extension's own (connectors: []), like custom_connector's HTTP API."""
    config_model = NoConfig
    provides_surface = True


def without_connectors(memory):
    return scenario(connectors=[], expected={"tools": []}, answer=None,
                    extensions={"probe-surface": {}, "memory": memory},
                    checks=[{"id": "nothing-booked", "source": "oracle", "expect": {"op": "eq", "value": 0},
                             "oracle": {"url": f"{ORIGIN}/__oracle/state", "pointer": "/confirmed_count"}}])


@pytest.mark.parametrize(("memory", "message"), [
    ({"stale": [QUOTE_FACT]},
     "stale.saved-quote.live: this scenario declares no connectors, and memory observes only connector calls"),
    ({"needs": {"get_quote": ["/structuredContent/total_minor"]}},
     "needs.get_quote: this scenario declares no connectors"),
])
async def test_memory_reads_need_a_connector_when_the_scenario_declares_none(tmp_path, monkeypatch, memory, message):
    monkeypatch.setitem(EXTENSIONS, "probe-surface", Surface)
    with pytest.raises(LabError, match=re.escape(message)):
        await ready(tmp_path, without_connectors(memory))
    # Memory files alone are standing context, with or without connectors.
    run = await ready(tmp_path, without_connectors({"memory": "- Taylor prefers quiet rooms."}))
    assert "Memory.md:\n- Taylor prefers quiet rooms." in run.system_prompt()


async def test_a_memory_binding_error_stops_the_trial_before_the_model_is_asked(tmp_path):
    driver, api = agent([answer("unused")])
    document = scenario(extensions={"memory": {"memory": "Reach Taylor at taylor.private@example.com."}})
    run, code, _ = await run_connector(tmp_path, document, driver=driver)
    assert code == 2 and api.inputs == [] and "private.email" in result_of(run)["detail"]


def test_fields_pointers_and_personal_data_are_read_like_the_scenario_reads_results():
    body = {"isError": False, "content": ['{"guest": {"full_name": "Taylor Example"}, "rooms": [{"id": "a"}]}'],
            "structuredContent": {"booking_id": "b-1", "guest_email": "t@example.com", "rooms": [{"id": "a"},
                                                                                                  {"id": "b"}],
                                  "contact": {"note": "write to t@example.com"}, "a/b": 1, "room_name": "Loft"}}
    paths, personal = fields(body)
    assert paths == {"/structuredContent/booking_id", "/structuredContent/guest_email", "/structuredContent/rooms/*/id",
                     "/structuredContent/contact/note", "/structuredContent/a~1b", "/structuredContent/room_name"}
    assert personal == {"/structuredContent/guest_email", "/structuredContent/contact/note"}
    # Without structuredContent, JSON text content is the result's data, at the raw /content/<n>/text pointer.
    text_only = {"content": [body["content"][0]]}
    assert fields(text_only) == ({"/content/0/text/guest/full_name", "/content/0/text/rooms/*/id"},
                                 {"/content/0/text/guest/full_name"})
    assert read(text_only, "/content/0/text/guest/full_name") == "Taylor Example"  # A native rendering.
    assert read({"content": [{"type": "text", "text": '{"total": 5}'}]}, "/content/0/text/total") == 5  # muse-cli.
    assert covered("/structuredContent/rooms/*/id", [segments("/structuredContent/rooms/0")])
    assert not covered("/structuredContent/room_name", [segments("/structuredContent/rooms")])
    assert personal_field("guest_name", "Taylor") and not personal_field("room_name", "Loft")
    assert not personal_field("search_session_id", "ss-1") and personal_field("phone_number", "+1 206")
