"""JSON media types, declared error bodies and host-controlled headers for developer APIs (I12)."""
# ruff: noqa: F811 — imported pytest fixtures
import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_api_scenario import call, done
from test_mapped_scenario import buy, inventory, mapping_document, run, spec_for  # noqa: F401

from consumer_agent_testing_tool.api_mapping import ApiMapping, OperationMap
from consumer_agent_testing_tool.api_transport import dispatch
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import digest

TARGET = SimpleNamespace(base_url="http://127.0.0.1:9", timeout_seconds=5, max_response_bytes=1 << 20)


class Events:
    def __init__(self):
        self.rows = []

    def event(self, kind, payload, **kwargs):
        self.rows.append((kind, payload))


def order(**request):
    problem = {"type": "object", "properties": {"type": {"type": "string"}, "title": {"type": "string"},
               "status": {"type": "integer"}}, "required": ["title", "status"]}
    return OperationMap.model_validate({
        "operation": "orders.submit", "description": "Submit one order.", "effect": "draft",
        "input_schema": {"type": "object", "properties": {
            "qty": {"type": "integer", "description": "Quantity."},
            "key": {"type": "string", "description": "Idempotency key."},
            "etag": {"type": "string", "description": "Current entity tag."}},
            "required": ["qty", "key"], "additionalProperties": False},
        "request": {"method": "POST", "path": "/orders", "body_parameters": {"qty": "qty"},
                    "header_parameters": {"Idempotency-Key": "key", "If-Match": "etag"}, **request},
        "responses": {"200": {"schema": {"type": "object"}}, "409": {"schema": problem},
                      "422": {"schema": problem}}})


async def send(operation, arguments, status, content_type):
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers))
        body = {"type": "about:blank", "title": "Conflict", "status": status}
        return httpx.Response(status, headers={"content-type": content_type}, content=json.dumps(body).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 headers={"Authorization": "Bearer host-secret"}) as client:
        events = Events()
        result = await dispatch(client, TARGET, operation, operation.request_for(arguments), events, "case")
    return result, seen["headers"], events


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8",
    "application/problem+json", "application/vnd.api+json", "application/hal+json", "Application/Problem+JSON"])
@pytest.mark.parametrize("status", [409, 422])
async def test_declared_json_error_bodies_are_returned_for_any_json_media_type(content_type, status):
    result, _, _ = await send(order(), {"qty": 1, "key": "k-1"}, status, content_type)
    assert result == {"status": status, "result": {"type": "about:blank", "title": "Conflict", "status": status}}


@pytest.mark.parametrize("content_type", ["text/plain", "application/xml", "application/jsonx", "", "json"])
async def test_non_json_media_types_are_still_rejected(content_type):
    with pytest.raises(LabError, match="Expected a JSON response") as error:
        await send(order(), {"qty": 1, "key": "k-1"}, 409, content_type)
    assert error.value.category == "tool/API"


async def test_header_parameters_are_sent_and_credentials_stay_host_owned():
    result, headers, events = await send(order(), {"qty": 1, "key": "order-7", "etag": "\"v3\""}, 200,
                                         "application/json")
    assert result["status"] == 200
    assert headers["idempotency-key"] == "order-7" and headers["if-match"] == "\"v3\""
    assert headers["authorization"] == "Bearer host-secret"
    request = next(payload for kind, payload in events.rows if kind == "network_request")
    assert request["header_names"] == ["Idempotency-Key", "If-Match"]
    assert "order-7" not in json.dumps(request)
    # Optional header arguments stay omitted.
    _, headers, _ = await send(order(), {"qty": 1, "key": "order-8"}, 200, "application/json")
    assert "if-match" not in headers


@pytest.mark.parametrize("name", ["Authorization", "Cookie", "Host", "Content-Length", "Transfer-Encoding",
    "Connection", "Proxy-Authorization", "Proxy-Anything", "X-Forwarded-For", "Sec-Fetch-Site", "Accept",
    "Content-Type", "X-HTTP-Method-Override", "X-API-Key", "X-Auth-Token", "Session-Id", "Bad Header", "X:Y"])
def test_reserved_credential_and_invalid_header_names_are_rejected(name):
    with pytest.raises(ValidationError, match="Header"):
        order(header_parameters={name: "key"})


@pytest.mark.parametrize("value", ["line\r\nInjected: yes", "nul\x00", "tab\tvalue", " padded", "", "é",
                                   "x" * 1025])
def test_header_values_cannot_split_or_smuggle_requests(value):
    with pytest.raises(LabError, match="header parameter value"):
        order().request_for({"qty": 1, "key": value})


def test_header_parameters_must_be_unique_scalar_and_mapped_once():
    with pytest.raises(ValidationError, match="unique"):
        order(header_parameters={"Idempotency-Key": "key", "idempotency-key": "etag"})
    document = order().model_dump(mode="json", by_alias=True)
    document["input_schema"]["properties"]["etag"] = {"type": "boolean", "description": "Not a header scalar."}
    with pytest.raises(ValidationError, match="Header schemas"):
        OperationMap.model_validate(document)
    document = order().model_dump(mode="json", by_alias=True)
    document["request"]["body_parameters"]["key"] = "key"
    with pytest.raises(ValidationError, match="exactly once"):
        OperationMap.model_validate(document)


def test_existing_mapping_digests_are_unchanged():
    mapping = ApiMapping.model_validate(mapping_document())
    dumped = mapping.model_dump(mode="json", by_alias=True)
    assert "credential_header" not in dumped
    assert all("header_parameters" not in row["request"] for row in dumped["operations"].values())
    # Recorded before header and credential-header support.
    assert digest(dumped) == "d2d5b5613eafc61a97cd5d38570ac1930363746369d300303f37b77ca168e61b"


def test_credential_header_is_host_supplied_and_not_model_mappable():
    document = mapping_document() | {"credential_header": "X-Inventory-Key"}
    mapping = ApiMapping.model_validate(document)
    assert mapping.credential_headers("secret-value") == {"X-Inventory-Key": "secret-value"}
    assert mapping.credential_headers(None) == {}
    assert ApiMapping.model_validate(mapping_document()).credential_headers("s") == {"Authorization": "Bearer s"}
    for reserved in ("Authorization", "Cookie", "Proxy-Authorization", "Host"):
        with pytest.raises(ValidationError, match="reserved"):
            ApiMapping.model_validate(mapping_document() | {"credential_header": reserved})
    clash = mapping_document() | {"credential_header": "X-Partner"}
    clash["operations"]["buy_item"]["request"]["header_parameters"] = {"x-partner": "key"}
    clash["operations"]["buy_item"]["request"]["query_parameters"].pop("key")
    with pytest.raises(ValidationError, match="credential header"):
        ApiMapping.model_validate(clash)


def problem_media(monkeypatch):
    original = BaseHTTPRequestHandler.send_header

    def send_header(handler, keyword, value):
        if keyword == "Content-Type" and value == "application/json":
            value = "application/problem+json"
        return original(handler, keyword, value)

    monkeypatch.setattr(BaseHTTPRequestHandler, "send_header", send_header)


async def test_problem_json_conflict_is_a_tool_result_and_the_trial_continues(inventory, tmp_path, monkeypatch):
    problem_media(monkeypatch)
    spec = spec_for(events=[{"on": "approval_requested", "occurrence": 1,
                             "action": "change_total", "args": {"amount_minor": 13500}}])
    evidence, api, checks = await run(inventory, tmp_path, [call("get_price"), buy, call("get_price"), buy, done()],
                                      spec)
    assert all(row["status"] == "passed" for row in checks), checks
    # The declared 409 body reached the model; the agent reread and the host denied the new price.
    results = [json.loads(m["content"]) for m in api.inputs[2]["messages"] if m["role"] == "tool"]
    assert {"status": 409, "result": {"status": "conflict"}} in results
    assert [r["path"] for r in inventory.requests].count("/buy") == 1
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == 0


async def test_custom_credential_header_carries_the_host_credential(inventory, tmp_path):
    document = mapping_document() | {"credential_header": "X-Inventory-Key"}
    document["operations"]["get_price"]["responses"]["403"] = {"schema": {"type": "object", "properties": {
        "status": {"type": "string"}}, "required": ["status"], "additionalProperties": False}}
    inventory.mapping = ApiMapping.model_validate(document)
    Path(inventory.target.api_mapping).write_text(inventory.mapping.model_dump_json(by_alias=True))
    _, api, checks = await run(inventory, tmp_path, [call("get_price"), done()], spec_for())
    headers = {key.lower(): value for key, value in inventory.requests[0]["headers"].items()}
    assert headers["x-inventory-key"] == inventory.secret and "authorization" not in headers
    assert inventory.secret not in json.dumps(api.inputs)
    assert all(row["status"] == "passed" for row in checks), checks


@pytest.mark.parametrize("name", ["X-Original-URL", "X-Original-URI", "X-Rewrite-URL", "X-Original-Host",
    "X-Host", "X-HTTP-Host-Override", "Max-Forwards", "Content-Range", "Content-Disposition", "Accept-Language",
    "Accept-Charset", "Range", "If-Range", "X-Forwarded", "X-Envoy-Original-Path", "True-Client-IP",
    "X-Client-IP", "X-Cluster-Client-IP", "X-Remote-Addr", "CF-Connecting-IP"])
def test_routing_override_range_and_client_address_headers_are_reserved(name):
    with pytest.raises(ValidationError, match="reserved for the host"):
        order(header_parameters={name: "key"})


@pytest.mark.parametrize("name", ["X-Tenant-Key", "X-Key-Id", "Key", "X-Signature", "X-CSRF-Token", "X-JWT"])
def test_key_and_signature_like_names_are_credentials(name):
    with pytest.raises(ValidationError, match="looks like a credential"):
        order(header_parameters={name: "key"})


@pytest.mark.parametrize("name", ["Idempotency-Key", "X-Idempotency-Key", "If-None-Match", "Prefer",
                                  "X-Request-Id", "X-Correlation-ID", "X-Api-Version", "X-Monkey"])
def test_ordinary_request_headers_and_the_idempotency_key_remain_mappable(name):
    headers = {name: "key", "If-Match": "etag"}
    assert order(header_parameters=headers).request.header_parameters == headers


def test_mapped_adapter_reports_non_finite_arguments_as_invalid_arguments():
    from consumer_agent_testing_tool.mapped_scenario import MappedAdapter, mapped_catalog

    document = mapping_document()
    document["operations"]["get_price"]["input_schema"]["properties"]["max_price"] = {
        "type": "number", "description": "Optional price ceiling."}
    document["operations"]["get_price"]["request"]["query_parameters"] = {"max_price": "max_price"}
    adapter = MappedAdapter.__new__(MappedAdapter)
    adapter.mapping = ApiMapping.model_validate(document)
    adapter.catalog = mapped_catalog(adapter.mapping, spec_for())
    # NaN passes JSON Schema "number" but cannot be sent: a LabError the approver
    # returns to the model as invalid_arguments, not an opaque ValueError.
    with pytest.raises(LabError, match="finite numbers"):
        adapter.arguments("get_price", {"max_price": float("nan")})
    assert adapter.arguments("get_price", {"max_price": 2.5}) == {"max_price": 2.5}
