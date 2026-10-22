"""Instinct selection changes the host, not the inference provider. All traffic is local."""
import json

import pytest
from test_connector_support import (
    API_KEY,
    Hotel,
    agent,
    booking_responses,
    events,
    public_text,
    result_of,
    run_connector,
    scenario,
)

from consumer_agent_testing_tool.connector_run import FIRST_LINE, effective_scenario
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback


def test_instinct_is_an_explicit_target_and_muse_remains_the_default():
    default = parse_scenario(scenario()).scenario
    instinct = parse_scenario(scenario(emulation_target="instinct")).scenario
    assert default.emulation_target == "muse"
    assert instinct.emulation_target == "instinct"
    assert instinct.connectors[0].client_profile == "generic-sdk"
    assert instinct.projection == "native"
    assert effective_scenario(default, emulation_target="instinct") == instinct


@pytest.mark.parametrize("change", [
    {"projection": "muse-cli"},
    {"extensions": {"presentation": {"channel": "app"}}},
    {"extensions": {"presentation": {"channel": "text", "widget_fields": ["price"]}}},
])
def test_instinct_rejects_unported_muse_treatments_before_any_run(change):
    with pytest.raises(LabError, match="Instinct"):
        parse_scenario(scenario(emulation_target="instinct", **change))


def test_instinct_rejects_muse_transport_in_scenarios_and_overrides():
    doc = scenario(emulation_target="instinct")
    doc["connectors"][0]["client_profile"] = "muse-template-unofficial-2026-10"
    with pytest.raises(LabError, match="Instinct"):
        parse_scenario(doc)
    with pytest.raises(LabError, match="Instinct"):
        effective_scenario(parse_scenario(scenario(emulation_target="instinct")).scenario,
                           client_profile="muse-template-unofficial-2026-10")


async def test_instinct_scripted_run_has_target_evidence_and_keeps_host_secrets(tmp_path):
    run, code, hotel = await run_connector(tmp_path, scenario(emulation_target="instinct"),
                                         hotel=Hotel(echo_key=True))
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["emulation_target"] == "instinct"
    assert result["interface_profile"]["name"] == "instinct-connector-surrogate-2026-10-21"
    assert result["run_lock"]["emulation_target"] == "instinct"
    assert result["run_lock"]["bases"]["permission-classes"]["basis"] == "assumed"
    assert result["guideline_findings"] is None
    assert result["metrics"]["extensions"]["presentation"]["channel"] == "text"
    assert result["approvals"]["basis"]["rules"]["platform-default"]["basis"] == "assumed"
    assert "Muse" not in json.dumps(result["approvals"])
    assert "muse.ai" not in json.dumps(events(run, "projection")[0]["basis"])
    assert any(check["id"] == "presentation.text_format" for check in result["checks"])
    assert all("instinct" in row["profile"] for row in events(run))
    assert playback(run.path)["integrity"] == "verified"
    assert API_KEY not in public_text(run)
    assert "taylor.private@example.com" not in public_text(run)
    assert len(hotel.calls) == 3


async def test_instinct_classification_keeps_conservative_rules_without_muse_citations(tmp_path):
    hotel = Hotel()
    hotel.catalog.append({
        "name": "manage_messages", "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"action": {"enum": ["list", "send"]}}}})
    doc = scenario(emulation_target="instinct")
    doc["permissions"]["important_reads"] = ["get_quote"]
    run, code, _ = await run_connector(tmp_path, doc, hotel=hotel)
    assert code == 0, run.summary()
    classified = {item["tool"]: item for item in events(run, "classification")[0]["classes"]}
    assert classified["manage_messages"]["tool_class"] == "sensitive-write"
    assert any("combines reads and writes" in reason for reason in classified["manage_messages"]["reasons"])
    assert any("important read" in reason for reason in classified["get_quote"]["reasons"])
    text = json.dumps(classified)
    assert "guidelines" not in text and "Help Center" not in text
    assert "CATT assumption for Instinct" in text


async def test_instinct_agent_prompt_and_subrun_are_target_specific_without_a_model_request(tmp_path):
    driver, api = agent(booking_responses())
    run, code, _ = await run_connector(tmp_path, scenario(emulation_target="instinct"), driver=driver)
    assert code == 0, run.summary()
    prompt = api.inputs[0]["messages"][0]["content"]
    assert not prompt.startswith(FIRST_LINE)
    assert "text messages" in prompt
    assert "Muse" not in prompt and "Instinct" not in prompt
    for word in ("oracle", "surrogate", "fixture", "benchmark"):
        assert word not in prompt.lower()
    result = result_of(run)
    manifest = json.loads((run.path / result["agent_runs"][0] / "manifest.json").read_text())
    assert manifest["run_lock"]["emulation_target"] == "instinct"
    assert manifest["run_lock"]["bases"] == result["run_lock"]["bases"]
    assert result["compatibility_pass"] is False


async def test_instinct_text_channel_detects_markup_without_rewriting_the_answer(tmp_path):
    from consumer_agent_testing_tool.connector_run import ScriptedDriver

    text = "## Booking\nSee [the booking](https://example.com/booking)."
    driver = ScriptedDriver([{"answer": text}])
    run, code, _ = await run_connector(
        tmp_path, scenario(emulation_target="instinct", checks=[], answer=None), driver=driver)
    assert code == 1
    result = result_of(run)
    assert result["answer"]["final_answer"] == text
    assert next(check for check in result["checks"]
                if check["id"] == "presentation.text_format")["status"] == "failed"


async def test_muse_can_opt_into_the_assumed_text_treatment(tmp_path):
    from consumer_agent_testing_tool.connector_run import ScriptedDriver

    text = "A plain message. " * 400
    doc = scenario(checks=[], answer=None, extensions={"presentation": {"channel": "text", "widget_fields": []}})
    run, code, _ = await run_connector(tmp_path, doc, driver=ScriptedDriver([{"answer": text}]))
    assert code == 0, run.summary()
    result = result_of(run)
    assert result["emulation_target"] == "muse" and result["answer"]["final_answer"] == text
    assert next(row for row in result["checks"] if row["id"] == "presentation.text_format")["status"] == "passed"
    assert not any(row["id"] == "presentation.length" for row in result["checks"])
    assert result["metrics"]["extensions"]["presentation"]["bases"]["text"]["basis"] == "assumed"


def test_switching_a_cli_scenario_validates_the_combined_treatments():
    doc = scenario(projection="muse-cli")
    doc["connectors"][0]["client_profile"] = "muse-template-unofficial-2026-10"
    loaded = parse_scenario(doc)
    result = effective_scenario(loaded.scenario, emulation_target="instinct", projection="native",
                                client_profile="generic-sdk")
    assert result.emulation_target == "instinct"
    assert result.projection == "native" and result.connectors[0].client_profile == "generic-sdk"
    assert loaded.scenario.projection == "muse-cli"  # No mutation of the caller's scenario.


@pytest.mark.parametrize(("config", "text", "status", "check"), [
    ({}, "A plain message. " * 400, "passed", "presentation.text_format"),
    ({"max_characters": 25}, "A message longer than the configured limit.", "failed", "presentation.length"),
    ({"enforce": False}, "## A heading", "observation-gap", "presentation.text_format"),
    ({"channel": "voice"}, "See https://example.com.", "failed", "presentation.voice_speakable"),
    ({"channel": "whatsapp"}, "See https://example.com.", "passed", "presentation.whatsapp_format"),
])
async def test_instinct_channel_defaults_and_explicit_treatments(tmp_path, config, text, status, check):
    from consumer_agent_testing_tool.connector_run import ScriptedDriver

    doc = scenario(emulation_target="instinct", checks=[], answer=None,
                   extensions={"presentation": config})
    run, code, _ = await run_connector(tmp_path, doc, driver=ScriptedDriver([{"answer": text}]))
    result = result_of(run)
    assert code == (1 if status == "failed" else 0), run.summary()
    assert next(row for row in result["checks"] if row["id"] == check)["status"] == status
    assert not any(row["id"] == "presentation.widget_fields" for row in result["checks"])
    if not config:
        assert not any(row["id"] == "presentation.length" for row in result["checks"])
    assert result["answer"]["final_answer"] == text


async def test_an_instinct_run_cannot_change_the_next_muse_runs_provenance(tmp_path):
    from consumer_agent_testing_tool.connector_run import BASES
    from consumer_agent_testing_tool.emulation import policy_record
    from consumer_agent_testing_tool.permissions import RULE_BASIS

    original = json.dumps({"host": BASES, "permissions": RULE_BASIS}, sort_keys=True)
    run, code, _ = await run_connector(tmp_path, scenario(emulation_target="instinct"))
    assert code == 0
    judgments = events(run, "answer_score")[0]["score"]["judgments"]
    assert all(row["basis"] == "assumed" and row["source"] is None for row in judgments)
    policy = policy_record("instinct")
    policy["sources"].clear()
    assert policy_record("instinct")["sources"]
    assert result_of(run)["run_lock"]["emulation_policy"]["sources"]
    muse, code, _ = await run_connector(tmp_path)
    assert code == 0
    assert muse.system_prompt().startswith(FIRST_LINE)
    assert result_of(muse)["approvals"]["basis"]["rules"]["platform-default"]["basis"] == "documented"
    assert result_of(muse)["guideline_findings"] is not None
    assert json.dumps({"host": BASES, "permissions": RULE_BASIS}, sort_keys=True) == original
