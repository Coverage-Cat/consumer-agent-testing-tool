"""Private transport integrity and real Inspect continuation across worker deletion."""
# ruff: noqa: F811 — imported pytest fixtures
import base64
import json
import os
import shutil
import time

import pytest
from test_api_lease import retained  # noqa: F401
from test_mapped_scenario import inventory  # noqa: F401
from test_worker_run import configuration

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.config import interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, run_lock
from consumer_agent_testing_tool.serialization import digest, file_digest, load
from consumer_agent_testing_tool.worker_checkpoint import (
    CHUNK_BYTES,
    CapsuleOwner,
    archive_run,
    export_chunk,
    prepare,
    read_record,
    receive,
    unpack,
    validate_metadata,
    verify_received,
    verify_tree,
)
from consumer_agent_testing_tool.workers import WorkerConfig, WorkerJournal

SOURCE = "worker-" + "1" * 32


def archive_fixture(root, **state):
    profile = interface_profile("api-tools-surrogate-v1")
    evidence = Evidence(root / "runs", lane="static", label="surrogate behavior",
                        profile=profile, lock=run_lock(profile=profile))
    evidence.manifest["resumable"] = True
    private = evidence.path / "private"
    private.mkdir(mode=0o700)
    (private / "trial.private.json").write_text(json.dumps({
        "status": "running", "deadline": time.time() + 300, "run_id": evidence.run_id, **state}))
    # This unit fixture tests byte transport, not database semantics. The
    # native SDK/Inspect test below resumes an actual reservation database.
    (private / "fixture.sqlite").write_bytes(os.urandom(2 * CHUNK_BYTES + 19))
    (private / "sensitive.private.json").write_text('{"contact":"archive-only@example.test"}')
    evidence.event("state_asserted", {"synthetic": True})
    evidence.finish("blocked", [{"id": "synthetic-pause", "status": "blocked"}])
    archive = root / "checkpoint.zip"
    metadata = archive_run(evidence.path, archive, SOURCE)
    return archive, metadata


def send(job, archive, metadata):
    data = archive.read_bytes()
    for offset in range(0, len(data), CHUNK_BYTES):
        message = {"mode": "chunk", "sha256": metadata["sha256"], "offset": offset,
                   "data_base64": base64.b64encode(data[offset:offset + CHUNK_BYTES]).decode()}
        assert receive(job, message) == receive(job, message)


def seeded(root, metadata):
    job = root / ("worker-" + "2" * 32)
    job.mkdir()
    (job / "bundle-lock.json").write_text(json.dumps({"checkpoint": metadata}))
    return job


def test_private_checkpoint_transfer_preserves_bytes_and_recovers_commit(tmp_path):
    archive, metadata = archive_fixture(tmp_path)
    job = seeded(tmp_path, metadata)
    send(job, archive, metadata)
    commit = {"mode": "commit", "sha256": metadata["sha256"]}
    assert receive(job, commit)["installed"] is True
    assert receive(job, commit)["installed"] is True
    verify_received(job, metadata)
    assert not (job / ".checkpoint-upload").exists()
    assert all((job / "runs" / name).stat().st_mode & 0o777 == 0o600 for name in metadata["files"])
    # A process exit between rename and receipt must not need another admission.
    (job / ".checkpoint-receipt.json").unlink()
    assert receive(job, commit)["installed"] is True
    verify_received(job, metadata)
    with pytest.raises(LabError, match="already installed"):
        receive(job, {"mode": "chunk", "sha256": metadata["sha256"], "offset": 0})


@pytest.mark.parametrize("fault", ["order", "bytes", "pin", "started", "cancelled", "incomplete", "corrupt"])
def test_bad_checkpoint_transfer_cannot_publish_state(tmp_path, fault):
    archive, metadata = archive_fixture(tmp_path)
    job = seeded(tmp_path, metadata)
    message = {"mode": "chunk", "sha256": metadata["sha256"], "offset": 0,
               "data_base64": base64.b64encode(archive.read_bytes()[:CHUNK_BYTES]).decode()}
    if fault == "order":
        message["offset"] = CHUNK_BYTES
    elif fault == "bytes":
        receive(job, message)
        message["data_base64"] = base64.b64encode(b"x" * CHUNK_BYTES).decode()
    elif fault == "pin":
        message["sha256"] = "0" * 64
    elif fault in ("started", "cancelled"):
        (job / fault).touch()
    elif fault in ("incomplete", "corrupt"):
        if fault == "corrupt":
            send(job, archive, metadata)
            with (job / ".checkpoint-upload").open("r+b") as stream:
                stream.write(b"broken")
        else:
            receive(job, message)
        message = {"mode": "commit", "sha256": metadata["sha256"]}
    with pytest.raises(LabError):
        receive(job, message)
    assert not (job / "runs").exists()
    assert not (job / ".checkpoint-receipt.json").exists()


@pytest.mark.parametrize("fault", ["traversal", "case-alias", "boolean-size", "too-large", "file", "symlink"])
def test_checkpoint_inventory_and_restored_state_are_verified(tmp_path, fault):
    archive, metadata = archive_fixture(tmp_path)
    if fault in ("file", "symlink"):
        unpack(archive, tmp_path / "restored", metadata)
        private = tmp_path / "restored" / metadata["run_id"] / "private"
        if fault == "file":
            (private / "fixture.sqlite").write_bytes(b"changed")
        else:
            (private / "fixture.sqlite").unlink()
            (private / "fixture.sqlite").symlink_to("trial.private.json")
        with pytest.raises(LabError):
            verify_tree(tmp_path / "restored", metadata)
    else:
        first = next(iter(metadata["files"]))
        if fault == "traversal":
            metadata["files"]["../escaping"] = metadata["files"].pop(first)
        elif fault == "case-alias":
            metadata["files"][first.upper()] = metadata["files"][first]
        elif fault == "boolean-size":
            metadata["files"][first]["bytes"] = True
        else:
            metadata["bytes"] = 65 * 1024 * 1024
        with pytest.raises(LabError):
            validate_metadata(metadata)


def capsule_fixture(tmp_path, **state):
    archive, metadata = archive_fixture(tmp_path, **state)
    worker = tmp_path / SOURCE
    capsule = worker / "private" / metadata["run_id"]
    capsule.mkdir(parents=True)
    shutil.copyfile(archive, capsule / "checkpoint.zip")
    unpack(archive, capsule / "run", metadata)
    (capsule / "receipt.json").write_text(json.dumps(metadata))
    (worker / "worker.json").write_text(json.dumps({
        "trial_id": SOURCE, "cleanup": "confirmed",
        "continuation": {"checkpoints": {metadata["run_id"]: digest(metadata)}}}))
    return worker, capsule, metadata


def test_checkpoint_owner_is_exclusive_and_single_admission(tmp_path):
    worker, capsule, metadata = capsule_fixture(tmp_path)
    replica_worker = tmp_path / "restored-backup" / SOURCE
    shutil.copytree(worker, replica_worker)
    replica = replica_worker / "private" / metadata["run_id"]
    with CapsuleOwner(capsule, root=tmp_path) as owner:
        with pytest.raises(LabError, match="active continuation owner"):
            CapsuleOwner(capsule, root=tmp_path)
        with pytest.raises(LabError, match="active continuation owner"):
            CapsuleOwner(replica, root=tmp_path)
        owner.consume("worker-" + "3" * 32)
        with pytest.raises(LabError, match="consumed"):
            owner.consume("worker-" + "4" * 32)
    with pytest.raises(LabError, match="already admitted"):
        CapsuleOwner(capsule, root=tmp_path)
    assert not (replica / "claim.json").exists()
    with pytest.raises(LabError, match="already admitted"):
        CapsuleOwner(replica, root=tmp_path)


@pytest.mark.parametrize("state", [
    {"status": "completed"}, {"resume_blocked_reason": "ambiguous effect"},
    {"deadline": 1}, {"deadline": True},
])
def test_unusable_checkpoint_never_consumes_admission(tmp_path, state):
    _, capsule, _ = capsule_fixture(tmp_path, **state)
    with pytest.raises(LabError, match="cannot be continued|deadline"):
        CapsuleOwner(capsule, root=tmp_path)
    assert not (capsule / "claim.json").exists()
    assert not list((tmp_path / ".consumer-agent-testing-tool-worker-claims").glob("*.json"))


@pytest.mark.parametrize("name", ["receipt.json", "checkpoint.zip", "claim.lock"])
def test_checkpoint_special_file_is_rejected_without_waiting(tmp_path, name):
    _, capsule, _ = capsule_fixture(tmp_path)
    path = capsule / name
    path.unlink(missing_ok=True)
    os.mkfifo(path)
    with pytest.raises(LabError, match="control record|archive integrity|ownership lock"):
        CapsuleOwner(capsule, root=tmp_path)


@pytest.mark.parametrize("content", ['{"key": 1, "key": 2}', '["not-an-object"]', 'key: yaml'])
def test_checkpoint_records_are_strict_json(tmp_path, content):
    path = tmp_path / "record.json"
    path.write_text(content)
    with pytest.raises(LabError):
        read_record(path)


async def test_partial_private_collection_recovers_each_run_without_recollecting(tmp_path):
    job = tmp_path / "jobs" / SOURCE
    (job / "runs").mkdir(parents=True)
    for number in range(2):
        root = tmp_path / f"fixture-{number}"
        root.mkdir()
        _, metadata = archive_fixture(root)
        shutil.copytree(root / "runs" / metadata["run_id"], job / "runs" / metadata["run_id"])
    document = {"retain_continuation": True}
    (job / "invocation.worker.json").write_text(json.dumps(document))
    (job / "bundle-lock.json").write_text(json.dumps({
        "coordinator": {"format": "consumer-agent-testing-tool-worker-run-v1", "invocation_sha256": digest(document)}}))
    snapshot = tmp_path / "snapshot"
    index = prepare(job, snapshot)
    assert len(index) == 2
    first, second = sorted(index)
    calls = []

    class InterruptedController(BaseException):
        pass

    class Gateway:
        dropped = False

        async def call(self, operation, payload):
            assert operation == "checkpoint"
            calls.append((payload["mode"], payload.get("run_id"), payload.get("offset")))
            if payload["mode"] == "list":
                return {"trial_id": SOURCE, "complete": True, "checkpoints": index}
            if payload["run_id"] == second and not self.dropped:
                self.dropped = True
                raise InterruptedController()
            return export_chunk(snapshot, payload)

    path = tmp_path / "runs" / SOURCE
    path.mkdir(parents=True)
    config = WorkerConfig(provider="local-process-reference", gateway=["unused"])
    gateway = Gateway()
    controller = WorkerJournal(config, gateway, path, {
        "trial_id": SOURCE, "expires_at": time.time() + 60,
        "events": [], "status": "completed", "retain_continuation": True})
    with pytest.raises(InterruptedController):
        await controller.collect_continuation()
    saved = load(path / "worker.json")
    assert set(saved["continuation"]["checkpoints"]) == {first}
    assert not (path / "private" / second).exists()
    before = sum(run == first for _, run, _ in calls)
    restored = WorkerJournal(config, gateway, path, saved)
    await restored.collect_continuation()
    assert restored.record["continuation"]["collection"] == "complete"
    assert set(restored.record["continuation"]["checkpoints"]) == {first, second}
    assert sum(run == first for _, run, _ in calls) == before
    count = len(calls)
    await restored.collect_continuation()
    assert len(calls) == count
    assert "archive-only@example.test" not in (path / "worker.json").read_text()
    # A batch export does not imply restoring every run into one continuation.
    destination = seeded(tmp_path, index[second])
    send(destination, path / "private" / second / "checkpoint.zip", index[second])
    receive(destination, {"mode": "commit", "sha256": index[second]["sha256"]})
    assert [p.name for p in (destination / "runs").iterdir()] == [second]


async def test_worker_socket_authority_is_exact_and_scoped(inventory, tmp_path, monkeypatch):
    from pathlib import Path

    from consumer_agent_testing_tool.api_control import control_path, worker_control_sockets

    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.chdir(job)
    with pytest.raises(LabError, match="escapes the workspace"):
        control_path(inventory.target)
    token = worker_control_sockets.set((inventory.target.api_control.socket + ".other",))
    try:
        with pytest.raises(LabError, match="escapes the workspace"):
            control_path(inventory.target)
    finally:
        worker_control_sockets.reset(token)
    token = worker_control_sockets.set((inventory.target.api_control.socket,))
    try:
        assert control_path(inventory.target) == Path(inventory.target.api_control.socket)
        os.chmod(inventory.target.api_control.socket, 0o666)
        with pytest.raises(LabError, match="owner-only"):
            control_path(inventory.target)
    finally:
        worker_control_sockets.reset(token)
        os.chmod(inventory.target.api_control.socket, 0o600)
    assert worker_control_sockets.get() == ()


@pytest.mark.parametrize("lost_reply", [None, "chunk", "commit", "start"])
async def test_native_worker_resume_preserves_private_history_and_one_effect(tmp_path, monkeypatch, lost_reply):
    from aiohttp import web
    from test_scenario_runtime import scenario

    from consumer_agent_testing_tool.workers import CommandGateway

    spec = scenario(events=[{"on": "tool_completed", "occurrence": 1,
                             "action": "interrupt", "args": {}}],
                    assertions=[{"scorer": "backend.draft_count", "expected": 1},
                                {"scorer": "backend.confirmed_count", "expected": 0}])
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(spec))
    requests, phases = [], []
    secret = "synthetic-checkpoint-model-secret"
    contact = "retained-shopper@example.test"
    monkeypatch.setenv("CHECKPOINT_MODEL_KEY", secret)

    def tool(name, args, identifier):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": identifier, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]}

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer " + secret
        body = await request.json()
        requests.append(body)
        assert body["provider"]["only"] == ["together"] and body["provider"]["zdr"] is True
        tools = {row["function"]["name"] for row in body.get("tools", [])}
        if body["messages"][-1].get("tool_call_id") == "probe":
            message = {"role": "assistant", "content": "probe-accepted-7"}
        elif "fixture_probe" in tools:
            message = tool("fixture_probe", {"payload": {"count": 7, "label": "probe"}}, "probe")
        elif body["messages"][-1]["role"] == "tool":
            assert body["messages"][-1]["tool_call_id"] == "draft"
            assert contact in json.dumps(body["messages"])
            message = {"role": "assistant", "content": "The service reported a nonbinding draft."}
        else:
            message = tool("reservations_create", {
                "slot_id": "afternoon", "contact": contact, "idempotency_key": "draft"}, "draft")
        return web.json_response({"id": "synthetic-worker-resume", "object": "chat.completion",
            "created": 1, "model": "meta/muse-glimmer-30b", "provider": "Together",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    config["model_profiles"] = {"selected": {
        "provider": "openrouter", "api": "chat-completions", "model_id": "meta/muse-glimmer-30b",
        "base_url": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        "auth": {"mode": "api_key", "env": "CHECKPOINT_MODEL_KEY"},
        "limits": {"timeout_seconds": 5, "max_retries": 0}}}
    config["defaults"] = {"model_profile": "selected", "interface_profile": spec["interface_profile"]}
    path.write_text(json.dumps(config))
    command = ["run", "scenario.json", "--config", str(path), "--surface", "api-tools",
               "--trials", "1", "--resumable"]
    original = CommandGateway.call
    dropped = False
    continuing = False

    async def gateway(self, operation, payload):
        nonlocal dropped
        phases.append((payload["trial_id"], operation, payload.get("mode")))
        result = await original(self, operation, payload)
        if continuing and lost_reply and not dropped and (
                operation == "checkpoint" and payload.get("mode") == lost_reply
                or lost_reply == "start" and operation == "run" and payload.get("mode") == "start"):
            dropped = True
            raise TimeoutError("Synthetic lost acknowledgement")
        return result

    monkeypatch.setattr(CommandGateway, "call", gateway)
    try:
        assert await execute(parser().parse_args(command)) == 3
        first_path = next((tmp_path / "runs").glob("worker-*/worker.json"))
        first = load(first_path)
        assert first["cleanup"] == "confirmed" and first["continuation"]["collection"] == "complete"
        assert len(first["continuation"]["checkpoints"]) == 1
        run_id = next(iter(first["continuation"]["checkpoints"]))
        capsule = first_path.parent / "private" / run_id
        original_state = capsule / "run" / run_id / "private/trial.private.json"
        original_state_sha = file_digest(original_state)
        assert contact.encode() in (capsule / "checkpoint.zip").read_bytes()
        assert contact not in first_path.read_text()
        assert not (tmp_path / "jobs" / first_path.parent.name).exists()
        assert len(requests) == 3
        # Configuration changes are rejected before creating another worker.
        config["execution"]["sandbox_image"] = "changed-sandbox:1"
        path.write_text(json.dumps(config))
        with pytest.raises(LabError, match="Execution configuration changed"):
            await execute(parser().parse_args([*command, "--resume", str(capsule)]))
        assert len(requests) == 3 and not (capsule / "claim.json").exists()
        assert len(list((tmp_path / "runs").glob("worker-*/worker.json"))) == 1
        config["execution"].pop("sandbox_image")
        path.write_text(json.dumps(config))
        continuing = True
        assert await execute(parser().parse_args([*command, "--resume", str(capsule)])) == 0
        assert len(requests) == 6 and dropped == bool(lost_reply)
        assert file_digest(original_state) == original_state_sha
        second_path = next(p for p in (tmp_path / "runs").glob("worker-*/worker.json") if p != first_path)
        second = load(second_path)
        assert second["collection"] == second["continuation"]["collection"] == "complete"
        assert second["cleanup"] == "confirmed"
        assert not (tmp_path / "jobs" / second_path.parent.name).exists()
        assert load(capsule / "claim.json")["trial_id"] == second_path.parent.name
        resumed = load(second_path.parent / "evidence/runs" / run_id / "manifest.json")
        assert resumed["backend_metrics"]["backend.draft_count"] == 1
        assert resumed["backend_metrics"]["backend.confirmed_count"] == 0
        assert sum(row["operation"] == "reservations.create" for row in resumed["target_attempts"]) == 1
        assert resumed["continuation_attempt"] == 2
        assert resumed["worker_execution_history"][0]["trial_id"] == first_path.parent.name
        assert resumed["worker_execution"]["trial_id"] == second_path.parent.name
        assert sum(op == "run" and mode == "start" for trial, op, mode in phases
                   if trial == second_path.parent.name) == 1
        for worker_path in (first_path, second_path):
            for file in (worker_path.parent / "evidence").rglob("*"):
                if file.is_file():
                    assert contact.encode() not in file.read_bytes()
            assert "private" not in json.dumps(load(worker_path)["artifacts"]["run_archive"]["files"])
        state = load(second_path.parent / "private" / run_id / "run" / run_id / "private/trial.private.json")
        assert state["attempt"] == 2 and state["turns"] == 2 and state["calls"] == 1
        with pytest.raises(LabError, match="already admitted"):
            await execute(parser().parse_args([*command, "--resume", str(capsule)]))
        assert len(requests) == 6
    finally:
        await server.cleanup()


@pytest.mark.parametrize("generated", [False, True])
async def test_worker_resume_preserves_developer_api_lease_and_bundled_paths(
        retained, tmp_path, monkeypatch, generated):
    from aiohttp import web
    from test_generated_resume import configure
    from test_mapped_scenario import spec_for
    from test_scenario_resume import interrupt

    if generated:
        await configure(retained, tmp_path)
    spec = spec_for(events=[interrupt("tool_completed", 2)],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    spec["limits"]["wall_seconds"] = 180
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    (tmp_path / "scenario.json").write_text(json.dumps(spec))
    config["targets"] = {"inventory": retained.target.model_dump(mode="json")}
    config["execution"]["worker"]["api_control_sockets"] = [retained.target.api_control.socket]
    key = "synthetic-mapped-worker-model-key"
    monkeypatch.setenv("CHECKPOINT_MODEL_KEY", key)
    requests = []

    def tool(name, arguments, identifier):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": identifier, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]}

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer " + key
        body = await request.json()
        requests.append(body)
        names = {row["function"]["name"] for row in body.get("tools", [])}
        previous = body["messages"][-1].get("tool_call_id")
        if previous == "probe":
            message = {"role": "assistant", "content": "probe-accepted-7"}
        elif "fixture_probe" in names:
            message = tool("fixture_probe", {"payload": {"count": 7, "label": "probe"}}, "probe")
        elif previous == "price":
            result = json.loads(body["messages"][-1]["content"])["result"]
            if generated:
                result = result["structuredContent"]
            message = tool("buy_item", {
                "amount_minor": result["amount_minor"], "revision": result["revision"],
                "key": "order-one"}, "buy")
        elif previous == "buy":
            assert "completed" in body["messages"][-1]["content"]
            message = {"role": "assistant", "content": "The order completed."}
        else:
            message = tool("get_price", {}, "price")
        return web.json_response({"id": "synthetic-api-resume", "object": "chat.completion",
            "created": 1, "model": "meta/muse-glimmer-30b", "provider": "Together",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    config["model_profiles"] = {"selected": {
        "provider": "openrouter", "api": "chat-completions", "model_id": "meta/muse-glimmer-30b",
        "base_url": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        "auth": {"mode": "api_key", "env": "CHECKPOINT_MODEL_KEY"},
        "limits": {"timeout_seconds": 5, "max_retries": 0}}}
    config["defaults"] = {"model_profile": "selected", "interface_profile": spec["interface_profile"]}
    path.write_text(json.dumps(config))
    command = ["run", "scenario.json", "--config", str(path), "--surface", "api-tools",
               "--trials", "1", "--resumable"]
    try:
        config["execution"]["worker"]["api_control_sockets"] = []
        path.write_text(json.dumps(config))
        with pytest.raises(LabError, match="explicit api_control_sockets"):
            await execute(parser().parse_args(command))
        assert not (tmp_path / "jobs").exists() and not requests
        config["execution"]["worker"]["api_control_sockets"] = [retained.target.api_control.socket]
        path.write_text(json.dumps(config))
        assert await execute(parser().parse_args(command)) == 3
        first_path = next((tmp_path / "runs").glob("worker-*/worker.json"))
        first = load(first_path)
        assert first["cleanup"] == "confirmed" and first["continuation"]["collection"] == "complete"
        run_id = next(iter(first["continuation"]["checkpoints"]))
        capsule = first_path.parent / "private" / run_id
        state = load(capsule / "run" / run_id / "private/trial.private.json")
        assert not (tmp_path / "jobs" / first_path.parent.name).exists()
        assert retained.lease and retained.controls.count("acquire_retained") == 1
        assert await execute(parser().parse_args([*command, "--resume", str(capsule)])) == 0
        second_path = next(p for p in (tmp_path / "runs").glob("worker-*/worker.json") if p != first_path)
        second = load(second_path)
        assert second["cleanup"] == "confirmed" and second["continuation"]["collection"] == "complete"
        assert not (tmp_path / "jobs" / second_path.parent.name).exists()
        after = load(second_path.parent / "private" / run_id / "run" / run_id / "private/trial.private.json")
        assert after["deadline"] == state["deadline"] and after["attempt"] == 2
        assert after["turns"] == 3 and after["calls"] == 2
        assert [row["path"] for row in retained.requests] == ["/price", "/buy"]
        assert retained.controls.count("acquire_retained") == retained.controls.count("resume") == 1
        assert retained.lease is None
        manifest = load(second_path.parent / "evidence/runs" / run_id / "manifest.json")
        assert manifest["run_lock"]["execution"]["api_control_socket_hashes"] == [
            digest(retained.target.api_control.socket)]
        assert retained.target.api_control.socket not in json.dumps(manifest)
        assert manifest["backend_metrics"]["backend.confirmed_count"] == 1
        assert len(requests) == 7
        assert "api_control" not in json.dumps(requests)
    finally:
        await server.cleanup()
