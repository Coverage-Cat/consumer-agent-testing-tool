"""Loopback fakes for the connector-lane MCP client and linking tests (no tests of their own).

- ``FakeMcp``: a scriptable legacy Streamable HTTP endpoint (stdlib server) that records every
  request, for template-client protocol behaviors.
- ``sdk_app``: the official SDK low-level server (both protocol eras).
- ``OAuthFixture``: an in-memory OAuth authorization server and protected MCP resource built from
  the SDK's server auth pieces, with a test login form and failure controls, so these tests do not
  depend on connector_fixture.py (test_linking_connector_fixture.py covers that integration).
"""
import json
import secrets
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl, urlsplit

import anyio
import mcp_types as types
import uvicorn
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    ProviderTokenVerifier,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

TOOL_NAMES = [["search_rooms", "get_quote"], ["create_booking"]]


def tool(name):
    entry = {"name": name, "description": f"{name.replace('_', ' ')} at Cedar Stays",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
             "annotations": {"readOnlyHint": name != "create_booking"}}
    if name == "get_quote":
        entry["outputSchema"] = {"type": "object", "properties": {"total_minor": {"type": "integer"}},
                                 "required": ["total_minor"]}
    return entry


@contextmanager
def serve_asgi(build):
    """Run ``build(base_url)`` on uvicorn in a thread on a pre-bound loopback port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = build(base)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def sdk_app(base, *, json_response=False, stateless=False):
    async def list_tools(context, params):
        page = 1 if params is not None and params.cursor == "p2" else 0
        return types.ListToolsResult(tools=[types.Tool.model_validate(tool(name)) for name in TOOL_NAMES[page]],
                                     next_cursor="p2" if page == 0 else None)

    async def call_tool(context, params):
        name, arguments = params.name, params.arguments or {}
        if name == "slow":
            await anyio.sleep(float(arguments.get("seconds", 2)))
        if name == "get_quote":  # violates its own outputSchema: total_minor must be an integer
            return types.CallToolResult(content=[types.TextContent(type="text", text="quote")],
                                        structured_content={"total_minor": "forty"})
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(arguments))],
                                    structured_content={"echo": arguments, "tool": name})

    server = Server("catt-sdk-fixture", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool)
    return server.streamable_http_app(json_response=json_response, stateless_http=stateless)


class FakeMcp:
    """A legacy Streamable HTTP MCP endpoint whose behavior each test scripts through options."""

    def __init__(self, **options):
        # notification_status: the HTTP status notifications/initialized gets (202 Accepted by default);
        # session_cursors: nextCursor names the session that issued it and works only in that session.
        self.options = {"version": None, "sse": False, "capabilities": {"tools": {}}, "pages": TOOL_NAMES,
                        "session": True, "redirect": None, "require": None, "cursor_loop": False,
                        "delay": 0.0, "initialize_error": None, "needs_prior_session": False,
                        "duplicate": False, "forbid": None, "notification_status": 202, "session_cursors": False,
                        **options}
        self.requests, self.calls, self.sessions, self.listed = [], [], [], set()
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.record(self, None)
                fake.reply(self, 405)

            do_DELETE = do_GET

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.loads(raw) if raw else None
                fake.record(self, body)
                fake.post(self, body)

        from consumer_agent_testing_tool.fixture_http import FixtureHTTPServer
        self.http = FixtureHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.http.handle_error = lambda request, address: None  # dropped and timed-out clients are expected
        threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    @property
    def url(self):
        return f"{self.base}/mcp"

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def record(self, handler, body):
        with self.lock:
            self.requests.append({"http": handler.command, "path": handler.path, "body": body,
                                  "headers": {key.lower(): value for key, value in handler.headers.items()}})

    def methods(self):
        return [(row["http"], (row["body"] or {}).get("method")) for row in self.requests]

    @staticmethod
    def reply(handler, status, payload=None, *, headers=(), sse=False, raw=None, length=None):
        if raw is not None:
            data = raw
        elif payload is None:
            data = b""
        elif sse:
            data = b"".join(f"event: message\ndata: {json.dumps(item)}\n\n".encode() for item in payload)
        else:
            data = json.dumps(payload).encode()
        handler.send_response(status)
        for key, value in headers:
            handler.send_header(key, value)
        if payload is not None or raw is not None:
            handler.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        handler.send_header("Content-Length", str(length if length is not None else len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def post(self, handler, body):
        options = self.options
        if options["redirect"] and urlsplit(handler.path).path == "/mcp":
            return self.reply(handler, 307, headers=[("Location", options["redirect"])])
        if options["require"] and not options["require"](handler):
            return self.reply(handler, 401, {"error": "unauthorized"},
                              headers=[("WWW-Authenticate", 'Bearer error="invalid_token"')])
        if options["forbid"]:
            return self.reply(handler, 403, {"error": "forbidden"},
                              headers=[("WWW-Authenticate", f'Bearer error="{options["forbid"]}"')])
        method, request_id = body.get("method"), body.get("id")
        session = handler.headers.get("mcp-session-id")
        if method == "server/discover":
            return self.reply(handler, 200, {"jsonrpc": "2.0", "id": request_id,
                                             "error": {"code": -32601, "message": "Method not found"}})
        if method == "initialize":
            if options["initialize_error"]:
                return self.reply(handler, 400, {"jsonrpc": "2.0", "id": request_id,
                                                 "error": options["initialize_error"]})
            version = options["version"] or body["params"]["protocolVersion"]
            headers = []
            if options["session"]:
                session = uuid.uuid4().hex
                with self.lock:
                    self.sessions.append(session)
                headers.append(("Mcp-Session-Id", session))
            result = {"protocolVersion": version, "capabilities": options["capabilities"],
                      "serverInfo": {"name": "fake-mcp", "version": "7"}, "instructions": "Book rooms."}
            return self.respond(handler, request_id, result, headers=headers)
        if options["session"] and session not in self.sessions:
            return self.reply(handler, 400 if session is None else 404, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600,
                "message": "Bad Request: Missing session ID" if session is None else "Session not found"}})
        if method == "notifications/initialized":
            return self.reply(handler, options["notification_status"])
        if method == "tools/list":
            with self.lock:
                self.listed.add(session)
            cursor = (body.get("params") or {}).get("cursor")
            if cursor and options["session_cursors"]:
                issued, _, cursor = cursor.rpartition(":")
                if issued != session:  # Paging state lives in the session that issued the cursor.
                    return self.respond(handler, request_id, None, error={"code": -32602, "message": "Invalid cursor"})
            index = int(cursor.removeprefix("page-")) if cursor and not options["cursor_loop"] else 0
            names = list(options["pages"][index]) + (["search_rooms"] if options["duplicate"] and index else [])
            result = {"tools": [tool(name) | {"x-vendor": "kept"} for name in names]}
            if options["cursor_loop"]:
                result["nextCursor"] = "same"
            elif index + 1 < len(options["pages"]):
                result["nextCursor"] = (f"{session}:" if options["session_cursors"] else "") + f"page-{index + 1}"
            return self.respond(handler, request_id, result)
        if method == "tools/call":
            return self.call(handler, request_id, body["params"], session)
        return self.respond(handler, request_id, None, error={"code": -32601, "message": "Method not found"})

    def respond(self, handler, request_id, result, *, headers=(), error=None, extra=()):
        message = {"jsonrpc": "2.0", "id": request_id, **({"error": error} if error else {"result": result})}
        if self.options["sse"]:
            return self.reply(handler, 200, [*extra, message], headers=headers, sse=True)
        return self.reply(handler, 200, message, headers=headers)

    def call(self, handler, request_id, params, session):
        name, arguments = params["name"], params.get("arguments") or {}
        with self.lock:
            self.calls.append({"name": name, "arguments": arguments, "session": session})
        text = [{"type": "text", "text": json.dumps(arguments)}]
        if name == "slow":
            time.sleep(self.options["delay"])
        if name == "drop":  # headers promise a body that never arrives
            return self.reply(handler, 200, raw=b'{"jsonrpc": "2.0", "id"', length=500)
        if name == "server_error":
            return self.reply(handler, 503, raw=b"upstream unavailable")
        if name == "fail":
            return self.respond(handler, request_id, None, error={"code": -32602, "message": "Invalid arguments"})
        if name == "book" and self.options["needs_prior_session"] and session not in self.listed:
            return self.reply(handler, 400, {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32600, "message": "Bad Request: session was not initialized by an earlier call"}})
        if name == "is_error":
            return self.respond(handler, request_id, {"content": text, "isError": True})
        if name == "session_bound":
            return self.respond(handler, request_id, {"isError": True, "content": [
                {"type": "text", "text": "Quote q-1 belongs to an earlier MCP session; request a new quote."}]})
        if name == "domain_session":  # A yoga "session": domain vocabulary, not MCP state.
            return self.respond(handler, request_id, {"isError": True, "content": [
                {"type": "text", "text": json.dumps({"error": "session_full", "message": "That session is full."})}]})
        if name == "domain_session_400":  # The same, as a JSON-RPC validation error some frameworks send with 400.
            return self.reply(handler, 400, {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32602, "message": "Invalid params: session s-100 is full"}})
        if name in ("server_error_32000", "server_error_32001"):  # Ordinary server error codes.
            code = int(name[-5:]) * -1
            return self.respond(handler, request_id, None, error={"code": code, "message": "Payment declined: "
                                                                                          "insufficient funds"})
        if name == "long_error":
            return self.respond(handler, request_id, None, error={"code": -32603, "message": "E" * 300_000})
        if name == "big":
            payload = {"jsonrpc": "2.0", "id": request_id, "result": {"content": [
                {"type": "text", "text": "A" * self.options.get("big_bytes", 4096)}]}}
            return self.reply(handler, 200, payload)
        if name == "elicit":  # The server asks the client first and answers only after a reply that never comes.
            question = {"jsonrpc": "2.0", "id": "srv-elicit", "method": "elicitation/create",
                        "params": {"message": "Confirm the recipient", "requestedSchema": {"type": "object"}}}
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(f"event: message\ndata: {json.dumps(question)}\n\n".encode())
            handler.wfile.flush()
            time.sleep(self.options.get("delay") or 1.0)
            return None
        if name == "drip":  # Each byte arrives before the per-read timeout; the whole reply takes long.
            data = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"content": [
                {"type": "text", "text": "ok"}]}}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            for index in range(len(data)):
                handler.wfile.write(data[index:index + 1])
                handler.wfile.flush()
                if index < 12:
                    time.sleep(self.options.get("drip_seconds", 0.25))
            return None
        extra = ()
        if name == "chatty":
            extra = ({"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage",
                      "params": {"messages": [], "maxTokens": 5}},
                     {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "x"}})
        return self.respond(handler, request_id, {"content": text, "structuredContent": {"echo": arguments}},
                            extra=extra)


class LoopbackClient(OAuthClientInformationFull):
    """RFC 8252 section 7.3: a loopback redirect URI matches on any port."""

    def validate_redirect_uri(self, redirect_uri):
        if redirect_uri is not None and redirect_uri.scheme == "http" and redirect_uri.host == "127.0.0.1" \
                and redirect_uri.path == "/callback":
            return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


class SubjectToken(OAuthToken):
    sub: str | None = None  # non-standard; real servers would use an id_token or introspection


class OAuthFixture:
    """An in-memory OAuth AS plus protected MCP resource: PRM, AS metadata, DCR, PKCE S256,
    authorization code through a test login form, refresh rotation and revocation."""

    def __init__(self, *, password="correct-horse", scopes=("read", "book"), required=("read",),
                 ttl=3600, refresh=True, subject_in_token=True, cimd=False, pkce=True, metadata=True,
                 authorization_endpoint=None):
        # Any username signs in with the fixture password; the username becomes the token subject.
        self.password, self.scopes, self.required, self.ttl = password, list(scopes), list(required), ttl
        self.refresh = refresh
        self.subject_in_token, self.cimd, self.pkce, self.metadata = subject_in_token, cimd, pkce, metadata
        self.authorization_endpoint = authorization_endpoint
        self.clients, self.pending, self.codes, self.access, self.refresh_tokens = {}, {}, {}, {}, {}
        self.authorizations, self.logins, self.registrations, self.token_requests = [], [], 0, []
        self.base = None

    # OAuthAuthorizationServerProvider
    async def get_client(self, client_id):
        if client_id in self.clients:
            return self.clients[client_id]
        if self.cimd and client_id.startswith("https://"):
            # The fixture trusts CIMD client ids without fetching the document (no network in tests).
            return LoopbackClient(client_id=client_id, token_endpoint_auth_method="none",
                                  scope=" ".join(self.scopes))
        return None

    async def register_client(self, client_info):
        self.registrations += 1
        self.clients[client_info.client_id] = LoopbackClient.model_validate(client_info.model_dump())

    def preregister(self, client_id, secret, scopes=None):
        self.clients[client_id] = LoopbackClient(client_id=client_id, client_secret=secret,
                                                 token_endpoint_auth_method="client_secret_post" if secret else "none",
                                                 scope=" ".join(scopes or self.scopes))

    async def authorize(self, client, params):
        request = secrets.token_urlsafe(8)
        self.pending[request] = (client.client_id, params)
        self.authorizations.append({"client_id": client.client_id, "resource": params.resource,
                                    "scopes": params.scopes, "challenge": bool(params.code_challenge)})
        return f"{self.base}/login?request={request}"

    async def load_authorization_code(self, client, code):
        return self.codes.get(code)

    def issue(self, client_id, scopes, resource, subject):
        access = secrets.token_urlsafe(24)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes, resource=resource,
                                          subject=subject, expires_at=int(time.time()) + self.ttl)
        refresh = None
        if self.refresh:
            refresh = secrets.token_urlsafe(24)
            self.refresh_tokens[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
                                                        resource=resource, subject=subject)
        return SubjectToken(access_token=access, refresh_token=refresh, expires_in=self.ttl, scope=" ".join(scopes),
                            sub=subject if self.subject_in_token else None)

    async def exchange_authorization_code(self, client, code):
        self.codes.pop(code.code, None)
        self.token_requests.append("authorization_code")
        return self.issue(client.client_id, code.scopes, code.resource, code.subject)

    async def load_refresh_token(self, client, token):
        return self.refresh_tokens.get(token)

    async def exchange_refresh_token(self, client, token, scopes):
        if self.refresh_tokens.pop(token.token, None) is None:
            raise TokenError(error="invalid_grant", error_description="refresh token was revoked")
        self.token_requests.append("refresh_token")
        return self.issue(client.client_id, scopes, token.resource, token.subject)

    async def load_access_token(self, token):
        return self.access.get(token)

    async def revoke_token(self, token):
        self.access.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)

    # Test controls
    def revoke_all(self):
        self.access.clear()
        self.refresh_tokens.clear()

    def expire_all(self, *, refresh=False):
        for token in [*self.access.values(), *(self.refresh_tokens.values() if refresh else ())]:
            token.expires_at = 1  # in the past; 0 would read as "never expires"

    async def login_page(self, request: Request):
        if request.method == "GET":
            ticket = request.query_params.get("request", "")
            return HTMLResponse(
                f'<form method="post" action="/login"><input type="hidden" name="request" value="{ticket}">'
                '<input type="text" name="username"><input type="password" name="password">'
                '<button type="submit" name="decision" value="deny">Deny</button>'
                '<button type="submit" name="decision" value="approve">Allow</button></form>')
        form = await request.form()
        self.logins.append(form.get("username"))
        if form.get("password") != self.password:
            return HTMLResponse('<form method="post" action="/login"><p>Wrong password</p>'
                                f'<input type="hidden" name="request" value="{form.get("request")}">'
                                '<input type="text" name="username"><input type="password" name="password">'
                                '<button type="submit" name="decision" value="approve">Allow</button></form>')
        client_id, params = self.pending.pop(form["request"])
        if form.get("decision") != "approve":
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), error="access_denied",
                                                           state=params.state), status_code=302)
        code = secrets.token_urlsafe(24)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or self.required, expires_at=time.time() + 300, client_id=client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
            subject=form["username"])
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)

    def app(self, base):
        self.base = base

        async def list_tools(context, params):
            return types.ListToolsResult(tools=[types.Tool.model_validate(tool("search_rooms"))])

        async def call_tool(context, params):
            return types.CallToolResult(content=[types.TextContent(type="text", text="ok")],
                                        structured_content={"rooms": 2})

        server = Server("catt-oauth-fixture", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool)
        registration = ClientRegistrationOptions(enabled=True, valid_scopes=self.scopes)
        settings = AuthSettings(issuer_url=AnyHttpUrl(base), resource_server_url=AnyHttpUrl(f"{base}/mcp"),
                                validate_token_resource=True, client_registration_options=registration,
                                revocation_options=RevocationOptions(enabled=True), required_scopes=self.required)
        inner = server.streamable_http_app(
            auth=settings, token_verifier=ProviderTokenVerifier(self), auth_server_provider=self,
            custom_starlette_routes=[Route("/login", self.login_page, methods=["GET", "POST"])])
        document = build_metadata(AnyHttpUrl(base), None, registration, RevocationOptions(enabled=True)).model_dump(
            mode="json", exclude_none=True)
        document["client_id_metadata_document_supported"] = self.cimd
        if not self.pkce:
            document.pop("code_challenge_methods_supported")
        if self.authorization_endpoint:
            document["authorization_endpoint"] = self.authorization_endpoint

        async def wrapped(scope, receive, send):
            path = scope.get("path", "")
            if scope["type"] == "http" and path == "/.well-known/oauth-authorization-server":
                response = JSONResponse(document) if self.metadata else JSONResponse({}, status_code=404)
                return await response(scope, receive, send)
            if scope["type"] == "http" and not self.metadata and path.startswith("/.well-known/"):
                return await JSONResponse({}, status_code=404)(scope, receive, send)
            return await inner(scope, receive, send)
        return wrapped


def query(url):
    return dict(parse_qsl(urlsplit(url).query))
