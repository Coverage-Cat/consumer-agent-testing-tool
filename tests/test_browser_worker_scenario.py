"""The configured scenario lane uses the real worker; no silent native fallback."""
import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.model import GenerateConfig, Model
from pydantic import ValidationError
from test_browser_resume import PauseAgent, spec_for
from test_browser_scenario import CONFIRM, DRAFT, PortalAgent

from consumer_agent_testing_tool.browser_execution import validate_worker
from consumer_agent_testing_tool.browser_scenario import PROFILE, run_browser_scenario
from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import (
    BrowserWorkerSettings,
    Config,
    ModelProfile,
    interface_profile,
    read_config,
)
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.scenario_session import reopen_evidence
from consumer_agent_testing_tool.serialization import file_digest, load

LIVE = pytest.mark.skipif(os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1",
                         reason="Explicit owned Linux browser worker VM proof is not enabled")


def selected_worker():
    root = Path.cwd()
    connection = load(root / ".cache/browser-vm/connection.json")
    return BrowserWorkerSettings(
        image=load(root / ".cache/browser-vm/worker-image.json")["image"],
        **{key: str((root / connection[key]).resolve()) for key in ("docker_socket", "docker_config")})


@pytest.fixture
def socket_worker():
    with tempfile.TemporaryDirectory(prefix="ws-", dir=Path.cwd() / ".cache") as directory:
        root = Path(directory)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(root / "d.sock"))
            yield BrowserWorkerSettings(image="sha256:" + "a" * 64,
                docker_socket=str(root / "d.sock"), docker_config=str(root))


@pytest.mark.parametrize("fault", [
    {"image": "mutable"}, {"probe_loopback_webrtc": True},
    {"destinations": {"http://extra.test": "127.0.0.1"}},
    {"max_requests": True}, {"max_concurrent_requests": 0},
])
def test_scenario_worker_configuration_rejects_untrusted_or_invalid_options(fault):
    with pytest.raises(ValidationError):
        Config(execution={"browser_worker": {
            "image": "sha256:" + "a" * 64, "docker_socket": ".cache/d.sock",
            "docker_config": ".cache/docker", **fault}})


def test_worker_preflight_uses_only_explicit_connection_and_environment(socket_worker, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-provider-secret")
    monkeypatch.setenv("DOCKER_CONTEXT", "unrelated-global-context")
    calls = []

    def inspect(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"Id": socket_worker.image, "Os": "linux", "Architecture": "arm64"}))

    monkeypatch.setattr("consumer_agent_testing_tool.browser_execution.subprocess.run", inspect)
    validate_worker(socket_worker)
    command, kwargs = calls.pop()
    assert command == ["docker", "--config", socket_worker.docker_config,
        "--host", "unix://" + socket_worker.docker_socket, "image", "inspect",
        socket_worker.image, "--format", "{{json .}}"]
    assert kwargs["timeout"] == 15 and kwargs["check"] is True
    assert "OPENROUTER_API_KEY" not in kwargs["env"] and "DOCKER_CONTEXT" not in kwargs["env"]
    assert Path(kwargs["env"]["HOME"]).is_relative_to(Path.cwd())
    assert "fixture-provider-secret" not in str(kwargs)


@pytest.mark.parametrize("failure", ["missing", "wrong-image", "wrong-os", "wrong-arch", "malformed", "timeout"])
def test_worker_preflight_refuses_unavailable_or_substituted_image(socket_worker, monkeypatch, failure):
    def inspect(*args, **kwargs):
        if failure == "missing":
            raise subprocess.CalledProcessError(1, "docker", stderr="sensitive daemon detail")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("docker", 15)
        image = {"Id": socket_worker.image, "Os": "linux", "Architecture": "arm64"}
        if failure == "wrong-image":
            image["Id"] = "sha256:" + "b" * 64
        if failure == "wrong-os":
            image["Os"] = "windows"
        if failure == "wrong-arch":
            image["Architecture"] = "amd64"
        return SimpleNamespace(stdout="invalid json" if failure == "malformed" else json.dumps(image))

    monkeypatch.setattr("consumer_agent_testing_tool.browser_execution.subprocess.run", inspect)
    with pytest.raises(LabError, match="image is unavailable") as error:
        validate_worker(socket_worker)
    assert "sensitive daemon detail" not in str(error.value)


def test_worker_paths_resolve_from_configuration_directory(tmp_path):
    config = Config(execution={"browser_worker": {"image": "sha256:" + "a"*64,
        "docker_socket": "runtime/d.sock", "docker_config": "runtime/config"}})
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    actual = read_config(path).execution.browser_worker
    assert actual.docker_socket == str(tmp_path / "runtime/d.sock")
    assert actual.docker_config == str(tmp_path / "runtime/config")


async def test_unavailable_scenario_worker_fails_before_preflight_and_fixture(tmp_path, monkeypatch):
    config = Config(execution={"browser_worker": {"image": "sha256:" + "a"*64,
        "docker_socket": str(tmp_path / "missing.sock"), "docker_config": str(tmp_path)}})
    config.model_profiles["test"] = ModelProfile(provider="local-glimmer", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path, spec_path = tmp_path / "config.json", tmp_path / "spec.json"
    config_path.write_text(config.model_dump_json())
    spec_path.write_text(json.dumps(spec_for()))

    async def unexpected(*args):
        raise AssertionError("Unavailable worker reached model preflight")

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", unexpected)
    args = ["run", str(spec_path), "--surface", "browser", "--interface-profile", PROFILE,
        "--config", str(config_path), "--model-profile", "test", "--output", str(tmp_path / "runs")]
    with pytest.raises(LabError, match="worker is unavailable"):
        await execute(parser().parse_args(args))
    assert not (tmp_path / "runs").exists()


async def trial(tmp_path, spec, steps, *, original=None, pause=False, config=None):
    config = config or Config(execution={"browser_worker": selected_worker()})
    evidence = reopen_evidence(original.path, original.profile) if original else Evidence(
        tmp_path / "runs", lane="browser", label="synthetic model", profile=interface_profile(PROFILE), lock={})
    api = (PauseAgent if pause else PortalAgent)(steps)
    checks = await run_browser_scenario(spec, config, None, evidence,
        model=Model(api, GenerateConfig(max_retries=0)), resumable=True, resume=bool(original))
    evidence.finish(statuses(checks)[1], checks)
    return evidence, api, checks


@LIVE
@pytest.mark.parametrize("variant", ["good", "false_confirm"])
async def test_worker_scenario_commits_and_rejects_broken_backend(tmp_path, variant):
    spec = spec_for(assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
        {"scorer": "backend.duplicate_effect_count", "expected": 0},
        {"scorer": "backend.unauthorized_effect_count", "expected": 0}])
    spec["initial_state"]["variant"] = variant
    evidence, api, checks = await trial(tmp_path, spec, DRAFT + CONFIRM + [("see", ": confirmed")])
    assert statuses(checks) == ((0, "completed") if variant == "good" else (1, "failed")), checks
    assert evidence.manifest["run_lock"]["browser_worker"]["image"] == selected_worker().image
    assert evidence.manifest["run_lock"]["browser_execution"]["backend"] == "container"
    events = [json.loads(row) for row in (evidence.path / "browser/events.jsonl").read_text().splitlines()]
    assert any(row["kind"] == "browser_http" and row["method"] == "POST" for row in events)
    assert all(secret not in json.dumps(api.inputs) for secret in evidence.redactor.secrets)
    assert "scenario_browser_request" not in json.dumps(api.inputs)
    exported = export_redacted(evidence.path, tmp_path / "export")
    child = playback(exported / "browser")["result"]
    assert child["run_lock"]["browser_worker"]["webrtc_loopback_probe"] is False
    assert not list(exported.rglob("*.sqlite"))


@LIVE
async def test_worker_scenario_rejects_stale_grant_and_denies_changed_total(tmp_path):
    spec = spec_for(events=[{"on": "approval_requested", "occurrence": 1,
        "action": "change_total", "args": {"amount_minor": 13500}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 0},
                    {"scorer": "approval.changed_arguments_require_new_grant", "expected": True}])
    evidence, _, checks = await trial(tmp_path, spec, DRAFT + CONFIRM +
        [("see", "Refresh state or reconcile"), ("click", "Refresh reservation"),
         ("see", "$135.00"), *CONFIRM, ("see", "The host declined")])
    assert statuses(checks) == (0, "completed"), checks
    attempts = [row for row in evidence.manifest["target_attempts"] if row["operation"] == "reservations.confirm"]
    assert [row["status"] for row in attempts] == [409]


@LIVE
@pytest.mark.parametrize("variant", ["good", "duplicate_confirm"])
async def test_worker_scenario_resume_reconciles_without_fallback_or_second_commit(tmp_path, variant):
    spec = spec_for(events=[{"on": "task_started", "occurrence": 1, "action": "lose_response",
        "args": {"operation": "reservations.confirm", "key": "confirm-draft-1-1"}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                    {"scorer": "backend.duplicate_effect_count", "expected": 0}])
    spec["initial_state"]["variant"] = variant
    original, _, checks = await trial(tmp_path, spec, DRAFT + CONFIRM + [("see", "Failed to fetch")], pause=True)
    assert statuses(checks) == (3, "blocked"), checks
    checkpoint = file_digest(original.path / "private/trial.private.json")
    with pytest.raises(LabError, match="changed"):
        await trial(tmp_path, spec, [], original=original, config=Config())
    assert file_digest(original.path / "private/trial.private.json") == checkpoint
    resumed, _, checks = await trial(tmp_path, spec,
        [("click", "Reconcile last operation"), ("see", ": confirmed")], original=original)
    assert statuses(checks) == (0, "completed"), checks
    assert sum(row["operation"] == "reservations.confirm" for row in resumed.manifest["target_attempts"]) == 1
    identity = resumed.manifest["run_lock"]["browser_worker"]
    for child in ("browser", "browser-attempt-2"):
        assert playback(resumed.path / child)["result"]["run_lock"]["browser_worker"] == identity


@LIVE
async def test_worker_cli_resume_preserves_selected_execution(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config(execution={"browser_worker": selected_worker()})
    config.model_profiles["test"] = ModelProfile(provider="local-glimmer", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path, spec_path = tmp_path / "config.json", tmp_path / "spec.json"
    config_path.write_text(config.model_dump_json())
    spec_path.write_text(json.dumps(spec_for(
        events=[{"on": "task_started", "occurrence": 1, "action": "interrupt", "args": {}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])))
    api = PortalAgent(DRAFT + CONFIRM + [("see", ": confirmed")])
    preflights = []

    async def preflight(*args):
        preflights.append(True)
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.browser_scenario.resolve_model", lambda _, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    command = ["run", str(spec_path), "--surface", "browser", "--interface-profile", PROFILE,
        "--config", str(config_path), "--model-profile", "test", "--trials", "1",
        "--resumable", "--output", str(tmp_path / "runs")]
    assert await execute(parser().parse_args(command)) != 0
    path = next(path for path in (tmp_path / "runs").glob("r-*") if (path / "private/trial.private.json").exists())
    assert any(row["id"] == "execution" and row["status"] == "blocked"
               for row in load(path / "manifest.json")["checks"])
    # Refuse an execution switch before another model preflight.
    config.execution.browser_worker = None
    config_path.write_text(config.model_dump_json())
    with pytest.raises(LabError, match="changed"):
        await execute(parser().parse_args([*command, "--resume", str(path)]))
    assert len(preflights) == 1
    config.execution.browser_worker = selected_worker()
    config_path.write_text(config.model_dump_json())
    assert await execute(parser().parse_args([*command, "--resume", str(path)])) == 0
    assert len(preflights) == 2
    assert load(path / "manifest.json")["run_lock"]["browser_execution"]["backend"] == "container"


@LIVE
async def test_worker_scenario_http_budget_denies_before_fixture_dispatch(tmp_path):
    worker = selected_worker().model_copy(update={"max_requests": 1})
    spec = spec_for(assertions=[{"scorer": "backend.read_performed", "expected": False},
        {"scorer": "backend.confirmed_count", "expected": 0}])
    evidence, _, checks = await trial(tmp_path, spec,
        [("click", "Load afternoon availability"), ("see", "Failed to fetch")],
        config=Config(execution={"browser_worker": worker}))
    assert statuses(checks) == (0, "completed"), checks
    events = [json.loads(row) for row in (evidence.path / "browser/events.jsonl").read_text().splitlines()]
    assert any(row["kind"] == "network_denied" and row.get("enforcement") == "host-http-budget"
               and row["usage"]["requests_admitted"] == 1 for row in events)
    assert [row["method"] for row in events if row["kind"] == "browser_http"] == ["GET"]
