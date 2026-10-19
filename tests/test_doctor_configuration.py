"""Environment checks never infer permission to use a saved model profile."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from consumer_agent_testing_tool.cli import doctor_configuration, execute, parser
from consumer_agent_testing_tool.config import Config, ModelProfile
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.offline import bootstrap
from consumer_agent_testing_tool.worker_run import credentials, selected_inputs

DEFAULT = Path("consumer-agent-testing-tool.yaml")


def test_a_missing_default_settings_file_is_an_empty_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = doctor_configuration(DEFAULT)
    # The built-in defaults: no model profile (so no provider preflight) and the bundled fixture target.
    assert config == Config() and not config.model_profiles and config.defaults.model_profile is None


def test_a_named_settings_file_must_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises((LabError, OSError, ValueError)):
        doctor_configuration(tmp_path / "agent.yaml")


def test_an_existing_default_settings_file_is_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    DEFAULT.write_text("model_profiles: {}\ntargets: {}\n")
    assert doctor_configuration(DEFAULT) == Config.model_validate({"model_profiles": {}, "targets": {}})
    DEFAULT.write_text("unknown_section: true\n")
    with pytest.raises((LabError, OSError, ValueError)):
        doctor_configuration(DEFAULT)


def saved_model():
    return Config(model_profiles={"fixture": ModelProfile(
        provider="openrouter", api="chat-completions", model_id="fixture/model",
        auth={"mode": "api_key", "env": "CATT_TEST_MODEL_KEY"})},
        defaults={"model_profile": "fixture"}, targets={})


@pytest.mark.parametrize("arguments", [
    [],
    ["--config", "consumer-agent-testing-tool.yaml"],
    ["--model-profile", "fixture"],
    ["--config", "consumer-agent-testing-tool.yaml", "--model-profile", "fixture"],
])
async def test_saved_model_never_authorizes_doctor_inference(tmp_path, monkeypatch, capsys, arguments):
    monkeypatch.chdir(tmp_path)
    DEFAULT.write_text(saved_model().model_dump_json())
    monkeypatch.setenv("CATT_TEST_MODEL_KEY", "fixture-provider-credential")
    preflight = AsyncMock(side_effect=AssertionError("Unexpected provider request"))
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.cli.consumer_stack_checks",
                        lambda root: [{"id": "fixture-stack", "status": "passed"}])
    assert await execute(parser().parse_args(["doctor", *arguments])) == 0
    preflight.assert_not_awaited()
    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    check = next(row for row in manifest["checks"] if row["id"] == "model")
    assert check["required"] is False and "--model-preflight" in check["detail"]
    assert "warning: model profile" not in capsys.readouterr().err


async def test_model_preflight_explicitly_checks_the_selected_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    DEFAULT.write_text(saved_model().model_dump_json())
    preflight = AsyncMock(return_value=[{"id": "fixture-preflight", "status": "passed"}])
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.cli.consumer_stack_checks", lambda root: [])
    assert await execute(parser().parse_args(["doctor", "--model-preflight"])) == 0
    preflight.assert_awaited_once()
    assert preflight.call_args.args[0].model_id == "fixture/model"


async def test_model_preflight_without_a_selected_profile_is_a_setup_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LabError, match="--model-preflight requires"):
        await execute(parser().parse_args(["doctor", "--model-preflight"]))
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("configured", [False, True])
def test_offline_doctor_does_not_admit_saved_remote_inference(tmp_path, monkeypatch, configured):
    monkeypatch.chdir(tmp_path)
    if configured:
        DEFAULT.write_text(saved_model().model_dump_json())
    monkeypatch.delenv("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY", raising=False)
    monkeypatch.setattr("consumer_agent_testing_tool.offline.platform.system", lambda: "Darwin")
    monkeypatch.setattr("shutil.which", lambda name: None)
    admitted = []
    monkeypatch.setattr("consumer_agent_testing_tool.offline.confine",
                        lambda ports, sockets, env: admitted.append((ports, sockets)) or 0)
    assert bootstrap(parser().parse_args(["doctor", "--offline"])) == 0
    assert admitted == [([], [])]
    if configured:
        with pytest.raises(Unsupported, match="rejects remote inference"):
            bootstrap(parser().parse_args(["doctor", "--offline", "--model-preflight"]))
        assert admitted == [([], [])]


@pytest.mark.parametrize("preflight", [False, True])
def test_worker_doctor_forwards_model_credentials_only_with_opt_in(monkeypatch, preflight):
    config = saved_model()
    monkeypatch.setenv("CATT_TEST_MODEL_KEY", "fixture-provider-credential")
    arguments = ["doctor", *(["--model-preflight"] if preflight else [])]
    args = parser().parse_args(arguments)
    name, _, _, targets = selected_inputs(args, config)
    scoped = credentials(args, SimpleNamespace(credential_env=[]), config, name, targets)
    assert name == ("fixture" if preflight else None)
    assert scoped == ({"CATT_TEST_MODEL_KEY": "fixture-provider-credential"} if preflight else {})
