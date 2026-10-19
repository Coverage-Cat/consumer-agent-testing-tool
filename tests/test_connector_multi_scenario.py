import sys
import textwrap

import pytest
from test_connector_multi_support import pine_connector, two_connectors
from test_connector_support import result_of, run_connector, scenario

import consumer_agent_testing_tool
from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    ScriptedDriver,
    discover,
    make_extensions,
)
from consumer_agent_testing_tool.connector_scenario import ConnectorScenario, parse_scenario
from consumer_agent_testing_tool.errors import LabError


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(expected={"tools": ["get_quote"]}),
     "expected.tools: with several connectors, name each tool as <connector>/<tool> ('get_quote')"),
    (lambda d: d.update(expected={"tools": ["elsewhere/get_quote"]}), "expected.tools: with several connectors"),
    (lambda d: d["permissions"].update(important_reads=["get_quote"]), "permissions.important_reads: with several"),
    (lambda d: d["permissions"].update(persisted_always=["cedar-stays/"]), "permissions.persisted_always: with"),
    (lambda d: d["permissions"].update(persisted_always=["pine-lodge/create_booking"]),
     "permissions.persisted_always cannot include sensitive writes: create_booking"),
    (lambda d: d["checks"].append({"id": "status", "source": "tool", "tool": {
        "name": "get_booking", "arguments": {"booking_id": "BK-1"}, "pointer": "/structuredContent/status"},
        "expect": {"op": "eq", "value": "confirmed"}}), "checks.status.connector: with several connectors"),
    (lambda d: d["connectors"][1].update(exposure="hidden"),
     "Input should be 'in-prompt', 'search-only' or 'disconnected'"),
    (lambda d: d["connectors"][1].update(name="cedar-stays"), "connector names must be unique"),
    (lambda d: d.update(reset=[]), "reset lists distinct URLs"),
    (lambda d: d.update(reset=["http://127.0.0.1:8765/r", "http://127.0.0.1:8765/r"]), "reset lists distinct URLs"),
    (lambda d: d.update(reset=["http://cedar.example/reset"]), "must use https"),
])
def test_several_connectors_name_whose_tools_scenario_rules_mean(change, message):
    document = two_connectors()
    change(document)
    with pytest.raises(LabError, match="Invalid connector scenario") as raised:
        parse_scenario(document)
    assert message in str(raised.value)


def test_a_valid_two_connector_scenario_scopes_its_tool_lists_per_connector():
    document = two_connectors(pine={"exposure": "disconnected"})
    document["permissions"].update(important_reads=["pine-lodge/get_quote"], persisted_always=["cedar-stays/get_quote"])
    document["checks"].append({"id": "pine-status", "source": "tool", "connector": "pine-lodge", "tool": {
        "name": "get_booking", "arguments": {"booking_id": {"$ref": "/captured/pine_booking_id"}},
        "pointer": "/structuredContent/status"}, "expect": {"op": "eq", "value": "confirmed"}})
    loaded = parse_scenario(document).scenario
    assert loaded.connectors[1].exposure == "disconnected" and loaded.connectors[0].exposure is None
    assert loaded.scoped(loaded.permissions.important_reads, "pine-lodge") == ["get_quote"]
    assert loaded.scoped(loaded.permissions.important_reads, "cedar-stays") == []
    assert loaded.scoped(loaded.expected.tools, "cedar-stays") == ["get_quote", "create_booking"]
    # One connector: entries are tool names as written (P1 scenarios are unchanged).
    single = parse_scenario(scenario()).scenario
    assert single.scoped(["get_quote"], "anything") == ["get_quote"] and single.resets() == []
    assert parse_scenario(scenario(reset="http://127.0.0.1:8765/__oracle/reset")).scenario.resets() == [
        "http://127.0.0.1:8765/__oracle/reset"]


async def test_tool_lists_name_tools_each_connector_must_list(tmp_path):
    from test_connector_multi_support import multi_services
    document = two_connectors(expected={"tools": ["pine-lodge/book_room"]})
    run, code, _ = await run_connector(tmp_path, document, services_=multi_services(),
                                       driver=ScriptedDriver([{"answer": "x"}]))
    assert code == 1 and result_of(run)["detail"] == "The scenario names tools the pine-lodge server does not list: " \
                                                    "book_room"


def write_module(directory, name, body):
    path = directory / f"ext_{name}.py"
    path.write_text(textwrap.dedent(body))
    return path


@pytest.fixture
def extension_dir(tmp_path, monkeypatch):
    """A directory on the package path for ext_<name> modules this test writes; cleaned up after."""
    directory = tmp_path / "extensions"
    directory.mkdir()
    monkeypatch.setattr(consumer_agent_testing_tool, "__path__", [*consumer_agent_testing_tool.__path__,
                                                                  str(directory)])
    before = set(EXTENSIONS)
    yield directory
    for name in set(EXTENSIONS) - before:
        EXTENSIONS.pop(name)
    for module in [name for name in sys.modules if name.startswith("consumer_agent_testing_tool.ext_multi_")]:
        sys.modules.pop(module)


FOUND = """
    from pydantic import BaseModel, ConfigDict

    from consumer_agent_testing_tool.connector_run import BaseExtension, register


    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        greeting: str = "hello"


    class Found(BaseExtension):
        config_model = Config
        private_names = ("found_token",)

        def system_context(self, run):
            return [f"Discovered says {self.config.greeting}."]


    register("multi-found", Found)
"""


async def test_an_unregistered_extension_is_imported_from_its_ext_module(tmp_path, extension_dir):
    write_module(extension_dir, "multi_found", FOUND)
    assert "multi-found" not in EXTENSIONS
    document = scenario(checks=[], answer=None, extensions={"multi-found": {"greeting": "hi"}})
    # Its private names may be referenced before the module registers anything at run time.
    document["connectors"][0]["tools"]["host_arguments"]["create_booking"]["idempotency_key"] = {
        "$ref": "/private/found_token"}
    run, code, _ = await run_connector(tmp_path, document, driver=ScriptedDriver([{"answer": "Nothing booked."}]))
    assert code == 0, run.summary()
    assert "multi-found" in EXTENSIONS and "Discovered says hi." in run.system_prompt()
    assert [item["name"] for item in result_of(run)["run_lock"]["extensions"]] == ["clock", "multi-found"]
    with pytest.raises(LabError, match=r"Invalid extensions.multi-found: greeting"):
        parse_scenario(scenario(extensions={"multi-found": {"greeting": 3}}))


def test_make_extensions_discovers_too(extension_dir):
    write_module(extension_dir, "multi_late", FOUND.replace("multi-found", "multi-late"))
    document = scenario(extensions={"multi-late": {}})
    loaded = ConnectorScenario.model_validate(document, context={"provided_private": {"found_token"}})
    assert [extension.name for extension in make_extensions(loaded)] == ["clock", "multi-late"]


@pytest.mark.parametrize("body, message", [
    ("", "Extension module consumer_agent_testing_tool.ext_multi_odd does not register extension 'multi-odd'"),
    ("raise RuntimeError('boom')", r"ext_multi_odd failed to import \(RuntimeError: boom\)"),
    ("import definitely_missing_dependency_xyz", "ext_multi_odd cannot import definitely_missing_dependency_xyz"),
])
def test_an_extension_module_that_does_not_register_cleanly_is_a_clear_error(extension_dir, body, message):
    write_module(extension_dir, "multi_odd", body)
    with pytest.raises(LabError, match=message):
        parse_scenario(scenario(extensions={"multi-odd": {}}))


def test_only_the_shared_registry_discovers_modules(extension_dir):
    write_module(extension_dir, "multi_private", FOUND.replace("multi-found", "multi-private"))
    own = {"clock": EXTENSIONS["clock"]}
    with pytest.raises(LabError) as raised:
        parse_scenario(scenario(extensions={"multi-private": {}}), registry=own)
    assert "Unknown scenario extension 'multi-private'; registered: clock" in str(raised.value)
    assert "no module" not in str(raised.value) and "multi-private" not in EXTENSIONS
    assert discover("Not A Name") is None and discover("multi-absent") is None


def test_disconnected_is_a_connector_exposure_only():
    with pytest.raises(LabError, match="Input should be 'in-prompt' or 'search-only'"):
        parse_scenario(scenario(exposure="disconnected"))
    document = scenario()
    document["connectors"][0]["exposure"] = "disconnected"
    assert parse_scenario(document).scenario.connectors[0].exposure == "disconnected"
    assert pine_connector()["name"] == "pine-lodge"


def test_an_unset_connector_exposure_keeps_a_p1_scenario_s_digest():
    from consumer_agent_testing_tool.connector_scenario import _masked
    from consumer_agent_testing_tool.serialization import digest
    loaded = parse_scenario(scenario())
    document = loaded.scenario.model_dump(mode="json", by_alias=True)
    document["private"] = _masked(document["private"])
    for connector in document["connectors"]:
        # The connector fields added after P1 (exposure, then limits and output_schema), all unset here.
        for key in ("exposure", "limits", "output_schema"):
            assert connector.pop(key) is None
    assert loaded.sha256 == digest(document)  # What P1 published for the same file.
    declared = scenario()
    declared["connectors"][0]["exposure"] = "in-prompt"
    assert parse_scenario(declared).sha256 != loaded.sha256  # A declared exposure is part of the scenario.
