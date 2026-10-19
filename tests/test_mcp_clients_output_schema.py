"""The output-schema treatment (P1.5): a client profile records whether a structuredContent that fails the tool's
outputSchema is kept and noted (lenient, the default) or refused (strict, as the official SDK's ClientSession
refuses it), for both clients."""
import httpx2
import mcp_types as types
import pytest
import test_mcp_clients_support as support
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from test_mcp_clients_support import FakeMcp, sdk_app, serve_asgi

from consumer_agent_testing_tool.linking import NoCredential
from consumer_agent_testing_tool.mcp_clients import (
    GENERIC_PROFILE,
    MUSE_TEMPLATE_PROFILE,
    OUTPUT_SCHEMA,
    MuseTemplateClient,
    SdkClient,
    output_problem,
)

SCHEMA = {"type": "object", "properties": {"total_minor": {"type": "integer"}}, "required": ["total_minor"]}


def notes(client, kind):
    return [note for note in client.observations if note["kind"] == kind]


def test_profiles_default_to_the_lenient_treatment_and_record_it():
    assert GENERIC_PROFILE.output_schema == MUSE_TEMPLATE_PROFILE.output_schema == "lenient"
    assert GENERIC_PROFILE.model_dump()["output_schema"] == "lenient"
    assert GENERIC_PROFILE.model_copy(update={"output_schema": "strict"}).model_dump()["output_schema"] == "strict"
    assert OUTPUT_SCHEMA["strict"]["basis"] == "documented" and "ClientSession" in OUTPUT_SCHEMA["strict"]["source"]
    assert OUTPUT_SCHEMA["lenient"]["basis"] == "assumed"


@pytest.mark.parametrize("result, problem", [
    ({"content": [], "structuredContent": {"total_minor": 41800}}, None),
    ({"content": []}, ("output-schema-missing", "/structuredContent")),
    ({"content": [], "structuredContent": None}, ("output-schema-missing", "/structuredContent")),
    ({"content": [], "structuredContent": {"total_minor": "secret-forty"}},
     ("output-schema-mismatch", "/structuredContent/total_minor (type)")),
    ({"content": [], "structuredContent": {}}, ("output-schema-mismatch", "/structuredContent (required)"))])
def test_output_problems_are_judged_as_the_sdk_judges_them_and_never_quote_the_value(result, problem):
    assert output_problem(SCHEMA, result) == problem
    assert "secret-forty" not in str(output_problem(SCHEMA, result))


def test_an_output_schema_that_is_no_schema_is_its_own_problem():
    assert output_problem({"type": "object", "properties": {"x": {"type": 12}}},
                          {"structuredContent": {"x": 1}}) == ("output-schema-invalid", "/outputSchema")


async def test_the_sdk_client_keeps_a_mismatch_leniently_and_refuses_it_strictly():
    with serve_asgi(sdk_app) as base:
        for treatment in ("lenient", "strict"):
            profile = GENERIC_PROFILE.model_copy(update={"output_schema": treatment})
            async with SdkClient(profile, f"{base}/mcp", NoCredential()) as client:
                await client.list_tools()
                quote = await client.call_tool("get_quote", {"text": "x"})  # Violates its own outputSchema.
                echo = await client.call_tool("search_rooms", {"text": "x"})  # Publishes none.
            assert echo.error is None and echo.result["structuredContent"]["tool"] == "search_rooms"
            if treatment == "lenient":
                assert quote.result["structuredContent"] == {"total_minor": "forty"} and quote.error is None
                assert notes(client, "output-schema-mismatch") == [
                    {"kind": "output-schema-mismatch", "tool": "get_quote", "count": 1}]
                assert notes(client, "output-schema-refused") == []
            else:
                # Refused as an unreadable outcome: no result reaches the host, the call was sent (uncertain).
                assert quote.result is None and quote.uncertain is True
                assert quote.error["category"] == "result" and "strict output-schema treatment" in \
                    quote.error["message"]
                assert "forty" not in quote.error["message"]
                assert quote.sent_name == "get_quote"
                [refused] = notes(client, "output-schema-refused")
                assert (refused["tool"], refused["problem"], refused["at"], refused["basis"]) == (
                    "get_quote", "output-schema-mismatch", "/structuredContent/total_minor (type)", "documented")


async def test_the_template_client_notes_or_refuses_by_its_profile_s_treatment():
    for treatment in ("lenient", "strict"):
        profile = MUSE_TEMPLATE_PROFILE.model_copy(update={"output_schema": treatment})
        with FakeMcp() as fake:  # Its get_quote answers without the total_minor its outputSchema requires.
            client = MuseTemplateClient(profile, fake.url, NoCredential())
            await client.connect()
            await client.list_tools()
            quote = await client.call_tool("get_quote", {"text": "x"})
            echo = await client.call_tool("search_rooms", {"text": "x"})
            await client.close()
        assert echo.error is None
        if treatment == "lenient":
            assert quote.result["structuredContent"] == {"echo": {"text": "x"}}
            assert [note["tool"] for note in notes(client, "output-schema-mismatch")] == ["get_quote"]
        else:
            assert quote.result is None and quote.uncertain and quote.error["category"] == "result"
            assert notes(client, "output-schema-refused")[0]["problem"] == "output-schema-mismatch"


def empty_schema_app(base):
    """One tool that publishes outputSchema {} (any JSON Schema is one under MCP 2026-07-28) and answers with
    content only."""
    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool.model_validate(
            {"name": "get_quote", "inputSchema": {"type": "object"}, "outputSchema": {}})])

    async def call_tool(context, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text="quote")])
    return Server("catt-empty-schema", version="1.0", on_list_tools=list_tools,
                  on_call_tool=call_tool).streamable_http_app()


async def test_an_empty_output_schema_still_requires_structured_content_as_the_sdk_does(monkeypatch):
    from consumer_agent_testing_tool.http_streams import StreamingTransport

    with serve_asgi(empty_schema_app) as base:
        async with httpx2.AsyncClient(trust_env=False, timeout=20,
                                     transport=StreamingTransport(trust_env=False)) as http, \
                Client(streamable_http_client(f"{base}/mcp", http_client=http), mode="auto") as official:
            assert official.protocol_version == "2026-07-28"
            await official.list_tools()
            with pytest.raises(RuntimeError, match="has an output schema but did not return structured content"):
                await official.call_tool("get_quote", {})  # The official client refuses this result.
        for treatment in ("lenient", "strict"):
            profile = GENERIC_PROFILE.model_copy(update={"output_schema": treatment})
            async with SdkClient(profile, f"{base}/mcp", NoCredential()) as client:
                await client.list_tools()
                quote = await client.call_tool("get_quote", {})
            assert client.protocol_version == "2026-07-28"
            if treatment == "lenient":
                assert quote.error is None and quote.result["content"][0]["text"] == "quote"
                assert notes(client, "output-schema-missing") == [
                    {"kind": "output-schema-missing", "tool": "get_quote", "count": 1}]
            else:
                assert quote.result is None and quote.uncertain and quote.error["category"] == "result"
                assert notes(client, "output-schema-refused")[0]["problem"] == "output-schema-missing"
    # The template client shares the check: its strict treatment refuses the same answer.
    listed = support.tool
    monkeypatch.setattr(support, "tool", lambda name: listed(name) | ({"outputSchema": {}} if name == "big" else {}))
    with FakeMcp(pages=[["big"]]) as fake:  # big answers with content only
        client = MuseTemplateClient(MUSE_TEMPLATE_PROFILE.model_copy(update={"output_schema": "strict"}), fake.url,
                                    NoCredential())
        await client.connect()
        await client.list_tools()
        big = await client.call_tool("big", {})
        await client.close()
    assert big.result is None and notes(client, "output-schema-refused")[0]["problem"] == "output-schema-missing"
    # A listed outputSchema that is no JSON object at all: the SDK refuses even the listing.
    assert output_problem("object", {"structuredContent": {}}) == ("output-schema-invalid", "/outputSchema")
    assert output_problem(True, {"structuredContent": {}}) == ("output-schema-invalid", "/outputSchema")
