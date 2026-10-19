import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest
import yaml
from test_connector_support import (
    Hotel,
    TrialOutcome,
    native_script,
    scenario,
    services,
    trial_scenario,
)

from consumer_agent_testing_tool import connector_run
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_scenario import load_scenario
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.replay import playback

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "connector"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def fake_services(monkeypatch, hotel):
    made = services(hotel)
    monkeypatch.setattr(connector_run, "default_services", lambda: made)
    return made


def write(path, value):
    path.write_text(json.dumps(value))
    return path.name


async def test_connector_run_cli_scripted_smoke_prints_the_sealed_run(workspace, monkeypatch, capsys):
    hotel = Hotel()
    made = fake_services(monkeypatch, hotel)
    arguments = ["connector-run", write(workspace / "scenario.json", scenario()), "--driver", "scripted",
                 "--script", write(workspace / "calls.json", native_script()), "--output", "runs"]
    assert await execute(parser().parse_args(arguments)) == 0
    out, err = capsys.readouterr()
    run = Path(out.splitlines()[0])
    assert run.parent == workspace / "runs" and run.name.startswith("connector-")
    assert f"Connector report: {run / 'report.html'}" in out and "failed" not in err
    assert playback(run)["integrity"] == "verified"
    assert made.linked[0]["auth"] == {"mode": "api_key", "env": "CEDAR_STAYS_KEY", "placement": "bearer_header",
                                      "name": None}
    assert made.linked[0]["store"] == workspace / ".cache" / "connector-links"


async def test_connector_run_cli_reports_failed_trials_and_pass_hat_k(workspace, monkeypatch, capsys):
    fake_services(monkeypatch, Hotel(total_minor=61000))
    arguments = ["connector-run", write(workspace / "scenario.json", scenario()), "--driver", "scripted",
                 "--script", write(workspace / "calls.json", native_script()), "--trials", "2",
                 "--exposure", "in-prompt"]
    assert await execute(parser().parse_args(arguments)) == 1
    out, err = capsys.readouterr()
    assert "Connector trial 1/2 of fixture-booking failed" in err and "Connector trial 2/2" in err
    assert "Connector trials: 0 of 2 succeeded; pass^1 0.00, pass^2 0.00; exit 1. Summary: " in err
    suite = Path(out.splitlines()[-1])
    manifest = json.loads((suite / "manifest.json").read_text())
    assert manifest["lane"] == "connector trials" and len(manifest["child_runs"]) == 2
    assert manifest["metrics"]["pass_hat_k"] == {"1": 0.0, "2": 0.0}


async def test_connector_run_cli_names_unshareable_and_errored_trials_and_their_exit_code_wins(workspace, monkeypatch,
                                                                                                  capsys):
    monkeypatch.setitem(connector_run.EXTENSIONS, "probe-trials", TrialOutcome)
    fake_services(monkeypatch, Hotel())
    arguments = ["connector-run", write(workspace / "scenario.json", trial_scenario(["leak", "blocked", "error"])),
                 "--driver", "scripted", "--script", write(workspace / "calls.json", native_script()),
                 "--trials", "3"]
    # Trials exit 2, 3 and 2: the blocked sibling never hides the evidence that is not shareable.
    assert await execute(parser().parse_args(arguments)) == 2
    out, err = capsys.readouterr()
    assert "Connector trial 1/3 of fixture-booking failed (reporting) at reporting: 1 registered secret value(s)" in err
    assert "; not shareable: trial 1; setup, harness or verification error: trial 3; exit 2. Summary: " in err
    manifest = json.loads((Path(out.splitlines()[-1]) / "manifest.json").read_text())
    assert manifest["execution_status"] == "failed"
    assert (manifest["metrics"]["unshareable_trials"], manifest["metrics"]["error_trials"]) == ([1], [3])
    assert "--trials N" in parser()._subparsers._group_actions[0].choices["connector-run"].epilog


async def test_connector_run_cli_examples_and_treatment_flags(workspace, monkeypatch, capsys):
    fake_services(monkeypatch, Hotel())
    examples = shutil.copytree(EXAMPLES, workspace / "examples")  # Scenario paths stay inside the workspace.
    with pytest.raises(LabError, match=r"The scenario .*fixture-booking.yaml is outside the workspace"):
        await execute(parser().parse_args(["connector-run", str(EXAMPLES / "fixture-booking.yaml")]))
    arguments = ["connector-run", str(examples / "fixture-booking-cli.yaml"), "--driver", "scripted",
                 "--script", str(examples / "fixture-booking-cli.calls.json"), "--set", "PORT=8765"]
    assert await execute(parser().parse_args(arguments)) == 0, capsys.readouterr().err
    run = Path(capsys.readouterr().out.splitlines()[0])
    result = json.loads((run / "result.json").read_text())
    assert result["treatment"] == "muse-cli/in-prompt/muse-template-unofficial-2026-10"
    assert all(check["status"] == "passed" for check in result["checks"]), result["checks"]
    arguments = ["connector-run", "examples/fixture-booking.yaml", "--driver", "scripted",
                 "--script", "examples/fixture-booking.calls.json"]
    assert await execute(parser().parse_args(arguments)) == 0, capsys.readouterr().err


async def test_connector_run_cli_needs_a_script_or_a_model_profile(workspace, monkeypatch):
    fake_services(monkeypatch, Hotel())
    scenario_name = write(workspace / "scenario.json", scenario())
    with pytest.raises(LabError, match="needs --script"):
        await execute(parser().parse_args(["connector-run", scenario_name, "--driver", "scripted"]))
    (workspace / "consumer-agent-testing-tool.yaml").write_text("schema_version: 1\n")
    with pytest.raises(LabError, match="need a configured model profile"):
        await execute(parser().parse_args(["connector-run", scenario_name]))
    with pytest.raises(LabError, match="applies only to --driver scripted"):
        await execute(parser().parse_args(["connector-run", scenario_name, "--script", "calls.json"]))
    with pytest.raises(LabError, match="NAME=VALUE"):
        await execute(parser().parse_args(["connector-run", scenario_name, "--driver", "scripted",
                                           "--script", "calls.json", "--set", "PORT"]))


async def test_scaffold_writes_a_reviewable_skeleton_and_never_overwrites(workspace, monkeypatch, capsys):
    made = fake_services(monkeypatch, Hotel())
    arguments = ["scaffold", "http://127.0.0.1:8765/mcp", "--output", "cedar.yaml", "--auth-env", "CEDAR_KEY"]
    assert await execute(parser().parse_args(arguments)) == 0
    assert capsys.readouterr().out.strip() == str(workspace / "cedar.yaml")
    text = (workspace / "cedar.yaml").read_text()
    # Suggestions only: comments, so each trial classifies the live catalog.
    assert "#     create_booking: sensitive-write  # annotation: annotations.destructiveHint=true" in text
    document = yaml.safe_load(text)
    assert "classes" not in document["connectors"][0]["tools"]
    assert "#   candidates: create_booking.guest_email" in text  # A host-private value to move off the model.
    assert "#     tool: {name: get_booking, arguments: {booking_id: <value>}" in text  # A read-tool check.
    assert document["connectors"][0]["name"] == "cedar-stays"
    assert document["connectors"][0]["auth"] == {"mode": "api_key", "env": "CEDAR_KEY", "placement": "bearer_header"}
    assert document["connectors"][0]["tools"]["allow"][-1] == "cancel_booking"
    assert made.clients[0].closed is True and made.linked[0]["profile"] == "generic-sdk"
    with pytest.raises(LabError, match=r"TODO\(scaffold\)"):
        load_scenario(workspace / "cedar.yaml")
    with pytest.raises(LabError, match="never overwrites"):
        await execute(parser().parse_args(arguments))
    with pytest.raises(LabError, match="must use https"):
        await execute(parser().parse_args(["scaffold", "http://cedar.example/mcp", "--output", "x.yaml"]))
    with pytest.raises(LabError, match="need --auth-env"):
        await execute(parser().parse_args(["scaffold", "http://127.0.0.1:8765/mcp", "--output", "y.yaml",
                                           "--auth-placement", "custom_header"]))


async def test_scaffold_refuses_a_json_output_its_commented_yaml_could_not_load_from(workspace, monkeypatch):
    # connector-run reads a .json file as JSON only (serialization.load): a YAML skeleton there never loads.
    made = fake_services(monkeypatch, Hotel())
    with pytest.raises(LabError, match="scaffold writes a commented YAML scenario; name it .yaml"):
        await execute(parser().parse_args(["scaffold", "http://127.0.0.1:8765/mcp", "--output", "cedar.JSON"]))
    assert not (workspace / "cedar.JSON").exists() and made.clients == []


async def test_check_muse_guidelines_delegates_to_guidelines_run_check(workspace, monkeypatch):
    fake_services(monkeypatch, Hotel())
    received = []
    module = ModuleType("consumer_agent_testing_tool.guidelines")

    async def run_check(target_endpoint, *, auth, manifest_path, output, profile):
        # The CLI validates the linking.TargetAuth before the check starts.
        received.append({"endpoint": target_endpoint, "auth": auth.model_dump(mode="json", exclude_none=True),
                         "manifest": manifest_path, "output": output, "profile": profile.name})
        return 1
    module.run_check = run_check
    monkeypatch.setitem(sys.modules, "consumer_agent_testing_tool.guidelines", module)
    arguments = ["check", "https://cedar.example/mcp", "--suite", "muse-guidelines", "--manifest", "m.yaml",
                 "--auth-env", "CEDAR_KEY", "--auth-placement", "custom_header", "--auth-name", "X-API-Key"]
    assert await execute(parser().parse_args(arguments)) == 1
    # generic-sdk by default: the unofficial template stays opt-in for readiness too.
    assert received == [{"endpoint": "https://cedar.example/mcp",
                         "auth": {"mode": "api_key", "env": "CEDAR_KEY", "placement": "custom_header", "name": "X-API-Key"},
                         "manifest": workspace / "m.yaml", "output": workspace / "runs",
                         "profile": "generic-sdk"}]
    (workspace / "consumer-agent-testing-tool.yaml").write_text(yaml.safe_dump({"schema_version": 1, "targets": {
        "cedar": {"kind": "mcp", "transport": "streamable-http", "base_url": "https://cedar.example/mcp",
                  "permitted_destinations": ["https://cedar.example/mcp"], "auth": {"mode": "api_key", "env": "CEDAR_KEY"}}}}))
    assert await execute(parser().parse_args(["check", "cedar", "--suite", "muse-guidelines",
                                              "--client-profile", "generic-sdk"])) == 1
    assert received[-1]["endpoint"] == "https://cedar.example/mcp" and received[-1]["profile"] == "generic-sdk"
    assert received[-1]["auth"] == {"mode": "api_key", "env": "CEDAR_KEY", "placement": "bearer_header"}
    assert await execute(parser().parse_args(["check", "https://cedar.example:8443/mcp", "--suite",
                                              "muse-guidelines"])) == 1  # Any port under generic-sdk.
    # The template's port rule is a limit of that opt-in profile (unsupported), never a guidelines finding.
    with pytest.raises(Unsupported, match="unofficial .* client profile .*generic-sdk"):
        await execute(parser().parse_args(["check", "https://cedar.example:8443/mcp", "--suite", "muse-guidelines",
                                           "--client-profile", "muse-template-unofficial-2026-10"]))
    with pytest.raises(LabError, match="needs --endpoint URL"):
        await execute(parser().parse_args(["check", "booking-service", "--suite", "muse-guidelines"]))
    (workspace / "consumer-agent-testing-tool.yaml").unlink()
    with pytest.raises(LabError, match=r"needs the MCP endpoint: pass it as TARGET \(a URL\) or --endpoint"):
        await execute(parser().parse_args(["check", "--suite", "muse-guidelines"]))


def test_existing_check_suites_and_flags_still_parse():
    assert parser().parse_args(["check"]).suite == "contract"
    assert parser().parse_args(["check", "--suite", "static"]).suite == "static"
    options = parser().parse_args(["connector-run", "s.yaml"])
    assert (options.driver, options.trials, options.projection, options.output) == ("agent", 1, None, Path("runs"))
    with pytest.raises(SystemExit):
        parser().parse_args(["connector-run", "s.yaml", "--projection", "browser"])


def test_connector_run_validates_trials_and_lists_exit_codes(capsys):
    for value in ("0", "-2", "many"):
        with pytest.raises(SystemExit):
            parser().parse_args(["connector-run", "s.yaml", "--trials", value])
    assert "expected a positive integer" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parser().parse_args(["connector-run", "--help"])
    help_text = capsys.readouterr().out
    assert "exit codes:" in help_text and "3  blocked or unsupported" in help_text


async def test_scaffold_and_check_link_with_the_auth_block_of_a_scenario(workspace, monkeypatch):
    made = fake_services(monkeypatch, Hotel())
    oauth = scenario()
    oauth["connectors"][0]["auth"] = {"mode": "oauth", "scopes": ["stays:read"], "consent": "fixture-auto",
                                      "test_login_env": "CEDAR_LOGIN"}
    source = write(workspace / "linked.json", oauth)
    assert await execute(parser().parse_args(["scaffold", "http://127.0.0.1:8765/mcp", "--output", "o.yaml",
                                              "--auth-from", source])) == 0
    assert made.linked[-1]["auth"]["mode"] == "oauth" and made.linked[-1]["auth"]["scopes"] == ["stays:read"]
    assert yaml.safe_load((workspace / "o.yaml").read_text())["connectors"][0]["auth"]["mode"] == "oauth"
    with pytest.raises(LabError, match="does not combine with --auth-env"):
        await execute(parser().parse_args(["scaffold", "http://127.0.0.1:8765/mcp", "--output", "p.yaml",
                                           "--auth-from", source, "--auth-env", "KEY"]))


async def test_trials_without_a_reset_warn_that_they_share_service_state(workspace, monkeypatch, capsys):
    fake_services(monkeypatch, Hotel())
    arguments = ["connector-run", write(workspace / "scenario.json", scenario()), "--driver", "scripted",
                 "--script", write(workspace / "calls.json", native_script()), "--trials", "2"]
    await execute(parser().parse_args(arguments))
    assert "no reset URL in the scenario" in capsys.readouterr().err


async def test_a_missing_configuration_names_init_and_the_scripted_driver(workspace, monkeypatch):
    fake_services(monkeypatch, Hotel())
    with pytest.raises(LabError, match=r"run consumer-agent-testing-tool init .* --driver scripted --script"):
        await execute(parser().parse_args(["connector-run", write(workspace / "scenario.json", scenario())]))


class Fetching(connector_run.BaseExtension):
    """An extension tool with a url argument, as the custom connector's http_request has."""
    config_model = connector_run.NoConfig
    fetched: ClassVar[list] = []

    def tools(self, run):
        from inspect_ai.tool import ToolDef, ToolParam, ToolParams

        async def fetch_page(url: str):
            Fetching.fetched.append(url)
            return "ok"
        return [ToolDef(fetch_page, name="fetch_page", description="Fetch a page.", parallel=False, max_output=0,
                        parameters=ToolParams(properties={"url": ToolParam(type="string", description="Page.")},
                                              required=["url"]))]


async def test_set_variables_fill_the_script_s_url_fields_too(workspace, monkeypatch, capsys):
    monkeypatch.setitem(connector_run.EXTENSIONS, "probe-fetching", Fetching)
    fake_services(monkeypatch, Hotel())
    Fetching.fetched = []
    document = scenario(extensions={"probe-fetching": {}}, checks=[], answer=None)
    steps = [{"tool": "fetch_page", "arguments": {"url": "http://127.0.0.1:${PORT:-8790}/v1/rooms"}},
             {"tool": "fetch_page", "arguments": {"url": "http://127.0.0.1:${PORT:-8790}/v1/me"}},
             {"answer": "Checked port ${PORT}."}]
    arguments = ["connector-run", write(workspace / "scenario.json", document), "--driver", "scripted",
                 "--script", write(workspace / "calls.json", steps)]
    assert await execute(parser().parse_args([*arguments, "--set", "PORT=9123"])) == 0, capsys.readouterr().err
    assert Fetching.fetched == ["http://127.0.0.1:9123/v1/rooms", "http://127.0.0.1:9123/v1/me"]
    # URL fields only, as in the scenario: other text keeps ${...} as written.
    run = Path(capsys.readouterr().out.splitlines()[0])
    assert json.loads((run / "result.json").read_text())["answer"]["final_answer"] == "Checked port ${PORT}."
    Fetching.fetched = []
    assert await execute(parser().parse_args(arguments)) == 0  # The default applies without --set.
    assert Fetching.fetched == ["http://127.0.0.1:8790/v1/rooms", "http://127.0.0.1:8790/v1/me"]


async def test_scripted_runs_refuse_options_only_an_agent_run_reads(workspace, monkeypatch):
    fake_services(monkeypatch, Hotel())
    base = ["connector-run", write(workspace / "scenario.json", scenario()), "--driver", "scripted",
            "--script", write(workspace / "calls.json", native_script())]
    for extra, flags in ((["--model-profile", "does-not-exist"], "--model-profile"),
                         (["--config", "nope.yaml"], "--config"),
                         (["--model-profile", "x", "--config", "nope.yaml"], "--model-profile and --config")):
        # Never silently ignored: a replay with a model profile named is not the agent run it looks like.
        with pytest.raises(LabError, match=f"^{flags} applies only to --driver agent"):
            await execute(parser().parse_args([*base, *extra]))
    assert await execute(parser().parse_args(base)) == 0
