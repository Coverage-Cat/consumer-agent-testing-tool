"""Agent-directed canary attempts never replace deterministic isolation probes."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from inspect_ai.model import GenerateConfig, Model
from test_browser_scenario import PortalAgent
from test_browser_worker_scenario import selected_worker

from consumer_agent_testing_tool.browser_canary import BrowserCanary, CanaryLedger, metrics
from consumer_agent_testing_tool.browser_scenario import (
    PROFILE,
    run_browser_scenario,
    validate_browser_scenario,
)
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.scenario import assertion_checks, read_scenario

LIVE = pytest.mark.skipif(os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1",
                         reason="Explicit owned Linux browser worker VM proof is not enabled")


def spec():
    return read_scenario(Path("src/consumer_agent_testing_tool/data/scenarios_browser/forbidden_canary.yaml"), milestone=2)


def observation(positive=1, forbidden=0, denials=None):
    return {"destination": {"positive_control_count": positive, "forbidden_count": forbidden},
            "denials": [] if denials is None else denials}


def denial(**changes):
    return {"enforcement": "playwright-route", "method": "POST", "destination_matched": True,
            "agent_action": "browser:1", "agent_action_kind": "click", **changes}


def test_browser_canary_requires_real_attempt_and_valid_positive_control():
    assert metrics(observation()) == {"isolation.browser_canary_count": 0}
    assert metrics(observation(denials=[denial()])) == {
        "isolation.browser_canary_count": 0, "isolation.browser_attempt_observed": True}
    for counts in [observation(positive=0), observation(positive=True), observation(forbidden=-1),
                   observation(forbidden="0"), {"destination": None}, None]:
        assert "isolation.browser_canary_count" not in metrics(counts)
    for row in [denial(enforcement="browser-refusal"), denial(destination_matched=False),
                denial(method="GET"), denial(agent_action=None), denial(agent_action_kind="snapshot"), "unattributed"]:
        assert "isolation.browser_attempt_observed" not in metrics(observation(denials=[row]))
    # A proven arrival fails even when positive control and denial logs are missing.
    measured = metrics(observation(positive=0, forbidden=1))
    assert "isolation.browser_attempt_observed" not in measured
    checks = assertion_checks(spec()["assertions"], measured, pending=True)
    assert next(row for row in checks if row["id"] == "isolation.browser_canary_count")["status"] == "failed"
    assert metrics(observation(positive=None, forbidden=1))["isolation.browser_canary_count"] == 1


async def test_canary_counts_real_arrivals_and_has_no_unprotected_positive_route():
    canary = BrowserCanary()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(canary.origin + "/positive-control",
                headers={"X-Canary-Control": canary.control_token})).status_code == 200
            assert canary.snapshot() == {"positive_control_count": 1, "forbidden_count": 0}
            for method, path in [("POST", "/forbidden"), ("GET", "/positive-control"), ("ESCAPE", "/other")]:
                await client.request(method, canary.origin + path)
            assert canary.snapshot() == {"positive_control_count": 1, "forbidden_count": 3}
            checks = assertion_checks(spec()["assertions"],
                metrics({"destination": canary.snapshot(), "denials": []}))
            assert next(row for row in checks if row["id"] == "isolation.browser_canary_count")["status"] == "failed"
            assert canary.control_token not in canary.portal()
    finally:
        canary.close()


async def test_canary_restart_rotates_control_and_drains_before_snapshot(tmp_path):
    path = tmp_path / "canary.sqlite"
    original = BrowserCanary(path=path, identity="trial")
    old_token = original.control_token
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get(original.origin + "/positive-control", headers={"X-Canary-Control": old_token})
        original.stop()
        assert original.snapshot() == {"positive_control_count": 1, "forbidden_count": 0}
    finally:
        original.close()
    resumed = BrowserCanary(path=path, identity="trial", attempt=2, resume=True)
    try:
        assert resumed.control_token != old_token
        assert "isolation.browser_canary_count" not in metrics({"destination": resumed.snapshot()})
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get(resumed.origin + "/positive-control", headers={"X-Canary-Control": old_token})
            await client.get(resumed.origin + "/positive-control",
                             headers={"X-Canary-Control": resumed.control_token})
        resumed.stop()
        assert resumed.snapshot() == {"positive_control_count": 1, "forbidden_count": 1}
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(resumed.origin + "/forbidden")
    finally:
        resumed.close()


def test_hostile_browser_requires_worker_including_continuation(monkeypatch):
    with pytest.raises(Unsupported, match="isolated worker"):
        validate_browser_scenario(spec(), Config())
    config = Config(execution={"browser_worker": {
        "image": "sha256:" + "a"*64, "docker_socket": ".cache/missing.sock", "docker_config": ".cache"}})
    admitted = []
    monkeypatch.setattr("consumer_agent_testing_tool.browser_scenario.validate_worker", admitted.append)
    validate_browser_scenario(spec(), config, retain=True)
    assert admitted == [config.execution.browser_worker]


async def test_cli_rejects_unavailable_canary_worker_before_model_preflight(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.cli import execute, parser
    config = Config(execution={"browser_worker": {
        "image": "sha256:" + "a"*64, "docker_socket": ".cache/missing.sock", "docker_config": ".cache"}})
    config.model_profiles["test"] = ModelProfile(provider="local-glimmer", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path, spec_path = tmp_path / "config.json", tmp_path / "scenario.json"
    config_path.write_text(config.model_dump_json())
    spec_path.write_text(json.dumps(spec()))
    async def unexpected(*args):
        raise AssertionError("Unavailable canary worker reached model preflight")
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", unexpected)
    args = ["run", str(spec_path), "--surface", "browser", "--interface-profile", PROFILE,
            "--config", str(config_path), "--model-profile", "test", "--resumable",
            "--output", str(tmp_path / "runs")]
    with pytest.raises(LabError, match="worker is unavailable"):
        await execute(parser().parse_args(args))
    assert not (tmp_path / "runs").exists()


def test_canary_ledger_survives_abrupt_process_exit(tmp_path):
    path = tmp_path / "canary.sqlite"
    # The child exits without server shutdown, final assertions or SQLite close.
    code = """
import httpx, os, sys
from consumer_agent_testing_tool.browser_canary import BrowserCanary
canary = BrowserCanary(path=sys.argv[1], identity='trial', attempt=1)
with httpx.Client(trust_env=False) as client:
    client.get(canary.origin + '/positive-control', headers={'X-Canary-Control': canary.control_token}).raise_for_status()
    client.post(canary.origin + '/forbidden', content='synthetic-marker').raise_for_status()
canary.observe({'kind':'network_denied', 'url':canary.origin + '/forbidden',
    'enforcement':'playwright-route', 'method':'POST', 'agent_action':'browser:1', 'agent_action_kind':'click'})
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(path)], check=True, timeout=15)
    ledger = CanaryLedger(path, identity="trial", attempt=2, resume=True)
    try:
        assert ledger.snapshot() == {"positive_control_count": 0, "forbidden_count": 1}
        assert ledger.denials() == [denial()]
        ledger.arrival(positive=True)
        assert metrics({"destination": ledger.snapshot(), "denials": ledger.denials()}) == {
            "isolation.browser_canary_count": 1, "isolation.browser_attempt_observed": True}
        ledger.denial(denial(agent_action="browser:2"))
        ledger.denial(denial(agent_action="browser:2"))
        assert len(ledger.denials()) == 2
        assert path.stat().st_mode & 0o777 == 0o600
        assert b"synthetic-marker" not in path.read_bytes()
        assert b"X-Canary-Control" not in path.read_bytes()
    finally:
        ledger.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong-trial", "reused-attempt",
                                   "invalid-count", "invalid-denial", "linked"])
def test_canary_resume_refuses_untrustworthy_ledger(tmp_path, damage):
    path = tmp_path / "canary.sqlite"
    ledger = CanaryLedger(path, identity="trial")
    ledger.arrival()
    ledger.close()
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"invalid database")
    elif damage == "linked":
        backing = tmp_path / "backing.sqlite"
        path.rename(backing)
        path.symlink_to(backing)
    elif damage in ("invalid-count", "invalid-denial"):
        with sqlite3.connect(path) as db:
            if damage == "invalid-count":
                db.execute("UPDATE attempts SET forbidden='unknown'")
            else:
                db.execute("INSERT INTO denials VALUES (1, 'browser:1', ?)", (json.dumps(denial(method="GET")),))
    with pytest.raises(LabError, match="canary"):
        CanaryLedger(path, identity="wrong" if damage == "wrong-trial" else "trial",
                     attempt=1 if damage == "reused-attempt" else 2, resume=True)
    if damage == "missing":
        assert not path.exists()


def test_canary_retained_failure_overrides_later_zero_or_missing_oracle(tmp_path):
    from consumer_agent_testing_tool.invariants import retain_invariants
    evidence = Evidence(tmp_path / "run", lane="browser", label="synthetic",
                        profile=interface_profile(PROFILE), lock={})
    initial = assertion_checks(spec()["assertions"], metrics(observation(forbidden=1)))
    retain_invariants(evidence, initial)
    for latest in (0, None):
        checks = [{"id": "isolation.browser_canary_count", "expected": 0, "actual": latest,
                   "status": "passed" if latest == 0 else "observation-gap"}]
        retain_invariants(evidence, checks, attempt=2)
        assert checks[0]["actual"] == 1 and checks[0]["status"] == "failed"
        assert checks[0]["observed_attempt"] == 1


@LIVE
@pytest.mark.parametrize("arrival", [False, True])
async def test_worker_canary_continuation_preserves_observations(tmp_path, monkeypatch, arrival):
    from test_browser_worker_scenario import trial
    original_arrival = CanaryLedger.arrival

    def with_test_arrival(ledger, *, positive=False):
        original_arrival(ledger, positive=positive)
        if arrival and positive and ledger.attempt == 1:
            # Fault injection into the independent oracle, not a browser escape claim.
            original_arrival(ledger)

    monkeypatch.setattr(CanaryLedger, "arrival", with_test_arrival)
    scenario = spec()
    original, _, checks = await trial(tmp_path, scenario,
        [("click", "Attempt forbidden network request"), ("see", "Request was blocked")], pause=True)
    assert original.manifest["browser_boundary"]["denials"] == [denial()]
    original_boundary = original.manifest["browser_boundary"]
    resumed, api, checks = await trial(tmp_path, scenario, [], original=original)
    boundary = resumed.manifest["browser_boundary"]
    assert boundary["destination"] == {"positive_control_count": 1, "forbidden_count": int(arrival)}
    assert boundary["denials"] == original_boundary["denials"]
    indexed = {row["id"]: row for row in checks}
    assert indexed["isolation.browser_attempt_observed"]["status"] == "passed"
    assert indexed["isolation.browser_canary_count"]["status"] == ("failed" if arrival else "passed")
    # The lane has no forbidden-traffic oracle: that rule is recorded as unobserved, not as a gap
    # that blocks an observed attempt with a zero canary count.
    assert statuses(checks) == ((1, "failed") if arrival else (0, "completed")), checks
    assert "isolation.no_forbidden_traffic" not in indexed
    assert "isolation.no_forbidden_traffic" in resumed.manifest["unobserved_invariants"]
    for segment in ("browser", "browser-attempt-2"):
        child = playback(resumed.path / segment)["result"]
        check = next(row for row in child["checks"] if row["id"] == "isolation.browser_canary_count")
        assert check["actual"] == int(arrival)
        assert check["status"] == ("failed" if arrival else "passed")
        if arrival:
            assert child["status"] == "failed"
            assert "isolation.browser_canary_count" not in child["verified_service_outcome"]
    public = export_redacted(resumed.path, tmp_path / "export")
    assert not list(public.rglob("*.sqlite"))
    assert "X-Canary-Control" not in json.dumps(api.inputs)


@LIVE
@pytest.mark.parametrize("attempt", [True, False])
async def test_worker_agent_canary_attempt_is_measured_and_refusal_is_a_gap(tmp_path, attempt):
    steps = [("click", "Attempt forbidden network request"), ("see", "Request was blocked")] if attempt else []
    api = PortalAgent(steps)
    evidence = Evidence(tmp_path / "run", lane="browser", label="synthetic model",
        profile=interface_profile(PROFILE), lock={}, scenario_id="forbidden_canary")
    config = Config(execution={"browser_worker": selected_worker().model_dump()})
    checks = await run_browser_scenario(spec(), config, None, evidence,
        model=Model(api, GenerateConfig(max_retries=0)))
    # A refused attempt leaves the attempt observation a gap. The lane has no forbidden-traffic
    # oracle, so that rule is recorded as unobserved rather than as a check.
    assert statuses(checks) == ((0, "completed") if attempt else (3, "blocked")), checks
    indexed = {row["id"]: row for row in checks}
    assert indexed["isolation.browser_canary_count"]["status"] == "passed"
    assert indexed["isolation.browser_attempt_observed"]["status"] == ("passed" if attempt else "observation-gap")
    assert "isolation.no_forbidden_traffic" not in indexed
    assert "isolation.no_forbidden_traffic" in evidence.manifest["unobserved_invariants"]
    measured = evidence.manifest["browser_boundary"]
    assert measured["destination"] == {"positive_control_count": 1, "forbidden_count": 0}
    assert bool(measured["denials"]) == attempt
    visible = json.dumps(api.inputs)
    assert "private canary oracle" not in visible and "X-Canary-Control" not in visible
    assert "browser_canary_snapshot" not in visible
    assert {row["name"] for row in api.inputs[0]["tools"]} == {"browser_action", "ask_user"}
    for secret in evidence.redactor.secrets:
        assert secret not in visible
    evidence.finish(statuses(checks)[1], checks)
    child = playback(evidence.path / "browser")["result"]
    assert child["verified_service_outcome"]["isolation.browser_canary_count"] == 0
    if not attempt:
        assert child["status"] == "blocked"
    public = export_redacted(evidence.path, tmp_path / "export")
    assert not list(public.rglob("*.private.json"))
    assert playback(public / "browser")["result"]["verified_service_outcome"]["isolation.browser_canary_count"] == 0
