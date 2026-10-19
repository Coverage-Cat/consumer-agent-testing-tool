"""Owned metadata/HTTP fixtures exercise gates; none qualify a live model."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from gguf import GGUFWriter

from consumer_agent_testing_tool import local_runtime
from consumer_agent_testing_tool.config import ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.serialization import file_digest


@pytest.fixture
async def runtime_fixture(tmp_path, monkeypatch):
    template = "{{ messages }}"
    weights = tmp_path / "metadata-only.gguf"
    runtime = tmp_path / "server-identity-only"
    runtime.write_text("This is not an executable model server.")
    props = {"build_info": "owned-metadata-fixture", "total_slots": 1,
        "default_generation_settings": {"n_ctx": 4096}, "model_path": str(weights),
        "chat_template": template}

    async def handler(request):
        if request.path == "/props":
            return web.json_response(props)
        payload = await request.json()
        if request.path == "/tokenize":
            return web.json_response({"tokens": [1] * 2048})
        assert len(payload["tokens"]) == 2048
        return web.json_response({"content": "owned representative fixture"})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    profile = ModelProfile(provider="local-glimmer", model_id="metadata-fixture",
        api="chat-completions", base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
        runtime_pid=os.getpid(), runtime_build=props["build_info"], memory_budget_mb=1,
        context_tokens=4096, generation={"max_output_tokens": 1024},
        limits={"timeout_seconds": 1, "max_retries": 0})
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    process = SimpleNamespace(cmdline=lambda: [str(runtime), "--jinja"], exe=lambda: str(runtime),
        cwd=lambda: str(tmp_path), create_time=lambda: 1)
    monkeypatch.setattr(local_runtime.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(local_runtime, "verify_listener", lambda *args: None)
    monkeypatch.setattr("consumer_agent_testing_tool.offline.kernel_denies_default_egress", lambda *args: True)

    def write_weights(architecture="muse-glimmer", include_template=True, include_file_type=True):
        writer = GGUFWriter(weights, architecture)
        if include_template:
            writer.add_chat_template(template)
        if include_file_type:
            writer.add_file_type(15)
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_tensors_to_file()
        writer.close()
        profile.artifacts = {name: {"path": str(path), "sha256": file_digest(path)}
                             for name, path in (("weights", weights), ("runtime", runtime))}

    write_weights()
    try:
        yield SimpleNamespace(profile=profile, evidence=evidence, props=props,
            process=process, write_weights=write_weights)
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("fault,match", [
    ("architecture", "not the muse-glimmer architecture"),
    ("template", "native chat template is missing"),
    ("quantization", "quantization file_type metadata is missing"),
    ("artifact", "artifact hash mismatch"),
    ("executable", "does not execute the pinned"),
    ("jinja", "requires the serving runtime"),
    ("build", "build_info differs"),
    ("context", "Actual per-slot context"),
    ("slots", "slot count differs"),
    ("server_template", "template differs"),
])
async def test_local_metadata_gate_rejects_mismatches(runtime_fixture, fault, match):
    fixture = runtime_fixture
    if fault == "architecture":
        fixture.write_weights(architecture="llama")
    elif fault == "template":
        fixture.write_weights(include_template=False)
    elif fault == "quantization":
        fixture.write_weights(include_file_type=False)
    elif fault == "artifact":
        Path(fixture.profile.artifacts["weights"]["path"]).write_bytes(b"changed artifact")
    elif fault == "executable":
        fixture.process.exe = lambda: "/wrong/server"
    elif fault == "jinja":
        fixture.process.cmdline = lambda: ["server"]
    elif fault == "build":
        fixture.props["build_info"] = "wrong-build"
    elif fault == "context":
        fixture.props["default_generation_settings"]["n_ctx"] = 2048
    elif fault == "slots":
        fixture.props["total_slots"] = 2
    elif fault == "server_template":
        fixture.props["chat_template"] = "{{ overridden }}"
    with pytest.raises(LabError, match=match):
        await local_runtime.inspect_runtime(fixture.profile, fixture.evidence, None)
    assert "local_runtime" not in fixture.evidence.manifest["run_lock"]


@pytest.mark.parametrize("fault,match", [
    ("memory", "exceeded the configured memory budget"),
    ("pid", "PID changed"),
    ("truncation", "did not account for the representative input"),
    ("usage", "lacks memory or input-token measurements"),
    ("timeout", "request timed out"),
])
async def test_resource_gate_fails_closed_and_drains_generation(runtime_fixture, monkeypatch, fault, match):
    fixture = runtime_fixture
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    initial_tasks = asyncio.all_tasks()
    monkeypatch.setattr(local_runtime, "memory_bytes",
                        lambda pid: (2 * 1024 * 1024 if fault == "memory" else 512, "fixture"))
    if fault == "pid":
        starts = iter([1, 2])
        fixture.process.create_time = lambda: next(starts)

    async def generate(*args):
        try:
            if fault in ("memory", "pid", "timeout"):
                await asyncio.Event().wait()
            await asyncio.sleep(.04)
            return SimpleNamespace(usage=None if fault == "usage" else SimpleNamespace(
                input_tokens=20 if fault == "truncation" else 2048))
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            finished.set()

    with pytest.raises((LabError, Unsupported), match=match):
        await local_runtime.stress_context(fixture.profile, SimpleNamespace(generate=generate),
                                          fixture.evidence, None)
    assert finished.is_set()
    if fault in ("memory", "pid", "timeout"):
        assert cancelled.is_set()
    # No sampler or inference coroutine is left running after a failed gate.
    assert not [task for task in asyncio.all_tasks() - initial_tasks
                if "stress_context" in str(task.get_coro())]


async def test_listener_identity_and_os_memory_use_the_real_process():
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        local_runtime.verify_listener(os.getpid(), f"http://127.0.0.1:{port}/v1")
        size, basis = local_runtime.memory_bytes(os.getpid())
        assert size > 0 and basis
    finally:
        server.close()
        await server.wait_closed()
    with pytest.raises(LabError, match="does not own"):
        local_runtime.verify_listener(os.getpid(), f"http://127.0.0.1:{port}/v1")
