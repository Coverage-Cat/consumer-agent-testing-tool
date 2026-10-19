"""Pinned FastMCP conversion and tool execution, with host-mediated HTTP only."""
import asyncio
import base64
import importlib.metadata
import json
import re
import sys

from .serialization import loads

PIN = "4.1.0"
METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def error_detail(error):
    """Keep native schema diagnostics without echoing an offending private instance."""
    from jsonschema.exceptions import ValidationError
    cause, seen = error, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ValidationError):
            return (type(error).__name__ + ": Invalid native structured result; validator "
                    + str(cause.validator) + ", schema path "
                    + json.dumps(list(cause.absolute_schema_path)))
        cause = cause.__cause__
    return type(error).__name__ + ": " + str(error)


class Engine:
    def __init__(self):
        self.http = self.client = self.pending = None
        self.sequence = 0
        self.active = None

    async def initialize(self, arguments):
        import httpx2
        from fastmcp import Client, FastMCP, settings
        from fastmcp.server.providers.openapi import MCPType, RouteMap

        if self.client is not None:
            raise ValueError("Generated API worker is already initialized")
        for package in ("fastmcp", "fastmcp-slim"):
            if importlib.metadata.version(package) != PIN:
                raise ModuleNotFoundError("Install the pinned consumer-agent-testing-tool[api] FastMCP version")
        if settings.telemetry_mode != "off" or settings.check_for_updates != "off":
            raise ValueError("Generated worker telemetry and update checks must be disabled")
        document, selected = arguments["document"], arguments["operations"]
        routes = {}
        for path, item in document.get("paths", {}).items():
            if not isinstance(item, dict):
                continue
            for method, operation in item.items():
                if method not in METHODS or not isinstance(operation, dict):
                    continue
                identity = operation.get("operationId")
                if identity in selected:
                    if identity in routes:
                        raise ValueError("Selected operationId is not unique: " + identity)
                    routes[identity] = {"operation": identity, "method": method.upper(), "path": path,
                                        "effect": selected[identity]}
        if not selected or set(routes) != set(selected):
            raise ValueError("Select nonempty, uniquely identified source operations")
        self.http = httpx2.AsyncClient(base_url=arguments["base_url"],
            transport=httpx2.MockTransport(self.capture), follow_redirects=False, trust_env=False,
            timeout=arguments["timeout_seconds"])
        mappings = {}

        def record(route, component):
            mappings[component.name] = dict(routes[route.operation_id])

        server = FastMCP.from_openapi(document, client=self.http, name="Consumer Agent Testing Tool generated baseline",
            route_maps=[RouteMap(methods=[row["method"]], pattern="^" + re.escape(row["path"]) + "$",
                                 mcp_type=MCPType.TOOL) for row in routes.values()] +
                       [RouteMap(mcp_type=MCPType.EXCLUDE)],
            mcp_component_fn=record)
        self.client = Client(server)
        await self.client.__aenter__()
        tools = await self.client.list_tools()
        self.tools = {tool.name for tool in tools}
        # FastMCP reports callback errors rather than propagating them. Verify
        # complete associations before accepting any generated tool.
        if set(mappings) != self.tools or len(mappings) != len(routes) or {
                row["operation"] for row in mappings.values()} != set(routes):
            raise ValueError("Generated tool/source associations are incomplete")
        return {"versions": {name: importlib.metadata.version(name)
                            for name in ("fastmcp", "fastmcp-slim", "httpx2", "mcp")},
                "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
                "mapping": mappings, "transport": "host-mediated; worker has no network transport",
                "runtime": {"telemetry": settings.telemetry_mode, "updates": settings.check_for_updates},
                "adapter": "fastmcp-generated-v1"}

    async def capture(self, request):
        import httpx2
        if self.pending is None or self.active["requests"] != 0:
            raise ValueError("Unsolicited or repeated generated HTTP request blocked")
        self.active["requests"] += 1
        body = await request.aread()
        if len(body) > 1024 * 1024:
            raise ValueError("Generated request body exceeds 1 MiB")
        headers = list(request.headers.multi_items())
        if len({key.lower() for key, _ in headers}) != len(headers):
            raise ValueError("Repeated generated request headers require a separate transport profile")
        await self.queue.put({"method": request.method, "url": str(request.url), "headers": dict(headers),
                              "body": base64.b64encode(body).decode() if body else None})
        reply = await self.response
        return httpx2.Response(reply["status"], headers=reply["headers"],
                               content=base64.b64decode(reply["body"], validate=True), request=request)

    async def prepare(self, arguments):
        if self.client is None or self.pending is not None:
            raise ValueError("Generated worker is uninitialized or another tool is pending")
        if arguments["tool"] not in self.tools:
            raise ValueError("Tool was not admitted into the generated catalog")
        self.sequence += 1
        self.active = {"call_id": self.sequence, "requests": 0}
        self.queue = asyncio.Queue(maxsize=1)
        self.response = asyncio.get_running_loop().create_future()
        self.pending = asyncio.create_task(self.client.call_tool_mcp(arguments["tool"], arguments["arguments"]))
        proposal = asyncio.create_task(self.queue.get())
        try:
            await asyncio.wait([self.pending, proposal], return_when=asyncio.FIRST_COMPLETED)
            if proposal.done():
                return {"call_id": self.sequence, "request": proposal.result()}
            result = await self.pending
            self.pending = None
            return {"call_id": self.sequence, "completed": result.model_dump(
                mode="json", by_alias=True, exclude_none=True)}
        finally:
            if not proposal.done():
                proposal.cancel()
            await asyncio.gather(proposal, return_exceptions=True)

    async def finish(self, arguments):
        if self.pending is None or arguments["call_id"] != self.active["call_id"] or self.response.done():
            raise ValueError("Generated response does not match the pending call")
        self.response.set_result(arguments["response"])
        try:
            result = await self.pending
            return {"call_id": self.active["call_id"],
                    "completed": result.model_dump(mode="json", by_alias=True, exclude_none=True)}
        finally:
            self.pending = None
            # Session credentials are not part of this stateless baseline.
            self.http.cookies.clear()

    async def abort(self, arguments):
        if self.pending is None or arguments["call_id"] != self.active["call_id"]:
            raise ValueError("Generated cancellation does not match the pending call")
        self.pending.cancel()
        await asyncio.gather(self.pending, return_exceptions=True)
        self.pending = None
        self.http.cookies.clear()
        return {"call_id": self.active["call_id"], "discarded_before_dispatch": True}

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
        if self.client is not None:
            await self.client.__aexit__(None, None, None)
        if self.http is not None:
            await self.http.aclose()


async def main():
    engine = Engine()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = None
            try:
                request = loads(line, json_only=True)
                method = {"init": engine.initialize, "prepare": engine.prepare, "finish": engine.finish,
                          "abort": engine.abort}.get(
                    request["method"])
                if method is None:
                    raise ValueError("Unsupported generated worker operation")
                result = await method(request["arguments"])
                reply = {"id": request["id"], "result": result}
            except Exception as error:  # noqa: BLE001 - serialize upstream boundary failures
                reply = {"id": request["id"] if request else None,
                         "error": error_detail(error),
                         "unsupported": isinstance(error, (ImportError, ModuleNotFoundError))}
            print(json.dumps(reply, allow_nan=False), flush=True)
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
