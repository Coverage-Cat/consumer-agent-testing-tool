"""The real installed coordinator behind the ordinary execution selector."""
import json
import os
import sys
from pathlib import Path

import pytest

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.worker_run import harness_identity, validate_invocation


def configuration(root, *, backend="worker"):
    gateway = root / "gateway.json"
    gateway.write_text(json.dumps({
        "provider": "local-process-reference",
        "gateway": [sys.executable, "-I", str(Path(__file__).resolve().parents[1] /
                    "src/consumer_agent_testing_tool/worker_gateway.py"), str(root / "jobs")],
        "ttl_seconds": 60, "call_timeout_seconds": 30,
    }))
    config = {"schema_version": 1, "execution": {
        "backend": backend, "worker": {"gateway_config": "gateway.json"}}}
    path = root / "config.json"
    path.write_text(json.dumps(config))
    return path, config


async def test_actual_static_cli_worker_keeps_execution_selection_and_complete_exports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, _ = configuration(tmp_path)
    assert await execute(parser().parse_args(["inspect", "--config", str(path)])) == 0
    journal = next((tmp_path / "runs").glob("worker-*/worker.json"))
    record = json.loads(journal.read_text())
    assert record["status"] == "completed" and record["cleanup"] == "confirmed"
    assert record["collection"] == "complete"
    archive = record["artifacts"]["run_archive"]
    assert len(archive["runs"]) == 1
    manifest = json.loads((journal.parent / "evidence" / next(iter(archive["runs"])) / "manifest.json").read_text())
    context = manifest["worker_execution"]
    assert context["backend"] == "worker" and context["trial_id"] == journal.parent.name
    assert context["harness"] == harness_identity()
    assert manifest["run_lock"]["execution"] == context["configuration"]
    assert "trial_id" not in manifest["run_lock"]["execution"]
    assert manifest["locations"]["coordinator"] == "worker"
    assert not (tmp_path / "jobs" / journal.parent.name).exists()
    assert await execute(parser().parse_args(["inspect", "--config", str(path)])) == 0
    other = next(item for item in (tmp_path / "runs").glob("worker-*/worker.json") if item != journal)
    repeated = json.loads(next((other.parent / "evidence").glob("runs/*/manifest.json")).read_text())
    assert repeated["run_lock"]["execution"] == manifest["run_lock"]["execution"]
    assert repeated["worker_execution"]["trial_id"] != context["trial_id"]


async def test_explicit_local_selection_overrides_worker_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, _ = configuration(tmp_path)
    assert await execute(parser().parse_args(["inspect", "--config", str(path), "--execution", "local"])) == 0
    assert not (tmp_path / "jobs").exists()
    manifest = json.loads(next((tmp_path / "runs").glob("r-*/manifest.json")).read_text())
    assert manifest["run_lock"]["execution"]["backend"] == "local"


@pytest.mark.parametrize("arguments,exception", [
    (["--offline"], Unsupported), (["--model-profile", "absent"], LabError),
])
async def test_invalid_worker_selection_never_provisions(tmp_path, monkeypatch, arguments, exception):
    monkeypatch.chdir(tmp_path)
    path, _ = configuration(tmp_path)
    with pytest.raises(exception):
        await execute(parser().parse_args(["inspect", "--config", str(path), *arguments]))
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("variant,expected,surface", [
    ("good", 0, "api-tools"), ("false_success", 1, "api-tools"),
    pytest.param("good", 0, "cli-skill", marks=pytest.mark.skipif(
        os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1", reason="Requires actual Docker boundary")),
])
async def test_worker_runs_native_inspect_scenario_and_independent_oracle(
        tmp_path, monkeypatch, variant, expected, surface):
    from aiohttp import web
    from test_scenario_runtime import scenario

    from consumer_agent_testing_tool.workers import CommandGateway

    phases = []
    original_call = CommandGateway.call

    async def captured_gateway(self, operation, payload):
        if operation == "run":
            phases.append((payload.get("mode"), sorted(payload.get("coordinator_environment", {}))))
        return await original_call(self, operation, payload)

    monkeypatch.setattr(CommandGateway, "call", captured_gateway)
    spec = scenario(assertions=[{"scorer": "backend.draft_count", "expected": 1},
                                {"scorer": "backend.confirmed_count", "expected": 0}])
    if surface == "cli-skill":
        spec.update(surface=surface, interface_profile="muse-cli-skills-public-2026-09")
        spec.pop("approvals", None)
    if variant != "good":
        spec["initial_state"]["variant"] = variant
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    if surface == "cli-skill":
        config["execution"]["worker"]["environment"] = {
            key: os.environ[key] for key in ("DOCKER_HOST", "DOCKER_CONFIG") if key in os.environ}
        gateway = json.loads((tmp_path / "gateway.json").read_text())
        gateway.update(ttl_seconds=180, call_timeout_seconds=120)
        (tmp_path / "gateway.json").write_text(json.dumps(gateway))
    key = "synthetic-scoped-model-credential"
    monkeypatch.setenv("WORKER_MODEL_CREDENTIAL", key)
    # This unrelated credential must not be part of the transported invocation.
    monkeypatch.setenv("UNRELATED_PROVIDER_SECRET", "never-forward-unselected-provider")
    requests = []

    def tool_call(name, args, identifier):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer " + key
        body = await request.json()
        requests.append(body)
        assert body["provider"]["only"] == ["together"] and body["provider"]["zdr"] is True
        if len(requests) == 1:
            message = tool_call("fixture_probe", {"payload": {"count": 7, "label": "probe"}}, "probe")
        elif len(requests) == 2:
            assert body["messages"][-1]["tool_call_id"] == "probe"
            message = {"role": "assistant", "content": "probe-accepted-7"}
        elif len(requests) == 3:
            if surface == "cli-skill":
                message = tool_call("shell", {"command": "env\nbooking reservations.create --args "
                    """'{"slot_id":"afternoon","contact":"Synthetic worker shopper","idempotency_key":"draft"}'"""
                    }, "draft")
            else:
                assert "reservations_create" in [row["function"]["name"] for row in body["tools"]]
                message = tool_call("reservations_create", {
                    "slot_id": "afternoon", "contact": "Synthetic worker shopper",
                    "idempotency_key": "draft"}, "draft")
        else:
            assert len(requests) == 4 and body["messages"][-1]["tool_call_id"] == "draft"
            assert "WORKER_MODEL_CREDENTIAL" not in json.dumps(body["messages"])
            message = (tool_call("submit", {"answer": "The service reported a nonbinding draft."}, "done")
                if surface == "cli-skill" else
                {"role": "assistant", "content": "The service reported a nonbinding draft."})
        return web.json_response({
            "id": "synthetic-worker", "object": "chat.completion", "created": 1,
            "model": "meta/muse-glimmer-30b", "provider": "Together",
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
    config["defaults"] = {"model_profile": "selected", "interface_profile": spec["interface_profile"]}
    path.write_text(json.dumps(config))
    (tmp_path / "scenario.json").write_text(json.dumps(spec))
    try:
        code = await execute(parser().parse_args(["run", "scenario.json", "--config", str(path),
            "--surface", surface, "--trials", "1", "--output", "selected-output"]))
    finally:
        await server.cleanup()
    assert code == expected
    assert len(requests) == 4 and key not in json.dumps(requests)
    journal = next((tmp_path / "selected-output").glob("worker-*/worker.json"))
    record = json.loads(journal.read_text())
    assert record["cleanup"] == "confirmed" and record["collection"] == "complete"
    assert record["execution"]["exit_code"] == expected
    assert record["coordinator_credential_env"] == ["WORKER_MODEL_CREDENTIAL"]
    assert [names for mode, names in phases if mode == "start"] == [["WORKER_MODEL_CREDENTIAL"]]
    assert any(mode == "poll" for mode, _ in phases)
    assert all(names == [] for mode, names in phases if mode == "poll")
    assert key not in journal.read_text()
    assert "never-forward-unselected-provider" not in journal.read_text()
    root = journal.parent / "evidence"
    manifests = [json.loads(path.read_text()) for path in root.glob("runs/*/manifest.json")]
    trial = next(row for row in manifests if row.get("scenario_id") == spec["id"])
    assert trial["backend_metrics"]["backend.draft_count"] == (1 if variant == "good" else 0)
    assert trial["worker_execution"]["model_profile"] == "selected"
    assert trial["run_lock"]["model"]["routing"]["only"] == ["together"]
    assert trial["locations"]["coordinator"] == "worker"
    assert trial["lane"] == "worker execution with remote inference"
    assert trial["inspect_log_artifacts"]
    for path in root.rglob("*.json"):
        assert key not in path.read_text()
    for path in (journal.parent / "inputs").rglob("*.json"):
        assert key not in path.read_text()


@pytest.mark.parametrize("name", ["consumer-agent-testing-tool.worker.json", "scenarios.worker/0.yaml", "runs/manifest.json"])
async def test_declared_input_cannot_overwrite_coordinator_files(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    asset = tmp_path / name
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("synthetic input")
    config["execution"]["worker"]["files"] = [name]
    path.write_text(json.dumps(config))
    with pytest.raises(LabError, match="reserved coordinator path"):
        await execute(parser().parse_args(["inspect", "--config", str(path)]))
    assert not (tmp_path / "jobs").exists()


async def test_changed_worker_harness_rejected_before_seed_and_cleaned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, _ = configuration(tmp_path)
    identity = harness_identity()
    monkeypatch.setattr("consumer_agent_testing_tool.worker_run.harness_identity",
                        lambda provider: {**identity, "implementation": "different implementation"})
    assert await execute(parser().parse_args(["inspect", "--config", str(path)])) == 2
    journal = next((tmp_path / "runs").glob("worker-*/worker.json"))
    record = json.loads(journal.read_text())
    assert record["status"] == "failed" and record["cleanup"] == "confirmed"
    assert not {"seed", "run"} & {event["operation"] for event in record["events"]}
    assert "pinned implementation" in record["detail"]


@pytest.mark.parametrize("fault", ["input", "identity", "trial"])
def test_coordinator_rechecks_input_and_runtime_before_execution(tmp_path, fault):
    from consumer_agent_testing_tool.serialization import digest, file_digest
    from consumer_agent_testing_tool.worker_run import COMMAND, FORMAT

    (tmp_path / "selected.txt").write_text("declared")
    document = {"format": FORMAT, "model_provider": None, "harness": harness_identity(),
        "inputs": {"selected.txt": file_digest(tmp_path / "selected.txt")},
        "execution": {"trial_id": tmp_path.name}}
    lock = {"command": COMMAND, "files": document["inputs"]}
    if fault == "identity":
        document["harness"] = {}
    if fault == "trial":
        document["execution"]["trial_id"] = "another-trial"
    lock["coordinator"] = {"format": FORMAT, "invocation_sha256": digest(document)}
    (tmp_path / "invocation.worker.json").write_text(json.dumps(document))
    (tmp_path / "bundle-lock.json").write_text(json.dumps(lock))
    if fault == "input":
        (tmp_path / "selected.txt").write_text("changed after seed")
    with pytest.raises(LabError):
        validate_invocation(tmp_path)


@pytest.mark.parametrize("fault", ["loader", "overlap", "missing"])
async def test_worker_credentials_reject_unsafe_or_unavailable_scope(tmp_path, monkeypatch, fault):
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    variable = "PYTHONPATH" if fault == "loader" else "SYNTHETIC_WORKER_AUTH"
    config["model_profiles"] = {"selected": {
        "provider": "openrouter", "api": "chat-completions", "model_id": "meta/muse-glimmer-30b",
        "auth": {"mode": "api_key", "env": variable}}}
    config["defaults"] = {"model_profile": "selected"}
    path.write_text(json.dumps(config))
    if fault == "missing":
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, "synthetic-worker-key")
    if fault == "overlap":
        gateway = json.loads((tmp_path / "gateway.json").read_text())
        gateway["credential_env"] = [variable]
        (tmp_path / "gateway.json").write_text(json.dumps(gateway))
    with pytest.raises(LabError):
        await execute(parser().parse_args(["doctor", "--model-preflight", "--config", str(path)]))
    assert not (tmp_path / "jobs").exists()


async def test_explicit_bundle_pin_cannot_be_overridden_by_generated_package(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, _ = configuration(tmp_path)
    gateway = json.loads((tmp_path / "gateway.json").read_text())
    gateway["expected_harness_sha256"] = "0" * 64
    (tmp_path / "gateway.json").write_text(json.dumps(gateway))
    with pytest.raises(LabError, match="explicitly configured pin"):
        await execute(parser().parse_args(["inspect", "--config", str(path)]))
    assert not (tmp_path / "jobs").exists()


async def test_worker_preserves_validated_fixed_sample_decision_contract(tmp_path, monkeypatch):
    from test_scenario_runtime import scenario

    from consumer_agent_testing_tool.decision import read_contract
    from consumer_agent_testing_tool.serialization import load

    spec = scenario()
    monkeypatch.chdir(tmp_path)
    path, config = configuration(tmp_path)
    config["model_profiles"] = {"selected": {
        "provider": "openrouter", "api": "chat-completions", "model_id": "meta/muse-glimmer-30b",
        "auth": {"mode": "api_key", "env": "WORKER_MODEL_CREDENTIAL"}}}
    config["defaults"] = {"model_profile": "selected", "interface_profile": spec["interface_profile"]}
    path.write_text(json.dumps(config))
    monkeypatch.setenv("WORKER_MODEL_CREDENTIAL", "synthetic-key")
    (tmp_path / "scenario.json").write_text(json.dumps(spec))
    contract_path = tmp_path / "decision.json"
    contract_path.write_text(json.dumps({"primary_scenarios": [spec["id"]],
        "trials_per_scenario": 30, "success_threshold": .8}))
    expected = read_contract(contract_path, [spec], 30)
    inspected = []

    async def packaging_only(self, package, **kwargs):
        # No inference: verify the staged contract can be revalidated by the
        # worker, including its original sample count and recomputed identity.
        invocation = load(self.path / "inputs/invocation.worker.json")
        arguments = parser().parse_args(invocation["arguments"])
        staged = self.path / "inputs"
        actual = read_contract(staged / arguments.decision_contract,
            [load(staged / "scenarios.worker/0.yaml")], arguments.trials)
        assert actual == expected
        inspected.append(actual)
        return {"cleanup": "confirmed", "collection": "complete", "execution": {"exit_code": 3}}

    monkeypatch.setattr("consumer_agent_testing_tool.workers.WorkerLifecycle.run", packaging_only)
    arguments = ["run", "scenario.json", "--surface", "api-tools", "--config", str(path),
                 "--decision-contract", str(contract_path), "--trials", "30"]
    assert await execute(parser().parse_args(arguments)) == 3
    assert len(inspected) == 1 and not (tmp_path / "jobs").exists()
    with pytest.raises(LabError, match="fixed sample budget"):
        await execute(parser().parse_args([*arguments[:-1], "1"]))
    assert len(inspected) == 1


def test_worker_forwards_every_set_alias_of_the_model_credential(monkeypatch):
    from types import SimpleNamespace

    from consumer_agent_testing_tool.config import Config
    from consumer_agent_testing_tool.worker_run import credentials
    config = Config.model_validate({"model_profiles": {"meta": {
        "provider": "meta", "api": "chat-completions", "model_id": "muse-spark-1.3",
        "auth": {"mode": "api_key", "env": "MODEL_API_KEY"}}}})
    args, placement = SimpleNamespace(command="run"), SimpleNamespace(credential_env=[])
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setenv("META_API_KEY", "synthetic-meta-alias-key")
    # Locally META_API_KEY is an accepted alias of MODEL_API_KEY; the worker must resolve it too.
    assert credentials(args, placement, config, "meta", set()) == {"META_API_KEY": "synthetic-meta-alias-key"}
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-meta-alias-key")
    assert set(credentials(args, placement, config, "meta", set())) == {"MODEL_API_KEY", "META_API_KEY"}
    monkeypatch.delenv("MODEL_API_KEY")
    monkeypatch.delenv("META_API_KEY")
    with pytest.raises(LabError, match="unavailable"):
        credentials(args, placement, config, "meta", set())
