"""The runner with the real lane modules and the loopback fixture (no network beyond 127.0.0.1)."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_connector_support import (
    agent,
    answer,
    call,
    public_text,
    result_of,
    structured,
)

from consumer_agent_testing_tool.connector_fixture import FixtureConfig, serve_fixture
from consumer_agent_testing_tool.connector_run import (
    FIRST_LINE,
    ConnectorRun,
    ScriptedDriver,
)
from consumer_agent_testing_tool.connector_scenario import load_scenario
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import load

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"
NATIVE = ("fixture-booking.yaml", "fixture-booking.calls.json")
CLI = ("fixture-booking-cli.yaml", "fixture-booking-cli.calls.json")


async def run_example(tmp_path, example, *, variant="good", driver=None, **options):
    scenario, calls = example
    async with serve_fixture(FixtureConfig(variant=variant, auth="none")) as running:
        loaded = load_scenario(EXAMPLES / scenario, variables={"PORT": str(urlsplit(running.mcp_url).port)})
        run = ConnectorRun(loaded, driver=driver or ScriptedDriver(load(EXAMPLES / calls)), root=tmp_path,
                           output=tmp_path / "runs", **options)
        code = await run.run()
    return run, code


def statuses(run):
    return {check["id"]: check["status"] for check in result_of(run)["checks"]}


@pytest.mark.parametrize("example", [NATIVE, CLI], ids=["native", "muse-cli"])
async def test_examples_pass_end_to_end_with_the_real_lane_modules(tmp_path, example):
    run, code = await run_example(tmp_path, example)
    assert code == 0, run.summary()
    assert set(statuses(run).values()) == {"passed"}
    assert playback(run.path)["integrity"] == "verified"
    assert "taylor@example.com" not in public_text(run)
    result = result_of(run)
    assert result["approvals"]["cards"][-1]["arguments"]["guest_email"] == "[private]"
    assert result["approvals"]["cards"][-1]["disclosure"]["seller"] == "Cedar Stays"


async def test_a_false_confirmation_from_the_service_is_caught_by_the_oracle(tmp_path):
    run, code = await run_example(tmp_path, NATIVE, variant="false-success")
    assert code == 1
    found = statuses(run)
    assert found["one-confirmed-booking"] == found["booking-status"] == "failed"
    assert found["answer.no_false_claims"] == "failed"


async def test_the_unofficial_template_refuses_redirects_the_generic_sdk_follows(tmp_path):
    run, code = await run_example(tmp_path, CLI, variant="needs-redirect")
    assert code == 3 and "follows no redirects" in result_of(run)["detail"]
    run, code = await run_example(tmp_path, NATIVE, variant="needs-redirect")
    assert code == 0, run.summary()
    catalog = next(json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()
                   if json.loads(line)["event_type"] == "catalog")
    assert any(item.get("kind") == "redirect" for item in catalog["client_observations"])
    assert result_of(run)["run_lock"]["server"]["client_observations"] == catalog["client_observations"]


async def test_an_agent_books_through_the_real_stack(tmp_path):
    driver, api = agent([
        call("search_rooms", check_in="2026-12-04", check_out="2026-12-06", adults=2),
        lambda input: call("get_quote", room_id=structured(input, "room_id"), check_in="2026-12-04",
                           check_out="2026-12-06", adults=2),
        lambda input: call("create_booking", quote_id=structured(input, "quote_id"), guest_name="Taylor Example"),
        lambda input: answer(f"Booked the room for Taylor Example: confirmation {structured(input, 'booking_id')}, "
                             f"total ${structured(input, 'total_minor') / 100:.2f} USD.")])
    run, code = await run_example(tmp_path, NATIVE, driver=driver)
    assert code == 0, run.summary()
    assert api.inputs[0]["messages"][0]["content"].startswith(FIRST_LINE)
    assert "taylor@example.com" not in json.dumps(api.inputs)
    metrics = result_of(run)["metrics"]
    assert metrics["approval"]["prompts"]["sensitive-write"] == 1 and metrics["task_success"] is True
