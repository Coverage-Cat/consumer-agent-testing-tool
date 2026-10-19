"""The readiness check end to end against the lane's loopback Cedar Stays fixture, through the real
linking and MCP client modules: the deliberately defective variants fail on their own rule."""
from pathlib import Path

import pytest

from consumer_agent_testing_tool import connector_fixture as fixture
from consumer_agent_testing_tool import mcp_clients as clients
from consumer_agent_testing_tool.guidelines import run_check
from consumer_agent_testing_tool.replay import playback

EXPECTED = {"good": [], "hidden-write": ["3.2-hidden-write"], "steering-description": ["3.2-steering"],
            "money-movement": ["4.6-money-movement"]}


@pytest.mark.parametrize("profile", ["generic-sdk", "muse-template-unofficial-2026-10"])
@pytest.mark.parametrize("variant", sorted(EXPECTED))
async def test_each_fixture_variant_fails_only_its_documented_rule(tmp_path, monkeypatch, capsys, variant, profile):
    monkeypatch.chdir(tmp_path)
    profiles = {item.name: item for item in (clients.GENERIC_PROFILE, clients.MUSE_TEMPLATE_PROFILE)}
    async with fixture.serve_fixture(fixture.FixtureConfig(variant=variant)) as running:
        code = await run_check(running.mcp_url, auth={"mode": "none"}, manifest_path=None,
                               output=tmp_path / "runs", profile=profiles[profile])
    path = Path(capsys.readouterr().out.strip().splitlines()[-1])  # The run path is printed last.
    result = playback(path)["result"]
    failed = sorted({row["rule"] for row in result["findings"] if row["severity"] == "fail"})
    assert failed == EXPECTED[variant], result["findings"]
    assert code == (1 if failed else 0) and result["server"]["tool_count"] >= 5
    assert result["target"]["client_profile"] == profile


async def test_the_shipped_readiness_manifest_example_passes_against_the_good_fixture(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    manifest = Path(__file__).resolve().parents[1] / "examples" / "connector" / "readiness.yaml"
    async with fixture.serve_fixture(fixture.FixtureConfig(variant="good")) as running:
        code = await run_check(running.mcp_url, auth={"mode": "none"}, manifest_path=manifest,
                               output=tmp_path / "runs", profile=clients.GENERIC_PROFILE)
    path = Path(capsys.readouterr().out.strip().splitlines()[-1])
    result = playback(path)["result"]
    assert code == 0 and result["manifest"]["path"] == str(manifest)
    assert [row for row in result["findings"] if row["severity"] == "fail"] == []
    # Every submission rule was evaluated against the declarations (no "not evaluated" rows).
    assert all(row["evaluated"] for row in result["findings"])
