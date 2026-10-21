"""Bounded public receipt polling through real HTTP, Inspect and generated FastMCP."""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from test_api_lease import retained  # noqa: F401
from test_api_scenario import call, done
from test_generated_resume import configure
from test_generated_scenario import buy as generated_buy
from test_mapped_resume import recovery_mapping, resume
from test_mapped_scenario import buy, inventory, run, spec_for  # noqa: F401

from consumer_agent_testing_tool.api_mapping import ApiMapping
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import ReceiptPolling
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.serialization import load

POLL = {"max_attempts": 5, "interval_seconds": 0.05, "wait_seconds": 2}


async def setup(store, tmp_path, generated, poll=None):
    poll = poll or POLL
    if generated:
        await configure(store, tmp_path, poll=poll)
    else:
        recovery_mapping(store)
        document = store.mapping.model_dump(mode="json", by_alias=True)
        document["recovery"]["buy_item"]["poll"] = poll
        document["operations"]["lookup_receipt"]["responses"]["202"] = {"schema": {"type": "object"}}
        store.mapping = ApiMapping.model_validate(document)
        Path(store.target.api_mapping).write_text(store.mapping.model_dump_json(by_alias=True))
    store.fault = "lost_response"
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, _, checks = await run(store, tmp_path,
        [call("get_price"), generated_buy if generated else buy], spec, resumable=True)
    assert statuses(checks)[0] != 0
    return original, spec


def poll_record(evidence):
    paths = list((evidence.path / "private").glob("receipt-poll-*.private.json"))
    assert len(paths) == 1
    return load(paths[0])["record"]


@pytest.mark.parametrize("generated", [False, True])
async def test_public_pending_receipt_reaches_native_continuation_without_repeating_write(
        retained, tmp_path, generated):
    original, spec = await setup(retained, tmp_path, generated)
    retained.pending_receipts = 2
    restored, api, checks = await resume(retained, original, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt", "/receipt", "/receipt"]
    assert retained.controls.count("grant") == 1 and retained.controls.count("acquire_retained") == 1
    assert len(api.inputs) == 1
    result = json.loads(api.inputs[0]["messages"][-1]["content"])
    assert result["reconciled"] is True
    assert (result["result"]["structuredContent"] if generated else result["result"]) == {"status": "completed"}
    record = poll_record(restored)
    assert record["attempts"] == 3 and record["response"]["status"] == 200
    with sqlite3.connect(retained.path) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
    exported = export_redacted(restored.path, tmp_path / "export")
    assert not (exported / "private").exists()
    events = [json.loads(line) for line in (restored.path / "events.jsonl").read_text().splitlines()]
    assert sum(row["event_type"] == "dispatch_reconciliation_poll" for row in events) == 3


@pytest.mark.parametrize("generated", [False, True])
async def test_public_poll_budget_survives_resume_and_never_renews_grant(retained, tmp_path, generated):
    # The wait stays below the interval so each resume polls at most once, and leaves a slow runner's
    # loopback lookup room: a lookup that outlasts the wait is reserved but never sent.
    original, spec = await setup(retained, tmp_path, generated,
        {"max_attempts": 2, "interval_seconds": 1.0, "wait_seconds": 0.5})
    retained.pending_receipts = 10
    first, api, checks = await resume(retained, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == [] and poll_record(first)["attempts"] == 1
    await asyncio.sleep(1.01)
    second, api, checks = await resume(retained, first, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == [] and poll_record(second)["attempts"] == 2
    third, api, checks = await resume(retained, second, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == [] and poll_record(third)["attempts"] == 2
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt", "/receipt"]
    assert retained.controls.count("grant") == 1
    assert load(third.path / "private/request-2.private.json")["status"] == "pending"


@pytest.mark.parametrize("field", ["run_id", "task_id", "principal", "operation", "arguments_sha256"])
@pytest.mark.parametrize("generated", [False, True])
async def test_wrong_pending_receipt_stops_before_second_poll_or_inference(
        retained, tmp_path, generated, field):
    original, spec = await setup(retained, tmp_path, generated)
    retained.pending_receipts = 2
    retained.receipt_patch = {field: "different"}
    restored, api, checks = await resume(retained, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == [] and poll_record(restored)["attempts"] == 1
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]


@pytest.mark.parametrize("field,value", [
    ("max_attempts", True), ("max_attempts", 0), ("max_attempts", 101),
    ("interval_seconds", True), ("interval_seconds", 0), ("interval_seconds", float("nan")),
    ("wait_seconds", float("inf")), ("wait_seconds", 61),
])
def test_public_poll_configuration_is_finite_and_bounded(field, value):
    with pytest.raises(ValueError):
        ReceiptPolling.model_validate({**POLL, field: value})


@pytest.mark.parametrize("generated", [False, True])
async def test_public_poll_retains_terminal_receipt_before_native_reconstruction(
        retained, tmp_path, monkeypatch, generated):
    original, spec = await setup(retained, tmp_path, generated)

    async def fail(*args):
        raise LabError("Synthetic interruption before result reconstruction")

    owner = "consumer_agent_testing_tool.generated_resume.GeneratedRecovery" if generated else "consumer_agent_testing_tool.mapped_scenario.MappedAdapter"
    with monkeypatch.context() as patch:
        patch.setattr(owner + ".restore_response", fail)
        interrupted, api, checks = await resume(retained, original, spec, [])
    assert statuses(checks)[0] != 0 and api.inputs == []
    assert poll_record(interrupted)["response"]["status"] == 200
    restored, api, checks = await resume(retained, interrupted, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert poll_record(restored)["attempts"] == 1 and len(api.inputs) == 1
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]


@pytest.mark.parametrize("generated", [False, True])
async def test_public_poll_timeout_stays_pending_without_inference(retained, tmp_path, monkeypatch, generated):
    original, spec = await setup(retained, tmp_path, generated,
                                {**POLL, "wait_seconds": 0.2})

    async def slow(*args):
        await asyncio.sleep(1)
        raise AssertionError("The bounded lookup must have been cancelled")

    owner = "consumer_agent_testing_tool.generated_resume.GeneratedRecovery" if generated else "consumer_agent_testing_tool.mapped_scenario.MappedAdapter"
    with monkeypatch.context() as patch:
        patch.setattr(owner + ".lookup_receipt", slow)
        interrupted, api, checks = await resume(retained, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert api.inputs == [] and poll_record(interrupted)["attempts"] == 1
    assert [r["path"] for r in retained.requests] == ["/price", "/buy"]
    _, api, checks = await resume(retained, interrupted, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert len(api.inputs) == 1
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]


@pytest.mark.parametrize("generated", [False, True])
async def test_public_poll_reserves_budget_before_process_exit(retained, tmp_path, generated):
    from consumer_agent_testing_tool.config import Config, interface_profile
    from consumer_agent_testing_tool.protocol import child_environment
    from consumer_agent_testing_tool.scenario_session import reopen_evidence

    original, spec = await setup(retained, tmp_path, generated)
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "config.json").write_text(Config(targets={"inventory": retained.target}).model_dump_json())
    code = """
import asyncio, os, sys
from pathlib import Path
from inspect_ai.model import Model, GenerateConfig
from test_api_scenario import Scripted
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.config import Config, interface_profile
from consumer_agent_testing_tool.generated_resume import GeneratedRecovery
from consumer_agent_testing_tool.mapped_scenario import MappedAdapter
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import load
root = Path(sys.argv[1])
async def crash(*args):
    os._exit(73)
MappedAdapter.lookup_receipt = GeneratedRecovery.lookup_receipt = crash
evidence = reopen_evidence(Path(sys.argv[2]), interface_profile('api-tools-surrogate-v1'))
asyncio.run(run_api_scenario(load(root/'spec.json'), Config.model_validate(load(root/'config.json')),
    None, evidence, model=Model(Scripted([]), GenerateConfig(max_retries=0)), resume=True))
"""
    env = {**child_environment(), "PYTHONPATH": os.pathsep.join(["src", "tests"]),
           "INVENTORY_TOKEN": retained.secret}
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(tmp_path), str(original.path),
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
    assert process.returncode == 73, (stdout, stderr)
    interrupted = reopen_evidence(original.path, interface_profile("api-tools-surrogate-v1"))
    assert poll_record(interrupted)["attempts"] == 1
    assert [r["path"] for r in retained.requests] == ["/price", "/buy"]
    restored, api, checks = await resume(retained, interrupted, spec, [done()])
    assert statuses(checks) == (0, "completed"), checks
    assert poll_record(restored)["attempts"] == 2 and len(api.inputs) == 1
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt"]


@pytest.mark.parametrize("generated", [False, True])
async def test_completed_public_receipt_does_not_finalize_pending_backend_work(retained, tmp_path, generated):
    original, spec = await setup(retained, tmp_path, generated)
    retained.pending_receipts = 1
    with sqlite3.connect(retained.path) as db:
        db.execute("INSERT INTO background_jobs VALUES ('settlement', 'pending')")
    pending, api, checks = await resume(retained, original, spec, [done()])
    assert statuses(checks) == (3, "blocked"), checks
    assert len(api.inputs) == 1
    assert pending.manifest["outcome_verification"]["status"] == "pending"
    assert poll_record(pending)["response"]["status"] == 200
    with sqlite3.connect(retained.path) as db:
        db.execute("UPDATE background_jobs SET status='completed'")
    complete, api, checks = await resume(retained, pending, spec, [])
    assert statuses(checks) == (0, "completed"), checks
    assert api.inputs == [] and complete.manifest["outcome_verification"]["status"] == "terminal"
    assert [r["path"] for r in retained.requests] == ["/price", "/buy", "/receipt", "/receipt"]
