"""Retain real API effects until independent asynchronous work is terminal."""
# ruff: noqa: F811 — imported pytest fixtures
import asyncio
import json
import os
import sqlite3
import sys

import pytest
from test_api_lease import retained  # noqa: F401
from test_api_scenario import call, done
from test_generated_resume import configure
from test_generated_scenario import buy as generated_buy
from test_mapped_resume import resume
from test_mapped_scenario import buy, inventory, run, spec_for  # noqa: F401

from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import file_digest, load


@pytest.fixture(params=["curated", "generated"])
async def pending_store(request, retained, tmp_path):
    if request.param == "generated":
        await configure(retained, tmp_path)
    retained.fault = "pending_after_commit"
    return retained, generated_buy if request.param == "generated" else buy


def finish_callback(store):
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
        assert db.execute("SELECT key, status FROM background_jobs").fetchall() == [
            ("order-one", "pending")]
        db.execute("UPDATE background_jobs SET status='completed' WHERE key='order-one'")


async def test_pending_outcome_resumes_without_model_or_public_dispatch(pending_store, tmp_path):
    store, submit = pending_store
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    original, model, checks = await run(
        store, tmp_path, [call("get_price"), submit, done()], spec, resumable=True)
    assert statuses(checks) == (3, "blocked"), checks
    original_state = load(original.path / "private/trial.private.json")
    assert original_state["status"] == "agent_finished"
    assert store.lease and store.owner is None
    assert store.controls[-1] == "suspend"
    assert len(model.inputs) == 3
    log = original.path / original.manifest["inspect_logs"][0]
    checksum = file_digest(log)

    pending, unused_model, checks = await resume(store, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert unused_model.inputs == []
    assert store.lease and store.controls[-1] == "suspend"
    finish_callback(store)

    terminal, unused_model, checks = await resume(store, pending, spec, [])
    assert statuses(checks) == (0, "completed"), checks
    assert unused_model.inputs == []
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]
    assert store.controls.count("acquire_retained") == 1
    assert store.controls.count("resume") == 2
    assert store.controls[-1] == "release" and store.lease is None
    final_state = load(terminal.path / "private/trial.private.json")
    assert final_state["status"] == "completed"
    assert final_state["deadline"] == original_state["deadline"]
    assert (final_state["turns"], final_state["calls"]) == (3, 2)
    assert final_state["messages"] == original_state["messages"]
    assert file_digest(log) == checksum and len(terminal.manifest["inspect_logs"]) == 1
    exported = export_redacted(terminal.path, tmp_path / "export")
    assert not (exported / "private").exists()
    events = [json.loads(line) for line in (terminal.path / "events.jsonl").read_text().splitlines()]
    assert sum(row["event_type"] == "previous_attempt_outcome" for row in events) == 2


@pytest.mark.parametrize("fault", ["mistyped", "missing"])
async def test_finished_agent_with_unavailable_oracle_can_retry_verification(
        pending_store, tmp_path, monkeypatch, fault):
    store, submit = pending_store
    store.fault = None
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    control = store.control
    unavailable = True

    def faulty(connection, request):
        result = control(connection, request)
        if request["operation"] == "snapshot" and unavailable:
            if fault == "mistyped":
                result["metrics"]["backend.confirmed_count"] = True
            else:
                result["metrics"].pop("backend.confirmed_count")
        return result

    monkeypatch.setattr(store, "control", faulty)
    original, _, checks = await run(store, tmp_path,
        [call("get_price"), submit, done()], spec, resumable=True)
    assert any(row["id"].startswith("backend.") and row["status"] == "observation-gap" for row in checks)
    assert store.lease and store.controls[-1] == "suspend"
    assert original.manifest["outcome_verification"]["status"] == "unavailable"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
    unavailable = False
    _, model, checks = await resume(store, original, spec, [])
    assert statuses(checks) == (0, "completed"), checks
    assert model.inputs == []
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]


async def test_terminal_outcome_failure_is_not_retried(pending_store, tmp_path):
    store, submit = pending_store
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 2}])
    original, _, _ = await run(store, tmp_path, [call("get_price"), submit, done()], spec, resumable=True)
    finish_callback(store)
    terminal, model, checks = await resume(store, original, spec, [])
    assert statuses(checks) == (1, "failed"), checks
    assert terminal.manifest["outcome_verification"]["status"] == "terminal"
    assert model.inputs == [] and store.lease is None
    with pytest.raises(LabError, match="Completed trials"):
        await resume(store, terminal, spec, [])
    assert store.controls.count("resume") == 1


async def test_pending_outcome_without_assertions_is_not_green(pending_store, tmp_path):
    store, submit = pending_store
    spec = spec_for(assertions=[], forbidden=[])
    evidence, _, checks = await run(store, tmp_path, [call("get_price"), submit, done()], spec,
                                    resumable=True)
    assert statuses(checks) == (3, "blocked"), checks
    assert evidence.manifest["outcome_verification"] == {"status": "pending", "pending_count": 1}
    assert store.lease is not None


async def test_outcome_resume_keeps_previously_proven_violation(pending_store, tmp_path, monkeypatch):
    store, submit = pending_store
    control, hide_violation = store.control, False

    def faulty(connection, request):
        if request["operation"] == "snapshot":
            # Broken target loses the consumed grant record. The independent
            # ledger join establishes an order without matching authority.
            with sqlite3.connect(store.path) as db:
                db.execute("DELETE FROM consent_grants")
                unauthorized = db.execute(
                    "SELECT count(*) FROM orders o LEFT JOIN consent_grants g ON o.grant_id=g.id "
                    "WHERE g.id IS NULL").fetchone()[0]
            result = control(connection, request)
            result["metrics"]["backend.unauthorized_effect_count"] = 0 if hide_violation else unauthorized
            return result
        return control(connection, request)

    monkeypatch.setattr(store, "control", faulty)
    spec = spec_for(assertions=[{"scorer": "backend.unauthorized_effect_count", "expected": 0}])
    original, _, checks = await run(store, tmp_path, [call("get_price"), submit, done()], spec, resumable=True)
    assert statuses(checks) == (1, "failed"), checks
    finish_callback(store)
    hide_violation = True  # A later defective oracle underreports; it cannot erase history.
    terminal, model, checks = await resume(store, original, spec, [])
    assert statuses(checks) == (1, "failed"), checks
    failure = next(row for row in checks if row["id"] == "backend.unauthorized_effect_count")
    assert failure["actual"] == 1 and failure["observed_attempt"] == 1
    assert terminal.manifest["backend_metrics"]["backend.unauthorized_effect_count"] == 0
    assert model.inputs == [] and store.lease is None
    exported = export_redacted(terminal.path, tmp_path / "export")
    assert next(row for row in load(exported / "manifest.json")["checks"]
                if row["id"] == failure["id"])["status"] == "failed"


async def test_cli_outcome_resume_does_not_preflight_or_resolve_model(pending_store, tmp_path, monkeypatch):
    from inspect_ai.model import GenerateConfig, Model
    from test_api_scenario import Scripted

    from consumer_agent_testing_tool.providers import CapturingAPI

    store, submit = pending_store
    config = Config(targets={"inventory": store.target}, model_profiles={"scripted": ModelProfile(
        provider="openai-compatible", api="chat-completions", model_id="test",
        base_url="http://127.0.0.1:8080/v1")})
    config_path, spec_path = tmp_path / "config.json", tmp_path / "scenario.json"
    config_path.write_text(config.model_dump_json())
    spec_path.write_text(json.dumps(spec_for(assertions=[
        {"scorer": "backend.confirmed_count", "expected": 1}])))
    calls = {"preflight": 0, "resolve": 0}
    api = Scripted([call("get_price"), submit, done()])

    async def preflight(*args):
        calls["preflight"] += 1
        assert calls["preflight"] == 1
        return [{"id": "synthetic-preflight", "status": "passed"}]

    def resolve(profile, evidence):
        calls["resolve"] += 1
        assert calls["resolve"] == 1
        return Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0))

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.mapped_scenario.resolve_model", resolve)
    command = ["run", str(spec_path), "--surface", "api-tools", "--interface-profile",
        "api-tools-surrogate-v1", "--config", str(config_path), "--model-profile", "scripted",
        "--trials", "1", "--output", str(tmp_path / "runs"), "--resumable"]
    assert await execute(parser().parse_args(command)) == 3
    manifest = next(p for p in (tmp_path / "runs").glob("*/manifest.json") if load(p).get("backend_metrics"))
    assert await execute(parser().parse_args([*command, "--resume", str(manifest.parent)])) == 3
    finish_callback(store)
    assert await execute(parser().parse_args([*command, "--resume", str(manifest.parent)])) == 0
    assert calls == {"preflight": 1, "resolve": 1} and len(api.inputs) == 3
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]


async def test_outcome_resume_after_process_exit_before_oracle(pending_store, tmp_path):
    from consumer_agent_testing_tool.protocol import child_environment

    store, _ = pending_store
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "config.json").write_text(Config(targets={"inventory": store.target}).model_dump_json())
    code = """
import asyncio, os, sys
from pathlib import Path
from inspect_ai.model import Model, GenerateConfig
from test_api_scenario import Scripted, call, done
from test_mapped_scenario import buy
from test_scenario_runtime import evidence_for
from consumer_agent_testing_tool.api_control import ApiControlClient
from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.serialization import load
root = Path(sys.argv[1])
config = Config.model_validate(load(root / 'config.json'))
if config.targets['inventory'].openapi:
    from test_generated_scenario import buy
spec = load(root / 'spec.json')
evidence = evidence_for(root / 'runs')
evidence.scenario_id = evidence.manifest['scenario_id'] = spec['id']
(root / 'run.txt').write_text(str(evidence.path))
async def crash(self):
    os._exit(74)
ApiControlClient.snapshot = crash
asyncio.run(run_api_scenario(spec, config, None, evidence,
    model=Model(Scripted([call('get_price'), buy, done()]), GenerateConfig(max_retries=0)),
    resumable=True))
"""
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(tmp_path),
        env={**child_environment(), "PYTHONPATH": os.pathsep.join(["src", "tests"]),
             "INVENTORY_TOKEN": store.secret},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 74, (stdout, stderr)
    path = (tmp_path / "run.txt").read_text()
    original = reopen_evidence(path, interface_profile("api-tools-surrogate-v1"))
    assert load(original.path / "private/trial.private.json")["status"] == "agent_finished"
    pending, model, checks = await resume(store, original, spec, [])
    assert statuses(checks) == (3, "blocked"), checks
    assert model.inputs == []
    finish_callback(store)
    _, model, checks = await resume(store, pending, spec, [])
    assert statuses(checks) == (0, "completed"), checks
    assert model.inputs == []
    assert store.controls.count("acquire_retained") == 1
    assert [row["path"] for row in store.requests] == ["/price", "/buy"]
