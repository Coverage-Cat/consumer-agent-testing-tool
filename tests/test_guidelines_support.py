"""Shared fixtures for the guidelines tests: Cedar Stays-like listings and local fakes for the lane's
linking and MCP client modules (loopback-free: nothing here opens a socket). No tests live here."""
import copy
import sys
import types
from types import SimpleNamespace

OBJECT = {"type": "object", "properties": {}}


def tool(name, description, *, annotations=None, properties=None, **extra):
    schema = {"type": "object", "properties": properties or {}}
    return {"name": name, "description": description, "inputSchema": schema,
            **({"annotations": annotations} if annotations is not None else {}), **extra}


READ = {"readOnlyHint": True}
SENSITIVE = {"destructiveHint": True}
CEDAR = [
    tool("search_rooms", "Search available rooms for the dates and number of adults.", annotations=READ,
         properties={"check_in": {"type": "string"}, "check_out": {"type": "string"}, "adults": {"type": "integer"}}),
    tool("get_quote", "Return the total, currency, taxes, payment timing and cancellation policy for a room. "
                      "To book it, call create_booking with the quote_id.", annotations=READ,
         properties={"room_id": {"type": "string"}}),
    tool("create_booking", "Book the quoted room and charge the selected payment method.", annotations=SENSITIVE,
         properties={"quote_id": {"type": "string"}, "guest_email": {"type": "string"},
                     "idempotency_key": {"type": "string"}}),
    tool("get_booking", "Look up a booking and its status.", annotations=READ,
         properties={"booking_id": {"type": "string"}}),
    tool("cancel_booking", "Cancel a booking.", annotations=SENSITIVE, properties={"booking_id": {"type": "string"}}),
]
MANIFEST = {
    "version": 1, "kind": "muse-readiness", "connector": "cedar-stays",
    "documentation": {"url": "https://docs.cedar.example/mcp", "covers": [
        "setup-instructions", "inputs-and-outputs", "read-write-classifications", "sensitive-designations",
        "permissions", "side-effects", "status-handling", "errors", "rate-limits"]},
    "authentication": {"method": "oauth", "scopes": ["bookings.read", "bookings.write"],
                       "read_only_scopes": ["bookings.read"], "credentials_environment": "test"},
    "test_account": {"sign_in_instructions": "Sign in with the reviewer account from the submission form.",
                     "required_permissions": "bookings.read and bookings.write",
                     "representative_test_data": "Three Portland properties with December availability.",
                     "limits": "none", "accessible_throughout_review": True},
    "transactions": {"has_transaction_tools": True, "tools": ["create_booking", "cancel_booking"],
                     "no_charge_test_path": "The test account uses the processor's test mode; no card is charged."},
    "classes": {"search_rooms": "read", "get_quote": "read", "create_booking": "sensitive-write",
                "get_booking": "read", "cancel_booking": "sensitive-write"},
}
PROFILE = SimpleNamespace(name="generic-sdk", basis="official SDK")


class FakeCredential:
    def __init__(self, secrets=(), description=None):
        self.secrets = list(secrets)
        self.description = description or {"mode": "none"}

    def apply(self, url, headers):
        return url, headers

    async def refresh(self):
        return False

    def describe(self):
        return dict(self.description)


class FakeClient:
    """A ConnectorClient that serves a fixed listing and refuses tools/call outright."""

    def __init__(self, tools, *, instructions=None, fail_on=None):
        self.tools, self.instructions, self.fail_on = copy.deepcopy(tools), instructions, fail_on
        self.server_info = {"name": "cedar-stays", "version": "1.4.0"}
        self.protocol_version, self.capabilities = "2025-06-18", {"tools": {"listChanged": False}}
        self.observations, self.events, self.profile = [{"kind": "session-per-call"}], [], PROFILE

    async def connect(self):
        self.events.append("connect")
        if self.fail_on == "connect":
            raise ConnectionRefusedError("connection refused")
        if self.fail_on == "unsupported":  # What the unofficial template raises for a redirecting endpoint.
            from consumer_agent_testing_tool.errors import Unsupported
            raise Unsupported("MCP initialize failed: the endpoint redirected (HTTP 307); the unofficial Muse "
                              "template follows no redirects")

    async def list_tools(self):
        self.events.append("tools/list")
        return copy.deepcopy(self.tools)

    async def call_tool(self, name, arguments):  # pragma: no cover — a readiness check never calls a tool
        self.events.append(f"tools/call {name}")
        raise AssertionError("readiness checks must never call tools")

    async def close(self):
        self.events.append("close")


def install_lane(monkeypatch, client, credential=None, *, link_error=None):
    """Replace the lane's linking and mcp_clients modules for one test; returns the recorded link calls."""
    calls = []
    linking = types.ModuleType("consumer_agent_testing_tool.linking")

    async def link(auth, endpoint, *, store, http_policy):
        calls.append({"auth": auth, "endpoint": endpoint, "store": store, "http_policy": http_policy})
        if link_error:
            raise link_error
        return credential or FakeCredential()

    linking.link = link
    clients = types.ModuleType("consumer_agent_testing_tool.mcp_clients")
    clients.make_client = lambda profile, endpoint, credential: client
    monkeypatch.setitem(sys.modules, "consumer_agent_testing_tool.linking", linking)
    monkeypatch.setitem(sys.modules, "consumer_agent_testing_tool.mcp_clients", clients)
    return calls


def fake_classify(tool, declared=None, *, important_read=False):
    """The permissions.classify_tool contract in miniature, for sheet logic tests only."""
    annotations = tool.get("annotations") or {}
    if annotations.get("readOnlyHint") is True:
        return SimpleNamespace(tool_class="read", basis="annotation", reasons=["annotations.readOnlyHint=true"])
    if annotations.get("destructiveHint") is True:
        return SimpleNamespace(tool_class="sensitive-write", basis="annotation",
                               reasons=["annotations.destructiveHint=true"])
    if tool["name"].startswith(("get_", "list_")):
        return SimpleNamespace(tool_class="read", basis="heuristic", reasons=["name verb 'get' (read)"])
    return SimpleNamespace(tool_class="write", basis="default", reasons=["unknown tools stay write-gated"])
