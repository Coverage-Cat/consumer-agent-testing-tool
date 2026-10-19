import json

import pytest
from inspect_ai.tool import ToolParams
from test_connector_support import CATALOG

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.projection import (
    DEFERRED_PARAMETERS,
    NO_DESCRIPTION,
    PROVIDER_NAME,
    CliCommand,
    CliError,
    ConnectorView,
    Projection,
    bounded,
    cli_output,
    cli_result,
    credential_shaped,
    credential_shapes,
    functional_key,
    mask_for_model,
    native_parameters,
    provider_names,
    render_host,
    render_result,
    render_service_error,
    strip_functional,
    summary_line,
)

SCHEMA = {
    "type": "object", "$defs": {"Guests": {"type": "integer", "minimum": 1, "exclusiveMaximum": 9},
                                "Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
    "properties": {
        "quote_id": {"type": "string"},
        "guest_name": {"type": "string", "title": "Guest name"},
        "guest_email": {"type": "string", "format": "email"},
        "adults": {"$ref": "#/$defs/Guests"},
        "mode": {"const": "standard"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "pay": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        "chain": {"$ref": "#/$defs/Node"},
        "remote": {"$ref": "https://schemas.example/x.json"},
    },
    "required": ["quote_id", "guest_name", "guest_email"],
}


def view(**options):
    return ConnectorView("cedar-stays", [dict(tool) for tool in CATALOG if tool["name"] != "cancel_booking"],
                         {"create_booking": ["guest_email"]}, "Hotel stays at Cedar Stays.",
                         options.pop("doc", "---\nname: cedar-stays\ndescription: Cedar Stays rooms.\n---\n# Cedar\n"),
                         **options)


def test_provider_names_are_safe_unique_and_never_shadow_host_tools():
    names = provider_names(["get.quote", "get/quote", "ask_user", "exec", "x" * 80, "", "x" * 80 + "y"])
    assert all(PROVIDER_NAME.fullmatch(name) for name in names.values())
    assert len(set(names.values())) == len(names)
    assert names["get.quote"] == "get_quote" and names["get/quote"].startswith("get_quote_")
    assert names["ask_user"] != "ask_user" and names["exec"] != "exec" and names[""] == "tool"
    # Deterministic for a given catalog order.
    assert names == provider_names(["get.quote", "get/quote", "ask_user", "exec", "x" * 80, "", "x" * 80 + "y"])


def test_native_parameters_record_every_schema_loss_and_rewrite():
    parameters, diff = native_parameters(SCHEMA, ["guest_email"])
    ToolParams.model_validate(parameters)  # Inspect accepts the projection as-is.
    properties = parameters["properties"]
    assert "guest_email" not in properties and parameters["required"] == ["quote_id", "guest_name"]
    assert diff["host_owned_removed"] == ["guest_email"]
    assert properties["adults"] == {"type": "integer", "minimum": 1,
                                    "description": "(no description provided by the service)"}
    assert properties["mode"]["enum"] == ["standard"] and properties["pay"]["anyOf"][1] == {"type": "integer"}
    assert properties["guest_name"]["description"] == "Guest name"
    assert parameters["additionalProperties"] is True  # JSON Schema's default, not Inspect's false.
    assert set(diff["lost_keywords"]) >= {"/properties/adults/exclusiveMaximum", "/properties/tags/minItems"}
    # An annotation, not a constraint the model loses (it backs the missing description above).
    assert "/properties/guest_name/title" not in diff["lost_keywords"]
    assert any("recursive" in item for item in diff["lost_keywords"])
    assert any("not local" in item for item in diff["lost_keywords"])
    assert "/properties/mode/const -> enum [value]" in diff["rewritten"]
    assert any(item.startswith("/properties/pay/oneOf -> anyOf") for item in diff["rewritten"])
    assert "/properties/adults (#/$defs/Guests)" in diff["inlined_refs"]
    assert "/properties/quote_id/description (placeholder)" in diff["descriptions_added"]
    assert diff["additional_properties"] == "absent in the server schema; projected as true"


def test_native_projection_maps_names_hides_host_fields_and_counts_losses():
    projection = Projection("native", "in-prompt", [view()])
    specs = {spec["name"]: spec for spec in projection.specs()}
    assert list(specs) == ["search_rooms", "get_quote", "create_booking", "get_booking"]
    assert "guest_email" not in specs["create_booking"]["parameters"]["properties"]
    assert projection.native_tool("create_booking") == ("cedar-stays", "create_booking")
    assert projection.native_tool("cancel_booking") is None  # Never in the reviewed allowlist.
    record = projection.record()
    assert record["basis"]["native"]["basis"] == "assumed"
    assert projection.lost_keywords() == 1  # get_quote's exclusiveMaximum.
    # The muse-cli view of the same tool is the server's real schema, minus host fields only.
    cli = Projection("muse-cli", "in-prompt", [view()])
    entry = cli.entry("cedar-stays", "get_quote")
    assert entry["inputSchema"]["properties"]["adults"]["exclusiveMaximum"] == 9
    assert "guest_email" not in cli.entry("cedar-stays", "create_booking")["inputSchema"]["properties"]
    assert "_meta" not in entry and entry["annotations"] == {"readOnlyHint": True}
    assert [spec["name"] for spec in cli.specs()] == ["exec"]


def test_projection_refuses_host_fields_a_closed_schema_does_not_declare():
    with pytest.raises(LabError, match="does not declare and does not allow"):
        Projection("native", "in-prompt", [ConnectorView("cedar-stays", [CATALOG[3]], {"get_booking": ["guest_email"]})])
    open_schema = {"name": "note", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}
    projection = Projection("native", "in-prompt", [ConnectorView("notes", [open_schema], {"note": ["author"]})])
    assert projection.diffs[("notes", "note")]["host_owned_undeclared"] == ["author"]


def test_search_only_loads_matching_tools_on_demand():
    projection = Projection("native", "search-only", [view()])
    # dev.meta.ai/docs/tool-search: names and descriptions from the start, parameter schemas on demand.
    specs = {spec["name"]: spec for spec in projection.specs()}
    assert list(specs) == ["tool_search", "search_rooms", "get_quote", "create_booking", "get_booking"]
    assert specs["get_quote"]["parameters"] == DEFERRED_PARAMETERS and specs["get_quote"]["description"].startswith(
        "Price a room")
    assert projection.native_tool("get_quote") is None and projection.deferred_tool("get_quote") == (
        "cedar-stays", "get_quote")
    result = projection.search("what does a room cost? a quote please")
    assert result["results"][0]["name"] == "get_quote" and "get_quote" in result["loaded"]
    assert result["untrusted"] is True and result["note"].startswith("Loaded tools are callable")
    loaded = {spec["name"]: spec for spec in projection.specs()}["get_quote"]
    assert set(loaded["parameters"]["properties"]) == {"room_id", "check_in", "check_out", "adults"}
    assert projection.deferred_tool("get_quote") is None
    again = projection.search("get_quote")
    assert again["results"][0]["name"] == "get_quote" and again["loaded"] == []
    assert projection.search("zzz unrelated")["results"] == []
    assert [entry["loaded"] for entry in projection.load_log][1:] == [[], []]
    assert projection.record()["basis"]["search-only"]["basis"] == "documented"
    cli = Projection("muse-cli", "search-only", [view()])
    # Namespace-level deferral (assumed): a service tool_search has not found does not exist yet.
    assert cli.parse("cedar-stays list-tools") == CliError(127, "exec: cedar-stays: command not found")
    assert cli.document("cedar-stays.md") is None and "cedar-stays" not in cli.help()
    found = cli.search("hotel booking")
    assert found["results"][0]["service"] == "cedar-stays" and found["loaded"] == ["cedar-stays"]
    assert cli.parse("cedar-stays list-tools") == CliCommand("list-tools", connector="cedar-stays")
    assert cli.document("cedar-stays.md")[0] == "cedar-stays"
    assert cli.record()["basis"]["search-only-cli"]["basis"] == "assumed" and "search-only" not in cli.record()["basis"]


def test_a_cli_call_to_a_service_not_loaded_yet_is_a_deferred_call_and_discovery_is_not():
    cli = Projection("muse-cli", "search-only", [view()])
    missing = "exec: cedar-stays: command not found"
    # Still "command not found" for the model; a well-formed call-tool of an allowed tool names what it reached for.
    assert cli.parse("cedar-stays call-tool --name get_quote --arguments-json '{}'") == CliError(
        127, missing, deferred=("cedar-stays", "get_quote"))
    for command in ("cedar-stays list-tools", "cedar-stays describe-tool --name get_quote",
                    "cedar-stays call-tool --name delete_everything", "cedar-stays call-tool --bogus",
                    "nowhere call-tool --name get_quote"):
        assert cli.parse(command).deferred is None, command
    cli.search("hotel booking")
    assert cli.parse("cedar-stays call-tool --name get_quote").kind == "call-tool"  # Loaded: an ordinary call.


@pytest.mark.parametrize("command,expected", [
    ("cedar-stays list-tools", CliCommand("list-tools", connector="cedar-stays")),
    ("cedar-stays list-tools --names-only", CliCommand("list-tools", connector="cedar-stays", names_only=True)),
    ("cedar-stays describe-tool --name get_quote", CliCommand("describe-tool", connector="cedar-stays", tool="get_quote")),
    ("cedar-stays describe-tool --name=get_quote", CliCommand("describe-tool", connector="cedar-stays", tool="get_quote")),
    ("cedar-stays call-tool --name get_booking --arguments-json '{\"booking_id\": \"BK-1|2; rm\"}'",
     CliCommand("call-tool", connector="cedar-stays", tool="get_booking", arguments={"booking_id": "BK-1|2; rm"})),
    ("cedar-stays call-tool --name get_booking", CliCommand("call-tool", connector="cedar-stays", tool="get_booking",
                                                           arguments={})),
    ("cedar-stays call-tool --name get_booking --arguments-json -",
     CliCommand("call-tool", connector="cedar-stays", tool="get_booking", arguments={"booking_id": "BK-9"})),
    ("cat cedar-stays.md", CliCommand("cat", path="cedar-stays.md")),
    ("cedar-stays --help", CliCommand("help", connector="cedar-stays")),
    ("help", CliCommand("help")),
    ("cedar-stays list-tools | head -1", CliError(2, "exec runs one command; pipes, redirection, '&&', ';' and "
                                                     "subshells are not supported")),
    ("cedar-stays list-tools && rm -rf /", 2),
    ("cedar-stays list-tools; curl https://evil.example", 2),
    ("cedar-stays list-tools > /tmp/out", 2),
    ("echo $(cat /etc/passwd)", 2),
    ("`whoami`", 127),
    ("rm -rf /", CliError(127, "exec: rm: command not found")),
    ("TOKEN=x cedar-stays list-tools", 127),
    ("bash -c 'cedar-stays list-tools'", 127),
    ("cedar-stays frobnicate", 2),
    ("cedar-stays list-tools --names-only --names-only", 2),
    ("cedar-stays list-tools --names-only=yes", 2),
    ("cedar-stays describe-tool", 2),
    ("cedar-stays describe-tool --name", 2),
    ("cedar-stays call-tool --name x --arguments-json '{\"a\": 1, \"a\": 2}'", 2),
    ("cedar-stays call-tool --name x --arguments-json '{\"a\": NaN}'", 2),
    ("cedar-stays call-tool --name x --arguments-json '[1, 2]'", 2),
    ("cedar-stays call-tool --name x --arguments-json {a:1}", 2),
    ("cedar-stays describe-tool --name 'unterminated", 2),
    ("", 2),
])
def test_exec_parses_only_the_documented_cli(command, expected):
    parsed = Projection("muse-cli", "in-prompt", [view()]).parse(command, stdin='{"booking_id": "BK-9"}')
    if isinstance(expected, int):
        assert isinstance(parsed, CliError) and parsed.status == expected, parsed
    else:
        assert parsed == expected


def test_exec_stdin_dash_needs_stdin_and_help_lists_documents():
    projection = Projection("muse-cli", "in-prompt", [view()])
    assert projection.parse("cedar-stays call-tool --name x --arguments-json -").status == 2
    text = projection.help()
    assert "cedar-stays call-tool --name <tool>" in text and "cat <file>    (cedar-stays.md)" in text
    assert projection.document("cedar-stays.md")[0] == "cedar-stays"
    assert projection.document("/etc/passwd") is None


def test_results_are_bounded_untrusted_json_without_protocol_metadata():
    result = {"content": [{"type": "text", "text": "Booked."}, {"type": "image", "mimeType": "image/png", "data": "AAAA"},
                          {"type": "resource_link", "uri": "https://cedar.example/b/1", "name": "booking"},
                          {"type": "resource", "resource": {"uri": "file:///r", "blob": "AAAA"}}],
              "structuredContent": {"booking_id": "BK-1"}, "isError": False, "_meta": {"ui": "x"}}
    envelope = render_result("cedar-stays", "create_booking", result)
    assert envelope["source"] == "cedar-stays" and envelope["untrusted"] is True
    assert envelope["result"]["content"][0] == "Booked."
    assert envelope["result"]["content"][1] == {"type": "image", "mimeType": "image/png", "note": "binary content not shown"}
    assert "data" not in json.dumps(envelope) and "_meta" not in json.dumps(envelope)
    assert envelope["result"]["structuredContent"] == {"booking_id": "BK-1"}
    large = render_result("cedar-stays", "search_rooms", {"content": [{"type": "text", "text": "x" * 50000}],
                                                          "structuredContent": {"rooms": ["y" * 20000]}})
    text = bounded(large, "result", 4096)
    parsed = json.loads(text)
    assert len(text) <= 4096 and parsed["truncated"]["field"] == "result"
    assert parsed["source"] == "cedar-stays" and parsed["untrusted"] is True
    assert parsed["result"].endswith("more characters]") and parsed["truncated"]["shown"] > 0
    host = render_host("create_booking", "The user declined this action.")
    assert host == {"source": "app", "tool": "create_booking", "message": "The user declined this action."}
    output = json.loads(bounded(cli_output(0, {"tools": ["a"] * 5000}, source="cedar-stays", untrusted=True),
                                "stdout", 2048))
    assert output["exit_status"] == 0 and isinstance(output["stdout"], str) and output["untrusted"] is True


def test_summary_line_prefers_doc_front_matter_then_prose_then_server_info():
    assert summary_line("---\nname: x\ndescription: 'Book stays.'\n---\nBody", None) == "Book stays."
    assert summary_line("# Title\n\nRooms and rates.\n", None) == "Rooms and rates."
    assert summary_line(None, {"name": "cedar", "title": "Cedar Stays"}) == "Cedar Stays"
    assert summary_line(None, None) == "A connected service."
    assert NO_DESCRIPTION.startswith("(no description")


@pytest.mark.parametrize("envelope, field", [
    (render_result("cedar-stays", "search_rooms", {"content": [{"type": "text", "text": "x" * 300_000}]}), "result"),
    (render_service_error("cedar-stays", "get_quote", {"code": -32603, "message": "E" * 300_000}), "error"),
    (render_host("create_booking", "M" * 300_000), "message"),
    (cli_output(0, {"rooms": ["r" * 1000] * 400}, source="cedar-stays", untrusted=True), "stdout"),
    (cli_output(1, "", "cedar-stays: error -32603: " + "E" * 300_000, source="cedar-stays", untrusted=True), "stdout"),
    (cli_output(2, "", "usage " * 60_000), "stdout"),
    ({"source": "cedar-stays", **{f"field{index}": "v" * 60 for index in range(2000)}}, "missing"),
    ({"source": "app", "tool": "t" * 5000, "message": "m" * 5000, "note": "n" * 5000}, "message"),
])
@pytest.mark.parametrize("limit", [1024, 4096, 16384])
def test_every_model_visible_envelope_fits_the_result_bound(envelope, field, limit):
    text = bounded(envelope, field, limit)
    assert len(text) <= limit
    parsed = json.loads(text)
    assert "message" in parsed or "truncated" in parsed


def test_model_views_reach_keys_deep_nesting_and_repeated_keys():
    secret = "sk-live-123456"
    nested = current = {}
    for _ in range(80):
        current["next"] = current = {}
    current["note"] = f"key {secret}"
    result = {"structuredContent": {secret: "booked", "rooms": {"94103": "Garden"}, "properties": {secret: {}},
                                    "deep": nested},
              "content": [{"type": "text", "text": f'{{"memo": "{secret}", "memo": "fine"}}'},
                          {"type": "text", "text": '{"memo": "fine", "id": 7}'}]}
    masked, paths = mask_for_model(result, [secret, "94103"])
    shown = json.dumps(masked)
    assert secret not in shown and "94103" not in shown
    data = masked["structuredContent"]
    # A registered value in a key is hidden like one in a value (schema property names too), never merged away.
    assert data["[REDACTED]"] == "booked" and data["rooms"] == {"[REDACTED]": "Garden"}
    assert data["properties"] == {"[REDACTED]": {}} and "/structuredContent/[REDACTED]" in paths
    # Past the depth limit the remainder is the nesting marker, never shown unmasked.
    level = data["deep"]
    while isinstance(level, dict):
        level = level["next"]
    assert level == "[REDACTED: nested deeper than 64 levels]"
    # A repeated key: the text is re-serialized as a parser reads it (the last value), never shown as written.
    assert json.loads(masked["content"][0]["text"]) == {"memo": "fine"}
    assert masked["content"][1]["text"] == '{"memo": "fine", "id": 7}'  # Nothing hidden: kept as written.
    assert mask_for_model({"a": {"b": 1}}, [secret]) == ({"a": {"b": 1}}, [])


def test_model_views_hide_credentials_but_keep_ids_sessions_and_page_tokens():
    result = {"structuredContent": {"session_id": "s-100", "sessionId": "s-101", "checkout_session_id": "cs-1",
                                    "next_page_token": "pg-2", "nextPageToken": "pg-3", "pageToken": "pg-4",
                                    "cursor": "c-5", "quote_token": "qt-6", "confirmation_token": "ct-7",
                                    "continuation_token": "co-8", "sync_token": "sy-9", "booking_id": "b-1",
                                    "access_token": "at-secret-1", "token": "plain-token-2", "client_secret": "cs-3",
                                    "echo": "key sk-live-123456 and 94103", "zip": 94103,
                                    "link": "https://x.example/cb?access_token=abc&page_token=pg-10"},
              "content": [{"type": "text", "text": json.dumps({"session_id": "s-100", "access_token": "at-2"})},
                          {"type": "text", "text": "Authorization: Bearer abc.def.ghi"}]}
    masked, paths = mask_for_model(result, ["sk-live-123456", "94103"])
    data = masked["structuredContent"]
    kept = ("session_id", "sessionId", "checkout_session_id", "next_page_token", "nextPageToken", "pageToken",
            "cursor", "quote_token", "confirmation_token", "continuation_token", "sync_token", "booking_id")
    assert all(data[key] == result["structuredContent"][key] for key in kept)
    assert data["access_token"] == data["token"] == data["client_secret"] == "[REDACTED]"
    assert data["echo"] == "key [REDACTED] and [REDACTED]" and data["zip"] == "[REDACTED]"
    assert data["link"] == "https://x.example/cb?access_token=[REDACTED]&page_token=pg-10"
    assert json.loads(masked["content"][0]["text"]) == {"session_id": "s-100", "access_token": "[REDACTED]"}
    assert masked["content"][1]["text"] == "Authorization: Bearer [REDACTED]"
    assert "/structuredContent/access_token" in paths and "/structuredContent/zip" in paths
    assert not any("session_id" in path or "page_token" in path for path in paths)
    # Schemas keep their property names: a parameter called api_key is not a credential value.
    schema, none = mask_for_model({"inputSchema": {"properties": {"api_key": {"type": "string"}}}}, [])
    assert schema["inputSchema"]["properties"]["api_key"] == {"type": "string"} and none == []
    assert strip_functional({"session_id": "s", "nested": [{"next_page_token": "p", "token": "t"}]}) == {
        "nested": [{"token": "t"}]}
    assert functional_key("searchSessionId") and not functional_key("session_token") and not functional_key("token")


def test_ui_resources_and_protocol_metadata_never_reach_the_model():
    ui = {"type": "resource", "resource": {"uri": "ui://cedar/rooms.html", "mimeType": "text/html;profile=mcp-app",
                                           "text": "<html><script>parent.postMessage('x')</script></html>",
                                           "_meta": {"ui": {"csp": {"connectDomains": ["https://cdn.example"]}}}}}
    result = {"content": [{"type": "text", "text": "2 rooms", "_meta": {"ui/hint": "inline"}}, ui],
              "structuredContent": {"rooms": 2}, "_meta": {"ui": {"resourceUri": "ui://cedar/rooms.html"}}}
    native = render_result("cedar", "show_rooms", result)["result"]["content"]
    assert native == ["2 rooms", {"type": "resource", "uri": "ui://cedar/rooms.html",
                                  "mimeType": "text/html;profile=mcp-app", "note": "UI resource not shown"}]
    shown = cli_result(result)
    assert "_meta" not in json.dumps(shown) and "<html>" not in json.dumps(shown)
    assert shown["content"][1]["resource"] == {"uri": "ui://cedar/rooms.html", "mimeType": "text/html;profile=mcp-app",
                                               "note": "UI resource not shown"}
    assert shown["structuredContent"] == {"rooms": 2}


def test_native_schemas_and_tool_counts_are_bounded():
    long = {"name": "notes", "description": "Notes.", "inputSchema": {"type": "object", "properties": {
        "text": {"type": "string", "description": "d" * 5000}}}}
    projection = Projection("native", "in-prompt", [ConnectorView("notes", [long])])
    description = projection.native["notes"]["parameters"]["properties"]["text"]["description"]
    assert len(description) == 1024 and description.endswith("…")
    assert projection.diffs[("notes", "notes")]["descriptions_truncated"] == [
        "/properties/text/description (5000 characters)"]
    huge = {"name": "huge", "inputSchema": {"type": "object", "properties": {
        f"p{index}": {"type": "string", "enum": [f"value-{n}" for n in range(40)]} for index in range(120)}}}
    with pytest.raises(LabError, match="too large to offer the model"):
        Projection("native", "in-prompt", [ConnectorView("big", [huge])])
    many = [{"name": f"tool_{index}", "inputSchema": {"type": "object"}} for index in range(129)]
    with pytest.raises(LabError, match="at most 128 functions per request: list the tools this task needs"):
        Projection("native", "in-prompt", [ConnectorView("many", many)])
    assert len(Projection("muse-cli", "in-prompt", [ConnectorView("many", many)]).specs()) == 1  # One exec tool.


def test_model_views_keep_continuation_tokens_and_a_bare_token_beside_a_page_of_records():
    # AWS-style nextToken, Azure skipToken, delta and resume tokens and operation holds: what the model
    # passes back to continue, never credentials.
    page = {"classes": [{"session_id": "s-100", "title": "Morning flow"}, {"session_id": "s-101", "title": "Yin"}],
            "nextToken": "pg-2", "NextToken": "pg-3", "next_token": "pg-4", "skipToken": "pg-5",
            "delta_token": "pg-6", "resume_token": "pg-7", "resumeToken": "pg-8", "polling_token": "pl-9",
            "hold_token": "h-10", "cart_token": "c-11", "checkout_token": "co-12", "token": "pg-13"}
    masked, paths = mask_for_model({"structuredContent": page, "content": [{"type": "text", "text": json.dumps(page)}]},
                                   [])
    assert masked["structuredContent"] == page and json.loads(masked["content"][0]["text"]) == page and paths == []
    assert strip_functional(page) == {"classes": [{"title": "Morning flow"}, {"title": "Yin"}]}  # Nothing to learn.
    # A bare token with no page beside it, and a functional word next to a credential word, stay hidden.
    named = []
    login, hidden = mask_for_model({"token": "tk-14", "user": {"id": "u-1"}, "scopes": ["read"]}, [], named=named)
    assert login["token"] == "[REDACTED]" and hidden == named == ["/token"]
    for key in ("page_access_token", "next_refresh_token", "access_token", "session_token", "upload_token",
                "sso_session_id", "access_key_id", "id_token"):
        listing = {"items": [{"id": 1}], key: "value-1234"}
        assert mask_for_model(listing, [])[0][key] == "[REDACTED]" and not functional_key(key, listing), key


def test_values_shaped_like_bearer_credentials_are_hidden_under_any_name_functional_ones_too():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0YXlsb3IiLCJzY29wZSI6ImFjY291bnQifQ.c2lnbmF0dXJlLXNlY3JldA"
    stripe = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
    result = {"structuredContent": {"session_id": jwt, "sso_session_id": jwt + "2", "auth_session_cursor": jwt + "3",
                                    "next_page_token": "pg-2", "note": f"use {stripe} here"},
              "content": [{"type": "text", "text": f"Resume at https://x.example/resume?session_id={jwt}"}]}
    masked, paths = mask_for_model(result, [])
    shown = json.dumps(masked)
    assert jwt not in shown and stripe not in shown and masked["structuredContent"]["next_page_token"] == "pg-2"
    assert masked["content"][0]["text"] == "Resume at https://x.example/resume?session_id=[REDACTED]"
    assert "/structuredContent/session_id" in paths and "/content/0/text" in paths
    # The host registers them (credential_shapes), so the provider guard refuses them from then on.
    assert set(credential_shapes(result)) == {jwt, jwt + "2", jwt + "3", stripe}
    # A documentation placeholder has a key's shape but none of its entropy.
    assert not credential_shaped("sk_test_xxxxxxxxxxxxxxxxxxxxxxxx") and credential_shaped(stripe)


@pytest.mark.parametrize("rooms", [250, 1000, 3000])
@pytest.mark.parametrize("limit", [4096, 16384])
def test_a_large_json_result_shows_the_longest_prefix_that_fits_at_least_half_the_bound(rooms, limit):
    # The payload is compact JSON escaped inside the envelope (each quote costs two characters): the cut is
    # sized on the envelope, so a larger result never shows less.
    data = {"rooms": [{"room_id": f"rm-{n}", "name": f"Room {n}", "floor": "ground", "nightly_minor": 18900}
                      for n in range(rooms)]}
    for envelope, field in ((cli_output(0, {"structuredContent": data}, source="cedar-stays", untrusted=True), "stdout"),
                            (render_result("cedar-stays", "search_rooms", {"content": [], "structuredContent": data}),
                             "result")):
        text = bounded(envelope, field, limit)
        shown = json.loads(text)["truncated"]["shown"]
        assert limit - 8 < len(text) <= limit and shown >= limit // 2, (len(text), shown)
