"""The classification sheet: MCPJam's Muse readiness row shape with the connector lane's classifier.

Format source: MCPJam/inspector sdk/src/muse-readiness/classification.ts at
da3b8ac30d2cc914be838637b5422bd66f6dd514 (MuseClassificationRow, formatMuseClassificationSheet).
"""
import sys

import pytest
from test_guidelines_support import CEDAR, READ, SENSITIVE, fake_classify, tool

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.guidelines import (
    MCPJAM_SHEET,
    classification_sheet,
    sheet_markdown,
)

MCPJAM_KEYS = {"tool", "suggested", "basis", "reasons"}  # plus "declared" when the submitter supplied one
MCPJAM_BASES = {"annotation", "combined-operations", "default"}


def test_rows_have_mcpjam_s_shape_in_listing_order():
    rows = classification_sheet(CEDAR, {"create_booking": "sensitive-write"}, classify=fake_classify)
    assert [row["tool"] for row in rows] == [entry["name"] for entry in CEDAR]
    for row in rows:
        assert set(row) == MCPJAM_KEYS | ({"declared"} if row["tool"] == "create_booking" else set())
        assert row["suggested"] in ("read", "write", "sensitive-write") and row["basis"] in MCPJAM_BASES | {"heuristic"}
        assert row["reasons"] and all(isinstance(reason, str) for reason in row["reasons"])
    assert rows[2]["declared"] == "sensitive-write"
    assert "da3b8ac30d2cc914be838637b5422bd66f6dd514" in MCPJAM_SHEET


def test_the_suggestion_ignores_the_declaration_so_disagreement_stays_visible():
    rows = classification_sheet(CEDAR[:1], {"search_rooms": "sensitive-write"}, classify=fake_classify)
    assert rows[0]["suggested"] == "read" and rows[0]["declared"] == "sensitive-write"


def test_lane_bases_map_onto_mcpjam_s_and_heuristic_is_the_labelled_extension():
    entries = [tool("get_rates", "Rates."), tool("frobnicate", "Does things."),
               tool("list_rooms", "Rooms.", annotations=READ)]
    rows = classification_sheet(entries, classify=fake_classify)
    assert [(row["suggested"], row["basis"]) for row in rows] == [
        ("read", "heuristic"), ("write", "default"), ("read", "annotation")]


def test_a_schema_enumerating_reads_and_writes_makes_the_row_a_write():
    entry = tool("manage_rooms", "Manage rooms.", annotations=READ,
                 properties={"op": {"enum": ["list", "delete_room"]}})
    row = classification_sheet([entry], classify=fake_classify)[0]
    assert row["suggested"] == "write" and row["basis"] == "combined-operations"
    assert row["reasons"][-1] == ('"op" offers both reads (list) and writes (delete_room); a combined tool is a '
                                  "write (§3.2)")
    sensitive = tool("purge_rooms", "Purge.", annotations=SENSITIVE, properties={"op": {"enum": ["get", "drop"]}})
    assert classification_sheet([sensitive], classify=fake_classify)[0]["suggested"] == "sensitive-write"


def test_markdown_matches_mcpjam_s_format():
    rows = [{"tool": "search_rooms", "suggested": "read", "basis": "annotation", "reasons": ["declares readOnlyHint"],
             "declared": "read"},
            {"tool": "create_booking", "suggested": "write", "basis": "default", "reasons": ["a", "b|c"],
             "declared": "sensitive-write"},
            {"tool": "odd|name", "suggested": "sensitive-write", "basis": "heuristic", "reasons": ["line\none"]}]
    assert sheet_markdown(rows) == (
        "| Tool | Classification | Basis |\n"
        "| --- | --- | --- |\n"
        "| `search_rooms` | Read | declared; matches the suggestion |\n"
        "| `create_booking` | Sensitive write | declared; suggested Write: a; b\\|c |\n"
        "| `odd\\|name` | Sensitive write | suggested: line one |")


def test_the_default_classifier_is_the_connector_lane_s_own():
    from consumer_agent_testing_tool import permissions  # A missing lane module fails, never skips.
    rows = classification_sheet(CEDAR)
    assert [row["suggested"] for row in rows] == [permissions.classify_tool(entry).tool_class for entry in CEDAR]
    assert [row["suggested"] for row in rows] == ["read", "read", "sensitive-write", "read", "sensitive-write"]


def test_a_missing_lane_classifier_is_a_setup_error_not_a_local_substitute(monkeypatch):
    monkeypatch.setitem(sys.modules, "consumer_agent_testing_tool.permissions", None)
    with pytest.raises(LabError, match="permissions.classify_tool is unavailable"):
        classification_sheet(CEDAR)
