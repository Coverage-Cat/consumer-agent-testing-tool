"""Actual evidence writers, binary exports, and failure/recovery before deletion."""
import base64
import json
import shutil

import pytest
from test_worker_evidence import CoordinatorCrash, journal, setup_job

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import digest, file_digest
from consumer_agent_testing_tool.worker_export import CHUNK_BYTES, prepare, verify_saved
from consumer_agent_testing_tool.workers import CommandGateway, WorkerLifecycle, reap

SECRET = "synthetic-scoped-worker-export-value"
PROGRAM = """
from pathlib import Path
from consumer_agent_testing_tool.config import interface_profile
from consumer_agent_testing_tool.evidence import Evidence, Redactor, run_lock
from consumer_agent_testing_tool.browser_segments import publish
from consumer_agent_testing_tool.sealed_report import finalize

profile = interface_profile('api-tools-surrogate-v1')
evidence = Evidence(Path('runs'), lane='static', label='protocol-tested', profile=profile,
    lock=run_lock(profile=profile), redactor=Redactor())
evidence.event('state_asserted', {
    'synthetic': True, 'content': 'synthetic-scoped-worker-export-value',
    'large_public_result': 'a' * (300 * 1024), 'unauthorized_effect_count': 1})
(evidence.path / 'checkpoint.private.json').write_text('never-export-private-checkpoint')
browser = evidence.path / 'browser'
browser.mkdir()
(browser / 'events.jsonl').write_text('')
(browser / 'diagnostics.private').mkdir()
(browser / 'diagnostics.private/trace.zip').write_bytes(b'never-export-raw-trace')
finalize(browser, {'status': 'failed', 'run_lock': {'synthetic': True},
                  'unauthorized_effect_count': 1, 'diagnostics': {'private': True}})
publish(evidence, browser)
evidence.finish('failed', [{'id': 'synthetic-invariant', 'status': 'failed', 'actual': 1}])
print('actual evidence written')
"""


def exported_root(controller, result):
    metadata = result["artifacts"]["run_archive"]
    assert metadata["bytes"] > CHUNK_BYTES
    root = controller.path / "evidence"
    verify_saved(root, metadata)
    return root / next(iter(metadata["runs"]))


@pytest.mark.parametrize("fault", [None, "lost-run", "lost-destroy"])
async def test_real_worker_export_retains_full_redacted_evidence_before_destroy(tmp_path, monkeypatch, fault):
    monkeypatch.setenv("WORKER_ACCESS", SECRET)  # Deliberately not a secret-pattern env name.
    package, config = setup_job(tmp_path, PROGRAM)
    config = config.model_copy(update={"credential_env": ["WORKER_ACCESS"]})
    chunks = []

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "destroy":
                saved = journal(tmp_path, payload["trial_id"])
                assert saved["collection"] == "complete"
                verify_saved(tmp_path / "runs" / payload["trial_id"] / "evidence",
                             saved["artifacts"]["run_archive"])
            result = await super().call(operation, payload)
            if operation == "export" and "offset" in payload:
                chunks.append(payload["offset"])
                assert len(base64.b64decode(result["data_base64"])) <= CHUNK_BYTES
            if (operation, fault) in (("run", "lost-run"), ("destroy", "lost-destroy")):
                raise TimeoutError("Synthetic lost reply")
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    result = await controller.run(package["bundle"])
    assert result["collection"] == "complete"
    assert result["status"] == ("completed" if fault is None else "failed")
    root = exported_root(controller, result)
    manifest = json.loads((root / "manifest.json").read_text())
    # Process completion is not a passing behavioral result.
    assert manifest["execution_status"] == "failed" and manifest["checks"][0]["actual"] == 1
    assert manifest["run_lock"]["source_hashes"]
    notice = manifest["dependency_notices"]
    assert file_digest(root / notice["path"]) == notice["sha256"]
    assert (root / notice["path"]).read_bytes().startswith(b"PK")
    artifact = json.loads((root / "events.jsonl").read_text().splitlines()[0])["artifacts"][0]
    assert file_digest(root / artifact["path"]) == artifact["sha256"]
    assert SECRET not in (root / artifact["path"]).read_text()
    assert "never-export" not in json.dumps(result)
    assert not list(root.rglob("*.private*"))
    from consumer_agent_testing_tool.replay import playback
    browser = playback(root / "browser")["result"]
    assert browser["unauthorized_effect_count"] == 1 and browser["status"] == "failed"
    assert chunks == list(range(0, result["artifacts"]["run_archive"]["bytes"], CHUNK_BYTES))
    assert not (tmp_path / "jobs" / controller.path.name).exists()
    assert not (tmp_path / "jobs/.control" / (controller.path.name + ".export")).exists()
    if fault == "lost-destroy":
        assert await reap(tmp_path, config, now=result["expires_at"] + 1) == [controller.path.name]
        assert journal(tmp_path, controller.path.name)["collection"] == "complete"


@pytest.mark.parametrize("fault", ["chunk-corrupt", "chunk-lost", "chunk-other-trial", "missing-artifact"])
async def test_run_export_failure_is_durable_before_cleanup(tmp_path, fault):
    package, config = setup_job(tmp_path, PROGRAM)

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "destroy":
                saved = journal(tmp_path, payload["trial_id"])
                assert saved["collection"] in ("failed", "incomplete")
                assert saved["status"] == "failed"
                assert "actual evidence written" in saved["artifacts"]["files"]["stdout.log"]
                assert not (tmp_path / "runs" / payload["trial_id"] / "evidence").exists()
            result = await super().call(operation, payload)
            if operation == "run" and fault == "missing-artifact":
                next((tmp_path / "jobs" / payload["trial_id"] / "runs").glob("*/artifacts/*.json")).unlink()
            if operation == "export" and "offset" in payload:
                if fault == "chunk-corrupt":
                    content = bytearray(base64.b64decode(result["data_base64"]))
                    content[0] ^= 1
                    result["data_base64"] = base64.b64encode(content).decode()
                if fault == "chunk-lost":
                    raise TimeoutError("Synthetic interrupted transfer")
                if fault == "chunk-other-trial":
                    result["trial_id"] = "worker-" + "0" * 32
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    result = await controller.run(package["bundle"])
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert result["artifacts_sha256"] == digest(result["artifacts"])
    assert not list(controller.path.glob(".worker-transfer-*"))


async def test_reaper_retries_interrupted_archive_transfer_without_reexecuting(tmp_path):
    package, config = setup_job(tmp_path, PROGRAM)

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "export" and payload.get("offset") == CHUNK_BYTES:
                raise CoordinatorCrash()
            return await super().call(operation, payload)

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    with pytest.raises(CoordinatorCrash):
        await controller.run(package["bundle"])
    saved = journal(tmp_path, controller.path.name)
    assert saved["collection"] == "pending"
    assert not (controller.path / "evidence").exists()
    assert await reap(tmp_path, config, now=saved["expires_at"] + 1) == [controller.path.name]
    recovered = journal(tmp_path, controller.path.name)
    assert recovered["collection"] == "complete"
    exported_root(controller, recovered)
    assert sum(event["operation"] == "run" for event in recovered["events"]) == 1


async def test_reaper_does_not_trust_corrupted_saved_run_after_remote_deletion(tmp_path):
    package, config = setup_job(tmp_path, PROGRAM)

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            result = await super().call(operation, payload)
            if operation == "destroy":
                raise CoordinatorCrash()
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    with pytest.raises(CoordinatorCrash):
        await controller.run(package["bundle"])
    saved = journal(tmp_path, controller.path.name)
    (exported_root(controller, saved) / "manifest.json").write_text("{}")
    with pytest.raises(LabError, match="recovery incomplete"):
        await reap(tmp_path, config, now=saved["expires_at"] + 1)
    assert journal(tmp_path, controller.path.name)["collection"] != "complete"


async def test_worker_export_retains_actual_inspect_binary_log(tmp_path):
    # Real Inspect execution with an in-process scripted model; no inference endpoint.
    from inspect_ai import Task, eval_async
    from inspect_ai.dataset import Sample
    from inspect_ai.log import read_eval_log
    from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
    from inspect_ai.solver import generate

    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.inspect_logs import export_logs
    from consumer_agent_testing_tool.providers import CapturingAPI

    class Scripted(ModelAPI):
        async def generate(self, *args, **kwargs):
            return ModelOutput.from_content("test/worker-export", "Synthetic recorded output")

    job = tmp_path / "job"
    evidence = Evidence(job / "runs", lane="static", label="protocol-tested", lock={},
                        profile=interface_profile("api-tools-surrogate-v1"))
    model = Model(CapturingAPI(Scripted("test/worker-export"), evidence),
                  GenerateConfig(max_retries=0))
    logs = await eval_async(Task(dataset=[Sample(input="Synthetic input")], solver=generate()),
        model=model, log_dir="memory://worker-export", log_format="json", log_model_api=False)
    export_logs(logs, evidence)
    evidence.finish("completed")
    metadata = prepare(job, tmp_path / "snapshot")
    from consumer_agent_testing_tool.worker_export import unpack
    unpack(tmp_path / "snapshot/runs.zip", tmp_path / "collected", metadata)
    root = tmp_path / "collected/runs" / evidence.run_id
    manifest = json.loads((root / "manifest.json").read_text())
    reference = manifest["inspect_log_artifacts"][0]
    assert file_digest(root / reference["path"]) == reference["sha256"]
    assert read_eval_log(str(root / reference["path"])).status == "success"


def test_transfer_inventory_rejects_unsafe_paths_and_unlinked_files(tmp_path):
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.worker_export import unpack, validate_metadata

    job = tmp_path / "job"
    evidence = Evidence(job / "runs", lane="static", label="protocol-tested", lock={},
                        profile=interface_profile("api-tools-surrogate-v1"))
    evidence.finish("completed")
    metadata = prepare(job, tmp_path / "snapshot")
    for name in ("../escape", "/absolute", "runs/r/../../escape", "runs\\r\\bad"):
        bad = metadata | {"files": {name: {"sha256": "0" * 64, "bytes": 1}}}
        with pytest.raises(LabError):
            validate_metadata(bad)
    unpack(tmp_path / "snapshot/runs.zip", tmp_path / "saved", metadata)
    shutil.copyfile(tmp_path / "saved/runs" / evidence.run_id / "manifest.json",
                    tmp_path / "saved/extra.json")
    with pytest.raises(LabError, match="inventory"):
        verify_saved(tmp_path / "saved", metadata)


def test_worker_export_keeps_sealed_surface_and_its_agent_manifest(tmp_path):
    from consumer_agent_testing_tool.config import interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.replay import playback
    from consumer_agent_testing_tool.sealed_report import finalize
    from consumer_agent_testing_tool.worker_export import unpack

    job = tmp_path / "job"
    surface = job / "runs/surface-synthetic"
    agent = Evidence(surface / "agent", lane="static", label="protocol-tested", lock={},
                     profile=interface_profile("api-tools-surrogate-v1"))
    agent.event("tool_result", {"synthetic": True})
    agent.finish("completed")
    finalize(surface, {"status": "completed", "run_lock": {"synthetic": True},
                       "agent_evidence": "agent/" + agent.run_id})
    metadata = prepare(job, tmp_path / "snapshot")
    unpack(tmp_path / "snapshot/runs.zip", tmp_path / "saved", metadata)
    saved = tmp_path / "saved/runs/surface-synthetic"
    assert playback(saved)["integrity"] == "verified"
    assert (saved / "agent" / agent.run_id / "manifest.json").is_file()
