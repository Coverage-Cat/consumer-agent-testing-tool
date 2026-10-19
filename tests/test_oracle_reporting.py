"""Independent backend checks must survive completion, interruption and playback."""
import json
from pathlib import Path

import pytest
from inspect_ai.model import Model, ModelAPI, ModelOutput

from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, ModelProfile, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.fixture import Fixture, oracle_metrics
from consumer_agent_testing_tool.providers import CapturingAPI, generation
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.runner import run_scenario
from consumer_agent_testing_tool.scenario import read_scenario


async def cli_trial(tmp_path, monkeypatch, *, effect=None, expected=None, invalid="valid", interrupt=False,
                    traffic="absent", network_required=False, finality="confirmed"):
    """Actual Inspect composition and SQLite effects; shell isolation remains a double."""
    fixture = Fixture(tmp_path / "fixture.sqlite", "oracle-reporting")

    class Boundary:
        image_id = "test-double"

        def __init__(self, image, run_id, *args):
            self.run_id = run_id

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def isolation_probe(self):
            return {"passed": True, "test_double": True}

        async def cli_identity(self):
            return {"test_double": True}

        async def quiesce(self):
            if finality == "error":
                raise LabError("Synthetic finalization failure")
            if finality != "confirmed":
                return finality
            return {"run_id": self.run_id, "agent_stopped": True, "public_requests_drained": True,
                    "canary_requests_drained": True, "test_double": True}

        async def oracle(self, operation="snapshot", **parameters):
            if operation == "reset":
                return fixture.reset(**parameters)
            snapshot = fixture.snapshot()
            if traffic != "absent":
                snapshot["canary_connections"] = traffic
            return snapshot

        async def shell(self, command):
            if effect:
                # Deliberately defective target ledger; independently queried below.
                with fixture.connect() as db:
                    for _ in range(2 if effect == "duplicate" else 1):
                        db.execute("INSERT INTO effects(run,principal,operation,key,reservation_id,effect) "
                                   "VALUES (?,?,?,?,?,?)", (fixture.run_id, "alice", "test.effect",
                                    "once", "test", "draft" if effect == "duplicate" else "commit"))
            return {"stdout": "Synthetic target claims success", "stderr": "", "returncode": 0}

    class Agent(ModelAPI):
        step = 0

        async def generate(self, *args):
            self.step += 1
            if self.step == 1 or interrupt:
                return ModelOutput.for_tool_call(self.model_name, "shell", {"command": "booking"},
                                                 tool_call_id=f"effect-{self.step}")
            return ModelOutput.for_tool_call(self.model_name, "submit", {"answer": "Observed target result"})

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="test",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="cli-skill", label="scripted composition",
        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={}, scenario_id="compare_slots")
    model = Model(CapturingAPI(Agent("test/oracle-reporting"), evidence), generation(profile))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.Sandbox", Boundary)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    if invalid != "valid":
        def invalid_metrics(snapshot):
            metrics = oracle_metrics(snapshot)
            metrics["backend.unauthorized_effect_count"] = invalid
            return metrics
        monkeypatch.setattr("consumer_agent_testing_tool.runner.oracle_metrics", invalid_metrics)
    spec = read_scenario(Path("src/consumer_agent_testing_tool/data/scenarios/compare_slots.yaml"))
    identifier = f"backend.{effect or 'unauthorized'}_effect_count"
    spec["assertions"] = [] if expected is None else [{"scorer": identifier, "expected": expected}]
    spec["forbidden"] = ["production_network"] if network_required else []
    if interrupt:
        spec["limits"]["tool_calls"] = 1
        with pytest.raises(LabError, match="tool-call budget exceeded"):
            await run_scenario(spec, Config(), profile, evidence)
        checks = evidence.manifest["checks"]
    else:
        checks = await run_scenario(spec, Config(), profile, evidence)
    evidence.finish(statuses(checks)[1], checks)
    return evidence, checks, fixture


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("traffic,expected", [
    ("absent", None), (None, None), (False, None), ([], True), ([{"peer": "127.0.0.1"}], False),
])
async def test_cli_preserves_network_observations_in_normal_and_interrupted_execution(
        tmp_path, monkeypatch, interrupt, traffic, expected):
    evidence, checks, _ = await cli_trial(tmp_path, monkeypatch, interrupt=interrupt,
                                         traffic=traffic, network_required=True)
    check = next(row for row in checks if row["id"] == "isolation.no_forbidden_traffic")
    assert check["actual"] is expected
    assert check["status"] == ("observation-gap" if expected is None else "passed" if expected else "failed")
    assert evidence.manifest["required_invariants"]["isolation.no_forbidden_traffic"] is True
    if expected is False:
        assert evidence.manifest["observed_invariant_failures"]["isolation.no_forbidden_traffic"]["actual"] is False


@pytest.mark.parametrize("effect", ["unauthorized", "duplicate"])
@pytest.mark.parametrize("expected", [None, 1])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_cli_independent_effect_failure_survives_omission_and_interruption(
        tmp_path, monkeypatch, effect, expected, interrupt):
    evidence, checks, fixture = await cli_trial(
        tmp_path, monkeypatch, effect=effect, expected=expected, interrupt=interrupt)
    with fixture.connect() as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == (2 if effect == "duplicate" else 1)
    identifier = f"backend.{effect}_effect_count"
    matches = [row for row in checks if row["id"] == identifier]
    assert len(matches) == 1 and matches[0]["actual"] == 1 and matches[0]["expected"] == 0
    assert matches[0]["status"] == "failed"
    assert matches[0]["observation_event"]
    assert statuses(checks) == (1, "failed")
    exported = export_redacted(evidence.path, tmp_path / "export")
    public = json.loads((exported / "manifest.json").read_text())
    assert public["observed_invariant_failures"][identifier]["actual"] == 1
    assert public["execution_status"] == "failed"


@pytest.mark.parametrize("invalid", [False, 0.0, -1, None, "0"])
async def test_cli_invalid_effect_observation_never_establishes_zero(tmp_path, monkeypatch, invalid):
    _, checks, _ = await cli_trial(tmp_path, monkeypatch, expected=0, invalid=invalid)
    assert statuses(checks) == (3, "blocked")
    assert next(row for row in checks if row["id"] == "backend.unauthorized_effect_count")[
        "status"] == "observation-gap"


@pytest.mark.parametrize("oracle", ["unavailable", "boolean-failed", "boolean-missing", "boolean-passed"])
async def test_browser_backend_verification_survives_playback_and_export(tmp_path, monkeypatch, oracle):
    from test_browser_scenario import run

    from consumer_agent_testing_tool import api_scenario

    original = api_scenario.oracle_metrics

    def broken(snapshot):
        if oracle == "unavailable":
            raise RuntimeError("Synthetic unavailable oracle")
        metrics = original(snapshot)
        if oracle == "boolean-missing":
            metrics.pop("backend.read_performed")
        return metrics

    monkeypatch.setattr(api_scenario, "oracle_metrics", broken)
    steps = [("click", "Load afternoon availability"), ("see", "Afternoon:")] if oracle == "boolean-passed" else []
    evidence, _, checks = await run(tmp_path, steps,
        assertions=[{"scorer": "backend.read_performed", "expected": True}], forbidden=[])
    expected = {"boolean-failed": "failed", "boolean-passed": "completed"}.get(oracle, "blocked")
    assert statuses(checks)[1] == expected
    child = playback(evidence.path / "browser")["result"]
    assert child["status"] == expected
    assert child["execution_status"] == "completed"
    assert child["reporting_status"] == "completed"
    outcome = {"backend.read_performed": True} if oracle == "boolean-passed" else None
    assert child["verified_service_outcome"] == outcome
    identifier = "backend.oracle" if oracle == "unavailable" else "backend.read_performed"
    assert any(row["id"] == identifier for row in child["checks"])
    exported = export_redacted(evidence.path, tmp_path / "export")
    public = playback(exported / "browser")["result"]
    for field in ("status", "execution_status", "reporting_status", "verified_service_outcome", "checks"):
        assert public[field] == child[field]


@pytest.mark.parametrize("violation,error_code,expected_code", [(True, 3, 1), (True, 2, 1), (False, 2, 2)])
async def test_cli_execution_error_keeps_independent_checks(
        tmp_path, monkeypatch, violation, error_code, expected_code):
    from consumer_agent_testing_tool.cli import execute, parser

    config = Config(model_profiles={"scripted": ModelProfile(
        provider="local-glimmer", api="chat-completions", model_id="test",
        base_url="http://127.0.0.1:8080/v1")})
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())

    async def preflight(*args):
        return [{"id": "synthetic-preflight", "status": "passed"}]

    async def interrupted(scenario, config, profile, evidence, **kwargs):
        evidence.manifest["checks"] = [{
            "id": "backend.unauthorized_effect_count", "actual": int(violation), "expected": 0,
            "status": "failed" if violation else "passed", "class": "verification"}]
        raise LabError("Synthetic post-effect interruption", code=error_code)

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.runner.run_scenario", interrupted)
    args = parser(development=True).parse_args([
        "run", "src/consumer_agent_testing_tool/data/scenarios/compare_slots.yaml", "--surface", "cli-skill",
        "--interface-profile", "muse-cli-skills-public-2026-09", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "1", "--output", str(tmp_path / "runs")])
    assert await execute(args) == expected_code
    manifest = next(json.loads(path.read_text()) for path in (tmp_path / "runs").glob("*/manifest.json")
                    if json.loads(path.read_text()).get("scenario_id") == "compare_slots")
    assert manifest["execution_status"] == "failed"
    assert any(row["id"] == "backend.unauthorized_effect_count" for row in manifest["checks"])
