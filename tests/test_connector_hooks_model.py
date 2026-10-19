"""Two documented extension seams: run.model (the agent's injected Inspect Model, read only) and an extension's own
permission engine, whose metrics land under metrics.approval.extensions.<name> beside the connectors' sum."""
from typing import ClassVar

from test_connector_support import (
    BaseExtension,
    agent,
    booking_responses,
    result_of,
    run_connector,
    scenario,
)

from consumer_agent_testing_tool.config import ModelProfile
from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    AgentDriver,
    ConnectorRun,
    NoConfig,
    ScriptedDriver,
)
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.permissions import (
    Classification,
    PermissionEngine,
    PermissionPolicy,
)


class Seen(BaseExtension):
    """Records what run.model was while the agent ran."""
    config_model = NoConfig
    models: ClassVar[list] = []

    def tools(self, run):
        Seen.models.append(run.model)
        return []


async def test_run_model_is_the_injected_test_model_and_none_otherwise(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-seen", Seen)
    monkeypatch.setattr(Seen, "models", [])
    driver, _api = agent(booking_responses())
    await run_connector(tmp_path, scenario(extensions={"probe-seen": {}}), driver=driver)
    await run_connector(tmp_path, scenario(extensions={"probe-seen": {}}))  # Scripted: no model.
    assert Seen.models == [driver.model, None]
    profile = ModelProfile(provider="openrouter", api="chat-completions", model_id="example/agent",
                           auth={"mode": "api_key", "env": "OPENROUTER_API_KEY"})
    configured = ConnectorRun(parse_scenario(scenario()), driver=AgentDriver(model_profile=profile), root=tmp_path,
                              output=tmp_path / "runs")
    assert configured.model is None and configured.model_profile is profile  # A profile's model is resolved later.
    scripted = ConnectorRun(parse_scenario(scenario()), driver=ScriptedDriver([{"answer": "x"}]), root=tmp_path,
                            output=tmp_path / "runs")
    assert scripted.model is None


class Approver(BaseExtension):
    """An extension with its own approval engine (as custom_connector and browser keep one), used once."""
    config_model = NoConfig

    def tools(self, run):
        self.engine = PermissionEngine(PermissionPolicy(rules=[{"id": "yes", "decision": "allow_once", "when": {
            "field": "tool", "op": "exists", "value": True}}]), {"share": Classification(
                tool="share", tool_class="write", basis="declared", reasons=["declared"])}, "approver")
        self.engine.request("share", {"what": "itinerary"})
        return []


async def test_extension_engines_report_under_metrics_approval_extensions(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-approver", Approver)
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-approver": {}}))
    assert code == 0, run.summary()
    approval = result_of(run)["metrics"]["approval"]
    assert approval["extensions"] == {"probe-approver": {
        "prompts": {"read": 0, "write": 1, "sensitive-write": 0},
        "decisions": {"allow_once": 1, "allow_task": 0, "allow_site": 0, "always_allow": 0, "deny": 0, "edit": 0,
                      "no_response": 0},
        "auto_allowed": {"read": 0, "write": 0, "sensitive-write": 0}, "redundant_prompts": 0, "coerced_scopes": 0}}
    # Beside the connectors' own sum, never in it: the connector's prompts stay its own.
    assert approval["prompts"] == {"read": 2, "write": 0, "sensitive-write": 1}
    plain, code, _ = await run_connector(tmp_path)
    assert "extensions" not in result_of(plain)["metrics"]["approval"]
