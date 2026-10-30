"""Shared entry points require a chosen host before connecting or running a model."""
import json
from pathlib import Path

import pytest
from test_connector_support import Hotel, native_script, scenario, services

from consumer_agent_testing_tool import connector_run
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_scenario import (
    ConnectorScenario,
    load_scenario,
    parse_scenario,
)
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback


def test_scenario_has_no_default_emulation_target():
    document = scenario()
    document.pop("emulation_target", None)
    assert "emulation_target" in ConnectorScenario.model_json_schema()["required"]
    with pytest.raises(LabError, match="emulation_target"):
        parse_scenario(document)


@pytest.mark.parametrize("command", ["connector-run", "scaffold", "surrogate"])
async def test_missing_target_stops_before_external_work(tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)
    document = scenario()
    document.pop("emulation_target", None)
    (tmp_path / "scenario.json").write_text(json.dumps(document))
    (tmp_path / "calls.json").write_text("[]")

    def forbidden(*args, **kwargs):
        pytest.fail("Target selection must precede service or model work")

    monkeypatch.setattr(connector_run, "default_services", forbidden)
    monkeypatch.setattr(connector_run, "_agent_driver", forbidden)
    arguments = {
        "connector-run": ["connector-run", "scenario.json", "--script", "calls.json", "--driver", "scripted"],
        "scaffold": ["scaffold", "https://fixture.example/mcp", "--output", "scaffold.yaml"],
        "surrogate": ["check", "https://fixture.example/mcp", "--suite", "surrogate"],
    }[command]
    with pytest.raises(LabError, match="emulation.target"):
        await execute(parser().parse_args(arguments))
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("target", ["muse", "instinct"])
def test_explicit_loader_target_migrates_an_unlabeled_scenario(tmp_path, target):
    document = scenario()
    document.pop("emulation_target", None)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document))
    loaded = load_scenario(path, target_if_missing=target)
    assert loaded.scenario.emulation_target == target
    legacy_digest = parse_scenario({**document, "emulation_target": "muse"}).sha256
    assert loaded.sha256 == legacy_digest
    assert "emulation_target" not in json.loads(path.read_text())


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_explicit_cli_target_runs_an_unlabeled_scenario(tmp_path, monkeypatch, capsys, target):
    monkeypatch.chdir(tmp_path)
    document = scenario()
    document.pop("emulation_target")
    (tmp_path / "scenario.json").write_text(json.dumps(document))
    (tmp_path / "calls.json").write_text(json.dumps(native_script()))
    hotel = Hotel()
    monkeypatch.setattr(connector_run, "default_services", lambda: services(hotel))
    assert await execute(parser().parse_args([
        "connector-run", "scenario.json", "--emulation-target", target,
        "--driver", "scripted", "--script", "calls.json", "--summary-json", "-"])) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["treatment"]["emulation_target"] == target
    result = playback(Path(summary["trials"][0]["run"]))["result"]
    assert result["emulation_target"] == result["run_lock"]["emulation_target"] == target
    legacy_digest = parse_scenario({**document, "emulation_target": "muse"}).sha256
    assert result["run_lock"]["scenario_sha256"] == legacy_digest
    assert len(hotel.bookings) == 1
    assert "emulation_target" not in json.loads((tmp_path / "scenario.json").read_text())


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_cli_migrates_legacy_target_and_projection_together(tmp_path, monkeypatch, capsys, target):
    monkeypatch.chdir(tmp_path)
    document = scenario(projection="muse-cli")
    document.pop("emulation_target")
    document["connectors"][0]["client_profile"] = "muse-template-unofficial-2026-10"
    (tmp_path / "scenario.json").write_text(json.dumps(document))
    (tmp_path / "calls.json").write_text(json.dumps(native_script()))
    hotel = Hotel()
    monkeypatch.setattr(connector_run, "default_services", lambda: services(hotel))
    assert await execute(parser().parse_args([
        "connector-run", "scenario.json", "--emulation-target", target, "--projection", "native",
        "--client-profile", "generic-sdk", "--driver", "scripted", "--script", "calls.json",
        "--summary-json", "-"])) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["treatment"]["emulation_target"] == target
    assert summary["treatment"]["projection"] == "native"
    assert len(hotel.bookings) == 1


@pytest.mark.parametrize("target", ["muse", "instinct"])
async def test_scaffold_records_either_explicit_target(tmp_path, monkeypatch, target):
    import yaml

    monkeypatch.chdir(tmp_path)
    hotel = Hotel()
    monkeypatch.setattr(connector_run, "default_services", lambda: services(hotel))
    assert await execute(parser().parse_args([
        "scaffold", "https://fixture.example/mcp", "--emulation-target", target,
        "--output", "scaffold.yaml"])) == 0
    assert yaml.safe_load((tmp_path / "scaffold.yaml").read_text())["emulation_target"] == target
    assert hotel.calls == []
