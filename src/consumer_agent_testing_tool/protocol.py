import asyncio
import os
import shutil
import socket
import sys
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx2
import pydantic
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import ClientSession, MCPError, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import (
    LATEST_HANDSHAKE_VERSION,
    LATEST_MODERN_VERSION,
    MODERN_PROTOCOL_VERSIONS,
)

from .config import CONTRACT_REVISIONS, Target
from .errors import LabError, Unsupported
from .fixture import Fixture, oracle_metrics
from .http_streams import StreamingTransport


def child_environment(extra=None):
    """Explicit baseline. Neither HOME nor provider configuration is inherited."""
    result = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
    if extra:
        result.update(extra)
    return result


@asynccontextmanager
async def connect(target: Target, *, db: Path | None = None, run_id="contract", evidence=None):
    if target.kind == "cli-skill":
        raise Unsupported("CLI descriptors cannot implicitly become MCP servers; declare a separate target")
    async with AsyncExitStack() as stack:
        url = target.base_url
        if target.kind == "fixture":
            if db is None:
                directory = stack.enter_context(tempfile.TemporaryDirectory())
                db = Path(directory) / "fixture.sqlite"
            command = [sys.executable, "-m", "consumer_agent_testing_tool.fixture_mcp", "--db", str(db),
                       "--run-id", run_id, "--variant", target.variant, "--seed", "--contract-probes"]
            if target.transport == "streamable-http":
                port = int(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_CONFORMANCE_PORT", "0"))
                if not port:
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                server = await asyncio.create_subprocess_exec(
                    *command, "--port", str(port), env=child_environment(),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                stack.push_async_callback(stop_server, server)
                for _ in range(100):
                    if server.returncode is not None:
                        raise LabError("Native HTTP fixture failed to start")
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(.05)
                else:
                    raise LabError("Native HTTP fixture readiness deadline exceeded")
                command, url = None, f"http://127.0.0.1:{port}/mcp"
        elif target.transport == "stdio":
            # Arbitrary target code requires an actual process/network boundary.
            from .sandbox import docker_available
            docker_available()
            if not target.container_image:
                raise Unsupported("Custom stdio targets require container_image; target commands "
                                  "cannot execute directly on the host")
            inspected = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", target.container_image, "--format", "{{.Id}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _stderr = await asyncio.wait_for(inspected.communicate(), 10)
            if inspected.returncode:
                raise LabError("Target image not available locally; build it during setup")
            image_id = stdout.decode().strip()
            if evidence:
                evidence.manifest["run_lock"]["target_image_id"] = image_id
            command = [shutil.which("docker"), "run", "--rm", "-i", "--pull=never",
                       "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                       "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m", "--user", "1000:1000",
                       "--entrypoint", target.command[0], image_id, *target.command[1:]]
        else:
            command = None
        if command:
            environment = child_environment()
            if target.kind == "fixture":
                # The SDK merges its safe defaults into env, including HOME.
                # Override that default so even the trusted reference process
                # cannot discover the coordinator's credential directory by HOME.
                environment["HOME"] = stack.enter_context(tempfile.TemporaryDirectory())
                environment.update(LOGNAME="fixture", USER="fixture", SHELL="/bin/sh")
            else:
                # Docker client is trusted control-plane tooling; only its nonsecret
                # local engine selectors are inherited. Container env is still separate.
                for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "HOME"):
                    if name in os.environ:
                        environment[name] = os.environ[name]
            transport = await stack.enter_async_context(stdio_client(
                StdioServerParameters(command=command[0], args=command[1:],
                                      env=environment)))
        else:
            headers = {}
            session_state = {"id": None}
            if target.auth.mode == "api_key":
                from .destinations import target_credential
                secret = target_credential(target)
                headers["Authorization"] = f"Bearer {secret}"
                if evidence:
                    evidence.redactor.add([secret])
            async def http_response(response):
                if evidence:
                    import hashlib
                    issued = response.headers.get("mcp-session-id")
                    session_id = issued or response.request.headers.get("mcp-session-id")
                    if session_id:
                        session_state["id"] = session_id
                    # The era is per request: 2026-07-28 stamps each POST with its version and method.
                    record = {"method": response.request.method, "status": response.status_code,
                        "session_id_sha256": hashlib.sha256(session_id.encode()).hexdigest()
                                             if session_id else None, "session_issued": bool(issued),
                        "protocol_version": response.request.headers.get("mcp-protocol-version"),
                        "mcp_method": response.request.headers.get("mcp-method")}
                    evidence.manifest.setdefault("http_transport", []).append(record)
                    evidence.event("network_request", record, provenance="direct MCP HTTP transport")
            client = await stack.enter_async_context(httpx2.AsyncClient(
                headers=headers, timeout=target.timeout_seconds, follow_redirects=False,
                transport=StreamingTransport(trust_env=False),
                trust_env=False, event_hooks={"response": [http_response]}))
            async def closed_session_probe():
                if target.kind == "fixture" and evidence and session_state["id"]:
                    request = types.JSONRPCRequest(jsonrpc="2.0", id="closed-session-probe", method="ping")
                    response = await client.post(url, json=request.model_dump(mode="json", exclude_none=True),
                        headers={"Mcp-Session-Id": session_state["id"],
                                 "Mcp-Protocol-Version": evidence.manifest.get("protocol_negotiation", {}).get(
                                     "negotiated") or LATEST_HANDSHAKE_VERSION,
                                 "Accept": "application/json, text/event-stream"})
                    evidence.manifest["closed_session_status"] = response.status_code
            # LIFO: run after SDK session/transport termination, while this HTTP
            # client and the owned fixture server still exist.
            stack.push_async_callback(closed_session_probe)
            transport = await stack.enter_async_context(
                streamable_http_client(url, http_client=client))
        read, write = transport[:2]
        session = await stack.enter_async_context(ClientSession(
            read, write, read_timeout_seconds=target.timeout_seconds))
        yield session, db


async def stop_server(server):
    if server.returncode is None:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), 5)
        except TimeoutError:
            server.kill()
            await server.wait()


UNKNOWN_REVISION = "Unsupported protocol version from the server: "
# No server serves this revision, so a 2026-07-28 server must refuse it and name its own (-32022).
UNASSIGNED_REVISION = "2099-01-01"


def protocol_era(revision):
    """2026-07-28 negotiates with server/discover and stamps every request; earlier revisions use initialize."""
    return "modern" if revision in MODERN_PROTOCOL_VERSIONS else "handshake"


async def negotiate(session, revision):
    """The profile's own negotiation, without evidence (for the disconnect probe's separate fixture)."""
    return await (session.discover() if protocol_era(revision) == "modern" else session.initialize())


def _hint(negotiated):
    if negotiated in CONTRACT_REVISIONS:
        return f"select --interface-profile mcp-contract-{negotiated}"
    return f"no contract profile covers it (supported: {', '.join(CONTRACT_REVISIONS)})"


def _record_negotiation(evidence, session, negotiated, era):
    info = session.server_info
    evidence.manifest["run_lock"].update(
        negotiated_protocol_revision=negotiated, negotiated_protocol_era=era,
        # A 2026-07-28 server may stay anonymous: its serverInfo is an optional _meta stamp.
        server_info=info.model_dump(mode="json", exclude_none=True) if info else None)


async def _initialize(session, evidence, revision):
    try:
        initialized = await session.initialize()
    except MCPError as error:
        if error.code != UNSUPPORTED_PROTOCOL_VERSION:
            raise
        # -32022 on initialize: a 2026-07-28-era server that serves no handshake revision at all. Name the
        # profile that negotiates with it rather than failing the run as a setup error.
        data = error.error.data if isinstance(error.error.data, dict) else {}
        supported = [str(item)[:64] for item in data["supported"][:20]] \
            if isinstance(data.get("supported"), list) else None
        evidence.manifest["protocol_negotiation"] = {
            "offered": LATEST_HANDSHAKE_VERSION, "negotiated": None, "profile_revision": revision,
            "method": "initialize", "refusal": {"code": error.code, "supported": supported}}
        modern = [item for item in MODERN_PROTOCOL_VERSIONS if item in (supported or [])]
        hint = (f"{_hint(modern[-1])}, which negotiates with server/discover" if modern else
                f"no contract profile covers it (supported: {', '.join(CONTRACT_REVISIONS)})")
        listed = ", ".join(supported or []) or "no listed version"
        raise Unsupported(f"Server refused the MCP {LATEST_HANDSHAKE_VERSION} initialize handshake (-32022 "
                          f"unsupported protocol version; it supports {listed}); {hint}. Revision-specific "
                          "coverage was not executed") from error
    except RuntimeError as error:
        # The SDK refuses a counter-offer outside its handshake revisions before returning it.
        if not str(error).startswith(UNKNOWN_REVISION):
            raise
        negotiated = str(error).removeprefix(UNKNOWN_REVISION)[:64]
        evidence.manifest["protocol_negotiation"] = {
            "offered": LATEST_HANDSHAKE_VERSION, "negotiated": negotiated, "profile_revision": revision}
        hint = (f"{negotiated} is negotiated with server/discover, never initialize: select --interface-profile "
                f"mcp-contract-{negotiated} (contract profiles: {', '.join(CONTRACT_REVISIONS)})"
                if negotiated in MODERN_PROTOCOL_VERSIONS else
                f"no contract profile covers it (supported: {', '.join(CONTRACT_REVISIONS)})")
        raise Unsupported(f"Server negotiated MCP {negotiated!r}, which the pinned MCP SDK refuses over the "
                          f"initialize handshake; {hint}. Revision-specific coverage was not executed") from error
    evidence.event("protocol_initialized", initialized.model_dump(mode="json"),
                   provenance="direct MCP SDK")
    negotiated = initialized.protocol_version
    # The SDK offers its newest handshake revision; a server may counter-offer an older one,
    # and the selected profile names the revision this run accepts.
    evidence.manifest["protocol_negotiation"] = {
        "offered": LATEST_HANDSHAKE_VERSION, "negotiated": negotiated, "profile_revision": revision,
        "era": "handshake", "method": "initialize"}
    _record_negotiation(evidence, session, negotiated, "handshake")
    if negotiated != revision:
        raise Unsupported(f"Server negotiated MCP {negotiated}, but {evidence.profile['name']} tests {revision}; "
                          f"{_hint(negotiated)}. Revision-specific coverage was not executed")
    return initialized


async def _discover(session, evidence, revision):
    """The SDK's modern-era negotiation: server/discover at the newest modern revision, one retry at a
    mutual one on -32022, then adopt. Every later request carries its own version envelope."""
    record = {"offered": LATEST_MODERN_VERSION, "profile_revision": revision, "method": "server/discover"}
    try:
        result = await session.discover()
    except (MCPError, RuntimeError, pydantic.ValidationError) as error:
        # Not modern evidence. As the SDK's auto mode does, fall back to initialize, here only to name
        # the era the server does speak; transport failures propagate as the SDK's do.
        record["discover_error"] = ({"code": error.code, "message": str(error)[:300]} if isinstance(error, MCPError)
                                    else {"type": type(error).__name__, "message": str(error)[:300]})
        evidence.manifest["protocol_negotiation"] = {**record, "negotiated": None, "fallback": "initialize"}
        refused = False
        try:
            negotiated = (await session.initialize()).protocol_version
        except (MCPError, RuntimeError) as fallback:
            # A counter-offer the SDK refuses is kept as _initialize keeps it, but it names no negotiated era.
            refused = str(fallback).startswith(UNKNOWN_REVISION)
            negotiated = str(fallback).removeprefix(UNKNOWN_REVISION)[:64] if refused else None
        evidence.manifest["protocol_negotiation"]["negotiated"] = negotiated
        if negotiated and not refused:
            evidence.manifest["protocol_negotiation"]["era"] = "handshake"
            _record_negotiation(evidence, session, negotiated, "handshake")
        spoke = (f"it negotiated MCP {negotiated} over the initialize fallback; {_hint(negotiated)}"
                 if negotiated and not refused else
                 f"its initialize fallback counter-offered {negotiated!r}, which the pinned MCP SDK refuses over "
                 "the initialize handshake" if negotiated else "the initialize fallback negotiated nothing either")
        raise Unsupported(f"Server did not negotiate MCP {revision} with server/discover ({str(error)[:200]}); "
                          f"{spoke}. Revision-specific coverage was not executed") from error
    evidence.event("protocol_discovered", result.model_dump(mode="json"), provenance="direct MCP SDK")
    negotiated = session.protocol_version
    evidence.manifest["protocol_negotiation"] = {
        **record, "negotiated": negotiated, "era": "modern", "supported_versions": list(result.supported_versions)}
    _record_negotiation(evidence, session, negotiated, "modern")
    if negotiated != revision:
        raise Unsupported(f"Server negotiated MCP {negotiated}, but {evidence.profile['name']} tests {revision}; "
                          f"{_hint(negotiated)}. Revision-specific coverage was not executed")
    return result


async def discover(session, evidence, revision):
    """Negotiate in the profile's era, record the negotiated era and version, and list every tools page."""
    negotiated = await (_discover if protocol_era(revision) == "modern" else _initialize)(session, evidence, revision)
    tools, cursor, seen = [], None, set()
    for _ in range(100):
        page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor)
                                        if cursor else None)
        evidence.event("surface_discovered", page.model_dump(mode="json"),
                       provenance="direct MCP SDK")
        tools.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None:
            break
        if cursor in seen:
            raise LabError("MCP pagination repeated a cursor", code=1, category="tool/API")
        seen.add(cursor)
    else:
        raise LabError("MCP pagination exceeded 100 pages", code=1, category="tool/API")
    if len({tool.name for tool in tools}) != len(tools):
        raise LabError("MCP published duplicate tool names", code=1, category="tool/API")
    return negotiated, tools


def lint_tools(tools):
    from .lint import usability_checks
    checks = []
    for tool in tools:
        try:
            Draft202012Validator.check_schema(tool.input_schema)
            if tool.output_schema:
                Draft202012Validator.check_schema(tool.output_schema)
            checks.append({"id": f"schema:{tool.name}", "status": "passed", "class": "schema error"})
        except SchemaError as exc:
            checks.append({"id": f"schema:{tool.name}", "status": "failed",
                           "class": "schema error", "detail": str(exc)})
        if not tool.description or len(tool.description.strip()) < 15:
            checks.append({"id": f"description:{tool.name}", "status": "warning",
                           "class": "heuristic usability warning", "detail": "Missing clear purpose"})
    return checks + usability_checks(tools)


async def check_contract(target, evidence):
    revision = evidence.profile["protocol_revision"]
    if revision not in CONTRACT_REVISIONS:
        raise Unsupported(f"The contract lane negotiates {', '.join(CONTRACT_REVISIONS)}; "
                          f"{revision} negotiation is not implemented")
    era, checks = protocol_era(revision), []
    offered = LATEST_MODERN_VERSION if era == "modern" else LATEST_HANDSHAKE_VERSION
    async with connect(target, run_id=evidence.run_id, evidence=evidence) as (session, db):
        async with asyncio.timeout(target.timeout_seconds * 4):
            _negotiated, tools = await discover(session, evidence, revision)
            checks += lint_tools(tools)
            checks.append({"id": "negotiation", "status": "passed",
                           "revision": session.protocol_version, "offered": offered})
            if era == "modern":
                # 2026-07-28 removed ping with the lifecycle; version negotiation is per request instead.
                checks.append(await version_rejection_check(session))
            else:
                await session.send_ping()
                checks.append({"id": "ping", "status": "passed"})
            exposed = [tool.model_dump(mode="json") for tool in tools]
            evidence.event("surface_exposed", {"tools": exposed, "transformations": [],
                                             "model_received": False})
            if target.kind == "fixture":
                calls = [
                    ("slots_list", {}, False),
                    ("reservations_create", {}, True),
                    ("reservations_create", {"slot_id": "afternoon", "contact": "Fixture Alice",
                                             "idempotency_key": "draft-key"}, False),
                    ("reservations_create", {"slot_id": "afternoon", "contact": "Fixture Alice",
                                             "idempotency_key": "draft-key"}, False),
                    ("operations_get", {"idempotency_key": "draft-key"}, False),
                ]
                schemas = {tool.name: tool.output_schema for tool in tools}
                for index, (name, arguments, expect_error) in enumerate(calls):
                    proposed = evidence.event("action_proposed", {"name": name, "arguments": arguments})
                    # Capture the offending payload before validating outputSchema.
                    # ClientSession.call_tool validates first and throws away that evidence.
                    result = await session.send_request(
                        types.CallToolRequest(params=types.CallToolRequestParams(
                            name=name, arguments=arguments)), types.CallToolResult)
                    evidence.event("tool_result", result.model_dump(mode="json"), parent_id=proposed)
                    valid = result.is_error == expect_error
                    detail = None
                    if not result.is_error and schemas[name]:
                        try:
                            Draft202012Validator(schemas[name]).validate(result.structured_content)
                        except ValidationError as exc:
                            valid, detail = False, exc.message
                    checks.append({"id": f"call:{index}:{name}",
                                   "status": "passed" if valid else "failed",
                                   "class": "documented contract violation", "detail": detail})
                snapshot = Fixture(db, evidence.run_id).snapshot()
                metrics = oracle_metrics(snapshot)
                evidence.event("state_asserted", {"snapshot": snapshot, "metrics": metrics},
                               provenance="independent SQLite oracle")
                for key, expected in {"backend.draft_count": 1,
                                      "backend.duplicate_effect_count": 0}.items():
                    checks.append({"id": key, "status": "passed" if metrics[key] == expected else "failed",
                                   "expected": expected, "actual": metrics[key], "class": "verification"})
                checks.extend(await cancellation_checks(session, db, evidence, era=era, transport=target.transport))
                try:
                    await session.send_request(types.Request(method="consumer-agent-testing-tool/unknown", params={}),
                                               types.EmptyResult, request_read_timeout_seconds=1)
                    protocol_error = None
                except MCPError as exc:
                    protocol_error = exc.code
                evidence.event("protocol_error_probe", {"code": protocol_error},
                               provenance="SDK unknown-method request, distinct from tool isError")
                checks.append({"id": "protocol-error-separation",
                               "status": "passed" if protocol_error == -32601 else "failed"})
            else:
                by_name = {tool.name: tool for tool in tools}
                if not target.contract_cases:
                    checks.append({"id": "operation-contracts", "status": "unsupported",
                                   "detail": "Supply explicit read/draft contract_cases for this target"})
                for index, case in enumerate(target.contract_cases):
                    if case.tool not in by_name:
                        checks.append({"id": f"call:{index}:{case.tool}", "status": "failed",
                                       "class": "discovery/exposure", "detail": "Declared tool absent"})
                        continue
                    proposed = evidence.event("action_proposed", case.model_dump(mode="json"),
                                               provenance="developer-declared contract case")
                    result = await session.send_request(
                        types.CallToolRequest(params=types.CallToolRequestParams(
                            name=case.tool, arguments=case.arguments)), types.CallToolResult)
                    evidence.event("tool_result", result.model_dump(mode="json"), parent_id=proposed)
                    valid, detail = result.is_error == case.expect_error, None
                    schema = by_name[case.tool].output_schema
                    if not result.is_error and schema:
                        try:
                            Draft202012Validator(schema).validate(result.structured_content)
                        except ValidationError as exc:
                            valid, detail = False, exc.message
                    checks.append({"id": f"call:{index}:{case.tool}",
                        "status": "passed" if valid else "failed", "detail": detail,
                        "class": "documented contract violation",
                        "scope": "Native response contract; independent backend outcomes not asserted"})
            negotiated = session.protocol_version
            capabilities = session.server_capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
    from .conformance import SPEC_REVISIONS, upstream_conformance
    if target.kind == "fixture":
        checks.append(await fixture_disconnect_check(target, evidence))
    if target.transport == "streamable-http" and era == "modern":
        trace = evidence.manifest.get("http_transport", [])
        issued = sum(record.get("session_issued", False) for record in trace)
        methods = sorted({record["method"] for record in trace})
        # A 2026-07-28 exchange is one self-contained POST; SDK clients never send back an issued session ID.
        checks.append({"id": "stateless-http", "status": "warning" if issued else "passed",
                       "actual": {"exchanges": len(trace), "http_methods": methods, "session_ids_issued": issued},
                       "detail": f"2026-07-28 exchanges carry their own version envelope; the server issued "
                                 f"Mcp-Session-Id {issued} time(s), which 2026-era clients ignore" if issued else
                                 "2026-07-28 exchanges carried their own version envelope; no session was issued",
                       **({"class": "documented contract violation"} if issued else {})})
    elif target.transport == "streamable-http":
        trace = evidence.manifest.get("http_transport", [])
        sessions = [record for record in trace if record["session_id_sha256"]]
        if sessions:
            deleted = any(record["method"] == "DELETE" and record["status"] in (200, 204)
                          for record in trace)
            checks.append({"id": "http-session-termination",
                           "status": "passed" if deleted else "unsupported",
                           "detail": "Observed session ID and SDK close request; resumability is separate"})
        if "closed_session_status" in evidence.manifest:
            checks.append({"id": "expired-http-session",
                "status": "passed" if evidence.manifest["closed_session_status"] == 404 else "failed",
                "actual": evidence.manifest["closed_session_status"], "expected": 404})
    checks.extend(await upstream_conformance(target, evidence))
    experimental = capabilities.get("experimental", {})
    not_applicable, unverified = {}, []
    if era == "modern":
        tested = ["server/discover", "unsupported-version rejection (-32022)", "tools/list (observed pages)",
                  "published schemas"]
        not_applicable["ping"] = "2026-07-28 removed ping with the initialize lifecycle"
        if target.transport == "streamable-http":
            not_applicable["HTTP sessions"] = ("2026-07-28 exchanges are stateless: no Mcp-Session-Id to "
                                               "terminate or expire")
            tested.append("stateless HTTP exchanges")
    else:
        tested = ["initialize", "ping", "tools/list (observed pages)", "published schemas"]
    if target.kind == "fixture":
        tested += ["missing input rejection", "structured output", "application errors",
            "fixture idempotency and independent backend state", "fixture request deadline",
            "explicit cancellation", "requests usable after cancellation" if era == "modern" else
            "session usable after cancellation", "unknown-method protocol error",
            "abrupt fixture transport disconnect with request in flight"]
        not_applicable["HTTP resumability"] = "Reference JSON-response fixture has no event store"
    else:
        if target.contract_cases:
            tested += ["developer-declared read/draft cases and their response schemas"]
        unverified += ["independent target backend outcomes", "target cancellation and deadline probes"]
        unverified.append("abrupt target transport disconnects")
        if target.transport == "streamable-http":
            unverified.append("HTTP resumability")
    if revision not in SPEC_REVISIONS:
        unverified.append(f"upstream conformance scenarios (the pinned suite defines none for {revision})")
    advertised = "server/discover" if era == "modern" else "initialize"
    if experimental:
        unverified += [f"experimental extension: {name}" for name in experimental]
    else:
        not_applicable["experimental extensions"] = f"No experimental capabilities advertised at {advertised}"
    unverified += [f"extension: {name}" for name in capabilities.get("extensions") or {}]
    if target.transport == "streamable-http":
        tested += [check["id"] for check in checks
                   if check["id"] in ("http-session-termination", "expired-http-session")]
    evidence.manifest["protocol_coverage"] = {
        "revision": revision, "era": era, "negotiation": advertised, "negotiated_revision": negotiated,
        "offered_revision": offered, "tested": tested, "not_applicable": not_applicable,
        "unverified": unverified, "advertised_capabilities": capabilities,
        "scope": "selected direct SDK and upstream scenarios; not complete MCP certification",
    }
    return checks


async def version_rejection_check(session):
    """2026-07-28: a discover at a revision the server does not serve gets -32022 naming the ones it does,
    which is how a client finds a mutual version (mcp-types 2.2.0 UnsupportedProtocolVersionErrorData)."""
    expected = {"code": UNSUPPORTED_PROTOCOL_VERSION, "supported_includes": session.protocol_version}
    try:
        raw = await session.send_discover(UNASSIGNED_REVISION)
    except MCPError as error:
        data = error.error.data if isinstance(error.error.data, dict) else {}
        supported = data.get("supported")
        actual = {"code": error.code, "supported": supported if isinstance(supported, list) else None}
        valid = error.code == UNSUPPORTED_PROTOCOL_VERSION and session.protocol_version in (actual["supported"] or [])
        return {"id": "unsupported-version-rejection", "status": "passed" if valid else "failed",
                "class": "documented contract violation", "expected": expected, "actual": actual,
                "detail": None if valid else f"server/discover at {UNASSIGNED_REVISION} was refused without "
                                             "-32022 naming the negotiated revision"}
    return {"id": "unsupported-version-rejection", "status": "failed", "class": "documented contract violation",
            "expected": expected, "actual": {"result_keys": sorted(raw)[:20]},
            "detail": f"The server answered server/discover at the unassigned revision {UNASSIGNED_REVISION}"}


async def fixture_disconnect_check(target, evidence):
    """Kill only a separate owned fixture, then observe the SDK's failed request."""
    from mcp.types import CONNECTION_CLOSED

    errors = []
    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    probe_target = Target(transport=target.transport, timeout_seconds=3)
    with tempfile.TemporaryDirectory(prefix="surface-disconnect-") as directory:
        db = Path(directory) / "fixture.sqlite"
        try:
            async with asyncio.timeout(5):
                async with connect(probe_target, db=db, run_id=evidence.run_id) as (session, _):
                    await negotiate(session, evidence.profile["protocol_revision"])
                    await session.send_request(types.CallToolRequest(
                        params=types.CallToolRequestParams(name="fixture_disconnect", arguments={})),
                        types.CallToolResult)
        except Exception as error:  # noqa: BLE001 — inspect every leaf; only connection-closed passes
            errors = leaves(error)
        snapshot = Fixture(db, evidence.run_id).snapshot()
    started = any(row["event"] == "disconnect_started" for row in snapshot["protocol_events"])
    closed = bool(errors) and all(
        isinstance(error, MCPError) and error.code == CONNECTION_CLOSED
        or target.transport == "streamable-http" and isinstance(error, httpx2.RemoteProtocolError)
        for error in errors)
    record = {"fault_reached": started, "transport": target.transport,
              "errors": [{"type": type(error).__name__,
                          "code": error.code if isinstance(error, MCPError) else None}
                         for error in errors],
              "effects": len(snapshot["effects"]),
              "scope": "separate owned fixture; abrupt process exit while a request is pending"}
    evidence.event("transport_disconnect_probe", record, provenance="native SDK and independent SQLite log")
    return {"id": "abrupt-fixture-disconnect", "status": "passed" if started and closed else "failed",
            "class": "documented contract violation", "detail": record}


async def cancellation_checks(session, db, evidence, *, era="handshake", transport="stdio"):
    """Use SDK cancellation and verify its effect independently at the target."""
    from mcp.types import REQUEST_TIMEOUT
    checks = []
    request = types.CallToolRequest(params=types.CallToolRequestParams(
        name="fixture_wait", arguments={"milliseconds": 5000}))
    try:
        await session.send_request(request, types.CallToolResult, request_read_timeout_seconds=.1)
        timed_out = False
    except MCPError as exc:
        timed_out = exc.code == REQUEST_TIMEOUT
    checks.append({"id": "request-deadline", "status": "passed" if timed_out else "failed"})
    pending = asyncio.create_task(session.send_request(request, types.CallToolResult))
    await asyncio.sleep(.1)
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    fixture = Fixture(db, evidence.run_id)
    for _ in range(100):
        events = fixture.snapshot()["protocol_events"]
        if sum(e["event"] == "wait_cancelled" for e in events) == 2:
            break
        await asyncio.sleep(.01)
    cancelled = sum(e["event"] == "wait_cancelled" for e in events)
    completed = sum(e["event"] == "wait_completed" for e in events)
    # 2026-07-28 over HTTP cancels by closing the response (no notification is posted), and processing
    # "SHOULD cease": a server that keeps going breaks a SHOULD. The pinned SDK server (mcp 2.2.0) stops
    # a handler on disconnect only in SSE mode, and the reference fixture answers in JSON.
    closing = era == "modern" and transport == "streamable-http"
    evidence.event("protocol_cancellation", {"target_events": events, "cancelled": cancelled,
        "completed": completed, "mechanism": "closed HTTP response" if closing else "notifications/cancelled"},
        provenance="SDK cancellation plus independent target SQLite events")
    stopped = cancelled == 2 and completed == 0
    checks.append({"id": "request-cancellation", "status": "passed" if stopped else "warning" if closing else "failed",
                   **({} if stopped or not closing else {
                       "class": "documented contract violation", "actual": {"cancelled": cancelled},
                       "detail": "The target kept processing after the client closed the 2026-07-28 response "
                                 "(processing SHOULD cease); the pinned SDK server stops handlers on disconnect "
                                 "only in SSE mode"})})
    if era == "modern":
        await session.list_tools()  # No ping at 2026-07-28: any request shows the connection still serves.
        checks.append({"id": "requests-after-cancellation", "status": "passed"})
    else:
        await session.send_ping()
        checks.append({"id": "session-after-cancellation", "status": "passed"})
    return checks
