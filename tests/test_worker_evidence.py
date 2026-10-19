"""Real subprocess exports plus explicit faulty gateway/recovery boundaries."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import digest
from consumer_agent_testing_tool.worker_gateway import handle
from consumer_agent_testing_tool.workers import (
    CommandGateway,
    WorkerConfig,
    WorkerLifecycle,
    bundle,
    reap,
)


def setup_job(root, code="print('synthetic diagnostic')", command=None):
    (root / "main.py").write_text(code)
    (root / "manifest.json").write_text(json.dumps({
        "files": ["main.py"], "command": command or [sys.executable, "main.py"]}))
    package = bundle(root, "manifest.json", "bundle.zip")
    config = WorkerConfig(provider="local-process-reference", run_mode="blocking",
        gateway=[sys.executable, str(Path(__file__).resolve().parents[1] /
                                    "src/consumer_agent_testing_tool/worker_gateway.py"), str(root / "jobs")],
        expected_harness_sha256=package["sha256"])
    return package, config


def journal(root, trial):
    return json.loads((root / "runs" / trial / "worker.json").read_text())


@pytest.mark.parametrize("fault", ["lost-run-reply", "expired", "cancelled"])
async def test_worker_failure_collects_real_logs_before_deletion(tmp_path, fault):
    package, config = setup_job(tmp_path)

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "destroy":
                saved = journal(tmp_path, payload["trial_id"])
                assert saved["collection"] == "complete"
                assert saved["artifacts_sha256"] == digest(saved["artifacts"])
                assert "synthetic diagnostic" in saved["artifacts"]["files"]["stdout.log"]
                assert (tmp_path / "jobs" / payload["trial_id"]).exists()
                if fault == "expired":
                    assert saved["execution"]["exit_code"] == 0
            result = await super().call(operation, payload)
            if operation == "run":
                if fault == "expired":
                    (tmp_path / "jobs" / payload["trial_id"] / "state.json").write_text(
                        json.dumps({"expires_at": 0}))
                    with pytest.raises(LabError, match="TTL expired"):
                        await super().call("ready", payload)
                elif fault == "cancelled":
                    raise asyncio.CancelledError()
                else:
                    raise TimeoutError("Synthetic lost response after execution")
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    result = await controller.run(package["bundle"])
    assert result["status"] == ("completed" if fault == "expired" else "failed")
    assert result["cleanup"] == "confirmed"
    assert result["collection"] == "complete"
    assert not (tmp_path / "jobs" / controller.path.name).exists()
    assert any(event["operation"] == "run" and event["status"] == "failed"
               for event in result["events"]) == (fault != "expired")


class CoordinatorCrash(BaseException):
    """Do not let the lifecycle's ordinary exception recovery hide this boundary."""


@pytest.mark.parametrize("deleted", [False, True])
async def test_worker_reaper_preserves_durable_export_across_destroy_reply_loss(tmp_path, deleted):
    package, config = setup_job(tmp_path)

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "destroy":
                assert journal(tmp_path, payload["trial_id"])["collection"] == "complete"
                if deleted:
                    await super().call(operation, payload)
                raise CoordinatorCrash()
            return await super().call(operation, payload)

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    with pytest.raises(CoordinatorCrash):
        await controller.run(package["bundle"])
    before = journal(tmp_path, controller.path.name)
    assert before["events"][-1] == {"operation": "destroy", "status": "requested"}
    assert before["execution"]["exit_code"] == 0
    assert (tmp_path / "jobs" / controller.path.name).exists() != deleted
    assert await reap(tmp_path, config, now=time.time() + 3601) == [controller.path.name]
    after = journal(tmp_path, controller.path.name)
    assert after["cleanup"] == "confirmed" and after["collection"] == "complete"
    assert after["artifacts"] == before["artifacts"]
    assert after["artifacts_sha256"] == digest(after["artifacts"])
    assert [e["operation"] for e in after["events"]].count("export") == 1
    assert [e["operation"] for e in after["events"]].count("run") == 1
    assert await reap(tmp_path, config, now=time.time() + 3601) == []


async def test_worker_reaper_collects_after_crash_before_export(tmp_path):
    package, config = setup_job(tmp_path)

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "export":
                raise CoordinatorCrash()
            return await super().call(operation, payload)

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    with pytest.raises(CoordinatorCrash):
        await controller.run(package["bundle"])
    assert journal(tmp_path, controller.path.name)["execution"]["exit_code"] == 0
    (tmp_path / "jobs" / controller.path.name / "state.json").write_text(json.dumps({"expires_at": 0}))
    assert await reap(tmp_path, config, now=time.time() + 3601) == [controller.path.name]
    saved = journal(tmp_path, controller.path.name)
    assert "synthetic diagnostic" in saved["artifacts"]["files"]["stdout.log"]
    assert saved["cleanup"] == "confirmed" and saved["collection"] == "complete"
    assert [e["operation"] for e in saved["events"]].count("run") == 1


@pytest.mark.parametrize("credential", ["original", "missing", "rotated"])
async def test_recovery_requires_original_redaction_values_before_export(tmp_path, monkeypatch, credential):
    secret = "synthetic-original-worker-value"
    package, config = setup_job(tmp_path, code=f"print({secret!r})")

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "export":
                raise CoordinatorCrash()
            return await super().call(operation, payload)

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config), output="custom-output")
    with pytest.raises(CoordinatorCrash):
        await controller.run(package["bundle"])
    # Simulate the durable metadata of a scoped coordinator whose process was
    # lost before its public export. The plaintext remains solely in worker logs.
    controller.record["coordinator_credential_env"] = ["RECOVERY_MODEL_AUTH"]
    controller.record["coordinator_redaction_sha256"] = [digest(secret)]
    controller.save()
    if credential == "missing":
        monkeypatch.delenv("RECOVERY_MODEL_AUTH", raising=False)
    else:
        monkeypatch.setenv("RECOVERY_MODEL_AUTH", secret if credential == "original" else "rotated-test-value")
    if credential == "original":
        assert await reap(tmp_path, config, now=time.time() + 3601,
                          output="custom-output") == [controller.path.name]
    else:
        with pytest.raises(LabError, match="recovery incomplete"):
            await reap(tmp_path, config, now=time.time() + 3601, output="custom-output")
    saved = json.loads((controller.path / "worker.json").read_text())
    assert saved["cleanup"] == "confirmed" and secret not in json.dumps(saved)
    if credential == "original":
        assert saved["collection"] == "complete"
        assert "[REDACTED]" in saved["artifacts"]["files"]["stdout.log"]
    else:
        assert saved["collection"] == "failed" and "artifacts" not in saved
        assert "redaction values are unavailable" in saved["collection_error"]


@pytest.mark.parametrize("code,command,issue", [
    ("print('before failing'); raise RuntimeError('synthetic failure')", None, None),
    ("", ["/nonexistent-synthetic-executable"], None),
    ("print('x' * (300 * 1024))", None, "truncated"),
    ("import os; os.write(1, b'\\xff')", None, "UTF-8"),
])
async def test_worker_partial_or_failed_execution_cannot_claim_complete_success(tmp_path, code, command, issue):
    package, config = setup_job(tmp_path, code, command)
    result = await WorkerLifecycle(tmp_path, config).run(package["bundle"])
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert result["collection"] == ("incomplete" if issue else "complete")
    assert result["artifacts_sha256"] == digest(result["artifacts"])
    if issue:
        assert any(issue in message for message in result["artifacts"]["issues"])
    else:
        if command:
            assert result["execution"]["reason"] == "guard_error"
            assert result["execution"]["exit_code"] != 0
        else:
            assert "synthetic failure" in result["artifacts"]["files"]["stderr.log"]
            assert result["execution"]["exit_code"] == 1


class ProtocolGateway:
    async def call(self, operation, payload):
        return {"trial_id": payload["trial_id"], "ready": True, "seeded": True,
                "exit_code": 0, "files": {"stdout.log": "synthetic result"},
                "complete": True, "issues": [], "cancelled": True, "destroyed": True}


@pytest.mark.parametrize("change", [
    {"complete": None}, {"complete": 1}, {"files": None}, {"files": {"stdout.log": 3}},
    {"issues": None}, {"issues": ["missing data"]}, {"trial_id": "wrong-trial"},
])
async def test_worker_export_requires_explicit_matching_complete_evidence(tmp_path, change):
    package, config = setup_job(tmp_path)

    class Gateway(ProtocolGateway):
        async def call(self, operation, payload):
            result = await super().call(operation, payload)
            return result | change if operation == "export" else result

    result = await WorkerLifecycle(tmp_path, config, Gateway()).run(package["bundle"])
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert result["collection"] == "failed"
    assert "artifacts" not in result


async def test_worker_export_redaction_and_hash_are_persisted_before_destroy(tmp_path, monkeypatch):
    secret = "synthetic-worker-credential-do-not-export"
    monkeypatch.setenv("TEST_WORKER_SECRET", secret)
    package, config = setup_job(tmp_path)
    config = config.model_copy(update={"credential_env": ["TEST_WORKER_SECRET"]})

    class Gateway(ProtocolGateway):
        async def call(self, operation, payload):
            result = await super().call(operation, payload)
            if operation == "export":
                result["files"]["stdout.log"] = secret
            if operation == "destroy":
                saved = journal(tmp_path, payload["trial_id"])
                assert secret not in json.dumps(saved)
                assert saved["artifacts_sha256"] == digest(saved["artifacts"])
            return result

    result = await WorkerLifecycle(tmp_path, config, Gateway()).run(package["bundle"])
    assert result["status"] == "completed"
    assert secret not in json.dumps(result)


async def test_worker_reaper_attempts_other_jobs_and_reports_collection_failure(tmp_path):
    package, config = setup_job(tmp_path)

    class Crash(ProtocolGateway):
        async def call(self, operation, payload):
            if operation == "export":
                raise CoordinatorCrash()
            return await super().call(operation, payload)

    controllers = [WorkerLifecycle(tmp_path, config, Crash()) for _ in range(2)]
    for controller in controllers:
        with pytest.raises(CoordinatorCrash):
            await controller.run(package["bundle"])
    calls = []

    class Recovery(ProtocolGateway):
        async def call(self, operation, payload):
            calls.append((operation, payload["trial_id"]))
            result = await super().call(operation, payload)
            if operation == "export" and payload["trial_id"] == controllers[0].path.name:
                raise TimeoutError("Synthetic export unavailable")
            return result

    with pytest.raises(LabError, match="recovery incomplete for 1"):
        await reap(tmp_path, config, Recovery(), now=time.time() + 3601)
    failed, complete = [journal(tmp_path, c.path.name) for c in controllers]
    assert failed["status"] == "failed" and failed["cleanup"] == "confirmed"
    assert failed["collection"] == "failed"
    assert complete["cleanup"] == "confirmed" and complete["collection"] == "complete"
    for controller in controllers:
        assert ("destroy", controller.path.name) in calls


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "fifo", "oversize", "running"])
def test_reference_gateway_export_rejects_unsafe_or_incomplete_artifacts(tmp_path, kind):
    trial = "worker-" + "a" * 32
    job = tmp_path / trial
    job.mkdir()
    for name in ("stdout.log", "stderr.log", "exit.json"):
        (job / name).write_text("{}")
    forbidden = tmp_path / "private-host-input"
    forbidden.mkdir()
    (forbidden / "result.json").write_text("synthetic secret must stay private")
    if kind == "file-link":
        (job / "stdout.log").unlink()
        (job / "stdout.log").symlink_to(forbidden / "result.json")
    elif kind == "directory-link":
        (job / "runs").symlink_to(forbidden, target_is_directory=True)
    elif kind == "fifo":
        (job / "stdout.log").unlink()
        os.mkfifo(job / "stdout.log")
    elif kind == "oversize":
        runs = job / "runs"
        runs.mkdir()
        for index in range(5):
            directory = runs / str(index)
            directory.mkdir()
            (directory / "result.json").write_text("x" * (256 * 1024))
    else:
        (job / "running").touch()
        with pytest.raises(ValueError, match="terminal receipt is invalid"):
            handle(tmp_path, {"operation": "export", "trial_id": trial})
        return
    result = handle(tmp_path, {"operation": "export", "trial_id": trial})
    assert result["complete"] is False
    assert result["issues"]
    assert "synthetic secret" not in json.dumps(result)
    assert sum(len(value.encode()) for value in result["files"].values()) <= 512 * 1024
