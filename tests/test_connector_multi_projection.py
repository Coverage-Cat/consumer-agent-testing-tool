import copy

import pytest
from test_connector_support import CATALOG

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.projection import (
    DEFERRED_PARAMETERS,
    PROVIDER_NAME,
    CliCommand,
    CliError,
    ConnectorView,
    Projection,
)

TOOLS = [tool for tool in CATALOG if tool["name"] != "cancel_booking"]


def view(name, exposure=None, tools=None, **options):
    return ConnectorView(name, copy.deepcopy(tools or TOOLS), {"create_booking": ["guest_email"]},
                         f"{name} rooms.", options.pop("doc", None), exposure)


def test_a_run_with_several_connectors_prefixes_from_the_start_and_later_names_never_shadow():
    projection = Projection("native", "in-prompt", [view("cedar-stays")], prefix=True,
                            reserved={"cedar-stays__get_booking"})
    names = list(projection.native)
    assert names[:3] == ["cedar-stays__search_rooms", "cedar-stays__get_quote", "cedar-stays__create_booking"]
    assert names[3].startswith("cedar-stays__get_booking_") and all(PROVIDER_NAME.fullmatch(name) for name in names)
    added = projection.add(view("pine-lodge"), reserved={"pine-lodge__search_rooms"})
    assert added[0].startswith("pine-lodge__search_rooms_") and added[1:] == [
        "pine-lodge__get_quote", "pine-lodge__create_booking", "pine-lodge__get_booking"]
    assert projection.native_tool("pine-lodge__get_quote") == ("pine-lodge", "get_quote")
    assert projection.model_names("pine-lodge")["pine-lodge__get_quote"] == "get_quote"
    record = projection.record()
    assert record["name_map"]["pine-lodge__get_quote"] == {"connector": "pine-lodge", "tool": "get_quote"}
    assert record["connectors"]["pine-lodge"]["exposure"] == "in-prompt"
    assert record["basis"]["connector-prefix"]["basis"] == "assumed"
    with pytest.raises(LabError, match="already projected"):
        projection.add(view("pine-lodge"))
    # One view and no prefix: P1's names and record, exactly.
    single = Projection("native", "in-prompt", [view("cedar-stays")])
    assert next(iter(single.native)) == "search_rooms" and "exposure" not in single.record()["connectors"]["cedar-stays"]
    assert "connector-prefix" not in single.record()["basis"]


def test_a_refused_view_leaves_the_projection_as_it_was():
    projection = Projection("native", "in-prompt", [view("cedar-stays")], prefix=True)
    before = (dict(projection.native), set(projection.loaded), dict(projection.entries), dict(projection.exposures))
    closed = ConnectorView("pine-lodge", [copy.deepcopy(CATALOG[3])], {"get_booking": ["guest_email"]})
    with pytest.raises(LabError, match="does not declare and does not allow"):
        projection.add(closed)
    many = [{"name": f"tool_{index}", "inputSchema": {"type": "object"}} for index in range(125)]
    with pytest.raises(LabError, match="The connectors offer 129 tools; providers accept at most 128"):
        projection.add(ConnectorView("many", many))
    assert (projection.native, projection.loaded, projection.entries, projection.exposures) == before
    assert "pine-lodge" not in projection.connectors and "many" not in projection.connectors


def test_each_view_keeps_its_own_exposure():
    projection = Projection("native", "in-prompt", [view("cedar-stays"), view("pine-lodge", "search-only")])
    assert projection.searchable and projection.specs()[0]["name"] == "tool_search"
    specs = {spec["name"]: spec for spec in projection.specs()}
    assert specs["cedar-stays__get_quote"]["parameters"]["properties"]
    assert specs["pine-lodge__get_quote"]["parameters"] == DEFERRED_PARAMETERS
    found = projection.search("get_quote")
    assert found["loaded"] == ["pine-lodge__get_quote"] and [row["service"] for row in found["results"]] == [
        "pine-lodge"]
    assert projection.native_tool("pine-lodge__get_quote") == ("pine-lodge", "get_quote")
    assert projection.record()["basis"]["search-only"]["basis"] == "documented"
    # muse-cli: an in-prompt service is reachable at once, a search-only one after tool_search.
    cli = Projection("muse-cli", "in-prompt", [view("cedar-stays", doc="# Cedar"), view("pine-lodge", "search-only",
                                                                                        doc="# Pine")])
    assert cli.parse("cedar-stays list-tools") == CliCommand("list-tools", connector="cedar-stays")
    assert cli.parse("pine-lodge list-tools") == CliError(127, "exec: pine-lodge: command not found")
    assert cli.document("pine-lodge.md") is None and cli.document("cedar-stays.md")[0] == "cedar-stays"
    assert cli.search("rooms")["loaded"] == ["pine-lodge"] and cli.reachable("pine-lodge")
    assert cli.add(view("birch-inn")) == ["birch-inn"] and cli.reachable("birch-inn")
    assert [spec["name"] for spec in cli.specs()] == ["tool_search", "exec"]
    # No search-only connector: no tool_search, even when the default is search-only for later ones.
    later = Projection("native", "search-only", [view("cedar-stays", "in-prompt")], prefix=True)
    assert not later.searchable and later.add(view("pine-lodge")) and later.searchable
    assert later.exposures == {"cedar-stays": "in-prompt", "pine-lodge": "search-only"}
    with pytest.raises(LabError, match="Unknown exposure 'disconnected'"):
        later.add(view("birch-inn", "disconnected"))
