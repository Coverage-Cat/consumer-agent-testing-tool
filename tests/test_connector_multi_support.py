"""Shared fakes for the multi-connector runner tests; this module holds no tests.

Two in-memory hotels on two loopback origins, each behind its own fake MCP client and credential (the
lane's real types), an oracle that answers per origin, and small test extensions that use the runner's
extension API (run.connect, run.call_tool, run.add_check, run.resume_message, private values).
"""
import copy
import json
from typing import Any, ClassVar

import httpx
from inspect_ai.tool import ToolDef, ToolParam, ToolParams
from pydantic import BaseModel, ConfigDict
from test_connector_support import (
    API_KEY,
    ORIGIN,
    QUOTE_JOIN,
    FakeClient,
    FakeCredential,
    Hotel,
    scenario,
)

from consumer_agent_testing_tool.connector_run import BaseExtension, Services
from consumer_agent_testing_tool.errors import LabError

PINE = "http://127.0.0.1:8766"
PINE_KEY = "catt-pine-api-key-5b1e8d44"


def pine_connector(**overrides):
    """A second hotel connector on its own origin, with its own key and tools."""
    connector = {
        "name": "pine-lodge", "endpoint": f"{PINE}/mcp", "allowed_origins": [PINE],
        "auth": {"mode": "api_key", "env": "PINE_LODGE_KEY"}, "client_profile": "generic-sdk",
        "tools": {"allow": ["search_rooms", "get_quote", "create_booking", "get_booking"],
                  "classes": {"create_booking": "sensitive-write"},
                  "host_arguments": {"create_booking": {"guest_email": {"$ref": "/private/email"}}},
                  "capture": {"pine_booking_id": {"from": "create_booking",
                                                  "pointer": "/structuredContent/booking_id"}},
                  "disclosure": {"create_booking": {
                      "total": {"from": "get_quote", "pointer": "/structuredContent/total_minor", "match": QUOTE_JOIN},
                      "currency": {"from": "get_quote", "pointer": "/structuredContent/currency",
                                   "match": QUOTE_JOIN},
                      "seller": {"value": "Pine Lodge"}}}}}
    connector.update(overrides)
    return connector


def two_connectors(pine=None, **overrides):
    """The booking scenario with a second connector (pine-lodge); tool references are qualified."""
    document = scenario(expected={"tools": ["cedar-stays/get_quote", "cedar-stays/create_booking"]})
    document["connectors"].append(pine_connector(**(pine or {})))
    document["checks"] = [
        {"id": "one-cedar-booking", "source": "oracle",
         "oracle": {"url": f"{ORIGIN}/__oracle/state", "pointer": "/confirmed_count"}, "expect": {"op": "eq", "value": 1}},
        {"id": "no-pine-booking", "source": "oracle",
         "oracle": {"url": f"{PINE}/__oracle/state", "pointer": "/confirmed_count"}, "expect": {"op": "eq", "value": 0}}]
    document["answer"] = {"claims": [{"state": "confirmed", "true_when": "one-cedar-booking"}]}
    document.update(overrides)
    return document


def oracles(hotels):
    """One private oracle per origin (httpx.MockTransport), like one test service per connector."""
    def handler(request):
        hotel = hotels.get(f"{request.url.scheme}://{request.url.host}:{request.url.port}")
        if hotel is None:
            return httpx.Response(404)
        if request.url.path == "/__oracle/state" and request.method == "GET":
            return httpx.Response(200, json=hotel.state())
        if request.url.path == "/__oracle/reset" and request.method == "POST":
            hotel.reset()
            return httpx.Response(204)
        return httpx.Response(404)
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def multi_services(hotels=None, *, secrets=None, link_errors=None):
    """The runner's outside-world boundaries for several connectors: each origin's own hotel, credential
    and client. `link_errors` maps an origin to the error linking it raises."""
    hotels = hotels or {ORIGIN: Hotel(), PINE: Hotel(total_minor=39000)}
    secrets = secrets or {ORIGIN: (API_KEY,), PINE: (PINE_KEY,)}
    linked, made = [], []

    def origin(endpoint):
        return endpoint.rsplit("/", 1)[0]

    async def link(auth, endpoint, *, store, http_policy, redactor=None):
        linked.append({"auth": auth.model_dump(mode="json"), "endpoint": endpoint, "profile": http_policy.name})
        if (error := (link_errors or {}).get(origin(endpoint))) is not None:
            raise error
        credential = FakeCredential(secrets.get(origin(endpoint), ()))
        if redactor is not None:
            redactor.add(credential.secrets)
        return credential

    def make_client(profile, endpoint, credential, *, allowed_tools=None, allow_loopback=True):
        made.append(FakeClient(hotels[origin(endpoint)], profile, endpoint, credential, allowed_tools))
        return made[-1]
    result = Services(link=link, make_client=make_client, http=oracles(hotels))
    result.linked, result.clients, result.hotels = linked, made, hotels
    return result


def calls_of(hotel):
    return [name for name, _ in hotel.calls]


async def ready(tmp_path, document, made=None):
    """A run after setup, as an extension sees it while the agent runs (no driver involved)."""
    from test_connector_support import services

    from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
    from consumer_agent_testing_tool.connector_scenario import parse_scenario
    run = ConnectorRun(parse_scenario(document, base=tmp_path), driver=ScriptedDriver([{"answer": "unused"}]),
                       root=tmp_path, output=tmp_path / "runs", services=made or services(Hotel()))
    await run.setup()
    run.agent_started, run.stage = True, "agent"
    return run


def string_parameter(name, description):
    return ToolParams(properties={name: ToolParam(type="string", description=description)}, required=[name])


class NoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Router(BaseExtension):
    """A stand-in for the routes extension: connect_service(service) calls run.connect and tells the model
    what happened; a failed link becomes a message, never a crash."""
    config_model = NoConfig
    connected: ClassVar[list] = []

    def tools(self, run):
        async def connect_service(service: str):
            try:
                info = await run.connect(service, source="router")
            except LabError as error:
                return json.dumps({"connected": False, "service": service, "reason": error.category})
            Router.connected.append(info)
            return json.dumps({"connected": True, "service": info.name, "tools": sorted(info.model_tools)})
        return [ToolDef(connect_service, name="connect_service", description="Connect one of your user's services.",
                        parameters=string_parameter("service", "The service to connect."), parallel=False,
                        max_output=0)]


class Wallet(BaseExtension):
    """A stand-in for a host-side caller (wallet, background job): pay_with_wallet() books the latest quote
    of a connector through run.call_tool and reports the outcome kind."""
    config_model = NoConfig
    outcomes: ClassVar[list] = []

    def tools(self, run):
        async def pay_with_wallet(quote_id: str):
            outcome = await run.call_tool("cedar-stays", "create_booking", {"quote_id": quote_id,
                                                                           "guest_name": "Taylor Example"},
                                          source="wallet")
            Wallet.outcomes.append(copy.deepcopy(outcome))
            return json.dumps({"outcome": outcome["kind"]})
        return [ToolDef(pay_with_wallet, name="pay_with_wallet", description="Pay with your user's wallet.",
                        parameters=string_parameter("quote_id", "The quote to pay."), parallel=False,
                        max_output=0)]


def native(name, **arguments):
    """A scripted native call."""
    return {"tool": name, "arguments": arguments}


def tool_messages(api):
    return [message for message in api.inputs[-1]["messages"] if message["role"] == "tool"]


def model_tools(api, turn=0):
    return [tool["name"] for tool in api.inputs[turn]["tools"]]


def every_event_text(run):
    return (run.path / "events.jsonl").read_text()


STAY: dict[str, Any] = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
