
import pytest

from consumer_agent_testing_tool.cli import initialize, parser
from consumer_agent_testing_tool.config import Constraint, read_config, read_openrouter_profile
from consumer_agent_testing_tool.errors import LabError


@pytest.mark.parametrize("arguments,provider", [
    (["--provider", "none"], None),
    (["--provider", "bedrock", "--auth", "api_key", "--region", "us-east-1", "--model",
      "arn:aws:bedrock:us-east-1:123456789012:inference-profile/example"], "bedrock"),
    (["--provider", "bedrock", "--auth", "aws_credentials", "--aws-profile", "developer",
      "--region", "us-east-1", "--model", "exact-id"], "bedrock"),
    (["--provider", "local-glimmer", "--base-url", "http://127.0.0.1:8080/v1",
      "--model", "muse-glimmer"], "local-glimmer"),
    (["--provider", "openai", "--model", "exact-id"], "openai"),
    (["--provider", "anthropic", "--model", "exact-id"], "anthropic"),
    (["--provider", "gemini", "--model", "exact-id"], "gemini"),
    (["--provider", "azure-openai", "--endpoint", "https://example.openai.azure.com",
      "--deployment", "exact-deployment", "--api", "responses"], "azure-openai"),
    (["--provider", "vertex-gemini", "--project", "example", "--region", "us-central1",
      "--model", "exact-id"], "vertex-gemini"),
    (["--provider", "meta", "--model", "exact-id"], "meta"),
    (["--provider", "openrouter", "--model", "publisher/model"], "openrouter"),
    (["--provider", "openai-compatible", "--base-url", "https://example.test/v1",
      "--key-env", "INFERENCE_API_KEY", "--model", "exact-id"], "openai-compatible"),
])
def test_provider_templates_are_valid_and_never_contain_secrets(tmp_path, monkeypatch, arguments, provider):
    monkeypatch.setenv("OPENAI_API_KEY", "should-never-be-written")
    path = tmp_path / "consumer-agent-testing-tool.yaml"
    # Template-only providers (no live adapter) and local Glimmer are development extensions.
    development = provider not in (None, "bedrock", "meta", "openrouter")
    options = parser(development=development).parse_args(["init", "--config", str(path), *arguments])
    initialize(options)
    config = read_config(path)
    if provider:
        profile = config.model_profiles[config.defaults.model_profile]
        assert profile.provider == provider
        assert profile.fallback == "disabled"
        if provider == "openrouter":
            assert profile.routing["only"] == ["together"]
            assert profile.routing["zdr"] is True
    else:
        assert not config.model_profiles
    assert "should-never-be-written" not in path.read_text()
    assert list((tmp_path / "api-scenarios").glob("*.yaml"))
    # CLI/skill scenarios are staged only for the development lane that can run them.
    assert bool(list((tmp_path / "scenarios").glob("*.yaml"))) == development


@pytest.mark.parametrize("model,route", [
    ("meta/muse-spark-open", "together"),
    ("meta/muse-spark-open", "phala"),
    ("meta/muse-glimmer-30b", "phala"),
    ("publisher/other-model", "phala"),
])
def test_init_does_not_inherit_old_live_provider_qualification(tmp_path, model, route):
    path = tmp_path / "router.yaml"
    initialize(parser().parse_args(["init", "--config", str(path), "--provider", "openrouter",
        "--model", model, "--routing-provider", route]))
    profile = read_openrouter_profile(path)
    assert profile.model_id == model
    assert profile.routing["only"] == [route]
    assert profile.routing["zdr"] is True
    assert profile.support == "specified/unverified"


def test_probe_settings_require_a_selected_openrouter_profile(tmp_path):
    import yaml

    path = tmp_path / "settings.yaml"
    initialize(parser().parse_args(["init", "--config", str(path), "--provider", "openrouter",
                                   "--model", "meta/muse-spark-open"]))
    config = read_config(path)
    profile = config.model_profiles["openrouter-dev"]
    profile.routing["zdr"] = False
    config.model_profiles["second"] = profile.model_copy(deep=True)
    config.defaults.model_profile = None
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    with pytest.raises(LabError, match="Select an existing"):
        read_openrouter_profile(path)
    with pytest.raises(LabError, match="Select an existing"):
        read_openrouter_profile(path, "missing")
    assert read_openrouter_profile(path, "second").routing["zdr"] is False
    config.model_profiles = {"router": profile}
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    assert read_openrouter_profile(path).routing["zdr"] is False
    config.model_profiles = {"other": profile.model_copy(update={"provider": "openai", "routing": None})}
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    with pytest.raises(LabError, match="requires an OpenRouter"):
        read_openrouter_profile(path)


def test_unknown_constraints_do_not_silently_become_false():
    with pytest.raises(ValueError):
        Constraint(value=False, basis="unknown", source=None, checked_at="2026-10-01",
                   evidence_gap="Unknown")
    assert Constraint(value=None, basis="unknown", source=None, checked_at="2026-10-01",
                      evidence_gap="Private host behavior").value is None


def test_init_rejects_placeholders_and_never_overwrites(tmp_path):
    path = tmp_path / "consumer-agent-testing-tool.yaml"
    options = parser().parse_args(["init", "--config", str(path), "--provider", "openai",
                                  "--model", "<model-id>"])
    with pytest.raises(ValueError):
        initialize(options)
    assert not path.exists()
    path.write_text("user contents")
    with pytest.raises(LabError):
        initialize(options)
    assert path.read_text() == "user contents"


@pytest.mark.parametrize("kind", ["fixture", "mcp"])
def test_unsupported_target_auth_is_rejected_instead_of_ignored(kind):
    from consumer_agent_testing_tool.config import Target

    fields = {"command": ["fixture-server"]} if kind == "mcp" else {}
    with pytest.raises(ValueError):
        Target(kind=kind, auth={"mode": "api_key", "env": "TARGET_AUTH"}, **fields)


@pytest.mark.parametrize("provider", ["bedrock", "local-glimmer"])
@pytest.mark.parametrize("setting", [{"endpoint": "https://ignored.test"},
                                    {"deployment": "ignored-deployment"}, {"routing": {"provider": "ignored"}}])
def test_mvp_profiles_reject_settings_that_would_not_reach_the_provider(provider, setting):
    from consumer_agent_testing_tool.config import ModelProfile

    fields = ({"api": "converse", "region": "us-east-1", "auth": {"mode": "aws_credentials"}}
              if provider == "bedrock" else
              {"api": "chat-completions", "base_url": "http://127.0.0.1:8080/v1"})
    with pytest.raises(ValueError, match="cannot be silently ignored"):
        ModelProfile(provider=provider, model_id="exact-fixture", **fields, **setting)


@pytest.mark.parametrize("base_url, where", [("http://127.0.0.1:8123/mcp", "local"), ("http://localhost:9/mcp", "local"),
                                             ("https://mcp.example.com/mcp", "remote"), (None, "local")])
def test_evidence_records_a_loopback_target_as_local(tmp_path, base_url, where):
    from consumer_agent_testing_tool.cli import make_evidence
    from consumer_agent_testing_tool.config import Target
    args = parser().parse_args(["check", "--suite", "contract", "--output", str(tmp_path / "runs")])
    target = Target(kind="mcp", transport="streamable-http", base_url=base_url,
                    permitted_destinations=[base_url]) if base_url else None
    evidence = make_evidence(args, {"name": "probe", "surface": "mcp", "label": "probe"}, None, target)
    # A local test server is no remote target (as the API lane records it).
    assert evidence.manifest["locations"]["target"] == where
