"""Container fixture: public HTTP and a separate, runner-only Unix control socket."""
import argparse
import json
import os
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .fixture import Fixture, FixtureError
from .fixture_http import FixtureHTTPServer

CONTROL = "/control/oracle.sock"


def serve(fixture, *, public=True, canary_enabled=True):
    canary_connections = []
    servers = []
    control_lock = threading.Lock()
    quiesced = False

    class DrainingHTTPServer(FixtureHTTPServer):
        daemon_threads = False
        block_on_close = True

    class DrainingTCPServer(socketserver.ThreadingTCPServer):
        daemon_threads = False
        block_on_close = True

    class Canary(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            canary_connections.append({"peer": self.client_address[0]})
            self.request.sendall(b"consumer-agent-testing-tool-owned-canary\n")

    class Public(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise FixtureError(400, "Invalid body size")
                request = json.loads(self.rfile.read(length))
                if set(request) != {"operation", "arguments"}:
                    raise FixtureError(400, "Expected operation and arguments")
                result = fixture.dispatch("alice", request["operation"], request["arguments"])
                status = 200
            except FixtureError as exc:
                status, result = exc.status, {"error": str(exc)}
            except (ValueError, TypeError):
                status, result = 400, {"error": "Invalid JSON request"}
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except (BrokenPipeError, ConnectionResetError):
                # The client may have been stopped after the request committed.
                pass

    class Control(socketserver.StreamRequestHandler):
        def handle(self):
            nonlocal quiesced
            self.request.settimeout(5)
            try:
                request = json.loads(self.rfile.readline(65537))
                with control_lock:
                    if request["operation"] == "quiesce":
                        if not quiesced:
                            for server in servers:
                                server.shutdown()
                            for server in servers:
                                server.server_close()  # Joins accepted request handlers.
                            quiesced = True
                        result = {"quiesced": True, "run_id": fixture.run_id}
                    elif request["operation"] == "reset" and not quiesced:
                        result = fixture.reset(request.get("clock", "2026-10-01T17:00:00Z"))
                    elif request["operation"] == "snapshot":
                        result = fixture.snapshot()
                        result["canary_connections"] = list(canary_connections)
                    else:
                        result = {"error": "Control operation unavailable or fixture is quiesced"}
                self.wfile.write(json.dumps(result).encode() + b"\n")
            except (ValueError, KeyError):
                self.wfile.write(b'{"error":"Invalid control request"}\n')

    os.makedirs(Path(CONTROL).parent, mode=0o700, exist_ok=True)
    control = socketserver.ThreadingUnixStreamServer(CONTROL, Control)
    os.chmod(CONTROL, 0o600)
    if canary_enabled:
        servers.append(DrainingTCPServer(("0.0.0.0", 8766), Canary))
    if public:
        servers.append(DrainingHTTPServer(("0.0.0.0", 8765), Public))
    for server in [*servers, control]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    # Quiescence closes public listeners; private snapshots remain available.
    threading.Event().wait()


def control(request):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(CONTROL)
        connection.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(connection.makefile("rb").readline())


def booking():
    # The CLI lane's agent reads this help and these errors through its shell: the program is "booking",
    # never the module path or test wording (review F5).
    parser = argparse.ArgumentParser(prog="booking", description="Reservation CLI: read operations and nonbinding drafts only.")
    parser.add_argument("operation", choices=["slots.list", "slots.get", "reservations.create",
        "reservations.get", "reservations.revise", "operations.get"])
    parser.add_argument("--args", default="{}", help="JSON object of operation arguments")
    args = parser.parse_args()
    try:
        arguments = json.loads(args.args)
    except ValueError as exc:
        parser.error(f"argument --args: not valid JSON ({exc})")
    data = json.dumps({"operation": args.operation, "arguments": arguments}).encode()
    request = Request("http://" + os.environ["FIXTURE_IP"] + ":8765", data=data,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            print(response.read().decode())
    except HTTPError as exc:
        print(json.dumps({"status": exc.code, "error": exc.read().decode()}))
        raise SystemExit(1)
    except OSError as exc:  # Unreachable or timed out: a plain error, not a traceback.
        print(json.dumps({"error": f"Reservation service unavailable ({type(exc).__name__})"}))
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "canary", "control", "control-stdin", "booking"])
    parser.add_argument("--run-id", default=os.environ.get("CONSUMER_AGENT_TESTING_TOOL_RUN_ID", "fixture"))
    parser.add_argument("--variant", default="good")
    parser.add_argument("--request", default='{"operation":"snapshot"}')
    parser.add_argument("--no-canary", action="store_true")
    args, remaining = parser.parse_known_args()
    if args.mode in ("serve", "canary"):
        serve(Fixture("/data/fixture.sqlite", args.run_id, args.variant), public=args.mode == "serve",
              canary_enabled=not args.no_canary)
    elif args.mode == "control":
        print(json.dumps(control(json.loads(args.request))))
    elif args.mode == "control-stdin":
        import sys
        print(json.dumps(control(json.loads(sys.stdin.readline()))))
    else:
        import sys
        sys.argv = [sys.argv[0], *remaining]
        booking()
