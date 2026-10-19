import importlib
import json
from pathlib import Path

import pytest

from consumer_agent_testing_tool.cli import execute, initialize, parser
from consumer_agent_testing_tool.config import Config, ModelProfile
from consumer_agent_testing_tool.errors import Unsupported
from consumer_agent_testing_tool.release import audit
from consumer_agent_testing_tool.release_scope import GATES, RELEASE_SCOPE, require_supported


def test_default_cli_hides_development_commands_and_local_model(capsys):
    for arguments in (["worker", "run", "config.json"], ["calibrate", "packet.json"],
                      ["init", "--provider", "local-glimmer"]):
        with pytest.raises(SystemExit):
            parser().parse_args(arguments)
    assert "worker" not in parser().format_help()
    args = parser(development=True).parse_args(["worker", "run", "config.json", "bundle.json"])
    assert args.development_extensions


def test_default_initialization_only_stages_consumer_tooling(tmp_path):
    initialize(parser().parse_args(["init", "--provider", "none", "--config", str(tmp_path / "lab.yaml")]))
    assert (tmp_path / "tooling/payment-ui/package-lock.json").is_file()
    assert not (tmp_path / "tooling/api").exists()
    assert not (tmp_path / "calibration-examples").exists()
    assert not (tmp_path / "openapi").exists()


def test_release_only_requires_supported_local_gates(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"gates": {"muse-calibration": {"status": "passed"}}}))
    result = audit(tmp_path, ledger)
    assert result["release_scope"] == RELEASE_SCOPE
    assert set(result["gates"]) == {"regression", "distribution", "transactional-consent", "replay-diff"}
    assert set(result["gates"]) == set(GATES)
    assert not result["release_qualified"]
    assert "muse-calibration" in result["excluded_capabilities"]


@pytest.mark.parametrize("selection", ["cloud", "local-model", "browser-worker"])
def test_excluded_execution_rejected_before_provider_or_worker_dispatch(selection):
    options = {}
    if selection == "local-model":
        options["model"] = ModelProfile(provider="local-glimmer", api="chat-completions",
                                       model_id="synthetic", base_url="http://127.0.0.1:8080/v1")
    elif selection == "cloud":
        options["command"] = "worker"
    else:
        options["worker"] = object()
    with pytest.raises(Unsupported, match=RELEASE_SCOPE):
        require_supported(**options)
    require_supported(experimental=True, **options)
    require_supported(config=Config())


async def test_manually_constructed_worker_command_cannot_bypass_default_scope():
    from argparse import Namespace
    with pytest.raises(Unsupported, match=RELEASE_SCOPE):
        await execute(Namespace(command="worker"))


def test_local_distribution_requires_its_full_scoped_proof(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path.cwd() / "scripts"))
    module = importlib.import_module("local_distribution")
    assert len(module.REQUIRED) == 19
    assert not any("worker" in name or "api-tools" in name for name in module.REQUIRED)
    for report in ({}, {"status": "passed", "release_scope": "legacy"}):
        with pytest.raises(ValueError, match="current-source"):
            module.verify(tmp_path, report)


@pytest.mark.parametrize("selection,subject", [
    ("command", "Worker lifecycle and real-Muse calibration commands are"),
    ("worker", "Isolated browser workers are"),
    ("config", "Worker execution is"),
    ("model", "Local Glimmer inference is"),
    ("target", "Generated OpenAPI and Prism adapters are"),
])
def test_scope_refusals_read_correctly(selection, subject):
    from types import SimpleNamespace
    options = {"command": {"command": "calibrate"}, "worker": {"worker": object()},
               "config": {"config": Config(execution={"browser_worker": {"image": "sha256:" + "a" * 64,
                          "docker_socket": ".cache/missing.sock", "docker_config": ".cache"}})},
               "model": {"model": ModelProfile(provider="local-glimmer", api="chat-completions",
                         model_id="synthetic", base_url="http://127.0.0.1:8080/v1")},
               "target": {"target": SimpleNamespace(openapi=True)}}[selection]
    with pytest.raises(Unsupported) as raised:
        require_supported(**options)
    assert str(raised.value).startswith(f"{subject} outside {RELEASE_SCOPE}. Use the local consumer path")


def test_optional_api_lock_excludes_postman_and_flagged_packages():
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src/consumer_agent_testing_tool/data/tooling/api"
    names = ["package.json", "package-lock.json", ".npmrc",
             *(f"stubs/postman-collection/{name}" for name in ("package.json", "index.js", "LICENSE"))]
    for name in names:
        assert (packaged / name).read_bytes() == (root / "tooling/api" / name).read_bytes(), name
    packages = json.loads((root / "tooling/api/package-lock.json").read_text())["packages"]
    assert packages["node_modules/postman-collection"]["resolved"] == "file:stubs/postman-collection"
    for location, package in packages.items():
        name = package.get("name", location.rsplit("node_modules/", 1)[-1])
        assert name not in ("sprintf-js", "uuid"), location
        if name == "@faker-js/faker":
            assert int(package["version"].split(".")[0]) >= 10, location


def test_development_initialization_stages_postman_stub(tmp_path):
    initialize(parser(development=True).parse_args(
        ["init", "--provider", "none", "--config", str(tmp_path / "lab.yaml")]))
    api = tmp_path / "tooling/api"
    assert "install-links=true" in (api / ".npmrc").read_text()
    assert json.loads((api / "stubs/postman-collection/package.json").read_text())["version"] == "0.0.0-catt.1"
