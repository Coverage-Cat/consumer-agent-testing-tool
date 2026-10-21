"""Target selection through public CLI workflows; loopback services and scripted calls only."""
import json
import shutil
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml
from jsonschema import Draft202012Validator
from test_connector_e2e_support import EXAMPLES, fixture, oracle
from test_connector_support import Hotel, scenario, services

from consumer_agent_testing_tool import connector_run
from consumer_agent_testing_tool.ci_outputs import SUMMARY_SCHEMA
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_scenario import load_scenario, parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import compare, export_public_run, playback


def test_summary_v1_still_accepts_the_treatment_in_older_muse_reports():
    legacy = {"projection": "native", "exposure": "in-prompt", "client_profile": "generic-sdk",
              "output_schema": "lenient"}
    schema = SUMMARY_SCHEMA["properties"]["treatment"]
    Draft202012Validator(schema).validate(legacy)
    assert list(Draft202012Validator(schema).iter_errors({**legacy, "emulation_target": "unknown"}))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    return tmp_path


@pytest.mark.parametrize("from_scenario", [True, False])
async def test_instinct_cli_seals_target_in_trials_summary_and_junit(workspace, capsys, from_scenario):
    name = "instinct-booking.yaml" if from_scenario else "fixture-booking.yaml"
    args = ["connector-run", f"examples/{name}", "--driver", "scripted",
            "--script", "examples/fixture-booking.calls.json", "--trials", "2",
            "--summary-json", "-", "--junit", "reports/instinct.xml"]
    if not from_scenario:
        args += ["--emulation-target", "instinct"]
    async with fixture() as running:
        assert await execute(parser().parse_args([*args, "--set", f"PORT={running.port}"])) == 0
        assert oracle(running)["confirmed_count"] == 1
    summary = json.loads(capsys.readouterr().out)
    Draft202012Validator(SUMMARY_SCHEMA).validate(summary)
    assert summary["treatment"]["emulation_target"] == "instinct"
    assert summary["treatment"]["projection"] == "native"
    aggregate = Path(summary["summary_run"])
    manifest = json.loads((aggregate / "manifest.json").read_text())
    assert manifest["run_lock"]["emulation_target"] == "instinct"
    aggregate_events = [json.loads(line) for line in (aggregate / "events.jsonl").read_text().splitlines()]
    assert {row["profile"] for row in aggregate_events} == {"instinct-connector-surrogate-2026-10-21"}
    for trial in summary["trials"]:
        path = Path(trial["run"])
        result = playback(path)["result"]
        assert result["emulation_target"] == result["run_lock"]["emulation_target"] == "instinct"
        assert result["guideline_findings"] is None
        assert "target <code>instinct</code>" in (path / "report.html").read_text()
        assert all(check["status"] == "passed" for check in result["checks"])
    for suite in ElementTree.parse(workspace / "reports/instinct.xml").getroot().findall("testsuite"):
        values = {item.get("name"): item.get("value") for item in suite.find("properties")}
        assert values["emulation_target"] == "instinct"


@pytest.mark.parametrize("extra", [
    ["--projection", "muse-cli"],
    ["--client-profile", "muse-template-unofficial-2026-10"],
])
async def test_invalid_target_override_precedes_preflight_and_service_start(
        workspace, monkeypatch, capsys, extra):
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid treatment must not start a model, service or authenticated client")
    from consumer_agent_testing_tool import service_start
    monkeypatch.setattr(connector_run, "_agent_driver", forbidden)
    monkeypatch.setattr(connector_run, "default_services", forbidden)
    monkeypatch.setattr(service_start, "started", forbidden)
    args = parser().parse_args([
        "connector-run", "examples/fixture-booking.yaml", "--emulation-target", "instinct",
        "--start", "python should-not-run.py", "--summary-json", "-", *extra])
    with pytest.raises(LabError, match="Instinct"):
        await execute(args)
    summary = json.loads(capsys.readouterr().out)
    Draft202012Validator(SUMMARY_SCHEMA).validate(summary)
    assert summary["status"] == "error" and summary["exit_code"] == 2
    assert summary["trials"] == []
    assert not (workspace / "runs").exists()


async def test_scaffold_records_instinct_without_calling_service_tools(workspace, monkeypatch, capsys):
    hotel = Hotel()
    hotel.catalog.append({
        "name": "manage_messages", "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"action": {"enum": ["list", "send"]}}}})
    made = services(hotel)
    monkeypatch.setattr(connector_run, "default_services", lambda: made)
    assert await execute(parser().parse_args([
        "scaffold", "https://hotel.example/mcp", "--emulation-target", "instinct",
        "--output", "instinct.yaml"])) == 0
    doc = yaml.safe_load((workspace / "instinct.yaml").read_text())
    assert doc["emulation_target"] == "instinct"
    assert doc["connectors"][0]["client_profile"] == "generic-sdk"
    assert hotel.calls == []
    assert "guidelines" not in (workspace / "instinct.yaml").read_text()
    before = len(made.linked)
    with pytest.raises(LabError, match="Instinct"):
        await execute(parser().parse_args([
            "scaffold", "https://hotel.example/mcp", "--emulation-target", "instinct",
            "--client-profile", "muse-template-unofficial-2026-10", "--output", "bad.yaml"]))
    assert len(made.linked) == before
    assert not (workspace / "bad.yaml").exists()


@pytest.mark.parametrize("command", ["inspect", "check", "doctor", "run"])
@pytest.mark.parametrize("selection", ["--interface-profile", "--profile", "default"])
@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_connector_profiles_cannot_label_other_commands(
        workspace, monkeypatch, command, selection, target):
    from consumer_agent_testing_tool.config import Config, interface_profile
    from consumer_agent_testing_tool.emulation import target_profile
    from consumer_agent_testing_tool.errors import Unsupported

    profile = target_profile(target)["name"]
    assert interface_profile(profile)["name"] == profile  # Still resolvable for replay and diff.
    config = Config()
    if selection == "default":
        config.defaults.interface_profile = profile
    path = workspace / "config.json"
    path.write_text(config.model_dump_json())

    def forbidden(*args, **kwargs):
        pytest.fail("An unsupported profile must fail before creating evidence or probing a provider")
    monkeypatch.setattr("consumer_agent_testing_tool.cli.Evidence", forbidden)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", forbidden)
    args = [command, *(["missing.yaml"] if command == "run" else []), "--config", str(path)]
    if selection != "default":
        args += [selection, profile]
    with pytest.raises(Unsupported, match="connector-run"):
        await execute(parser().parse_args(args))
    assert not (workspace / "runs").exists()


async def test_instinct_export_retains_target_and_diff_reports_a_target_change(workspace, capsys):
    runs = []
    async with fixture() as running:
        for target in ("muse", "instinct"):
            assert await execute(parser().parse_args([
                "connector-run", "examples/fixture-booking.yaml", "--emulation-target", target,
                "--driver", "scripted", "--script", "examples/fixture-booking.calls.json",
                "--set", f"PORT={running.port}", "--summary-json", "-"])) == 0
            runs.append(Path(json.loads(capsys.readouterr().out)["trials"][0]["run"]))
    exported = workspace / "exported"
    export_public_run(runs[1], exported)
    result = playback(exported)["result"]
    assert result["emulation_target"] == "instinct"
    assert result["run_lock"]["emulation_policy"]["target"] == "instinct"
    assert not (exported / "private").exists()
    assert "taylor@example.com" not in json.dumps(result)
    diff = compare(*runs)
    assert diff["lock_compatible"] is False
    assert any(item["path"] == "/emulation_target" for item in diff["unexplained_lock_changes"])


def test_target_digests_preserve_legacy_muse_and_distinguish_instinct():
    from consumer_agent_testing_tool.serialization import digest

    default = parse_scenario(scenario())
    explicit = parse_scenario(scenario(emulation_target="muse"))
    instinct = parse_scenario(scenario(emulation_target="instinct"))
    assert default.sha256 == explicit.sha256 != instinct.sha256
    old = default.scenario.model_dump(mode="json", by_alias=True)
    old.pop("emulation_target")
    old["private"] = {key: "[private]" for key in old["private"]}
    for connector in old["connectors"]:
        for key in ("exposure", "limits", "output_schema"):
            if connector[key] is None:
                connector.pop(key)
    assert digest(old) == default.sha256
    assert load_scenario(EXAMPLES / "instinct-booking.yaml").scenario.emulation_target == "instinct"
