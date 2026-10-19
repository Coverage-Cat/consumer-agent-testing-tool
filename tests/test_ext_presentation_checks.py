"""presentation (P2.8): the channel in standing context, WhatsApp and voice checks on the final answer, and
the fields Muse's own list widgets need, as evidence over the list results the model received."""
import copy
import json

import pytest
from test_connector_e2e_support import BOOKING, EXAMPLES, fixture, run_scenario
from test_connector_e2e_support import result_of as e2e_result
from test_connector_support import Hotel, native_script, result_of, run_connector, scenario

from consumer_agent_testing_tool.connector_run import ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_presentation import ANNOUNCEMENTS, BASES
from consumer_agent_testing_tool.serialization import load

PLAIN = "Booked: Garden double, $420.00 USD. Confirmation BK-1000. Details: https://cedar.example/b/BK-1000"


def channel(presentation, **overrides):
    """The booking scenario with the presentation extension; its outcome checks stay, the answer is not scored."""
    document = scenario(answer=None, **overrides)
    document["extensions"] = {"clock": {"now": "2026-11-30T09:00:00-08:00"}, "presentation": presentation}
    return document


async def trial(tmp_path, presentation, final=PLAIN, *, hotel=None, steps=None, **overrides):
    run, code, _ = await run_connector(tmp_path, channel(presentation, **overrides), hotel=hotel,
                                       driver=ScriptedDriver(steps or native_script(final)))
    return run, code, {check["id"]: check for check in result_of(run)["checks"]}


def presented(run):
    return result_of(run)["metrics"]["extensions"]["presentation"]


@pytest.mark.parametrize("final, findings", [
    (PLAIN, []),
    ("*Booked* the Garden double for $420.00:\n- 2 nights\n- _free cancellation_ until ~Dec 1~ Dec 2\n```BK-1000```", []),
    ("| Room | Total |\n|---|---|\n| Garden double | $420.00 |", ["a markdown table"]),
    ("<b>Booked</b> the Garden double for $420.00.", ["HTML markup"]),
    ("## Your booking\nGarden double, $420.00.", ["a markdown heading"]),
    ("See [your booking](https://cedar.example/b/BK-1000) for $420.00.",
     ["a markdown link (WhatsApp shows links as bare URLs)"]),
    ("![room](https://cedar.example/r.png) Booked for $420.00.", ["a markdown image"]),
])
async def test_whatsapp_answers_are_plain_text(tmp_path, final, findings):
    run, code, checks = await trial(tmp_path, {"channel": "whatsapp"}, final)
    check = checks["presentation.whatsapp_format"]
    assert check["findings"] == findings and check["channel"] == "whatsapp"
    assert (check["status"], code) == (("failed", 1) if findings else ("passed", 0)), run.summary()
    if findings:
        assert check["class"] == "presentation" and result_of(run)["failure_class"] == "presentation"
    assert checks["presentation.length"]["status"] == "passed" and checks["presentation.length"]["actual"] == {
        "characters": len(final)}


async def test_whatsapp_and_custom_lengths_are_bounded(tmp_path):
    _, code, checks = await trial(tmp_path, {"channel": "whatsapp"}, "x" * 4097)
    assert code == 1 and checks["presentation.length"]["detail"].endswith("4097 characters, over the 4096-character "
                                                                           "limit")
    _, code, checks = await trial(tmp_path, {"channel": "app", "max_characters": 40}, PLAIN)
    assert code == 1 and checks["presentation.length"]["expected"] == "at most 40 characters"
    assert "presentation.whatsapp_format" not in checks


@pytest.mark.parametrize("final, config, findings", [
    ("Your Garden double is booked for four hundred twenty dollars.", {}, []),
    ("Booked, confirmation BK-1000.", {}, ["identifiers from results (booking_id)"]),
    ("Booked, confirmation BK-1000.", {"speakable_ids": ["booking_id"]}, []),
    ("Booked room garden-2.", {"speakable_ids": ["booking_id"]}, ["identifiers from results (room_id)"]),
    ("Booked. Details at https://cedar.example/b.", {}, ["1 URL(s)"]),
    ("Booked:\n- Garden double\n- $420.00", {}, ["markdown or markup a voice cannot read"]),
    # Emphasis and code markup a voice reads out as symbols (EMPHASIS).
    ("Your stay is **booked** for four hundred twenty dollars.", {}, ["markdown or markup a voice cannot read"]),
    ("The `Garden double` is booked for four hundred twenty dollars.", {},
     ["markdown or markup a voice cannot read"]),
    ("Booked, reference 9f8e7d6c5b4a39281706f5e4.", {}, ["a machine identifier"]),
])
async def test_voice_answers_are_speakable(tmp_path, final, config, findings):
    run, code, checks = await trial(tmp_path, {"channel": "voice", **config}, final)
    check = checks["presentation.voice_speakable"]
    assert check["findings"] == findings
    assert code == (1 if findings else 0), run.summary()


async def test_voice_answers_are_short(tmp_path):
    run, code, checks = await trial(tmp_path, {"channel": "voice"}, "Your stay is booked. " * 30)
    assert code == 1 and checks["presentation.length"]["expected"] == "at most 500 characters"
    assert presented(run)["limit"] == 500 and presented(run)["answer"]["characters"] == 630


async def test_enforce_false_reports_findings_as_non_required_gaps(tmp_path):
    run, code, checks = await trial(tmp_path, {"channel": "whatsapp", "enforce": False}, "<b>Booked</b> for $420.00.")
    assert code == 0, run.summary()
    check = checks["presentation.whatsapp_format"]
    assert (check["status"], check["required"], check["class"]) == ("observation-gap", False, "observation gap")


async def test_the_channel_is_named_in_standing_context_unless_announce_is_off(tmp_path):
    for config, line in (({"channel": "whatsapp"}, ANNOUNCEMENTS["whatsapp"]), ({"channel": "voice"}, ANNOUNCEMENTS["voice"])):
        run, _, _ = await trial(tmp_path, config)
        assert run.system_prompt().endswith(line)
    for config in ({"channel": "whatsapp", "announce": False}, {"channel": "app"}):
        run, code, checks = await trial(tmp_path, config, "| A | B |\n|---|---|\n| 1 | 2 |")
        assert not any(line in run.system_prompt() for line in ANNOUNCEMENTS.values())
    assert code == 0 and not any(identifier.startswith("presentation.") and identifier != "presentation.widget_fields"
                                 for identifier in checks)  # The app channel has no format checks.
    assert BASES["channels"]["basis"] == "documented" and BASES["voice"]["basis"] == "assumed"


class Listings(Hotel):
    """Search results as a list widget would need them, or deliberately missing currency or structure."""

    def __init__(self, rooms, *, text_only=False, ui=False):
        super().__init__()
        self.rooms, self.text_only, self.ui = rooms, text_only, ui

    def call(self, name, arguments):
        result = super().call(name, arguments)
        if name == "search_rooms":
            data = {"rooms": copy.deepcopy(self.rooms)}
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": False,
                      **({} if self.text_only else {"structuredContent": data})}
            if self.ui:
                result["content"].append({"type": "resource", "resource": {
                    "uri": "ui://cedar/rooms", "mimeType": "text/html;profile=mcp-app", "text": "<div>rooms</div>"}})
        return result


async def test_list_results_report_the_fields_a_widget_shows(tmp_path):
    run, code, checks = await trial(tmp_path, {})  # The default hotel: names and ids only.
    assert code == 0, run.summary()
    gap = checks["presentation.widget_fields"]
    assert (gap["status"], gap["required"]) == ("observation-gap", False)
    assert gap["actual"] == {"search_rooms /structuredContent/rooms": ["price", "status", "link"]}
    full = [{"room_id": "garden-2", "name": "Garden double", "price": {"amount": 21000, "currency": "USD"},
             "availability": "available", "url": "https://cedar.example/rooms/garden-2"},
            {"room_id": "loft-5", "title": "Loft king", "nightly_rate": "$240.00", "status": "few left",
             "link": "https://cedar.example/rooms/loft-5"}]
    run, code, checks = await trial(tmp_path, {}, hotel=Listings(full))
    assert "presentation.widget_fields" not in checks
    assert presented(run)["widgets"]["lists"] == [{
        "tool": "search_rooms", "path": "/structuredContent/rooms", "records": 2, "seen": 1,
        "fields": {"title": 2, "price": 2, "status": 2, "id": 2, "link": 2}, "price_without_currency": 0}]
    bare = [{"room_id": "garden-2", "name": "Garden double", "nightly": 21000}]
    search_only = [native_script()[0], {"answer": "The Garden double is available."}]
    run, code, checks = await trial(tmp_path, {"widget_fields": ["price"]}, hotel=Listings(bare, text_only=True, ui=True),
                                    steps=search_only, checks=[])
    assert code == 0, run.summary()
    row = presented(run)["widgets"]["lists"][0]
    assert row["path"] == "/content/0/text/rooms" and row["price_without_currency"] == 1
    assert checks["presentation.widget_fields"]["actual"] == {"search_rooms /content/0/text/rooms": ["price"]}
    assert presented(run)["ui_resources_hidden"] == 1  # The projection never shows MCP Apps UI; counted here.
    run, _, checks = await trial(tmp_path, {"widget_fields": ["title", "id"]})
    assert "presentation.widget_fields" not in checks


async def test_without_a_final_answer_only_the_results_are_judged(tmp_path):
    # The agent stops at its turn limit; the runner still calls on_final (final_text None). It said nothing, so no
    # channel check passes or fails (never a vacuous pass); the list results it received still report their
    # widget fields.
    limits = {"model_turns": 2, "tool_calls": 12, "wall_seconds": 120}
    for presentation in ({"channel": "voice"}, {"channel": "whatsapp", "max_characters": 300}):
        steps = [native_script()[0]] * 3 + [{"answer": "unreached"}]
        run, code, checks = await trial(tmp_path, presentation, steps=steps, limits=limits)
        assert code == 1 and result_of(run)["answer"]["final_answer"] is None, run.summary()
        assert [identifier for identifier in checks if identifier.startswith("presentation.")] == [
            "presentation.widget_fields"]
        assert checks["presentation.widget_fields"]["actual"] == {
            "search_rooms /structuredContent/rooms": ["price", "status", "link"]}
        assert presented(run)["answer"] is None and presented(run)["results"] == 2


async def test_both_extensions_on_the_real_fixture(tmp_path):
    scenario_ = load(EXAMPLES / "injection-quarantine.yaml")
    scenario_["extensions"]["presentation"] = {"channel": "whatsapp", "widget_fields": ["title", "price", "id"]}
    final = f"Booked: Garden Queen, Dec 4-6, {BOOKING}, $448.36 USD."
    async with fixture() as running:
        run, code = await run_scenario(tmp_path, running, scenario_,
                                       script=[*load(EXAMPLES / "fixture-booking.calls.json")[:-1], {"answer": final}])
    assert code == 0, run.summary()
    metrics = e2e_result(run)["metrics"]["extensions"]
    rooms = metrics["presentation"]["widgets"]["lists"][0]
    # The fixture states one currency for the whole search: its rooms' prices count.
    assert rooms["fields"] == {"title": 4, "price": 4, "status": 0, "id": 4, "link": 0} and rooms["records"] == 4
    assert metrics["content_trust"]["flags"]["true_positives"] == 2
    assert run.system_prompt().endswith(ANNOUNCEMENTS["whatsapp"])


@pytest.mark.parametrize("config, message", [
    ({"channel": "sms"}, "channel"), ({"max_characters": 5}, "max_characters"),
    ({"widget_fields": ["photo"]}, "widget_fields"), ({"enforce": "yes"}, "enforce"), ({"colour": "red"}, "colour"),
])
def test_presentation_config_is_strict(tmp_path, config, message):
    with pytest.raises(LabError, match=message):
        parse_scenario(channel(config), base=tmp_path)
