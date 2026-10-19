import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.calibration import evaluate
from consumer_agent_testing_tool.consent import ConsentBroker, canonical, effective_arguments
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.fixture import FixtureError
from consumer_agent_testing_tool.release import audit, source_digest
from consumer_agent_testing_tool.replay import ContractTape, compare, differences, playback, seal
from consumer_agent_testing_tool.serialization import digest, file_digest
from consumer_agent_testing_tool.surface import SurfaceRun
from consumer_agent_testing_tool.transactions import SCHEMA, TransactionFixture
from consumer_agent_testing_tool.workers import WorkerConfig, WorkerLifecycle, bundle, reap

NOW = "2026-10-01T17:00:00Z"
LATER = "2026-10-01T17:05:00Z"


def arguments():
    return {"id": "draft-1", "expected_revision": 1, "amount_minor": 12500,
            "currency": "USD", "idempotency_key": "commit-1"}


def grant(fixture, args, operation="commit"):
    return fixture.issue_grant("alice", {"commit": "reservations.confirm", "cancel": "reservations.cancel"}[operation],
                               args, decision="approve_once", expires_at=LATER)["id"]


def test_consent_canonical_cross_language_vectors():
    vectors = [
        {"z": -0.0, "a": 2.0, "emoji": "😀", "controls": "\b\n\u0001"},
        {"\ue000": 1, "\U00010000": 2, "a": [None, False, 9007199254740991]},
        # Fractional schema numbers use ECMAScript Number-to-String, as in Node.
        {"maximum": 999.99, "examples": [2.5, 0.1, 1e-7, 1e21, 123e-20, -0.000001], "default": 0.3},
    ]
    script = """const canon=x=>Array.isArray(x)?'['+x.map(canon).join(',')+']':
      x!==null&&typeof x==='object'?'{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+canon(x[k])).join(',')+'}':
      JSON.stringify(x);let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>console.log(canon(JSON.parse(s))));"""
    for value in vectors:
        result = subprocess.run(["node", "-e", script], input=json.dumps(value),
                                capture_output=True, text=True, check=True)
        assert canonical(value) == result.stdout.strip()
    for value in (float("nan"), float("inf"), 2**53, "\ud800"):
        with pytest.raises(LabError):
            canonical(value)


def test_transaction_response_loss_is_exactly_once_and_cancel_requires_new_grant(tmp_path):
    fixture = TransactionFixture(tmp_path / "state.sqlite", "trial")
    fixture.seed()
    args = arguments()
    identifier = grant(fixture, args)
    with pytest.raises(TimeoutError):
        fixture.execute("commit", args, identifier, now=NOW, lose_response=True)
    assert fixture.execute("commit", args, identifier, now=NOW)["state"] == "confirmed"
    assert sum(e["effect"] in ("commit", "cancel") for e in fixture.snapshot()["effects"]) == 1
    cancel = {**args, "expected_revision": 2, "idempotency_key": "cancel-1"}
    with pytest.raises(FixtureError):
        fixture.execute("cancel", cancel, identifier, now=NOW)
    fresh = grant(fixture, cancel, "cancel")
    assert fixture.execute("cancel", cancel, fresh, now=NOW)["state"] == "cancelled"
    assert sum(e["effect"] in ("commit", "cancel") for e in fixture.snapshot()["effects"]) == 2


@pytest.mark.parametrize("change", ["total", "principal", "expiry", "revocation", "arguments"])
def test_bad_grants_have_no_effect(tmp_path, change):
    fixture = TransactionFixture(tmp_path / "state.sqlite", "trial")
    fixture.seed()
    args = arguments()
    identifier = grant(fixture, args)
    principal, now = "alice", NOW
    if change == "total":
        fixture.change_total(13500)
    if change == "principal":
        principal = "bob"
    if change == "expiry":
        now = LATER
    if change == "revocation":
        fixture.broker.revoke(identifier)
    if change == "arguments":
        args["idempotency_key"] = "different"
    with pytest.raises(FixtureError):
        fixture.execute("commit", args, identifier, now=now, principal=principal)
    assert not any(e["effect"] in ("commit", "cancel") for e in fixture.snapshot()["effects"])


def test_atomic_grant_consumption_across_restarts(tmp_path):
    fixture = TransactionFixture(tmp_path / "state.sqlite", "trial")
    fixture.seed()
    args = arguments()
    identifier = grant(fixture, args)
    def consume():
        broker = ConsentBroker(fixture.path)
        try:
            broker.consume(identifier, fixture.envelope("alice", "reservations.confirm", args), SCHEMA, args, now=NOW)
            return True
        except LabError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: consume(), range(4))) == 1


def test_defaults_and_schema_digest_are_bound(tmp_path):
    fixture = TransactionFixture(tmp_path / "state.sqlite", "trial")
    schema = {"type": "object", "properties": {"options": {"type": "object", "properties": {
        "quantity": {"type": "integer", "default": 1}}}}, "additionalProperties": False}
    args = {"options": {}}
    assert effective_arguments(schema, args) == {"options": {"quantity": 1}}
    envelope = fixture.envelope("alice", "reservations.confirm", arguments())
    issued = fixture.broker.issue(envelope, schema, args, decision="approve_once", now=NOW, expires_at=LATER)
    schema["properties"]["options"]["properties"]["quantity"]["default"] = 2
    with pytest.raises(LabError, match="binding changed"):
        fixture.broker.consume(issued["id"], envelope, schema, args, now=NOW)


@pytest.mark.parametrize("agent", [False, True])
async def test_surface_consent_is_host_owned_and_resumable(tmp_path, agent):
    from test_surface_agent import driver
    planner, api = driver() if agent else (None, None)
    contract = scenario()
    fixture = TransactionFixture(tmp_path / "fixture.sqlite", "trial")
    contract.tools["prepare"].consent = {
        "reply_key": "prepare-approval", "now": NOW, "expires_at": LATER,
        "envelope": fixture.envelope("alice", "reservations.confirm", arguments()),
        "schema": {"type": "object", "properties": {"token": {"type": "string"}},
                   "required": ["token"], "additionalProperties": False},
    }
    host = FakeHost()
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "secret"}, planner=planner)
    assert await first.run() == 3
    assert [name for name, _ in host.calls] == ["status"]
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path, planner=planner,
                         replies={"prepare-approval": "approve_once"})
    assert await resumed.run() == 0
    assert "consent_consumed" in (resumed.path / "events.jsonl").read_text()
    if api:
        assert "approve_once" not in json.dumps(api.inputs)


async def test_sealed_playback_diff_and_tamper_detection(tmp_path):
    first = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    second = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    assert await first.run() == await second.run() == 0
    assert playback(first.path)["execution"] is False
    result = compare(first.path, second.path)
    assert result["lock_compatible"]
    assert result["outcomes"] == []
    with (first.path / "events.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(LabError, match="integrity"):
        playback(first.path)


def test_contract_replay_fails_closed():
    args = {"id": "test"}
    fixture = ContractTape([{"request": {"step": "read", "operation": "get", "arguments": args,
        "headers": {}, "body_sha256": digest(args), "fixture_version": "v1"},
        "response": {"ok": True}}], "v1")
    with pytest.raises(LabError, match="mismatch"):
        fixture.request(step="read", operation="delete", arguments=args)
    assert fixture.request(step="read", operation="get", arguments=args) == {"ok": True}
    fixture.complete()
    with pytest.raises(LabError, match="exhausted"):
        fixture.request(step="read", operation="get", arguments=args)


async def test_diff_requires_declared_treatment_and_complete_seal(tmp_path):
    assert differences({}, {"explicit_unknown": None})[0]["change"] == "added"
    first = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    second = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    assert await first.run() == await second.run() == 0
    path = second.path / "result.json"
    result = json.loads(path.read_text())
    result["run_lock"]["model"] = {"revision": "changed"}
    path.write_text(json.dumps(result))
    seal(second.path)
    assert not compare(first.path, second.path)["lock_compatible"]
    assert compare(first.path, second.path, treatments=["/model"])["lock_compatible"]
    (first.path / "evidence.sha256.json").write_text("{}")
    with pytest.raises(LabError, match="incomplete"):
        playback(first.path)


async def test_replay_rejects_linked_artifact_omission_and_filesystem_links(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "secret"})
    assert await run.run() == 0
    outside = tmp_path / "other-evidence.json"
    outside.write_text("{}")
    manifest = run.path / "evidence.sha256.json"
    hashes = json.loads(manifest.read_text())
    (run.path / "linked.json").symlink_to(outside)
    manifest.write_text(json.dumps({**hashes, "linked.json": file_digest(outside)}))
    with pytest.raises(LabError, match="links"):
        playback(run.path)
    manifest.write_text(json.dumps({**hashes, "../other-evidence.json": file_digest(outside)}))
    with pytest.raises(LabError, match="path"):
        playback(run.path)
    result_path = run.path / "result.json"
    result = json.loads(result_path.read_text())
    result["agent_runs"] = ["agent/missing"]
    result_path.write_text(json.dumps(result))
    hashes["result.json"] = file_digest(result_path)
    manifest.write_text(json.dumps(hashes))
    with pytest.raises(LabError, match="missing"):
        playback(run.path)


@pytest.mark.parametrize("filename", ["changed.py", "data/scenario.yaml"])
def test_release_audit_requires_current_source_and_unexpired_evidence(tmp_path, filename):
    now = datetime.now(UTC)
    artifact = tmp_path / "evidence.json"
    artifact.write_text(json.dumps({
        "gate": "transactional-consent", "basis": "test-report", "status": "passed",
        "checks": [{"status": "passed"}], "source_sha256": source_digest(tmp_path)}))
    entry = {"status": "passed", "basis": "test-report", "artifact": artifact.name,
             "sha256": file_digest(artifact), "expires_at": (now + timedelta(hours=1)).isoformat()}
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"gates": {"transactional-consent": entry}}))
    assert audit(tmp_path, ledger, now)["gates"]["transactional-consent"]["status"] == "passed"
    assert not audit(tmp_path, ledger, now)["release_qualified"]
    assert audit(tmp_path, ledger, now + timedelta(hours=2))["gates"]["transactional-consent"]["status"] == "unverified"
    source = tmp_path / "src/consumer_agent_testing_tool" / filename
    source.parent.mkdir(parents=True)
    source.write_text("# different harness\n")
    assert audit(tmp_path, ledger, now)["gates"]["transactional-consent"]["status"] == "unverified"


def test_calibration_heldout_visibility_staleness_and_hashes(tmp_path):
    artifact = tmp_path / "observed.json"
    artifact.write_text('{"visible_outcome": true}')
    now = datetime.now(UTC)
    packet = {
        "cohort": "authorized-test", "authorized": True,
        "observed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "surface_revision": "observed-cohort-1", "profile_sha256": "profile",
        "visibility": {"model": "unknown", "policy": "partial", "browser": "observed",
                       "discovery": "unknown"},
        "observations": [{"scenario_id": "heldout", "trial_id": "1", "split": "held-out",
                          "local_success": True, "muse_success": False, "artifact": artifact.name,
                          "artifact_sha256": file_digest(artifact)}],
    }
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(packet))
    result = evaluate(tmp_path, path, profile_sha256="profile", now=now)
    assert result["groups"]["held-out"]["false_local_passes"] == 1
    assert result["visibility"]["model"] == "unknown"
    assert evaluate(tmp_path, path, profile_sha256="changed", now=now)["stale"]
    artifact.write_text("changed")
    with pytest.raises(LabError, match="mismatch"):
        evaluate(tmp_path, path, profile_sha256="profile", now=now)


def test_release_audit_does_not_promote_excluded_live_gate(tmp_path):
    artifact = tmp_path / "mock.json"
    artifact.write_text(json.dumps({"gate": "bedrock-iam", "basis": "test-report", "status": "passed",
                                    "checks": [{"status": "passed"}]}))
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"gates": {"bedrock-iam": {
        "status": "passed", "basis": "test-report", "artifact": artifact.name,
        "sha256": file_digest(artifact), "expires_at": "2099-01-01T00:00:00Z"}}}))
    result = audit(tmp_path, ledger)
    assert not result["release_qualified"]
    assert "bedrock-iam" not in result["gates"]
    assert "bedrock-iam" in result["excluded_capabilities"]


@pytest.mark.skipif(not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "P_PID")),
                    reason="worker supervision needs os.waitid (on macOS from Python 3.13)")
async def test_actual_worker_gateway_bundle_run_export_destroy(tmp_path):
    (tmp_path / "main.py").write_text("print('isolated synthetic worker result')\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": ["main.py"], "command": [sys.executable, "main.py"]}))
    package = bundle(tmp_path, manifest, "worker.zip")
    gateway_script = Path(__file__).resolve().parents[1] / "src/consumer_agent_testing_tool/worker_gateway.py"
    config = WorkerConfig(provider="local-process-reference", run_mode="blocking",
        gateway=[sys.executable, str(gateway_script), str(tmp_path / "workers")],
        expected_harness_sha256=package["sha256"])
    controller = WorkerLifecycle(tmp_path, config)
    result = await controller.run(package["bundle"])
    assert result["status"] == "completed" and result["cleanup"] == "confirmed"
    assert "synthetic worker" in result["artifacts"]["files"]["stdout.log"]
    assert not (tmp_path / "workers" / controller.path.name).exists()
    assert [event["operation"] for event in result["events"]] == [
        "provision", "ready", "seed", "run", "export", "destroy"]


async def test_worker_failure_and_ttl_reaper_attempt_cleanup(tmp_path):
    class Gateway:
        def __init__(self):
            self.calls = []
        async def call(self, operation, payload):
            self.calls.append(operation)
            if operation == "run":
                raise TimeoutError()
            return {"trial_id": payload["trial_id"], "ready": True, "seeded": True,
                    "destroyed": operation == "destroy", "cancelled": True,
                    "files": {}, "complete": True, "issues": []}
    package = tmp_path / "bundle.zip"
    package.write_bytes(b"test")
    config = WorkerConfig(provider="test", gateway=["unused"], expected_harness_sha256=file_digest(package))
    gateway = Gateway()
    controller = WorkerLifecycle(tmp_path, config, gateway)
    result = await controller.run(package)
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert gateway.calls[-3:] == ["cancel", "export", "destroy"]
    record = json.loads((controller.path / "worker.json").read_text())
    record.update(cleanup="pending", expires_at=0)
    (controller.path / "worker.json").write_text(json.dumps(record))
    assert await reap(tmp_path, config.model_copy(update={"gateway": ["another-host"]}),
                      gateway, now=1) == []
    assert await reap(tmp_path, config, gateway, now=1) == [controller.path.name]


async def test_worker_cannot_claim_cleanup_from_an_empty_response(tmp_path):
    class Gateway:
        async def call(self, operation, payload):
            return {"ready": True, "exit_code": 0}
    package = tmp_path / "bundle.zip"
    package.write_bytes(b"test")
    config = WorkerConfig(provider="test", gateway=["unused"], expected_harness_sha256=file_digest(package))
    result = await WorkerLifecycle(tmp_path, config, Gateway()).run(package)
    assert result["status"] == "failed" and result["cleanup"] == "failed"


async def test_ssh_adapter_pins_identity_hosts_and_transfers_bundle(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.workers import CommandGateway, SSHGateway
    for name in ("identity", "known-hosts"):
        (tmp_path / name).write_text("fixture")
    package = tmp_path / "bundle.zip"
    package.write_bytes(b"synthetic")
    config = WorkerConfig(provider="ssh", ssh_target="test@example.invalid",
        ssh_identity_file="identity", ssh_known_hosts_file="known-hosts",
        expected_harness_sha256=file_digest(package))
    calls = []
    async def call(self, operation, payload):
        calls.append((self.config.gateway, operation, payload))
        return {"seeded": True}
    monkeypatch.setattr(CommandGateway, "call", call)
    await SSHGateway(tmp_path, config).call("seed", {"bundle": str(package), "trial_id": "fixture"})
    argv, _, payload = calls[0]
    assert "StrictHostKeyChecking=yes" in argv
    assert any(arg == f"UserKnownHostsFile={tmp_path / 'known-hosts'}" for arg in argv)
    assert "bundle" not in payload and payload["bundle_base64"]
