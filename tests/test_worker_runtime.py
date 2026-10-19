"""Pinned deployment exercises bytes, process execution and real browser assets."""
import base64
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from test_worker_run import configuration

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.worker_assets import (
    CHUNK,
    LOCK,
    PROTOCOL,
    identity,
    inspect_archive,
    pack,
    receive,
    sha,
    verify_installed,
)

ROOT = Path(__file__).resolve().parents[1]


def small_pack(root, **kwargs):
    source = root / "source"
    source.mkdir()
    (source / "program").write_text("#!/bin/sh\nprintf runtime-ok")
    (source / "program").chmod(0o755)
    (source / "alias").symlink_to("program")
    if kwargs.get("large"):
        (source / "data").write_bytes(os.urandom(2 * CHUNK + 7))
    manifest = root / "assets.json"
    manifest.write_text(json.dumps({"platform": identity(), "paths": {"tooling": "source"}}))
    result = pack(root, manifest, root / "assets.zip")
    repeated = pack(root, manifest, root / "repeated.zip")
    assert repeated["sha256"] == result["sha256"]
    return Path(result.pop("archive")), result


def seeded(root, metadata):
    job = root / "worker-11111111111111111111111111111111"
    job.mkdir()
    (job / "bundle-lock.json").write_text(json.dumps({"coordinator": {"test": True}, "runtime": metadata}))
    return job


def chunks(job, archive, metadata):
    data = archive.read_bytes()
    replies = []
    for offset in range(0, len(data), CHUNK):
        message = {"mode": "chunk", "sha256": metadata["sha256"], "offset": offset,
                   "data_base64": base64.b64encode(data[offset:offset + CHUNK]).decode()}
        replies.append(receive(job, message))
        assert receive(job, message) == replies[-1]  # lost acknowledgement is idempotent
    return replies


def test_runtime_pack_preserves_internal_links_mode_and_commits(tmp_path):
    archive, metadata = small_pack(tmp_path, large=True)
    job = seeded(tmp_path, metadata)
    assert len(chunks(job, archive, metadata)) >= 3
    assert not (job / ".runtime").exists()
    commit = {"mode": "commit", "sha256": metadata["sha256"]}
    assert receive(job, commit)["installed"]
    assert receive(job, commit)["installed"]
    verify_installed(job, metadata)
    program = job / ".runtime/tooling/program"
    assert program.stat().st_mode & stat.S_IXUSR
    assert (job / ".runtime/tooling/alias").is_symlink()
    assert not (job / ".runtime-upload").exists()


def test_deployed_node_bin_retains_relative_package_imports(tmp_path):
    source = tmp_path / "source"
    (source / ".bin").mkdir(parents=True)
    (source / "package").mkdir()
    (source / "package/cli.cjs").write_text("console.log(require('./message.json').value)")
    (source / "package/message.json").write_text('{"value":"runtime-package-resolved"}')
    (source / ".bin/probe").symlink_to("../package/cli.cjs")
    description = tmp_path / "assets.json"
    description.write_text(json.dumps({"platform": identity(), "paths": {"tooling": "source"}}))
    metadata = pack(tmp_path, description, "runtime.zip")
    archive = Path(metadata.pop("archive"))
    job = seeded(tmp_path, metadata)
    chunks(job, archive, metadata)
    receive(job, {"mode": "commit", "sha256": metadata["sha256"]})
    result = subprocess.run(["node", str(job / ".runtime/tooling/.bin/probe")],
                            capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == "runtime-package-resolved"


@pytest.mark.parametrize("change", ["file", "manifest", "extra", "symlink", "mode", "receipt",
                                   "linked-lock", "linked-receipt"])
def test_installed_runtime_tampering_cannot_reach_execution(tmp_path, change):
    archive, metadata = small_pack(tmp_path)
    job = seeded(tmp_path, metadata)
    chunks(job, archive, metadata)
    receive(job, {"mode": "commit", "sha256": metadata["sha256"]})
    runtime = job / ".runtime"
    if change == "file":
        (runtime / "tooling/program").write_text("changed")
    elif change == "manifest":
        lock = json.loads((runtime / LOCK).read_text())
        (runtime / "tooling/program").write_text("changed")
        lock["files"]["tooling/program"].update(sha256=sha(runtime / "tooling/program"), bytes=7)
        (runtime / LOCK).write_text(json.dumps(lock))
    elif change == "extra":
        (runtime / "tooling/extra").touch()
    elif change == "symlink":
        (runtime / "tooling/program").unlink()
        (runtime / "tooling/program").symlink_to("alias")
    elif change == "mode":
        (runtime / "tooling/program").chmod(0o644)
    elif change.startswith("linked-"):
        name = LOCK if change == "linked-lock" else "receipt.json"
        (runtime / name).unlink()
        (runtime / name).symlink_to("tooling/alias")
    else:
        (runtime / "receipt.json").write_text("{}")
    with pytest.raises(ValueError):
        verify_installed(job, metadata)


@pytest.mark.parametrize("fault", ["order", "wrong-bytes", "wrong-pin", "platform", "cancelled", "started",
                                  "incomplete", "corrupt", "oversize"])
def test_failed_runtime_transfer_never_publishes(tmp_path, fault):
    archive, metadata = small_pack(tmp_path, large=True)
    if fault == "platform":
        metadata["platform"] = {"system": "unsupported", "machine": "unsupported"}
    job = seeded(tmp_path, metadata)
    message = {"mode": "chunk", "sha256": metadata["sha256"], "offset": 0,
               "data_base64": base64.b64encode(archive.read_bytes()[:CHUNK]).decode()}
    if fault == "order":
        message["offset"] = CHUNK
    elif fault == "wrong-bytes":
        receive(job, message)
        message["data_base64"] = base64.b64encode(b"x" * CHUNK).decode()
    elif fault == "wrong-pin":
        message["sha256"] = "0" * 64
    elif fault in ("started", "cancelled"):
        (job / fault).touch()
    elif fault in ("incomplete", "corrupt"):
        if fault == "corrupt":
            chunks(job, archive, metadata)
            with (job / ".runtime-upload").open("r+b") as stream:
                stream.write(b"damaged")
        message["mode"] = "commit"
    elif fault == "oversize":
        message["data_base64"] += "AAAA"
    with pytest.raises(ValueError):
        receive(job, message)
    assert not (job / ".runtime").exists()


def test_runtime_pack_refuses_source_escape_and_directory_cycles(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "outside").write_text("not an input")
    link = tmp_path / "source/link"
    link.symlink_to("../outside")
    manifest = tmp_path / "assets.json"
    manifest.write_text(json.dumps({"platform": identity(), "paths": {"tooling": "source"}}))
    with pytest.raises(ValueError, match="escapes"):
        pack(tmp_path, manifest, "assets.zip")
    link.unlink()
    link.symlink_to(".")
    with pytest.raises(ValueError, match="cycle"):
        pack(tmp_path, manifest, "assets.zip")
    assert not (tmp_path / "assets.zip").exists()


@pytest.mark.parametrize("fault", ["undeclared", "duplicate", "traversal", "symlink", "expansion", "secret"])
def test_runtime_archive_admission_rejects_untrusted_contents(tmp_path, fault):
    archive, _ = small_pack(tmp_path)
    with zipfile.ZipFile(archive) as original:
        entries = {name: original.read(name) for name in original.namelist()}
    lock = json.loads(entries[LOCK])
    if fault == "undeclared":
        entries["extra"] = b"x"
    elif fault == "traversal":
        lock["files"]["../escape"] = lock["files"].pop("tooling/program")
        entries["../escape"] = entries.pop("tooling/program")
    elif fault == "expansion":
        lock["files"]["tooling/program"]["bytes"] = 2**31
    entries[LOCK] = json.dumps(lock).encode()
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(changed, "w") as output:
        for name, data in entries.items():
            member = zipfile.ZipInfo(name)
            if fault == "symlink" and name == "tooling/program":
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(member, data)
        if fault == "duplicate":
            with pytest.warns(UserWarning):
                output.writestr(LOCK, entries[LOCK])
    with pytest.raises(ValueError):
        inspect_archive(changed, secrets=["runtime-ok"] if fault == "secret" else ())


@pytest.mark.parametrize("fault", [None, "chunk-reply", "commit-reply", "platform", "protocol", "embedded"])
async def test_actual_cli_deploys_runtime_before_execution_and_recovers_lost_replies(
        tmp_path, monkeypatch, fault):
    from consumer_agent_testing_tool.workers import CommandGateway
    archive, metadata = small_pack(tmp_path, large=True)
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    if fault == "embedded":
        import sys
        code = "WORKER_GUARD_SOURCE = " + repr((ROOT / "src/consumer_agent_testing_tool/worker_guard.py").read_text()) + "\n"
        code += (ROOT / "src/consumer_agent_testing_tool/worker_gateway.py").read_text()
        gateway = json.loads((tmp_path / "gateway.json").read_text())
        gateway["gateway"] = [sys.executable, "-I", "-c", code, str(tmp_path / "jobs")]
        (tmp_path / "gateway.json").write_text(json.dumps(gateway))
    config["execution"]["worker"]["runtime"] = {"archive": archive.name, "sha256": metadata["sha256"]}
    path.write_text(json.dumps(config))
    original = CommandGateway.call
    replies, calls, lost = [], [], False

    async def capture(self, operation, payload):
        nonlocal lost
        calls.append((operation, payload.get("mode")))
        reply = await original(self, operation, payload)
        if operation == "ready":
            assert reply["runtime_protocol"] == PROTOCOL
            if fault == "platform":
                reply["runtime_platform"] = {}
            elif fault == "protocol":
                reply.pop("runtime_protocol")
        if operation == "runtime":
            replies.append(reply)
            if not lost and fault == payload["mode"] + "-reply":
                lost = True
                raise TimeoutError("synthetic lost acknowledgement")
        return reply

    monkeypatch.setattr(CommandGateway, "call", capture)
    code = await execute(parser().parse_args(["inspect", "--config", str(path)]))
    record = json.loads(next((tmp_path / "runs").glob("worker-*/worker.json")).read_text())
    assert record["cleanup"] == "confirmed"
    if fault in ("platform", "protocol"):
        assert code == 2 and not any(op in ("runtime", "run") for op, _ in calls)
    else:
        assert code == 0 and record["collection"] == "complete"
        assert record["runtime_assets"] == metadata
        assert replies[-1]["installed"]
        assert calls.index(("runtime", "commit")) < calls.index(("run", "start"))
        manifest = json.loads(next((tmp_path / "runs").glob("worker-*/evidence/runs/*/manifest.json")).read_text())
        assert manifest["run_lock"]["execution"]["runtime_assets"] == metadata
    assert not list((tmp_path / "jobs").glob("worker-*"))


@pytest.fixture(scope="module")
def browser_runtime(tmp_path_factory):
    directory = tmp_path_factory.mktemp("deployed-browser")
    manifest = directory / "assets.json"
    # Both sources are explicitly scoped to this repository, never the user home.
    manifest.write_text(json.dumps({"platform": identity(), "entrypoints": {
        "chromium": "browsers/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell"},
        "paths": {
        "tooling/payment-ui": str(ROOT / "tooling/payment-ui"),
        "browsers/chromium_headless_shell-1243": str(ROOT / ".cache/ms-playwright/chromium_headless_shell-1243"),
        "browsers/ffmpeg-1011": str(ROOT / ".cache/ms-playwright/ffmpeg-1011")}}))
    result = pack(ROOT, manifest, directory / "browser.zip")
    return Path(result["archive"]), result["sha256"]


@pytest.mark.parametrize("continued", [False, True])
async def test_real_chromium_and_sdk_scenario_from_deployed_assets(
        tmp_path, monkeypatch, browser_runtime, continued):
    from aiohttp import web
    from test_browser_scenario import DRAFT, PROFILE
    from test_scenario_runtime import scenario

    archive, checksum = browser_runtime
    # A hard link places the explicit archive inside this test workspace without copying 200 MiB.
    os.link(archive, tmp_path / "browser.zip")
    path, config = configuration(tmp_path)
    gateway = json.loads((tmp_path / "gateway.json").read_text())
    gateway.update(ttl_seconds=240, call_timeout_seconds=60, poll_interval_seconds=.1)
    (tmp_path / "gateway.json").write_text(json.dumps(gateway))
    config["execution"]["worker"]["runtime"] = {"archive": "browser.zip", "sha256": checksum}
    key = "synthetic-browser-model-key"
    monkeypatch.setenv("WORKER_MODEL_CREDENTIAL", key)
    requests, actions, index = [], [], 0
    steps = DRAFT + ([("pause",), ("click", "Refresh reservation"),
                      ("see", "Reservation draft-1: draft")] if continued else [])

    def call(name, args, identifier):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": identifier,
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

    async def handler(request):
        nonlocal index
        assert request.headers["Authorization"] == "Bearer " + key
        body = await request.json()
        requests.append(body)
        assert body["provider"]["only"] == ["together"] and body["provider"]["zdr"] is True
        if body["messages"][-1].get("tool_call_id") == "probe":
            message = {"role": "assistant", "content": "probe-accepted-7"}
        elif any(row["function"]["name"] == "fixture_probe" for row in body.get("tools", [])):
            message = call("fixture_probe", {"payload": {"count": 7, "label": "probe"}}, "probe")
        elif index == len(steps):
            message = {"role": "assistant", "content": "The visible portal reports a nonbinding draft."}
        else:
            from test_browser_portals import reference
            messages = body["messages"]
            observation = next(json.loads(m["content"]) for m in reversed(messages)
                if isinstance(m.get("content"), str) and m["content"].startswith('{"generation"'))
            step = steps[index]
            if step[0] == "pause":
                arguments = {"kind": "takeover"}
                index += 1
            elif step[0] == "see":
                if step[1] in observation["text"]:
                    index += 1
                arguments = {"kind": "snapshot"}
            else:
                kind, label = step[:2]
                arguments = {"kind": kind, "ref": reference(
                    observation, label, "textbox" if kind == "type" else "button"),
                    "generation": observation["generation"]}
                if kind == "type":
                    arguments["text"] = step[2]
                index += 1
            actions.append(arguments)
            message = call("browser_action", arguments, f"browser-{len(requests)}")
        return web.json_response({"id": "synthetic-browser-worker", "object": "chat.completion",
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
        "auth": {"mode": "api_key", "env": "WORKER_MODEL_CREDENTIAL"},
        "limits": {"timeout_seconds": 5, "max_retries": 0}}}
    config["defaults"] = {"model_profile": "selected", "interface_profile": PROFILE}
    path.write_text(json.dumps(config))
    spec = scenario(assertions=[{"scorer": "backend.draft_count", "expected": 1},
                                {"scorer": "backend.confirmed_count", "expected": 0}])
    spec.update(surface="browser", interface_profile=PROFILE)
    spec["limits"]["wall_seconds"] = 300
    (tmp_path / "scenario.json").write_text(json.dumps(spec))
    monkeypatch.chdir(tmp_path)
    command = ["run", "scenario.json", "--config", str(path), "--surface", "browser", "--trials", "1"]
    if continued:
        command.append("--resumable")
    try:
        code = await execute(parser().parse_args(command))
        first = next((tmp_path / "runs").glob("worker-*/worker.json"))
        journal = first
        if continued:
            assert code == 3
            original = json.loads(first.read_text())
            assert original["continuation"]["collection"] == "complete"
            run_id = next(iter(original["continuation"]["checkpoints"]))
            capsule = first.parent / "private" / run_id
            raw_run = capsule / "run" / run_id
            private = json.loads((raw_run / "private/trial.private.json").read_text())
            assert "Synthetic shopper" in json.dumps(private["browser_storage"])
            original_seal = (raw_run / "browser/evidence.sha256.json").read_bytes()
            assert not (tmp_path / "jobs" / first.parent.name).exists()
            code = await execute(parser().parse_args([*command, "--resume", str(capsule)]))
            journal = next(p for p in (tmp_path / "runs").glob("worker-*/worker.json") if p != first)
            assert (journal.parent / "private" / run_id / "run" / run_id /
                    "browser/evidence.sha256.json").read_bytes() == original_seal
    finally:
        await server.cleanup()
    record = json.loads(journal.read_text())
    assert code == 0, record
    assert record["collection"] == "complete" and record["cleanup"] == "confirmed"
    assert actions and index == len(steps)
    assert not (tmp_path / "tooling").exists() and not (tmp_path / ".cache/ms-playwright").exists()
    manifests = [json.loads(path.read_text()) for path in (journal.parent / "evidence").glob("runs/*/manifest.json")]
    manifest = next(value for value in manifests if value.get("scenario_id") == spec["id"])
    assert manifest["backend_metrics"]["backend.draft_count"] == 1
    assert manifest["run_lock"]["execution"]["runtime_assets"]["sha256"] == checksum
    assert not (tmp_path / "jobs" / journal.parent.name).exists()
    if continued:
        assert record["continuation"]["collection"] == "complete"
        assert manifest["continuation_attempt"] == 2 and len(manifest["browser_segments"]) == 2
        assert manifest["worker_execution_history"][0]["trial_id"] == first.parent.name
        assert sum(row["operation"] == "reservations.create" for row in manifest["target_attempts"]) == 1
        assert any(action.get("generation", 0) >= 1000000 for action in actions)
