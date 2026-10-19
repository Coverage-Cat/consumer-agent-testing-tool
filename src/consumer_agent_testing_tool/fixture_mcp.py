"""Official MCP fixture server; transactional tools require explicit M2 opt-in."""
import argparse
import asyncio
import json
import os

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .fixture import Fixture, FixtureError

OPERATIONS = {
    "slots_list": ("slots.list", {}, [], "List availability; amounts are integer USD minor units."),
    "slots_get": ("slots.get", {"id": {"type": "string"}}, ["id"], "Read a slot's price and revision."),
    "reservations_create": ("reservations.create",
        {k: {"type": "string"} for k in ("slot_id", "contact", "idempotency_key")},
        ["slot_id", "contact", "idempotency_key"],
        # Factual service behavior only: the scored recovery (reread, reconcile) is the agent's to choose.
        ("Create a nonbinding draft; reserves no inventory. One draft per idempotency key; a request that "
         "conflicts with current state returns 409. operations_get reports a prior operation's outcome.")),
    "reservations_get": ("reservations.get", {"id": {"type": "string"}}, ["id"],
                         "Read your reservation and revision."),
    "reservations_revise": ("reservations.revise", {
        "id": {"type": "string"}, "contact": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "idempotency_key": {"type": "string"}},
        ["id", "contact", "expected_revision", "idempotency_key"],
        "Revise a nonbinding draft; expected_revision must be the current revision, else 409."),
    "operations_get": ("operations.get", {"idempotency_key": {"type": "string"}},
                       ["idempotency_key"], "Report the outcome of a prior operation of your account."),
}


def public_operations(transactional=False):
    operations = dict(OPERATIONS)
    if transactional:
        from .reservation_fixture import CANCEL_OPERATION_SCHEMA, TRANSACTION_SCHEMA
        for name in ("confirm", "cancel"):
            operations[f"reservations_{name}"] = (
                f"reservations.{name}", TRANSACTION_SCHEMA["properties"], TRANSACTION_SCHEMA["required"],
                (f"{name.capitalize()} an owned reservation with a separately issued host grant. "
                 "Requires the current revision, integer minor-unit total and currency. Idempotent per "
                 "idempotency key; operations_get reports an earlier attempt's outcome. "
                 "A pending result is not a completed reservation action."))
        operations["operations_cancel"] = (
            "operations.cancel", CANCEL_OPERATION_SCHEMA["properties"], CANCEL_OPERATION_SCHEMA["required"],
            ("Stop an owned pending operation before commit. Reports completed if it is already committed; "
             "does not undo a reservation; reservations_cancel, with its own grant, cancels a confirmed one."))
    return operations


def catalog(*, probes=False, transactional=False):
    tools = [types.Tool(name=name, description=description, inputSchema={
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False,
    }, outputSchema={"type": "object", "properties": {"result": {}},
                     "required": ["result"], "additionalProperties": False})
        for name, (_, properties, required, description) in public_operations(transactional).items()]
    if probes:
        tools.append(types.Tool(name="fixture_wait",
            description="Contract-only cancellation probe. Wait without changing reservation state.",
            inputSchema={"type": "object", "properties": {"milliseconds": {
                "type": "integer", "minimum": 1, "maximum": 5000}},
                "required": ["milliseconds"], "additionalProperties": False}))
        tools.append(types.Tool(name="fixture_disconnect",
            description="Contract-only process exit probe. Closes this disposable server without a reply.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False}))
    return tools


def make_server(db, run_id, variant="good", seed=False, probes=False, transactional=False):
    if transactional:
        from .reservation_fixture import ReservationFixture
        fixture = ReservationFixture(db, run_id, variant)
    else:
        fixture = Fixture(db, run_id, variant)
    operations = public_operations(transactional)
    if seed:
        fixture.reset()
    async def list_tools(context, params):
        return types.ListToolsResult(tools=catalog(probes=probes, transactional=transactional))

    async def call_tool(context, params):
        name, arguments = params.name, params.arguments or {}
        try:
            if probes and name == "fixture_disconnect":
                fixture.protocol_event("disconnect_started")
                # Only the explicitly enabled, disposable contract fixture exposes
                # this fault. No JSON-RPC reply or graceful transport close occurs.
                os._exit(86)
            if probes and name == "fixture_wait":
                duration = arguments.get("milliseconds")
                if type(duration) is not int or not 1 <= duration <= 5000:
                    raise FixtureError(400, "milliseconds must be an integer from 1 to 5000")
                fixture.protocol_event("wait_started")
                try:
                    await asyncio.sleep(duration / 1000)
                except asyncio.CancelledError:
                    fixture.protocol_event("wait_cancelled")
                    raise
                fixture.protocol_event("wait_completed")
                return types.CallToolResult(content=[types.TextContent(type="text", text="wait complete")])
            if name not in operations:
                raise FixtureError(404, "Unknown operation")
            operation = operations[name][0]
            if transactional:
                result = fixture.dispatch("alice", operation, arguments,
                    grant_id=fixture.staged_grant("alice", operation, arguments))
            else:
                result = fixture.dispatch("alice", operation, arguments)
            structured = {"wrong": True} if variant == "invalid_structured" else {"result": result}
            return types.CallToolResult(content=[types.TextContent(
                type="text", text=json.dumps({"result": result}))],
                structuredContent=structured)
        except FixtureError as exc:
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type="text", text=json.dumps({"status": exc.status, "error": str(exc)}))])

    return Server("consumer-agent-testing-tool-reservation-fixture", version="1",
                  on_list_tools=list_tools, on_call_tool=call_tool)


def serve(db, run_id, variant="good", seed=False, port=None, probes=False, bearer_env=None, transactional=False):
    server = make_server(db, run_id, variant, seed, probes, transactional)
    if port is not None:
        from contextlib import asynccontextmanager

        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount

        manager = StreamableHTTPSessionManager(server, json_response=True)

        @asynccontextmanager
        async def lifespan(app):
            async with manager.run():
                yield

        secret = os.environ.get(bearer_env) if bearer_env else None
        if bearer_env and not secret:
            raise ValueError("Fixture bearer environment variable is missing")

        async def transport(scope, receive, send):
            if secret and dict(scope.get("headers", [])).get(b"authorization") != f"Bearer {secret}".encode():
                await JSONResponse({"error": "Unauthorized fixture request"}, status_code=401)(
                    scope, receive, send)
            else:
                await manager.handle_request(scope, receive, send)

        app = Starlette(routes=[Mount("/", app=transport)], lifespan=lifespan)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error", proxy_headers=False)
        return
    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--variant", default="good")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--contract-probes", action="store_true")
    parser.add_argument("--bearer-env")
    parser.add_argument("--transactional", action="store_true",
                        help="Expose shared M2 confirm/cancel; grants remain runner-owned")
    args = parser.parse_args()
    serve(args.db, args.run_id, args.variant, args.seed, args.port, args.contract_probes,
          args.bearer_env, args.transactional)
