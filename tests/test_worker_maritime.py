"""Local HTTP contracts plus actual subprocesses; never a cloud qualification."""
import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_worker_evidence import setup_job

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.worker_maritime import MaritimeGateway, state_lock
from consumer_agent_testing_tool.workers import (
    WorkerConfig,
    WorkerLifecycle,
    reap,
    selected_gateway,
)


def configuration(**updates):
    values = {"provider": "maritime", "credential_env": ["MARITIME_API_KEY"],
              "ttl_seconds": 20, "call_timeout_seconds": 10, "poll_interval_seconds": .05,
              "maritime": {"namespace": "synthetic", "template_id": "pinned-test-harness",
                           "api_url": "https://maritime.example.test", "python": sys.executable}}
    values.update(updates)
    return WorkerConfig.model_validate(values)


class Provider:
    """Implements only the documented routes; exec really runs the bridge."""
    def __init__(self, root, fault=None):
        self.root, self.fault = root, fault
        self.volume = root / "provider-volume"
        self.volume.mkdir()
        self.agent, self.external = None, None
        self.calls, self.commands, self.messages = [], [], []
        self.executions = {}
        self.deleted = False
        self.used_fault = False
        self.before_delete = None

    async def __call__(self, request):
        assert request.url.host == "maritime.example.test"
        assert request.headers["authorization"] == "Bearer synthetic-provisioning-secret"
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        if path.startswith("/api/v1/end-users/"):
            external = path.split("/")[4]
            if path.endswith("/agents"):
                if method == "GET":
                    return httpx.Response(200, json=[] if self.agent is None else [{"id": self.agent}])
                assert method == "POST"
                assert json.loads(request.content) == {
                    "templateId": "pinned-test-harness", "name": external}
                if self.fault != "lost-create-without-resource":
                    self.external, self.agent = external, "agent-synthetic"
                if self.fault in ("lost-create", "lost-create-without-resource") and not self.used_fault:
                    self.used_fault = True
                    raise httpx.ReadTimeout("private details must not be exported", request=request)
                return httpx.Response(201, json={"id": self.agent})
            if method == "DELETE":
                assert self.agent is None
                self.external = None
                return httpx.Response(204)
            assert method == "GET"
            return httpx.Response(404 if self.external is None else 200,
                                  json={} if self.external is None else {"externalId": self.external})
        assert path.startswith("/api/agents/agent-synthetic")
        base = "/api/agents/agent-synthetic"
        if path == base:
            if method == "GET":
                return httpx.Response(404 if self.agent is None else 200,
                                      json={} if self.agent is None else {"id": self.agent})
            assert method == "DELETE"
            if self.before_delete:
                self.before_delete()
            self.agent = None
            self.deleted = True
            if self.volume.exists():
                shutil.rmtree(self.volume)
            if self.fault == "lost-delete" and not self.used_fault:
                self.used_fault = True
                raise httpx.ReadTimeout("lost deletion acknowledgement", request=request)
            return httpx.Response(204)
        if self.agent is None:
            return httpx.Response(404)
        if path == base + "/files/list":
            return httpx.Response(200, json={"root": str(self.volume), "entries": []})
        if path == base + "/files/upload":
            assert method == "POST"
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode() + request.content)
            fields = {part.get_param("name", header="content-disposition"): part
                      for part in message.iter_parts()}
            assert set(fields) == {"dest_dir", "file"}  # No attachment/chat delivery.
            directory = Path(fields["dest_dir"].get_content().strip())
            assert directory.is_relative_to(self.volume)
            assert directory.stat().st_mode & 0o077 == 0
            path = directory / fields["file"].get_filename()
            path.write_bytes(fields["file"].get_payload(decode=True))
            return httpx.Response(200, json={"ok": True, "path": str(path)})
        if path == base + "/files/download":
            return httpx.Response(200, content=Path(request.url.params["path"]).read_bytes())
        if path == base + "/files/delete":
            Path(request.url.params["path"]).unlink(missing_ok=True)
            return httpx.Response(204)
        if path.startswith(base + "/exec/"):
            assert method == "GET"
            return httpx.Response(200, json=self.executions[path.rsplit("/", 1)[1]])
        assert path == base + "/exec" and method == "POST"
        body = json.loads(request.content)
        assert body["detached"] is True and 1 <= body["timeout"] <= 300
        command = body["command"]
        self.commands.append(command)
        assert "synthetic-provisioning-secret" not in json.dumps(command)
        assert "synthetic-inference-secret" not in json.dumps(command)
        message = None
        if len(command) == 7:
            message = json.loads(Path(command[4]).read_text())
            self.messages.append(message)
        child = await asyncio.create_subprocess_exec(*command, env={"PATH": os.defpath},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
        assert child.returncode == 0, stderr.decode()
        assert stdout == b""
        execution = uuid.uuid4().hex
        self.executions[execution] = {"executionId": execution, "status": "completed",
            "exitCode": 0, "stdout": "[output truncated]\nnot a protocol result", "stderr": ""}
        if self.fault == "lost-start" and message and message["operation"] == "run" \
                and message["mode"] == "start" and not self.used_fault:
            self.used_fault = True
            raise httpx.ReadTimeout("lost start acknowledgement", request=request)
        return httpx.Response(202, json={"executionId": execution, "status": "running"})


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("MARITIME_API_KEY", "synthetic-provisioning-secret")
    monkeypatch.setenv("UNSELECTED_CLOUD_SECRET", "must-not-enter-worker")


@pytest.mark.parametrize("change", [
    {"maritime": None}, {"gateway": ["unexpected"]}, {"run_mode": "blocking"},
    {"credential_env": []}, {"provider": "ssh"},
    {"maritime": {"namespace": "x", "template_id": "x", "api_url": "http://example.test"}},
    {"maritime": {"namespace": "x", "template_id": "x", "api_url": "https://secret@example.test"}},
    {"maritime": {"namespace": "x", "template_id": "x", "api_url": "https://example.test/api"}},
    {"maritime": {"namespace": "../escape", "template_id": "x"}},
    {"maritime": {"namespace": "x", "template_id": "x", "python": "../python"}},
])
def test_explicit_provider_configuration(change):
    with pytest.raises(ValidationError):
        configuration(**change)


def test_gateway_selection_is_shared_with_recovery(tmp_path):
    assert isinstance(selected_gateway(tmp_path, configuration()), MaritimeGateway)


def test_local_container_receipt_cannot_authorize_a_cloud_connection(tmp_path):
    from consumer_agent_testing_tool.worker_container import host_identity
    with pytest.raises(LabError, match="does not accept"):
        host_identity(tmp_path, configuration())


@pytest.mark.parametrize("fault", [None, "lost-start", "lost-delete"])
async def test_actual_reference_job_through_provider_contract(tmp_path, key, fault):
    package, _ = setup_job(tmp_path,
        "import os,time\nfrom pathlib import Path\n"
        "assert not any('SECRET' in key or key == 'MARITIME_API_KEY' for key in os.environ)\n"
        "with Path('executions').open('a') as out: out.write('once\\n')\n"
        "print('synthetic cloud bridge proof',flush=True)\ntime.sleep(.3)\n")
    config = configuration(expected_harness_sha256=package["sha256"])
    provider = Provider(tmp_path, fault)
    gateway = MaritimeGateway(tmp_path, config, transport=httpx.MockTransport(provider))
    controller = WorkerLifecycle(tmp_path, config, gateway)
    def before_delete():
        saved = json.loads((controller.path / "worker.json").read_text())
        assert saved["collection"] == "complete"
        assert "synthetic cloud bridge proof" in saved["artifacts"]["files"]["stdout.log"]
    provider.before_delete = before_delete
    result = await controller.run(package["bundle"])
    assert result["execution"]["exit_code"] == 0 and result["collection"] == "complete"
    assert result["cleanup"] == ("failed" if fault == "lost-delete" else "confirmed")
    if fault == "lost-delete":
        recovered = await reap(tmp_path, config, gateway, now=time.time() + 30)
        assert recovered == [controller.path.name]
    assert provider.deleted and provider.agent is None and provider.external is None
    assert not provider.volume.exists()
    assert sum(operation == "POST" and path.endswith("/agents") for operation, path in provider.calls) == 1
    assert sum(row["operation"] == "run" and row["mode"] == "start" for row in provider.messages) == 1
    state = json.loads(next(gateway.directory.glob("*.json")).read_text())
    assert state["phase"] == "destroyed"
    calls = len(provider.calls)
    with pytest.raises(LabError, match="Retired"):
        await gateway.call("provision", {"trial_id": controller.path.name, "expires_at": state["expires_at"]})
    assert len(provider.calls) == calls
    assert "synthetic-provisioning-secret" not in (controller.path / "worker.json").read_text()


@pytest.mark.parametrize("fault", ["lost-create", "lost-create-without-resource"])
async def test_unknown_create_is_reconciled_without_another_creation(tmp_path, key, fault):
    package, _ = setup_job(tmp_path)
    config = configuration(expected_harness_sha256=package["sha256"])
    provider = Provider(tmp_path, fault)
    gateway = MaritimeGateway(tmp_path, config, transport=httpx.MockTransport(provider))
    controller = WorkerLifecycle(tmp_path, config, gateway)
    result = await controller.run(package["bundle"])
    assert result["status"] == "failed"
    assert result["cleanup"] == ("confirmed" if fault == "lost-create" else "failed")
    assert sum(method == "POST" and path.endswith("/agents") for method, path in provider.calls) == 1
    assert not any(message["operation"] == "run" for message in provider.messages)
    if fault == "lost-create-without-resource":
        assert "uncertain" in result["cleanup_error"]
        # A resource arriving after the client timeout must remain discoverable.
        provider.agent, provider.external = "agent-synthetic", "late"
        with pytest.raises(LabError, match="recovery incomplete"):
            await reap(tmp_path, config, gateway, now=time.time() + 30)
        assert provider.deleted
        saved = json.loads((controller.path / "worker.json").read_text())
        assert saved["cleanup"] == "confirmed" and saved["collection"] != "complete"


async def test_existing_resource_is_not_adopted_or_deleted(tmp_path, key):
    package, _ = setup_job(tmp_path)
    config = configuration(expected_harness_sha256=package["sha256"])
    provider = Provider(tmp_path)
    provider.agent, provider.external = "agent-synthetic", "someone-else"
    gateway = MaritimeGateway(tmp_path, config, transport=httpx.MockTransport(provider))
    result = await WorkerLifecycle(tmp_path, config, gateway).run(package["bundle"])
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert not provider.deleted
    assert provider.agent == "agent-synthetic"
    assert not any(method == "POST" or method == "DELETE" for method, _ in provider.calls)


@pytest.mark.parametrize("mode", ["deadline", "busy", "credential_name", "credential_value", "path"])
async def test_local_admission_rejects_before_network(tmp_path, key, mode):
    def forbidden(request):
        raise AssertionError("Admission failure contacted the provider")
    gateway = MaritimeGateway(tmp_path, configuration(), transport=httpx.MockTransport(forbidden))
    payload = {"trial_id": "worker-" + "a" * 32, "expires_at": time.time() + 10}
    operation = "provision"
    if mode == "deadline":
        payload["expires_at"] = time.time() + 100
    if mode.startswith("credential"):
        operation = "run"
        payload.update(mode="start", coordinator_environment={
            "MARITIME_API_KEY" if mode == "credential_name" else "INFERENCE_KEY":
                "synthetic-provisioning-secret"})
    if mode == "path":
        payload["trial_id"] = "../escape"
    if mode == "busy":
        with state_lock(gateway.directory, payload["trial_id"]), pytest.raises(LabError, match="busy"):
            await gateway.call(operation, payload)
    else:
        with pytest.raises(LabError):
            await gateway.call(operation, payload)


@pytest.mark.parametrize("fault", ["redirect", "oversize", "json", "execution-id", "legacy", "server"])
async def test_bad_responses_fail_without_resubmitting_exec(tmp_path, key, fault):
    calls = []
    def handler(request):
        calls.append(request)
        if fault == "redirect":
            return httpx.Response(307, headers={"location": "https://unselected.example.test/"})
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1))
        if fault == "json":
            return httpx.Response(200, content=b"not-json")
        if fault == "legacy":
            return httpx.Response(200, json={"exitCode": 0, "stdout": "{}"})
        if fault == "server":
            return httpx.Response(503, json={"detail": {"code": "guest_command_unavailable"}})
        return httpx.Response(202 if request.method == "POST" else 200,
            json={"executionId": "first" if request.method == "POST" else "different",
                  "status": "running"})
    gateway = MaritimeGateway(tmp_path, configuration(), transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(base_url=gateway.settings.api_url,
                                transport=gateway.transport) as client:
        with pytest.raises(LabError):
            await gateway.execution(client, "synthetic", ["command"])
    assert sum(request.method == "POST" for request in calls) == 1


@pytest.mark.parametrize("status,code", [(409, "vm_not_ready"), (503, "guest_command_unavailable")])
async def test_retry_requires_explicit_nonadmission(tmp_path, key, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, json={"detail": {"code": code, "execution_started": False}})
        return httpx.Response(200, json={"executionId": "one", "status": "completed", "exitCode": 0})
    gateway = MaritimeGateway(tmp_path, configuration(), transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(base_url=gateway.settings.api_url,
                                transport=gateway.transport) as client:
        await gateway.execution(client, "synthetic", ["command"])
    assert len(calls) == 2


async def test_reaper_uses_same_provider_factory(tmp_path, key, monkeypatch):
    package, _ = setup_job(tmp_path)
    config = configuration(expected_harness_sha256=package["sha256"])
    provider = Provider(tmp_path)
    gateway = MaritimeGateway(tmp_path, config, transport=httpx.MockTransport(provider))
    monkeypatch.setattr("consumer_agent_testing_tool.workers.selected_gateway", lambda root, selected: gateway)
    controller = WorkerLifecycle(tmp_path, config)
    # Simulate coordinator loss after provisioning, before any seed or run.
    await controller.operation("provision")
    with pytest.raises(LabError, match="recovery incomplete"):
        await reap(tmp_path, config, now=time.time() + 30)
    assert provider.deleted
    assert not any(message["operation"] == "run" for message in provider.messages)


async def test_main_cli_checks_installed_harness_and_collects_chunked_export(tmp_path, key, monkeypatch):
    from test_worker_run import configuration as main_configuration

    from consumer_agent_testing_tool.cli import execute, parser
    from consumer_agent_testing_tool.worker_export import verify_saved
    from consumer_agent_testing_tool.worker_run import harness_identity

    monkeypatch.chdir(tmp_path)
    path, _ = main_configuration(tmp_path)
    config = configuration(ttl_seconds=60, call_timeout_seconds=30)
    (tmp_path / "gateway.json").write_text(config.model_dump_json())
    provider = Provider(tmp_path)
    gateway = MaritimeGateway(tmp_path, config, transport=httpx.MockTransport(provider))
    monkeypatch.setattr("consumer_agent_testing_tool.workers.selected_gateway", lambda root, selected: gateway)
    assert await execute(parser().parse_args(["inspect", "--config", str(path)])) == 0
    journal = next((tmp_path / "runs").glob("worker-*/worker.json"))
    result = json.loads(journal.read_text())
    assert result["status"] == "completed"
    assert result["cleanup"] == "confirmed" and result["collection"] == "complete"
    assert result["cloud"]["provider"] == "maritime"
    assert result["cloud"]["resource_ttl_verified"] is False
    verify_saved(journal.parent / "evidence", result["artifacts"]["run_archive"])
    manifest = json.loads(next((journal.parent / "evidence").glob("runs/*/manifest.json")).read_text())
    assert manifest["worker_execution"]["harness"] == harness_identity()
    assert manifest["run_lock"]["execution"]["provider"] == "maritime"
    assert manifest["locations"]["coordinator"] == "worker"
    assert any(message["operation"] == "export" and "offset" in message for message in provider.messages)
    assert not any(message.get("coordinator_environment") for message in provider.messages)
    assert provider.deleted


async def test_native_inspect_inference_credentials_use_only_private_start_input(tmp_path, key, monkeypatch):
    from aiohttp import web
    from test_scenario_runtime import scenario
    from test_worker_run import configuration as main_configuration

    from consumer_agent_testing_tool.cli import execute, parser

    spec = scenario(assertions=[{"scorer": "backend.draft_count", "expected": 1},
                                {"scorer": "backend.confirmed_count", "expected": 0}])
    monkeypatch.chdir(tmp_path)
    path, config = main_configuration(tmp_path)
    worker = configuration(ttl_seconds=120, call_timeout_seconds=30)
    (tmp_path / "gateway.json").write_text(worker.model_dump_json())
    provider = Provider(tmp_path)
    gateway = MaritimeGateway(tmp_path, worker, transport=httpx.MockTransport(provider))
    monkeypatch.setattr("consumer_agent_testing_tool.workers.selected_gateway", lambda root, selected: gateway)
    secret = "synthetic-inference-secret"
    monkeypatch.setenv("TEST_MODEL_KEY", secret)
    requests = []
    async def handler(request):
        assert request.headers["authorization"] == "Bearer " + secret
        body = await request.json()
        requests.append(body)
        assert body["provider"]["only"] == ["together"] and body["provider"]["zdr"] is True
        if len(requests) in (1, 3):
            name, args, identifier = (
                ("fixture_probe", {"payload": {"count": 7, "label": "probe"}}, "probe")
                if len(requests) == 1 else
                ("reservations_create", {"slot_id": "afternoon", "contact": "Synthetic shopper",
                                         "idempotency_key": "draft"}, "draft"))
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": identifier, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            assert len(requests) in (2, 4)
            assert body["messages"][-1]["tool_call_id"] == ("probe" if len(requests) == 2 else "draft")
            message = {"role": "assistant",
                       "content": "probe-accepted-7" if len(requests) == 2 else "Draft requested."}
        return web.json_response({"id": "synthetic-cloud-model", "object": "chat.completion",
            "created": 1, "model": "meta/muse-glimmer-30b", "provider": "Together",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
    application = web.Application()
    application.router.add_post("/v1/chat/completions", handler)
    server = web.AppRunner(application)
    await server.setup()
    try:
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        config["model_profiles"] = {"selected": {
            "provider": "openrouter", "api": "chat-completions", "model_id": "meta/muse-glimmer-30b",
            "base_url": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
            "auth": {"mode": "api_key", "env": "TEST_MODEL_KEY"},
            "limits": {"timeout_seconds": 5, "max_retries": 0}}}
        config["defaults"] = {"model_profile": "selected", "interface_profile": spec["interface_profile"]}
        path.write_text(json.dumps(config))
        (tmp_path / "scenario.json").write_text(json.dumps(spec))
        assert await execute(parser().parse_args([
            "run", "scenario.json", "--config", str(path), "--surface", "api-tools", "--trials", "1"])) == 0
    finally:
        await server.cleanup()
    assert len(requests) == 4 and secret not in json.dumps(requests)
    credential_messages = [row for row in provider.messages if row.get("coordinator_environment")]
    assert len(credential_messages) == 1
    assert credential_messages[0]["operation"] == "run" and credential_messages[0]["mode"] == "start"
    assert credential_messages[0]["coordinator_environment"] == {"TEST_MODEL_KEY": secret}
    journal = next((tmp_path / "runs").glob("worker-*/worker.json"))
    result = json.loads(journal.read_text())
    assert result["status"] == "completed" and result["collection"] == "complete"
    assert result["cleanup"] == "confirmed" and provider.deleted
    manifests = [json.loads(path.read_text())
                 for path in (journal.parent / "evidence").glob("runs/*/manifest.json")]
    trial = next(row for row in manifests if row.get("scenario_id") == spec["id"])
    assert trial["backend_metrics"]["backend.draft_count"] == 1
    assert trial["backend_metrics"]["backend.confirmed_count"] == 0
    for artifact in journal.parent.rglob("*.json"):
        assert secret not in artifact.read_text()
        assert "synthetic-provisioning-secret" not in artifact.read_text()
