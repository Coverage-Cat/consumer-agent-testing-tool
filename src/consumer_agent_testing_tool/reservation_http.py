"""Local-only REST/portal adapter with a separate runner-owned Unix control socket."""
import argparse
import json
import os
import secrets
import signal
import socket
import socketserver
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import LabError
from .fixture import FixtureError
from .fixture_auth import AuthenticationError
from .fixture_http import FixtureHTTPServer
from .reservation_fixture import TRANSACTIONS, ReservationFixture
from .serialization import loads
from .socket_paths import socket_address
from .surface import inside


def route(method, path, body):
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if any("/" in part or part in (".", "..") for part in parts):
        raise FixtureError(404, "Unknown route")
    if method == "GET":
        if parts == ["slots"]:
            return "slots.list", {}
        if len(parts) == 2 and parts[0] in ("slots", "reservations", "operations"):
            key = "idempotency_key" if parts[0] == "operations" else "id"
            return parts[0] + ".get", {key: parts[1]}
    if method == "POST" and parts == ["reservations"]:
        return "reservations.create", body
    if method == "POST" and len(parts) == 3 and parts[0] == "operations" and parts[2] == "cancel":
        if set(body) != {"operation"}:
            raise FixtureError(400, "Operation name is required; key comes from the route")
        return "operations.cancel", {**body, "idempotency_key": parts[1]}
    if len(parts) >= 2 and parts[0] == "reservations" and "id" in body:
        raise FixtureError(400, "Reservation ID comes from the route")
    if method == "PATCH" and len(parts) == 2 and parts[0] == "reservations":
        return "reservations.revise", {**body, "id": parts[1]}
    if method == "POST" and len(parts) == 3 and parts[0] == "reservations" \
            and parts[2] in ("confirm", "cancel"):
        return "reservations." + parts[2], {**body, "id": parts[1]}
    raise FixtureError(404, "Unknown route")


class ReservationServer:
    def __init__(self, fixture, control_path, *, port=0, portal_principal="alice", canary_options=None):
        if portal_principal not in ("alice", "bob"):
            raise FixtureError(400, "Unknown portal principal")
        self.fixture, self.control_path = fixture, Path(control_path)
        self.portal_principal = portal_principal
        self.cookie = secrets.token_urlsafe(32)
        # Disposable fixture credentials, emitted only to the trusted launcher.
        self.tokens = {secrets.token_urlsafe(32): actor for actor in ("alice", "bob")}
        self.browser_canary = None
        self.canary_options = canary_options or {}
        owner = self

        class Public(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *args):
                pass

            def respond(self, status, result, *, html=False, cookie=False):
                data = result.encode() if html else json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                if status == 429:
                    # The transient fixture accepts an identical retry; signal it like a real service.
                    self.send_header("Retry-After", "1")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 f"connect-src 'self' {owner.browser_canary.origin if owner.browser_canary else ''}; "
                                 "frame-ancestors 'none'; form-action 'self'")
                if cookie:
                    self.send_header("Set-Cookie", f"fixture_session={owner.cookie}; HttpOnly; SameSite=Strict; Path=/")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A disconnected client cannot roll back an already committed transaction.

            def principal(self):
                header = self.headers.get("Authorization", "")
                if header:
                    if not header.startswith("Bearer ") or header[7:] not in owner.tokens:
                        raise FixtureError(401, "Unknown credential")
                    return owner.tokens[header[7:]]
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                supplied = cookie.get("fixture_session")
                if not supplied or not secrets.compare_digest(supplied.value, owner.cookie):
                    raise FixtureError(401, "Session required")
                return owner.portal_principal

            def handle_public(self):
                try:
                    host = self.headers.get("Host")
                    if host != urlsplit(owner.origin).netloc:
                        raise FixtureError(403, "Unexpected host")
                    origin = self.headers.get("Origin")
                    if origin and origin != owner.origin:
                        raise FixtureError(403, "Cross-origin request denied")
                    url = urlsplit(self.path)
                    if url.query or url.fragment or url.netloc:
                        raise FixtureError(400, "Query parameters and absolute URLs are not supported")
                    if self.command == "GET" and url.path == "/portal":
                        html = files("consumer_agent_testing_tool").joinpath("data/fixtures/reservations.html").read_text()
                        if owner.browser_canary:
                            # The packaged portal is an HTML fragment, without a
                            # closing body tag. Append before sending it to Chromium.
                            html += owner.browser_canary.portal()
                        self.respond(200, html, html=True, cookie=True)
                        return
                    principal = self.principal()
                    body = {}
                    if self.command != "GET":
                        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                            raise FixtureError(415, "JSON content type required")
                        if self.headers.get("Transfer-Encoding"):
                            raise FixtureError(400, "Chunked requests are not supported")
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 65536:
                            raise FixtureError(400, "Invalid request size")
                        body = loads(self.rfile.read(length).decode(), json_only=True)
                        if not isinstance(body, dict):
                            raise FixtureError(400, "Expected a JSON object")
                    operation, args = route(self.command, url.path, body)
                    grant_id = owner.fixture.staged_grant(principal, operation, args) \
                        if operation in TRANSACTIONS else None
                    result = owner.fixture.dispatch(principal, operation, args, grant_id=grant_id)
                    self.respond(202 if isinstance(result, dict) and result.get("status") == "pending" else 200, result)
                except TimeoutError:
                    # Genuine transport response loss after the database transaction.
                    self.close_connection = True
                except FixtureError as error:
                    detail = {"class": error.category, "auth_state": error.observation["state"]} \
                        if isinstance(error, AuthenticationError) else {}
                    self.respond(error.status, {"error": str(error), **detail})
                except (LabError, ValueError, TypeError, KeyError):
                    self.respond(400, {"error": "Invalid request"})

            do_GET = handle_public
            do_POST = handle_public
            do_PATCH = handle_public

        class Control(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    line = self.rfile.readline(65537)
                    if len(line) > 65536:
                        raise FixtureError(400, "Control request too large")
                    request = loads(line.decode(), json_only=True)
                    result = owner.control(request)
                    response = {"result": result}
                except (FixtureError, LabError, KeyError, TypeError, ValueError) as error:
                    response = {"error": str(error)}
                self.wfile.write(json.dumps(response).encode() + b"\n")

        self.http = FixtureHTTPServer(("127.0.0.1", port), Public)
        self.http.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.http.server_port}"
        self.control_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Do not unlink an existing socket: it may belong to a live fixture.
            self.private = socketserver.ThreadingUnixStreamServer(socket_address(self.control_path), Control)
        except BaseException:
            self.http.server_close()
            raise
        self.private.daemon_threads = True
        os.chmod(self.control_path, 0o600)
        self.threads = []

    def control(self, request):
        """Called only by the runner-side socket handler, never by public HTTP."""
        operation = request["operation"]
        if operation == "reset":
            return self.fixture.reset(request.get("clock", "2026-10-01T17:00:00Z"))
        if operation == "snapshot":
            return self.fixture.snapshot()
        if operation == "browser_canary_snapshot" and self.browser_canary:
            return self.browser_canary.snapshot()
        if operation == "advance":
            return self.fixture.advance(request["clock"])
        if operation == "change_total":
            return self.fixture.change_total(request["amount_minor"], request.get("slot_id", "afternoon"))
        if operation == "event":
            return self.fixture.apply_event(request["event_id"], request["action"], request["args"])
        if operation == "schedule_fault":
            return self.fixture.schedule_fault(request["target_operation"], request["key"], request["fault"],
                ready_at=request.get("ready_at"), principal=request.get("principal", "alice"))
        if operation == "grant":
            return self.fixture.issue_grant(request["principal"], request["target_operation"],
                request["arguments"], decision=request["decision"], expires_at=request["expires_at"],
                proposal_id=request.get("proposal_id"))
        if operation == "revoke":
            # Check ownership before revoking, including when several runs share a database.
            if not any(row["id"] == request["grant_id"] for row in self.fixture.snapshot()["grants"]):
                raise FixtureError(404, "Grant not found in this run")
            self.fixture.broker.revoke(request["grant_id"])
            return {"revoked": True}
        raise FixtureError(404, "Unknown control operation")

    def start(self):
        if self.fixture.variant == "hostile_portal":
            from .browser_canary import BrowserCanary
            self.browser_canary = BrowserCanary(**self.canary_options)
        for server in (self.http, self.private):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def close(self):
        for server in (self.http, self.private):
            if self.threads:
                server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)
        self.control_path.unlink(missing_ok=True)
        if self.browser_canary:
            self.browser_canary.close()


@contextmanager
def serve(fixture, control_path, **options):
    server = ReservationServer(fixture, control_path, **options)
    try:
        server.start()
        yield server
    finally:
        server.close()


def main():
    parser = argparse.ArgumentParser(description="Local synthetic REST/portal reservation fixture")
    parser.add_argument("--database", default=".cache/reservations.sqlite")
    parser.add_argument("--control", default=".cache/reservations.sock")
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--variant", choices=[
        "good", "false_success", "false_confirm", "stale_grant", "duplicate_confirm",
        "false_operation_cancel"], default="good")
    parser.add_argument("--principal", choices=["alice", "bob"], default="alice")
    parser.add_argument("--control-request", help="Runner-only JSON request sent over the Unix control socket")
    args = parser.parse_args()
    root = Path.cwd()
    database, control = inside(root, args.database), inside(root, args.control)
    if args.control_request:
        request = loads(args.control_request, json_only=True)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect(socket_address(control))
            connection.sendall(json.dumps(request).encode() + b"\n")
            response = json.loads(connection.makefile("rb").readline(1024 * 1024))
        print(json.dumps(response))
        if "error" in response:
            raise SystemExit(1)
        return
    if not args.run_id:
        parser.error("--run-id is required when starting a fixture")
    database.parent.mkdir(parents=True, exist_ok=True)
    fixture = ReservationFixture(database, args.run_id, args.variant)
    # Install handlers before announcing readiness, including for background jobs
    # that inherited SIGINT ignored. Handlers only flag; they take no locks.
    stopping = []
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopping.append(True))
    # Seeding/reset is explicit on the control socket so a restart does not erase evidence.
    with serve(fixture, control, port=args.port, portal_principal=args.principal) as server:
        print(json.dumps({"origin": server.origin, "control": str(control),
                          "test_credentials": {actor: token for token, actor in server.tokens.items()}}),
              flush=True)
        while not stopping:
            time.sleep(0.1)


if __name__ == "__main__":
    main()
