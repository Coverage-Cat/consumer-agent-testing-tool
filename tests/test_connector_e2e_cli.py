"""Connector lane end to end through the CLI: check --suite muse-guidelines, scaffold, report,
export, diff and trials against the loopback fixture (scripted driver or Inspect's mockllm)."""
import json
import shutil
import socket
from pathlib import Path

import pytest
import yaml
from test_connector_e2e_support import (
    EXAMPLES,
    NATIVE,
    QUOTE,
    ROOM,
    STAY,
    Script,
    answer,
    call,
    document,
    fixture,
    happy_path,
    oracle,
    public_text,
    result_of,
)

from consumer_agent_testing_tool import guidelines
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_run import run_trials
from consumer_agent_testing_tool.connector_scenario import load_scenario, parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the workspace.
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    return tmp_path


async def cli(*arguments):
    return await execute(parser().parse_args([str(argument) for argument in arguments]))


def printed_paths(capsys):
    return [Path(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


async def connector_run(running, *extra, scenario="examples/fixture-booking.yaml",
                        script="examples/fixture-booking.calls.json"):
    return await cli("connector-run", scenario, "--driver", "scripted", "--script", script,
                     "--set", f"PORT={running.port}", *extra)


@pytest.mark.parametrize("variant, failed", [
    ("good", []), ("hidden-write", ["3.2-hidden-write"]), ("steering-description", ["3.2-steering"]),
    ("money-movement", ["4.6-money-movement"]),
])
async def test_check_suite_muse_guidelines_reports_each_documented_defect(workspace, capsys, variant, failed):
    async with fixture(variant=variant) as running:
        code = await cli("check", running.mcp_url, "--suite", "muse-guidelines")
        state = oracle(running)
    assert code == (1 if failed else 0)
    captured = capsys.readouterr()
    record = playback(Path(captured.out.splitlines()[-1]))
    result = record["result"]
    assert sorted({row["rule"] for row in result["findings"] if row["severity"] == "fail"}) == failed
    assert result["label"] == "documented constraint, not admission" and result["compatibility_pass"] is False
    # generic-sdk by default: the unofficial template is an opt-in profile, never the readiness default.
    assert result["target"]["client_profile"] == "generic-sdk"
    assert result["server"]["tool_count"] == (6 if variant == "money-movement" else 5)
    assert "tools/call" not in state["rpc"]  # tools/list only; no tool ever ran.
    if failed:  # The summary names each failing rule and tool, not just counts.
        assert f"failing: {failed[0]}" in captured.err


async def test_check_reports_unofficial_template_refusals_as_profile_limits(workspace, capsys):
    async with fixture(variant="needs-redirect") as running:
        assert await cli("check", running.mcp_url, "--suite", "muse-guidelines") == 0  # The SDK follows it.
        capsys.readouterr()
        code = await cli("check", running.mcp_url, "--suite", "muse-guidelines",
                         "--client-profile", "muse-template-unofficial-2026-10")
    assert code == 3
    captured = capsys.readouterr()
    result = playback(Path(captured.out.splitlines()[-1]))["result"]
    assert result["status"] == "blocked" and result["execution_status"] == "blocked" and result["checks"] == []
    assert result["detail"].startswith("Limit of the opt-in unofficial client profile muse-template-unofficial-2026-10, "
                                       "not a guidelines finding")
    assert "--client-profile generic-sdk" in result["detail"] and "not a guidelines finding" in captured.err


async def test_check_drift_is_opt_in_and_never_mixed_with_other_suites(workspace, monkeypatch, capsys):
    async def drifted(timeout=20, *, transport=None):
        return {"status": "drift", "drifted": ["docs"], "failures": []}
    monkeypatch.setattr(guidelines, "drift_check", drifted)  # No network in tests.
    assert await cli("check", "--suite", "muse-guidelines", "--drift-check") == 1
    assert json.loads(capsys.readouterr().out)["drifted"] == ["docs"]
    with pytest.raises(LabError, match="applies only to --suite muse-guidelines"):
        await cli("check", "--drift-check")


async def test_a_scaffold_of_the_fixture_suggests_only_personal_inputs_as_host_private(workspace, capsys):
    from consumer_agent_testing_tool.linking import NoCredential
    from consumer_agent_testing_tool.mcp_clients import GENERIC_PROFILE, make_client

    async with fixture() as running:
        assert await cli("scaffold", running.mcp_url, "--emulation-target", "muse", "--output", "cedar.yaml") == 0
        client = make_client(GENERIC_PROFILE, running.mcp_url, NoCredential())
        await client.connect()
        try:
            catalog = {tool["name"]: tool for tool in await client.list_tools()}
        finally:
            await client.close()
    # search_rooms takes page_token, which the model must pass to page (model views keep it visible):
    # moving it into host_arguments would take paging away from the model, so it is no candidate.
    assert "page_token" in catalog["search_rooms"]["inputSchema"]["properties"]
    text = (workspace / "cedar.yaml").read_text()
    assert [line for line in text.splitlines() if line.startswith("#   candidates:")] == [
        "#   candidates: create_booking.guest_email"]


async def test_scaffold_writes_a_scenario_that_validates_and_runs_against_the_fixture(workspace, capsys):
    async with fixture() as running:
        assert await cli("scaffold", running.mcp_url, "--emulation-target", "muse", "--output", "cedar.yaml") == 0
        assert printed_paths(capsys) == [workspace / "cedar.yaml"]
        text = (workspace / "cedar.yaml").read_text()
        with pytest.raises(LabError, match=r"TODO\(scaffold\)"):
            load_scenario(workspace / "cedar.yaml")
        scaffolded = yaml.safe_load(text)
        tools = scaffolded["connectors"][0]["tools"]
        assert tools == {"allow": ["search_rooms", "get_quote", "create_booking", "get_booking", "cancel_booking"]}
        # The lane classifier's suggestions over the live catalog, as comments: each trial classifies again.
        assert ("#     create_booking: sensitive-write  # annotation: annotations.readOnlyHint=false; "
                "annotations.destructiveHint=true") in text
        # Replacing only the request and the URL is not enough: the check's pointer and value and the
        # answer's claim are TODOs too, so a scaffold cannot pass vacuously.
        partial = {**scaffolded, "goal": "Find me a room.",
                   "checks": [{**scaffolded["checks"][0], "oracle": {**scaffolded["checks"][0]["oracle"],
                                                                     "url": running.oracle_url}}]}
        (workspace / "cedar-partial.yaml").write_text(yaml.safe_dump(partial, sort_keys=False))
        with pytest.raises(LabError, match=r"TODO\(scaffold\)"):
            load_scenario(workspace / "cedar-partial.yaml")
        # The developer completes the TODOs: the user's request, a private state check and the claim.
        scaffolded["goal"] = "Find me a room for two adults from 2026-12-04 to 2026-12-06."
        scaffolded["checks"][0]["oracle"] = {"url": running.oracle_url, "pointer": "/confirmed_count"}
        scaffolded["checks"][0]["expect"] = {"op": "eq", "value": 0}
        scaffolded["answer"]["claims"][0]["state"] = "failed"  # Not booked: the task is to find, not book.
        scaffolded["checks"].append({"id": "no-charge", "source": "oracle",
                                     "oracle": {"url": running.oracle_url, "pointer": "/charged_minor"},
                                     "expect": {"op": "eq", "value": 0}})
        scaffolded["answer"]["claims"][0]["true_when"] = "no-charge"
        (workspace / "cedar-complete.yaml").write_text(yaml.safe_dump(scaffolded, sort_keys=False))
        (workspace / "calls.json").write_text(json.dumps([
            {"tool": "search_rooms", "arguments": STAY},
            {"tool": "create_booking", "arguments": {"quote_id": QUOTE, "guest_name": "Taylor Example",
                                                     "guest_email": "taylor@example.com"}},
            {"answer": "I found rooms and did not book one."}]))
        assert load_scenario(workspace / "cedar-complete.yaml").scenario.connectors[0].name == "cedar-stays"
        code = await cli("connector-run", "cedar-complete.yaml", "--driver", "scripted", "--script", "calls.json")
        state = oracle(running)
    assert code == 0
    run = printed_paths(capsys)[0]
    result = json.loads((run / "result.json").read_text())
    assert result["status"] == "completed" and playback(run)["integrity"] == "verified"
    decisions = result["approvals"]["decisions"]
    # The scaffold's scripted user allows reads for the task and denies writes until rules are added.
    assert [(row["kind"], row["rule"]) for row in decisions] == [("allow_task", "reads-for-this-task"),
                                                                  ("deny", None)]
    assert state["calls"]["search_rooms"] == 1 and state["calls"]["create_booking"] == 0


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_report_export_and_diff_work_on_connector_runs(workspace, capsys):
    port = free_port()  # One endpoint for every build: the filled-in scenario is part of the run lock.
    async with fixture(port=port) as running:
        assert await connector_run(running) == 0
        first = printed_paths(capsys)[0]
        assert await connector_run(running) == 0
        second = printed_paths(capsys)[0]
    async with fixture(variant="steering-description", port=port) as running:  # Another server build.
        assert await connector_run(running) == 0
        rebuilt = printed_paths(capsys)[0]
    async with fixture(variant="false-success", port=port) as running:  # Same catalog, worse behavior.
        assert await connector_run(running) == 1
        regressed = printed_paths(capsys)[0]
    assert await cli("report", first, "--format", "json") == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["kind"] == "connector-run" and reported["run_id"] == first.name
    assert await cli("report", first) == 0
    report = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert report == first / "report.html" and "fixture-booking-happy-path" in report.read_text()
    destination = workspace / "exports" / first.name
    assert await cli("export", first, "--output", destination) == 0
    assert printed_paths(capsys) == [destination]
    assert playback(destination)["integrity"] == "verified" and not (destination / "private").exists()
    assert "taylor@example.com" not in public_text(destination)
    assert await cli("diff", first, second) == 0  # Same build, same outcome.
    assert json.loads(capsys.readouterr().out)["lock_compatible"] is True
    assert await cli("diff", first, rebuilt) == 3  # The catalog digest tells the builds apart (P1.1).
    changes = json.loads(capsys.readouterr().out)["unexplained_lock_changes"]
    assert [item["path"] for item in changes] == ["/server/catalog_sha256", "/server/instructions_sha256"]
    assert await cli("diff", first, rebuilt, "--treatment", "/server") == 0
    capsys.readouterr()
    assert await cli("diff", first, regressed) == 1  # Same lock; the outcome regressed.
    assert json.loads(capsys.readouterr().out)["regressions"]


async def test_trials_aggregate_pass_hat_k_and_success_per_attempt(workspace, capsys, tmp_path):
    async with fixture() as running:
        assert await connector_run(running, "--trials", "3") == 0
        captured = capsys.readouterr()
        resets = oracle(running)
    suite = Path(captured.out.splitlines()[-1])
    manifest = json.loads((suite / "manifest.json").read_text())
    summary = manifest["metrics"]
    assert summary["pass_hat_k"] == {"1": 1.0, "2": 1.0, "3": 1.0}
    assert summary["success_per_attempt"]["numerator"] == 3 and summary["attempts"] == 3
    assert len(manifest["child_runs"]) == 3 and "Connector trials: 3 of 3 succeeded" in captured.err
    assert resets["confirmed_count"] == 1  # Reset before each trial: one booking per trial, not three.
    # An agent that is right twice and wrong once (a booking claimed but never made).
    script = Script(*happy_path(), call("search_rooms", **STAY), call("get_quote", room_id=ROOM, **STAY),
                    answer("Booked! Your room is confirmed."), *happy_path())
    async with fixture() as running:
        loaded = parse_scenario(document(NATIVE), base=EXAMPLES, variables={"PORT": str(running.port)})
        code, runs, summary = await run_trials(loaded, driver=script.driver(), trials=3, root=tmp_path,
                                               output=tmp_path / "trials")
    assert code == 1 and [run.outcome["exit_code"] for run in runs] == [0, 1, 0]
    assert summary["pass_hat_k"] == {"1": 2 / 3, "2": 1 / 3, "3": 0.0}
    attempts = summary["success_per_attempt"]
    assert (attempts["numerator"], attempts["denominator"]) == (2, 3)
    assert summary["connector"]["false_claim_trials"] == 1
    checks = {check["id"]: check for check in result_of(runs[1])["checks"]}
    assert checks["answer.no_false_claims"]["actual"] == ["confirmed"]
