"""Tool classes (Muse connector guidelines 3.2): declared, annotated, heuristic or default write."""
import time
from enum import Enum
from typing import Literal, get_args

import pytest
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import ValidationError

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.guidelines import classification_sheet
from consumer_agent_testing_tool.permissions import (
    CLASS_BASIS,
    COMBINED,
    GUIDELINES,
    HELP_CENTER,
    RULE_BASIS,
    Classification,
    PermissionEngine,
    PermissionPolicy,
    ToolClass,
    classify_tool,
    combined_operations,
    selector_name,
)

READ_ONLY = {"readOnlyHint": True}
# The Cedar Stays test service's get_quote texts (connector_fixture.py, 2026-10-16), kept local.
GOOD_QUOTE = ("Quote one room for a stay: subtotal, taxes, fees, total, payment timing, cancellation policy "
              "and quote expiry. Amounts are integer minor units with a currency code. A quote does not reserve "
              "the room or charge anything; book it with create_booking before it expires.")
HIDDEN_WRITE_QUOTE = ("Quote one room for a stay: subtotal, taxes, fees, total, payment timing, cancellation "
                      "policy and quote expiry. Amounts are integer minor units with a currency code. Available "
                      "rates are secured instantly with the card on file.")


def test_a_declared_class_wins_and_records_what_it_overrides():
    tool = {"name": "get_quote", "description": "Gets a quote and books the room.", "annotations": READ_ONLY}
    result = classify_tool(tool, "read")
    assert (result.tool_class, result.basis) == ("read", "declared")
    assert result.reasons[:3] == ["declared read", "overrides heuristic class sensitive-write",
                                  "annotations.readOnlyHint=true"]
    # The overridden signal stays visible for the developer's review.
    assert any(reason.startswith("hidden-write-signal") for reason in result.reasons)
    same = classify_tool({"name": "create_booking", "annotations": {"destructiveHint": True}}, "sensitive-write")
    assert same.reasons == ["declared sensitive-write", "annotations.destructiveHint=true"]


def test_read_only_annotations_make_reads():
    result = classify_tool({"name": "search_rooms", "description": "Search available rooms.",
                            "annotations": READ_ONLY})
    assert (result.tool_class, result.basis, result.reasons) == (
        "read", "annotation", ["annotations.readOnlyHint=true"])


@pytest.mark.parametrize("annotations", [{"destructiveHint": True}, {"readOnlyHint": False, "destructiveHint": True}])
def test_destructive_annotations_make_sensitive_writes(annotations):
    result = classify_tool({"name": "get_room", "annotations": annotations})
    assert (result.tool_class, result.basis) == ("sensitive-write", "annotation")


def test_conflicting_hints_take_the_stricter_class():
    result = classify_tool({"name": "list_rooms", "annotations": {"readOnlyHint": True, "destructiveHint": True}})
    assert (result.tool_class, result.basis) == ("sensitive-write", "annotation")
    assert result.reasons[-1].startswith("annotation-conflict")


def test_an_explicit_non_read_only_hint_is_at_least_a_write():
    plain = classify_tool({"name": "get_report", "annotations": {"readOnlyHint": False}})
    assert (plain.tool_class, plain.basis) == ("write", "annotation")  # A read verb never downgrades it.
    booking = classify_tool({"name": "book_room", "annotations": {"readOnlyHint": False, "destructiveHint": False}})
    assert (booking.tool_class, booking.basis) == ("sensitive-write", "heuristic")
    assert "name verb 'book' (sensitive-write)" in booking.reasons


def test_idempotent_and_open_world_hints_only_add_reasons():
    base = classify_tool({"name": "save_favorite", "annotations": {"readOnlyHint": False}})
    hinted = classify_tool({"name": "save_favorite", "annotations": {
        "readOnlyHint": False, "idempotentHint": True, "openWorldHint": False}})
    assert (hinted.tool_class, hinted.basis) == (base.tool_class, base.basis) == ("write", "annotation")
    assert hinted.reasons == ["annotations.readOnlyHint=false", "annotations.idempotentHint=true",
                              "annotations.openWorldHint=false"]


def test_malformed_annotations_are_ignored_and_noted():
    hint = classify_tool({"name": "search_rooms", "annotations": {"readOnlyHint": "true"}})
    assert (hint.tool_class, hint.basis) == ("read", "heuristic")
    assert hint.reasons[0] == "annotations.readOnlyHint ignored: not a boolean"
    shape = classify_tool({"name": "weather", "annotations": ["readOnlyHint"]})
    assert (shape.tool_class, shape.basis) == ("write", "default")
    assert shape.reasons[0] == "annotations ignored: not an object"


def test_sdk_field_names_are_a_fallback_for_wire_names():
    result = classify_tool({"name": "rooms", "annotations": {"read_only_hint": True}})
    assert (result.tool_class, result.reasons) == ("read", ["annotations.readOnlyHint=true"])


@pytest.mark.parametrize("tool,tool_class,signal", [
    ({"name": "book_room"}, "sensitive-write", "name verb 'book'"),
    ({"name": "get_quote", "description": "Gets a quote and books the room."}, "sensitive-write",
     "description verb 'books'"),
    ({"name": "get_quote", "description": "Checks availability, then charges your card."}, "sensitive-write",
     "description verb 'charges'"),
    ({"name": "get_status", "description": "Returns the status. It also sends a confirmation email."},
     "sensitive-write", "description verb 'sends'"),
    ({"name": "lookup", "title": "Book Room"}, "sensitive-write", "title verb 'book'"),
    ({"name": "lookup", "annotations": {"title": "Pay invoice"}}, "sensitive-write", "annotations.title verb 'pay'"),
    ({"name": "get_profile", "description": "Gets the profile and updates the last-seen time."}, "write",
     "description verb 'updates'"),
    ({"name": "quote", "inputSchema": {"type": "object", "properties": {"idempotency_key": {"type": "string"}}}},
     "write", "input property 'idempotency_key'"),
    ({"name": "get_or_create_session"}, "write", "name verb 'create'"),
    ({"name": "get_details", "description": "Share details with the host."}, "sensitive-write",
     "description verb 'share'"),
    ({"name": "get_summary", "description": "Builds a summary. Then posts to the team channel."},
     "sensitive-write", "description verb 'posts'"),
    ({"name": "get_rooms", "description": "Book for two adults when available."}, "sensitive-write",
     "description verb 'book'"),
    ({"name": "get_booking", "description": "Update status of the booking."}, "write",
     "description verb 'update'"),
    ({"name": "get_quote", "description": HIDDEN_WRITE_QUOTE}, "sensitive-write",
     "description payment phrase 'with the card on file'"),
    ({"name": "get_quote", "description": "Needs no login but books the room."}, "sensitive-write",
     "description verb 'books'"),
])
def test_hidden_write_signals_override_a_read_annotation(tool, tool_class, signal):
    result = classify_tool({**tool, "annotations": {**READ_ONLY, **tool.get("annotations", {})}})
    assert (result.tool_class, result.basis) == (tool_class, "heuristic"), result.reasons
    assert result.reasons[0] == "annotations.readOnlyHint=true"
    assert any(reason.startswith("hidden-write-signal: " + signal) for reason in result.reasons), result.reasons
    assert result.reasons[-1] == COMBINED


@pytest.mark.parametrize("description", [
    "Get a booking by id. Use cancel_booking to cancel it.",
    "Returns a quote including taxes and charges.",
    "Returns room rates, taxes and charges.",
    "Does not book anything; call create_booking to book.",
    "Order status for an order id.",
    "Lists cancelled and refunded bookings.",
    "You can book later with create_booking.",
    "Email address of the guest on file.",
    "Transfer history for the account.",
    "Search results are cached for five minutes.",
    "Never charges your card.",
    "Charges apply only when you confirm with create_booking.",
    "Charges for late cancellation apply.",
    "Returns fees and charges for the stay.",
    "Check-in time is 3pm; checkout time is 11am.",
    GOOD_QUOTE,
    "Never books or charges anything.",
    "No charge to the card on file until you book.",
    "Cancel it via cancel_booking.",
])
def test_read_annotated_tools_without_action_signals_stay_reads(description):
    result = classify_tool({"name": "get_booking", "description": description, "annotations": READ_ONLY})
    assert (result.tool_class, result.basis, result.reasons) == (
        "read", "annotation", ["annotations.readOnlyHint=true"])


@pytest.mark.parametrize("name,tool_class", [
    ("search_rooms", "read"), ("get_quote", "read"), ("check_availability", "read"), ("list_bookings", "read"),
    ("booking_status", "read"), ("order_status", "read"), ("getHTTPStatus", "read"), ("rooms.search", "read"),
    ("create_booking", "write"), ("update_profile", "write"), ("save_favorite", "write"), ("add_to_cart", "write"),
    ("set_preference", "write"), ("bookings.create", "write"), ("createBooking", "write"), ("check_in", "write"),
    ("pay_invoice", "sensitive-write"), ("purchase_item", "sensitive-write"), ("checkout", "sensitive-write"),
    ("check_out", "sensitive-write"), ("book_room", "sensitive-write"), ("reserve_table", "sensitive-write"),
    ("submit_application", "sensitive-write"), ("send_message", "sensitive-write"),
    ("transfer_funds", "sensitive-write"), ("delete_item", "sensitive-write"), ("cancel_booking", "sensitive-write"),
    ("create_payment", "sensitive-write"), ("place_order", "sensitive-write"), ("sendEmail", "sensitive-write"),
    ("booking_cancel", "sensitive-write"), ("status", "read"), ("room_availability", "read"),
    ("transfer_history", "read"), ("payment_status", "read"), ("cedar_search_rooms", "read"),
    ("v2_get_booking", "read"), ("github_create_issue", "write"), ("update_status", "write"),
    ("slack_send_message", "sensitive-write"), ("cedar_book_room", "sensitive-write"),
    ("share_report", "sensitive-write"), ("email_report", "sensitive-write"), ("bank_transfer", "sensitive-write"),
])
def test_name_verbs_suggest_a_class(name, tool_class):
    result = classify_tool({"name": name})
    assert (result.tool_class, result.basis) == (tool_class, "heuristic"), result.reasons


@pytest.mark.parametrize("description,tool_class", [
    ("Searches available rooms.", "read"),
    ("Returns the guest's saved preferences.", "read"),
    ("Creates a draft booking from a quote.", "write"),
    ("Creates a payment for the order.", "sensitive-write"),
    ("Searches and books rooms.", "sensitive-write"),
    ("Sends the receipt to the guest.", "sensitive-write"),
    ("Check out the cart with the saved card.", "sensitive-write"),
    ("Subscribes the user to the premium plan.", "sensitive-write"),
    ("Looks up a booking by reference.", "read"),
])
def test_descriptions_suggest_a_class_when_the_name_has_no_verb(description, tool_class):
    result = classify_tool({"name": "cedar", "description": description})
    assert (result.tool_class, result.basis) == (tool_class, "heuristic"), result.reasons


@pytest.mark.parametrize("name,tool_class", [("search_and_book", "sensitive-write"),
                                             ("get_or_create_session", "write")])
def test_tools_combining_reads_and_writes_are_writes(name, tool_class):
    result = classify_tool({"name": name})
    assert result.tool_class == tool_class
    assert result.reasons[-1] == COMBINED


@pytest.mark.parametrize("tool", [
    {"name": "weather"}, {"name": "cedar", "description": "Room inventory for the hotel."},
    {"name": "address_book"}, {"name": "rooms", "annotations": {"title": None}}, {"name": "status_update"},
])
def test_unknown_tools_default_to_write(tool):
    result = classify_tool(tool)
    assert (result.tool_class, result.basis) == ("write", "default")
    assert result.reasons[-1].endswith("unknown tools stay write-gated")


def test_classification_stays_linear_on_hostile_catalog_text():
    started = time.perf_counter()
    result = classify_tool({"name": "get_quote_" * 1000, "description": "and book for " * 200_000,
                            "annotations": READ_ONLY})
    assert result.tool_class == "read" and time.perf_counter() - started < 10


def test_the_important_read_flag_is_recorded_where_it_matters():
    read = classify_tool({"name": "get_booking", "annotations": READ_ONLY}, important_read=True)
    assert read.reasons[-1] == "important read: asks under Ask for some actions (Help Center)"
    write = classify_tool({"name": "save_favorite"}, important_read=True)
    assert write.reasons[-1] == "important-read flag has no effect: writes always ask"


@pytest.mark.parametrize("tool,declared", [({}, None), ({"name": ""}, None), ({"name": 3}, None),
                                           ("search_rooms", None), ({"name": "x"}, "sensitive")])
def test_bad_entries_and_declarations_are_setup_errors(tool, declared):
    with pytest.raises(LabError) as caught:
        classify_tool(tool, declared)
    assert caught.value.code == 2


def test_classifications_are_frozen_strict_records():
    result = classify_tool({"name": "search_rooms"})
    with pytest.raises(ValidationError):
        result.tool_class = "write"
    with pytest.raises(ValidationError):
        Classification(tool="x", tool_class="read", basis="annotation", reasons=[], source="server")
    with pytest.raises(ValidationError):
        Classification(tool="x", tool_class="readonly", basis="annotation", reasons=[])


def test_every_class_and_rule_carries_a_basis_and_source():
    assert set(CLASS_BASIS) == set(get_args(ToolClass))
    for entry in (*CLASS_BASIS.values(), *RULE_BASIS.values()):
        assert entry["basis"] in ("documented", "assumed")
        if entry["basis"] == "documented":
            assert entry["source"] and entry["quote"]
        else:
            assert entry["source"] is None and entry["detail"]
    assert {entry["source"] for entry in CLASS_BASIS.values()} == {f"{GUIDELINES} section 3.2"}
    assert RULE_BASIS["combined-read-write"]["source"] == RULE_BASIS["hidden-write"]["source"] == \
        f"{GUIDELINES} section 3.2"
    assert RULE_BASIS["sensitive-every-use"]["source"] == f"{GUIDELINES} section 3.3"
    assert RULE_BASIS["changed-details"]["source"] == f"{GUIDELINES} section 3.4"
    assert HELP_CENTER == "https://www.meta.com/help/artificial-intelligence/1385290430137537/"
    for key in ("allow-task", "allow-site", "deny", "ask-for-some", "always-ask"):
        assert RULE_BASIS[key]["source"] == HELP_CENTER
    assumed = {key for key, entry in RULE_BASIS.items() if entry["basis"] == "assumed"}
    assert {"edit", "no-response", "heuristic", "unknown-is-write"} <= assumed


@pytest.mark.parametrize("enum,expected", [
    (["list", "delete_room"], "sensitive-write"), (["get_item", "createItem"], "write"),
    (["list", "search"], "read"), (["get", "deleted_at", "budget"], "read"),
])
def test_a_read_whose_schema_enumerates_writes_is_a_combined_write(enum, expected):
    # Guidelines 3.2 via MCPJam tool-shape.ts: the same rule the classification sheet applies; each
    # enumerated operation is classed with the lexicon, so a delete makes the combined tool sensitive.
    tool = {"name": "manage_rooms", "description": "Manage rooms.", "annotations": READ_ONLY,
            "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": enum}}}}
    result = classify_tool(tool)
    assert result.tool_class == expected
    if expected != "read":
        assert result.basis == "heuristic" and result.reasons[-1].startswith("combined-operations: 'action'")
        assert result.reasons[-1].endswith(COMBINED)
        assert result.reasons[-2].startswith("operations: 'action' offers") and expected in result.reasons[-2]
        assert classify_tool(tool, "read").tool_class == "read"  # A declared class still wins.
    sensitive = {**tool, "annotations": {"destructiveHint": True}}
    assert classify_tool(sensitive).tool_class == "sensitive-write"  # Never lowered.


@pytest.mark.parametrize("tool", [
    # A read-annotated combined tool whose write is a send (guidelines 3.2: never hide a send in a read).
    {"name": "manage_messages", "description": "Work with the user's messages.", "annotations": READ_ONLY,
     "inputSchema": {"type": "object", "properties": {"operation": {"enum": ["list", "send"]},
                                                      "body": {"type": "string"}}}},
    {"name": "messages", "inputSchema": {"type": "object", "properties": {"operation": {"enum": ["list", "send"]}}}},
    {"name": "files", "description": "Access files.", "annotations": READ_ONLY,
     "inputSchema": {"type": "object", "properties": {"op": {"enum": ["get", "delete"]}}}},
    # A selector parameter needs no read value: every value is an operation of the tool.
    {"name": "mailbox", "annotations": READ_ONLY,
     "inputSchema": {"type": "object", "properties": {"actions": {"enum": ["forward"]}}}},
    {"name": "tables", "annotations": {"readOnlyHint": False, "destructiveHint": False},
     "inputSchema": {"type": "object", "properties": {"method": {"enum": ["select", "drop"]}}}},
])
def test_enumerated_send_or_delete_operations_make_a_sensitive_write_whatever_the_annotations(tool):
    result = classify_tool(tool)
    assert (result.tool_class, result.basis) == ("sensitive-write", "heuristic")
    assert any(reason.startswith("operations: ") and "(sensitive-write)" in reason for reason in result.reasons)
    assert RULE_BASIS["operations"]["basis"] == "assumed"


def test_enum_values_outside_an_operation_selector_are_data_not_actions():
    # A filter or channel enum is data: "email" names a channel here, not this tool's action.
    tool = {"name": "list_contacts", "description": "List contacts.", "annotations": READ_ONLY,
            "inputSchema": {"type": "object", "properties": {"channel": {"enum": ["email", "sms"]},
                                                             "status": {"enum": ["pending", "cancelled"]}}}}
    assert classify_tool(tool).tool_class == "read"
    single = {**tool, "inputSchema": {"type": "object", "properties": {"action": {"enum": ["create"]}}}}
    assert classify_tool(single).tool_class == "write"  # A read whose only operation is a write.


@pytest.mark.parametrize("tool", [
    {"name": "wipe_account", "description": "Erase all of the user's data.", "annotations": {"readOnlyHint": False}},
    {"name": "purge_records", "description": "Permanently erases every record.", "annotations": {"readOnlyHint": False}},
    {"name": "destroy_vault", "annotations": {"readOnlyHint": False}},
])
def test_irreversible_verbs_are_sensitive_even_without_a_destructive_hint(tool):
    assert classify_tool(tool).tool_class == "sensitive-write"
    # MCP's default destructiveHint is not applied to plain writes; the assumption is labeled.
    plain = classify_tool({"name": "rename_vault", "annotations": {"readOnlyHint": False}})
    assert (plain.tool_class, plain.basis) == ("write", "annotation")
    assert "default destructiveHint=true is not applied" in RULE_BASIS["annotations"]["assumed"]
    assert "includes payment" in RULE_BASIS["heuristic"]["detail"]


class Operation(str, Enum):
    LIST = "list"
    SEND = "send"


async def sdk_selectors():
    """Read-annotated operation selectors as the pinned MCP Python SDK publishes them: a Python Enum
    (a $ref into $defs), an optional Literal (anyOf with null) and a list of Enum values (array items)."""
    server = MCPServer("selectors")
    read = ToolAnnotations(read_only_hint=True)

    @server.tool(name="mailbox_enum", description="Work with the user's messages.", annotations=read)
    async def mailbox_enum(operation: Operation, body: str | None = None) -> str:
        return "ok"

    @server.tool(name="mailbox_optional", description="Work with the user's messages.", annotations=read)
    async def mailbox_optional(operation: Literal["list", "send"] | None = None) -> str:
        return "ok"

    @server.tool(name="mailbox_batch", description="Work with the user's messages.", annotations=read)
    async def mailbox_batch(operations: list[Operation]) -> str:
        return "ok"
    return {tool.name: tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in await server.list_tools()}


async def test_sdk_generated_operation_selectors_are_classed_by_every_value_they_offer():
    tools = await sdk_selectors()
    assert tools["mailbox_enum"]["inputSchema"]["properties"]["operation"] == {"$ref": "#/$defs/Operation"}
    assert "anyOf" in tools["mailbox_optional"]["inputSchema"]["properties"]["operation"]
    for name, tool in tools.items():
        result = classify_tool(tool)
        assert (result.tool_class, result.basis) == ("sensitive-write", "heuristic"), (name, result.reasons)
        assert any(reason.startswith("operations: ") and "'send' (sensitive-write)" in reason
                   for reason in result.reasons), result.reasons
        assert combined_operations(tool)["reads"] == ["list"] and combined_operations(tool)["writes"] == ["send"]
        assert classification_sheet([tool])[0]["suggested"] == "sensitive-write"
        # Always allow on a list cannot cover a later send: a sensitive write asks on every use (3.3).
        engine = PermissionEngine(PermissionPolicy(rules=[{"id": "always", "decision": "always_allow",
                                                           "when": {"field": "tool", "op": "eq", "value": name}}]),
                                  {name: result}, "mail")
        first = engine.request(name, {"operation": "list"})
        assert first.card.offered == ["allow_once", "deny"] and first.scope == "once"
        assert engine.request(name, {"operation": "send", "body": "wire the money"}).card is not None


@pytest.mark.parametrize("schema", [
    {"properties": {"operation": {"oneOf": [{"const": "list"}, {"const": "send"}]}}},
    {"properties": {"action": {"anyOf": [{"const": "get"}, {"const": "delete"}]}}},
    {"properties": {"operation": {"const": "send"}}},
    {"$defs": {"Op": {"anyOf": [{"$ref": "#/$defs/Read"}, {"const": "send"}]}, "Read": {"enum": ["list"]}},
     "properties": {"operation": {"anyOf": [{"$ref": "#/$defs/Op"}, {"type": "null"}]}}},
    {"properties": {"request": {"type": "object", "properties": {"action": {"enum": ["list", "send"]}}}}},
    {"properties": {"ops": {"type": "array", "items": {"type": "object", "properties": {"op": {"enum": ["get", "delete"]}}}}}},
    {"oneOf": [{"properties": {"operation": {"const": "list"}}},
               {"properties": {"operation": {"const": "send"}, "body": {"type": "string"}}}]},
    {"$ref": "#/$defs/Input", "$defs": {"Input": {"type": "object", "properties": {"op": {"enum": ["list", "send"]}}}}},
    {"properties": {"operation_type": {"enum": ["archive", "delete"]}}},
    {"properties": {"operationType": {"enum": ["send"]}}},
    {"properties": {"action_name": {"enum": ["list", "forward"]}}},
    {"properties": {"subcommand": {"enum": ["status", "publish"]}}},
])
def test_enumerated_operations_count_however_the_schema_enumerates_them(schema):
    tool = {"name": "mailbox", "description": "Work with the user's messages.", "annotations": READ_ONLY,
            "inputSchema": {"type": "object", **schema}}
    result = classify_tool(tool)
    assert result.tool_class == "sensitive-write", result.reasons
    assert classification_sheet([tool])[0]["suggested"] == "sensitive-write"


def test_selector_words_inside_data_names_are_not_operation_selectors():
    assert all(map(selector_name, ("operation", "ops", "actions", "operation_type", "operationType", "action_name",
                                   "subcommand", "cmd", "method", "http_method", "verb", "intent")))
    assert not any(map(selector_name, ("payment_method", "delivery_method", "transaction", "payment_intent",
                                       "transaction_type", "channel", "mode", "status", "optional")))
    tool = {"name": "list_orders", "description": "List orders.", "annotations": READ_ONLY,
            "inputSchema": {"type": "object", "properties": {
                "payment_method": {"enum": ["apple_pay", "card"]}, "transaction": {"enum": ["refund", "charge"]},
                "delivery_method": {"enum": ["email", "post"]}}}}
    assert classify_tool(tool).tool_class == "read"  # Filters name data, not this tool's actions.


@pytest.mark.parametrize("name, parameter, values", [
    ("calculate", "operation", ["add", "subtract", "multiply", "divide"]),
    ("date_math", "op", ["add", "subtract"]),
    ("matrix", "operation", ["transpose", "multiply", "add"]),
    ("text_tools", "action", ["count", "replace", "reverse"]),
])
def test_a_read_only_tool_whose_operations_are_computations_stays_a_read(name, parameter, values):
    tool = {"name": name, "description": "Perform an operation on the inputs.", "annotations": READ_ONLY,
            "inputSchema": {"type": "object", "properties": {parameter: {"type": "string", "enum": values}}}}
    result = classify_tool(tool)
    assert (result.tool_class, result.basis) == ("read", "annotation"), result.reasons
    assert classification_sheet([tool])[0]["suggested"] == "read"
    # Ask for some actions never asks for a read, so the calculator answers without a card.
    engine = PermissionEngine(PermissionPolicy(mode="ask-for-some"), {name: result}, "tools")
    ticket = engine.request(name, {parameter: values[0], "a": 1, "b": 2})
    assert ticket.allowed and ticket.card is None
    # The same words beside a read operation (a combined tool) or in a tool not annotated read-only count.
    combined = {**tool, "inputSchema": {"type": "object", "properties": {parameter: {"enum": [*values, "get"]}}}}
    assert classify_tool(combined).tool_class == "write"
    assert classify_tool({**tool, "annotations": {}}).tool_class == "write"
    # Sensitive verbs and MCPJam's write verbs count whatever else the selector offers.
    assert classify_tool({**tool, "inputSchema": {"type": "object", "properties": {
        parameter: {"enum": [*values, "delete"]}}}}).tool_class == "sensitive-write"
    assert classify_tool({**tool, "inputSchema": {"type": "object", "properties": {
        parameter: {"enum": [*values, "update"]}}}}).tool_class == "write"
    assert "calculator stays a read" in RULE_BASIS["operations"]["detail"]
