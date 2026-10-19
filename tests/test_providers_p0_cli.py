"""Supported defaults (D4), outcome lines (D5), support labels (D8) and the diff --ignore flag."""
import json
from argparse import Namespace
from pathlib import Path

import pytest
from inspect_ai.model import GenerateConfig, Model
from test_api_scenario import Scripted, done, draft_call
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.cli import (
    BROWSER_SETUP,
    NODE_SETUP,
    consumer_stack_checks,
    execute,
    initialize,
    parser,
)
from consumer_agent_testing_tool.config import (
    Config,
    ModelProfile,
    Target,
    read_config,
    select_interface_profile,
)
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.scenario import read_scenario

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / "src/consumer_agent_testing_tool/data"


def scripted_config(tmp_path, **defaults):
    config = Config()
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    for key, value in defaults.items():
        setattr(config.defaults, key, value)
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    return path


def scripted_model(monkeypatch, api, preflight_result=None):
    from consumer_agent_testing_tool.providers import CapturingAPI

    async def preflight(*args):
        if isinstance(preflight_result, Exception):
            raise preflight_result
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.api_scenario.resolve_model", lambda profile, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))


def manifests(root):
    return [json.loads(path.read_text()) for path in root.glob("*/manifest.json")]


async def test_run_without_interface_profile_uses_the_surface_profile(tmp_path, monkeypatch, capsys):
    # The user's local file keeps the old development-only default; run must not trip over it.
    config_path = scripted_config(tmp_path, interface_profile="muse-cli-skills-public-2026-09")
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario()))
    api = Scripted([draft_call(), done()])
    scripted_model(monkeypatch, api)
    code = await execute(parser().parse_args(["run", str(path), "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "1", "--output", str(tmp_path / "runs")]))
    assert code == 0 and len(api.inputs) == 2
    trial = next(m for m in manifests(tmp_path / "runs") if m.get("backend_metrics"))
    assert trial["run_lock"]["interface_profile"]["name"] == "api-tools-surrogate-v1"
    # D8: an unverified profile is announced, never silently treated as qualified.
    assert "warning: model profile 'scripted'" in capsys.readouterr().err


@pytest.mark.parametrize("command,surface,configured,target,expected", [
    ("run", "api-tools", "muse-cli-skills-public-2026-09", None, "api-tools-surrogate-v1"),
    ("run", "browser", "api-tools-surrogate-v1", None, "muse-browser-public-2026-09"),
    ("run", "browser", "muse-browser-public-2026-09", None, "muse-browser-public-2026-09"),
    ("inspect", None, "api-tools-surrogate-v1", "fixture", "muse-cli-skills-public-2026-09"),
    ("inspect", None, "api-tools-surrogate-v1", "mcp", "mcp-contract-2025-11-25"),
    # The fixture publishes MCP and CLI/skill surfaces: a configured default for either one is kept.
    ("inspect", None, "mcp-contract-2025-06-18", "fixture", "mcp-contract-2025-06-18"),
    ("inspect", None, "muse-cli-skills-public-2026-09", "fixture", "muse-cli-skills-public-2026-09"),
    ("inspect", None, "muse-browser-public-2026-09", "fixture", "muse-cli-skills-public-2026-09"),
    ("inspect", None, "mcp-contract-2025-06-18", "cli", "muse-cli-skills-public-2026-09"),
    ("check", None, "api-tools-surrogate-v1", "fixture", "mcp-contract-2025-11-25"),
    ("check", None, "mcp-contract-2025-06-18", "mcp", "mcp-contract-2025-06-18"),
    ("doctor", None, "api-tools-surrogate-v1", None, "api-tools-surrogate-v1"),
])
def test_default_interface_profile_follows_surface_and_target(command, surface, configured, target, expected):
    config = Config(defaults={"interface_profile": configured})
    targets = {"fixture": Target(), "mcp": Target(kind="mcp", command=["server"]),
               "cli": Target.model_construct(kind="cli-skill")}
    args = Namespace(command=command, surface=surface, interface_profile=None, profile=None,
                     **({"suite": "contract"} if command == "check" else {}))
    assert select_interface_profile(args, config, targets.get(target))["name"] == expected
    args.interface_profile = "muse-browser-public-2026-09"
    assert select_interface_profile(args, config, targets.get(target))["name"] == "muse-browser-public-2026-09"


async def test_run_mismatches_name_the_flag_and_profile_to_use(tmp_path, monkeypatch):
    config_path = scripted_config(tmp_path)
    scripted_model(monkeypatch, Scripted([]))
    browser_scenario = PACKAGED / "scenarios_browser/stale_total.yaml"
    base = ["--config", str(config_path), "--model-profile", "scripted", "--trials", "1",
            "--output", str(tmp_path / "runs")]
    with pytest.raises(Unsupported, match="pass --interface-profile api-tools-surrogate-v1"):
        await execute(parser().parse_args(["run", str(browser_scenario), "--interface-profile",
                                           "muse-browser-public-2026-09", *base]))
    with pytest.raises(LabError, match="pass --surface browser --interface-profile muse-browser-public-2026-09"):
        await execute(parser().parse_args(["run", str(browser_scenario), *base]))
    with pytest.raises(Unsupported, match="--development-extensions"):
        await execute(parser().parse_args(["run", str(PACKAGED / "scenarios/direct_draft.yaml"), *base]))
    assert not (tmp_path / "runs").exists()


def test_init_writes_supported_defaults_and_stages_only_runnable_scenarios(tmp_path):
    from consumer_agent_testing_tool.api_scenario import validate_api_scenario
    from consumer_agent_testing_tool.browser_scenario import validate_browser_scenario

    path = tmp_path / "consumer-agent-testing-tool.yaml"
    initialize(parser().parse_args(["init", "--config", str(path), "--provider", "none"]))
    config = read_config(path)
    assert config.defaults.interface_profile == "api-tools-surrogate-v1"
    assert "interface_profile: api-tools-surrogate-v1" in path.read_text()
    assert not (tmp_path / "scenarios").exists()
    staged = [*sorted((tmp_path / "api-scenarios").glob("*.yaml")),
              *sorted((tmp_path / "browser-scenarios").glob("*.yaml"))]
    assert staged and not (tmp_path / "browser-scenarios/forbidden_canary.yaml").exists()
    for item in staged:
        spec = read_scenario(item, milestone=2)
        if spec["surface"] == "api-tools":
            validate_api_scenario(spec, config)
        else:
            validate_browser_scenario(spec, config)
    development = tmp_path / "development"
    initialize(parser(development=True).parse_args(
        ["init", "--config", str(development / "lab.yaml"), "--provider", "none"]))
    assert list((development / "scenarios").glob("*.yaml"))
    assert (development / "browser-scenarios/forbidden_canary.yaml").is_file()


def test_init_refuses_template_only_providers_outside_development(tmp_path):
    path = tmp_path / "template.yaml"
    arguments = ["init", "--config", str(path), "--provider", "openai", "--model", "exact-id"]
    with pytest.raises(Unsupported, match="no live adapter.*--development-extensions"):
        initialize(parser().parse_args(arguments))
    assert not path.exists()
    initialize(parser(development=True).parse_args(arguments))
    assert read_config(path).model_profiles["openai-dev"].support == "specified/unverified"


def test_doctor_stack_checks_report_missing_node_tooling_and_chromium(tmp_path, monkeypatch):
    monkeypatch.setattr("consumer_agent_testing_tool.cli.shutil.which", lambda name: None)
    checks = {check["id"]: check for check in consumer_stack_checks(tmp_path)}
    assert checks["node"]["status"] == "blocked" and "not on PATH" in checks["node"]["detail"]
    assert checks["consumer-node-tooling"]["status"] == "blocked"
    assert NODE_SETUP in checks["consumer-node-tooling"]["detail"]
    assert "@playwright/mcp is not installed" in checks["consumer-node-tooling"]["detail"]
    assert checks["chromium"]["status"] == "blocked" and BROWSER_SETUP in checks["chromium"]["detail"]
    assert checks["protocol-tooling"]["required"] is False


def test_doctor_stack_checks_pass_on_the_installed_workspace():
    checks = {check["id"]: check for check in consumer_stack_checks(ROOT)}
    assert checks["node"]["status"] == "passed" and checks["node"]["version"].startswith("v")
    assert checks["node"]["engines"]["playwright"].startswith(">=")
    assert checks["consumer-node-tooling"]["status"] == "passed"
    assert set(checks["consumer-node-tooling"]["installed"]) == {
        "playwright", "esbuild", "@modelcontextprotocol/client", "@modelcontextprotocol/ext-apps", "@playwright/mcp"}
    assert checks["chromium"]["status"] == "passed"
    assert Path(checks["chromium"]["executable"]).is_file()
    assert ".cache/ms-playwright" in checks["chromium"]["executable"]


def test_node_engine_floors_are_compared_numerically():
    from consumer_agent_testing_tool.cli import node_version_satisfies
    assert node_version_satisfies("v25.9.0", ">=20") is True
    assert node_version_satisfies("v22.18.4", ">=22.19.0") is False
    assert node_version_satisfies("v20.0.0", ">=20.0.0") is True
    assert node_version_satisfies("v25.9.0", "^20 || ^22") is None


async def test_doctor_checks_the_consumer_stack_and_docker_only_where_needed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.json"
    path.write_text(Config().model_dump_json())
    docker_calls = []

    def docker_unavailable():
        docker_calls.append(True)
        raise Unsupported("Docker engine unavailable")

    monkeypatch.setattr("consumer_agent_testing_tool.sandbox.docker_available", docker_unavailable)
    assert await execute(parser().parse_args(["doctor", "--config", str(path), "--output", "runs"])) == 3
    manifest = manifests(tmp_path / "runs")[0]
    ids = [check["id"] for check in manifest["checks"]]
    assert {"node", "consumer-node-tooling", "chromium", "protocol-tooling", "model"} <= set(ids)
    assert "docker-engine" not in ids and "shell-isolation" not in ids and docker_calls == []
    assert "Doctor check blocked (setup/infrastructure) at consumer-node-tooling" in capsys.readouterr().err
    # A configured custom stdio MCP target needs Docker, so doctor checks it and requires it.
    config = Config(targets={"stdio-server": Target(kind="mcp", command=["server"], container_image="server:1")})
    path.write_text(config.model_dump_json())
    assert await execute(parser().parse_args(["doctor", "--config", str(path), "--output", "stdio-runs"])) == 3
    docker = next(check for check in manifests(tmp_path / "stdio-runs")[0]["checks"] if check["id"] == "docker-engine")
    assert docker["required"] is True and "stdio-server" in docker["detail"] and docker_calls == [True]


async def test_doctor_keeps_stack_checks_when_preflight_fails(tmp_path, monkeypatch, capsys):
    config_path = scripted_config(tmp_path)

    async def preflight(*args):
        raise LabError("Provider preflight: insufficient credits (HTTP 402): Insufficient credits")

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    # On the installed workspace the stack passes, so the provider setup error decides the exit code.
    monkeypatch.chdir(ROOT)
    code = await execute(parser().parse_args(["doctor", "--model-preflight", "--config", str(config_path), "--model-profile",
                                              "scripted", "--output", str(tmp_path / "runs")]))
    assert code == 2
    checks = {check["id"]: check for check in manifests(tmp_path / "runs")[0]["checks"]}
    assert checks["model-preflight"]["status"] == "failed" and checks["chromium"]["status"] == "passed"
    err = capsys.readouterr().err
    assert "warning: model profile 'scripted'" in err
    assert ("Doctor check failed (setup/infrastructure) at model-preflight: Provider preflight: "
            "insufficient credits (HTTP 402): Insufficient credits") in err
    # Without the Node tooling the blocked stack is reported too, beside the failed preflight.
    monkeypatch.chdir(tmp_path)
    assert await execute(parser().parse_args(["doctor", "--model-preflight", "--config", str(config_path), "--model-profile",
                                              "scripted", "--output", str(tmp_path / "bare")])) == 3
    checks = {check["id"]: check for check in manifests(tmp_path / "bare")[0]["checks"]}
    assert checks["model-preflight"]["status"] == "failed" and checks["chromium"]["status"] == "blocked"
    # Every problem prints with its fix, so a failed preflight cannot hide the blocked setup.
    lines = capsys.readouterr().err.splitlines()
    assert any(line.startswith("Doctor check failed (setup/infrastructure) at model-preflight: ")
               for line in lines)
    assert any(line.startswith("Doctor check blocked (setup/infrastructure) at consumer-node-tooling: ")
               and NODE_SETUP in line for line in lines)
    assert any(line.startswith("Doctor check blocked (setup/infrastructure) at chromium: ")
               and BROWSER_SETUP in line for line in lines)
    # Optional checks (protocol tooling, no model) stay out of the problem list.
    assert not any(" at protocol-tooling: " in line for line in lines)


async def test_run_prints_status_class_and_reason_beside_the_evidence_path(tmp_path, monkeypatch, capsys):
    config_path = scripted_config(tmp_path)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario()))
    arguments = ["run", str(path), "--config", str(config_path), "--model-profile", "scripted",
                 "--trials", "1", "--output", str(tmp_path / "runs")]
    scripted_model(monkeypatch, Scripted([]), LabError("Provider preflight: insufficient credits (HTTP 402): "
                                                      "Insufficient credits. Add more"))
    assert await execute(parser().parse_args(arguments)) == 2
    out, err = capsys.readouterr()
    gate = out.strip().splitlines()[-1]
    assert (f"Model preflight failed (setup/infrastructure) at model-preflight: Provider preflight: insufficient "
            f"credits (HTTP 402): Insufficient credits. Add more. Evidence: {gate}") in err

    async def failing_trial(spec, config, model, evidence, **kwargs):
        return [{"id": "backend.draft_count", "status": "failed", "class": "verification",
                 "expected": 1, "actual": 0}, {"id": "backend.confirmed_count", "status": "passed"}]

    scripted_model(monkeypatch, Scripted([]))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.run_scenario", failing_trial)
    assert await execute(parser().parse_args(arguments)) == 1
    out, err = capsys.readouterr()
    paths = out.strip().splitlines()
    assert all(Path(line).is_dir() for line in paths)  # stdout keeps one evidence path per line
    trial_id = scenario()["id"]
    assert (f"Trial 1/1 of {trial_id} failed (verification) at backend.draft_count: expected 1, actual 0. "
            f"Evidence: {paths[1]}") in err
    assert f"Run failed: 1 failed, 0 blocked and 0 completed of 1 trials. Evidence: {paths[2]}" in err


async def test_resume_prints_preflight_and_trial_outcomes(tmp_path, monkeypatch, capsys):
    from test_scenario_resume import interrupt

    config_path = scripted_config(tmp_path)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario(events=[interrupt("tool_completed")])))
    command = ["run", str(path), "--config", str(config_path), "--model-profile", "scripted", "--trials", "1",
               "--output", str(tmp_path / "runs"), "--resumable"]
    scripted_model(monkeypatch, Scripted([draft_call(), done()]))
    assert await execute(parser().parse_args(command)) == 3
    original = next(p.parent for p in (tmp_path / "runs").glob("*/manifest.json")
                    if json.loads(p.read_text()).get("backend_metrics"))
    err = capsys.readouterr().err
    assert "Trial 1/1 of direct_draft blocked" in err and "at execution: Scenario interrupted" in err
    scripted_model(monkeypatch, Scripted([]), LabError("Provider preflight: rate limit (HTTP 429): Slow down"))
    assert await execute(parser().parse_args([*command, "--resume", str(original)])) == 2
    err = capsys.readouterr().err
    assert "Model preflight failed (setup/infrastructure) at model-preflight: Provider preflight: rate limit" in err
    assert "Resumed trial failed (setup/infrastructure) at model-preflight" in err and str(original) in err


def test_diff_accepts_repeatable_ignore_pointers():
    args = parser().parse_args(["diff", "runs/a", "runs/b", "--ignore", "/run_id", "--ignore", "/checks/0/detail"])
    assert args.ignore == ["/run_id", "/checks/0/detail"]
    assert parser().parse_args(["diff", "runs/a", "runs/b"]).ignore == []


def test_an_unavailable_configured_default_names_its_field(tmp_path):
    config = Config(defaults={"interface_profile": "api-tools-v1"})
    args = Namespace(command="run", surface="api-tools", interface_profile=None, profile=None)
    with pytest.raises(Unsupported, match="defaults.interface_profile: .*api-tools-v1.*api-tools-surrogate-v1"):
        select_interface_profile(args, config, None)


async def test_agent_drivers_warn_about_unverified_profiles_before_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    scripted_config(tmp_path, model_profile="scripted")
    dispatched = []

    async def surface_stub(args):
        dispatched.append(args.command)
        return 0

    monkeypatch.setattr("consumer_agent_testing_tool.surface_host.run_from_cli", surface_stub)
    assert await execute(parser().parse_args(["surface-run", "scenario.yaml", "--driver", "agent",
                                              "--config", "config.json"])) == 0
    assert dispatched == ["surface-run"]
    assert "warning: model profile 'scripted' (openai-compatible test) is specified/unverified" in \
        capsys.readouterr().err


async def test_inspect_keeps_a_configured_mcp_default_for_the_fixture(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(Config(defaults={"interface_profile": "mcp-contract-2025-11-25"}).model_dump_json())
    assert await execute(parser().parse_args(["inspect", "booking-service", "--config", str(path),
                                              "--output", str(tmp_path / "runs")])) == 0
    manifest = manifests(tmp_path / "runs")[0]
    assert manifest["run_lock"]["interface_profile"]["name"] == "mcp-contract-2025-11-25"
    # Inspected over the fixture's MCP server, not its CLI/skill surface.
    assert manifest["run_lock"]["negotiated_protocol_revision"] == "2025-11-25"


def test_init_help_names_the_live_adapters(capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["init", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Live adapters: bedrock, meta, openrouter;" in help_text and "--development-extensions" in help_text
