import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler

import httpx
import pytest

from consumer_agent_testing_tool.api_contract import check_api
from consumer_agent_testing_tool.api_mapping import ApiMapping
from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Config, Target, interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.serialization import load


def document():
    return {"schema_version": 1, "provenance": "Test developer's committed request/response contracts",
        "operations": {"lookup_item": {"operation": "inventory.lookup",
            "description": "Read the item price in minor currency units before creating a draft.",
            "effect": "read", "input_schema": {"type": "object", "properties": {
                "id": {"type": "string"}, "details": {"type": "boolean", "default": False}},
                "required": ["id"], "additionalProperties": False},
            "request": {"method": "GET", "path": "/items/{item}",
                        "path_parameters": {"item": "id"}, "query_parameters": {"include": "details"}},
            "responses": {"200": {"schema": {"type": "object", "properties": {
                "id": {"type": "string"}, "amount_minor": {"type": "integer"},
                "internal": {"type": "string"}},
                "required": ["id", "amount_minor", "internal"], "additionalProperties": False},
                "project": ["id", "amount_minor"]},
                "404": {"schema": {"type": "object", "properties": {"error": {"type": "string"}},
                                  "required": ["error"], "additionalProperties": False}}}}}}


@pytest.fixture
def server():
    requests = []
    state = {"status": 200, "body": {"id": "item-one", "amount_minor": 12500, "internal": "developer-only"},
             "type": "application/json"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
            if state.get("delay"):
                time.sleep(state["delay"])
            body = state.get("raw", json.dumps(state["body"]).encode())
            self.send_response(state["status"])
            self.send_header("Content-Type", state["type"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", self.server.origin + "/forbidden")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        do_POST = do_GET

        def log_message(self, *args):
            pass

    from consumer_agent_testing_tool.fixture_http import FixtureHTTPServer
    http = FixtureHTTPServer(("127.0.0.1", 0), Handler)
    http.origin = f"http://127.0.0.1:{http.server_port}"
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield http.origin + "/v1", state, requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def target_for(tmp_path, origin, mapping=None, cases=None):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping or document()))
    return Target(kind="api", transport="http", source_revision="test-service-revision",
        api_mapping=str(path), base_url=origin, permitted_destinations=[origin],
        contract_cases=cases if cases is not None else [
            {"tool": "lookup_item", "arguments": {"id": "item-one", "details": False}, "effect": "read"}])


def evidence_for(tmp_path):
    return Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                    profile=interface_profile("api-tools-surrogate-v1"), lock={})


def test_mapping_preserves_unions_null_omission_and_explicit_projection():
    doc = document()
    item = doc["operations"]["lookup_item"]
    item["input_schema"]["properties"]["payload"] = {"anyOf": [{"type": "null"}, {"type": "object"}]}
    item["request"]["body_parameters"] = {"data": "payload"}
    item["request"]["method"] = "POST"
    mapping = ApiMapping.model_validate(doc)
    operation = mapping.operations["lookup_item"]
    assert operation.request_for({"id": "one"}) == {"method": "POST", "path": "/items/one", "body": {}}
    assert operation.request_for({"id": "one", "details": False, "payload": None}) == {
        "method": "POST", "path": "/items/one?include=false", "body": {"data": None}}
    assert mapping.record()["effective"]["lookup_item"]["input_schema"] == item["input_schema"]
    result = operation.responses["200"].project_result({"id": "one", "amount_minor": 1, "internal": "hidden"})
    assert result == {"id": "one", "amount_minor": 1}
    with pytest.raises(LabError):
        operation.request_for({"id": "one", "payload": "not a declared union member"})


@pytest.mark.parametrize("change", ["dropped", "duplicate", "remote_ref", "route_escape", "optional_path",
                                  "unknown", "projection_union", "projection_missing", "root_ref"])
def test_invalid_or_lossy_mappings_fail_before_execution(change):
    doc = document()
    operation = doc["operations"]["lookup_item"]
    if change == "dropped":
        operation["request"]["query_parameters"] = {}
    elif change == "duplicate":
        operation["request"]["body_parameters"] = {"copy": "id"}
    elif change == "remote_ref":
        operation["input_schema"]["properties"]["id"] = {"$ref": "https://unreachable.invalid/schema"}
    elif change == "root_ref":
        operation["responses"]["200"]["schema"]["properties"]["id"] = {"$ref": "#/properties/internal"}
    elif change == "route_escape":
        operation["request"]["path"] = "/../items/{item}"
    elif change == "optional_path":
        operation["input_schema"]["required"] = []
    elif change == "unknown":
        operation["request"]["headers"] = {"Authorization": "untrusted"}
    elif change == "projection_union":
        operation["responses"]["200"]["schema"]["anyOf"] = [{"required": ["internal"]}]
    else:
        operation["responses"]["200"]["project"] = ["missing"]
    with pytest.raises(ValueError):
        ApiMapping.model_validate(doc)


@pytest.mark.parametrize("identifier", ["..", "x/y", "%2e", "x?y", "x#y", "x\\y"])
def test_path_arguments_cannot_escape_the_declared_route(identifier):
    with pytest.raises(LabError):
        ApiMapping.model_validate(document()).operations["lookup_item"].request_for({"id": identifier})


async def test_direct_api_checks_record_source_projection_auth_and_redacted_export(tmp_path, server, monkeypatch):
    origin, _, requests = server
    target = target_for(tmp_path, origin)
    target.auth.mode, target.auth.env = "api_key", "LOCAL_API_TEST_TOKEN"
    monkeypatch.setenv("LOCAL_API_TEST_TOKEN", "synthetic-target-credential")
    monkeypatch.setenv("HTTP_PROXY", "http://unreachable.invalid:1111")
    evidence = evidence_for(tmp_path)
    checks = await check_api(target, evidence)
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks) == (0, "completed")
    assert requests[0]["path"] == "/v1/items/item-one?include=false"
    assert requests[0]["headers"]["Authorization"] == "Bearer synthetic-target-credential"
    assert evidence.manifest["locations"]["inference"] == "none"
    assert evidence.manifest["locations"]["target"] == "local"
    assert evidence.manifest["run_lock"]["api_mapping"]["source"]["operations"]["lookup_item"] \
        ["responses"]["200"]["project"] == ["id", "amount_minor"]
    exported = export_redacted(evidence.path, tmp_path / "export")
    assert all(b"synthetic-target-credential" not in path.read_bytes()
               for path in exported.rglob("*") if path.is_file())
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    source = next(row for row in events if row["event_type"] == "api_source_response")
    effective = next(row for row in events if row["event_type"] == "tool_result")
    assert load(evidence.path / source["artifacts"][0]["path"])["result"]["internal"] == "developer-only"
    assert "internal" not in load(evidence.path / effective["artifacts"][0]["path"])["result"]


@pytest.mark.parametrize("fault", ["redirect", "schema", "content_type", "duplicate_json", "too_big", "status"])
async def test_bad_api_responses_fail_without_following_redirects(tmp_path, server, fault):
    origin, state, requests = server
    target = target_for(tmp_path, origin)
    if fault == "redirect":
        state["status"] = 302
    elif fault == "schema":
        state["body"]["amount_minor"] = "wrong unit type"
    elif fault == "content_type":
        state["type"] = "text/html"
    elif fault == "duplicate_json":
        state["raw"] = b'{"id":"one","id":"two"}'
    elif fault == "too_big":
        target.max_response_bytes = 2
    else:
        state["status"] = 503
    evidence = evidence_for(tmp_path)
    checks = await check_api(target, evidence)
    assert statuses(checks) == (1, "failed")
    assert len(requests) == 1 and all("/forbidden" not in row["path"] for row in requests)


async def test_entire_case_plan_is_validated_before_any_request(tmp_path, server):
    origin, _, requests = server
    target = target_for(tmp_path, origin, cases=[
        {"tool": "lookup_item", "arguments": {"id": "one"}, "effect": "read"},
        {"tool": "missing", "arguments": {}, "effect": "read"}])
    with pytest.raises(LabError):
        await check_api(target, evidence_for(tmp_path))
    assert not requests
    doc = document()
    doc["operations"]["lookup_item"]["effect"] = "commit"  # GET must not imply read-only authorization.
    target = target_for(tmp_path, origin, doc)
    with pytest.raises(LabError):
        await check_api(target, evidence_for(tmp_path))
    assert not requests


async def test_cli_inspects_without_credentials_or_inference_and_runs_contract(tmp_path, server):
    origin, _, requests = server
    target = target_for(tmp_path, origin)
    target.auth.mode, target.auth.env = "api_key", "MISSING_API_TARGET_CREDENTIAL"
    config = Config(targets={"inventory": target})
    path = tmp_path / "consumer-agent-testing-tool.json"
    path.write_text(config.model_dump_json())
    args = parser().parse_args(["inspect", "inventory", "--config", str(path),
                               "--output", str(tmp_path / "inspect")])
    assert await execute(args) == 0
    assert not requests
    target.auth.mode, target.auth.env = "none", None
    path.write_text(config.model_dump_json())
    args = parser().parse_args(["check", "inventory", "--config", str(path),
                               "--output", str(tmp_path / "contract")])
    assert await execute(args) == 0
    assert len(requests) == 1


async def test_failed_draft_stops_remaining_cases_without_retry(tmp_path, server):
    origin, state, requests = server
    state["status"] = 500
    doc = copy.deepcopy(document())
    doc["operations"]["lookup_item"]["effect"] = "draft"
    doc["operations"]["lookup_item"]["request"]["method"] = "POST"
    target = target_for(tmp_path, origin, doc, cases=[
        {"tool": "lookup_item", "arguments": {"id": "one"}, "effect": "draft"}] * 2)
    evidence = evidence_for(tmp_path)
    assert statuses(await check_api(target, evidence)) == (1, "failed")
    assert len(requests) == 1
    assert any("remaining requests" in gap for gap in evidence.manifest["observation_gaps"])


async def test_expected_error_and_empty_response_contracts(tmp_path, server):
    origin, state, _ = server
    state.update(status=404, body={"error": "missing"})
    target = target_for(tmp_path, origin, cases=[
        {"tool": "lookup_item", "arguments": {"id": "one"}, "effect": "read", "expect_error": True}])
    assert statuses(await check_api(target, evidence_for(tmp_path))) == (0, "completed")
    doc = document()
    doc["operations"]["lookup_item"]["responses"]["204"] = {"schema": None}
    target = target_for(tmp_path, origin, doc)
    state.update(status=204, raw=b"")
    assert statuses(await check_api(target, evidence_for(tmp_path))) == (0, "completed")


async def test_api_transport_timeout_is_infrastructure_and_never_retries_a_draft(tmp_path, server):
    origin, state, requests = server
    state["delay"] = 2
    doc = document()
    doc["operations"]["lookup_item"]["effect"] = "draft"
    doc["operations"]["lookup_item"]["request"]["method"] = "POST"
    target = target_for(tmp_path, origin, doc, cases=[
        {"tool": "lookup_item", "arguments": {"id": "one"}, "effect": "draft"}] * 2)
    target.timeout_seconds = 1
    evidence = evidence_for(tmp_path)
    with pytest.raises(LabError) as failure:
        await check_api(target, evidence)
    assert failure.value.code == 2 and failure.value.category == "setup/infrastructure"
    assert len(requests) == 1
    assert any("Uncertain API draft" in gap for gap in evidence.manifest["observation_gaps"])


def test_local_definitions_are_preserved_and_unresolved_references_rejected():
    doc = document()
    schema = doc["operations"]["lookup_item"]["responses"]["200"]["schema"]
    schema["$defs"] = {"identifier": {"type": "string"}}
    schema["properties"]["id"] = {"$ref": "#/$defs/identifier"}
    mapping = ApiMapping.model_validate(doc)
    assert mapping.operations["lookup_item"].responses["200"].project_result({
        "id": "one", "amount_minor": 12500, "internal": "hidden"})["id"] == "one"
    schema["properties"]["id"]["$ref"] = "#/$defs/missing"
    with pytest.raises(ValueError, match="Unresolved"):
        ApiMapping.model_validate(doc)


@pytest.mark.parametrize("field,schema", [
    ("details", {"type": ["boolean", "null"]}),
    ("details", {"type": "array", "items": {"type": "string"}}),
    ("details", {"type": "object"}),
    ("details", {"anyOf": [{"type": "string"}, {"type": "boolean"}]}),
    ("id", {"type": "number"}),
    ("id", {"type": ["string", "null"]}),
])
def test_unimplemented_wire_types_are_rejected_before_exposure(field, schema):
    doc = document()
    doc["operations"]["lookup_item"]["input_schema"]["properties"][field] = schema
    with pytest.raises(ValueError):
        ApiMapping.model_validate(doc)


def test_scalar_union_and_referenced_wire_types_keep_the_original_schema():
    doc = document()
    schema = doc["operations"]["lookup_item"]["input_schema"]
    schema["$defs"] = {"query": {"type": ["boolean", "string"]}}
    schema["properties"]["details"] = {"$ref": "#/$defs/query"}
    schema["properties"]["id"] = {"type": ["string", "integer"]}
    mapping = ApiMapping.model_validate(doc)
    assert mapping.catalog()["lookup_item"]["input_schema"] == schema
    op = mapping.operations["lookup_item"]
    assert op.request_for({"id": 1.0, "details": False})["path"] == "/items/1.0?include=false"
    assert op.request_for({"id": "one", "details": "a&b"})["path"] == "/items/one?include=a%26b"


async def test_api_offline_rejects_remote_before_http_request(tmp_path, monkeypatch):
    target = target_for(tmp_path, "https://owned-staging.invalid/v1")
    config = Config(targets={"inventory": target})
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())

    def forbidden(*args, **kwargs):
        pytest.fail("Offline rejection must precede HTTP client construction")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    args = parser().parse_args(["check", "inventory", "--config", str(path), "--offline"])
    with pytest.raises(LabError, match="offline rejects remote"):
        await execute(args)


async def test_contract_checks_send_the_maps_credential_header(tmp_path, server, monkeypatch):
    origin, _, requests = server
    target = target_for(tmp_path, origin, mapping=document() | {"credential_header": "X-Inventory-Key"})
    target.auth.mode, target.auth.env = "api_key", "LOCAL_API_TEST_TOKEN"
    monkeypatch.setenv("LOCAL_API_TEST_TOKEN", "synthetic-target-credential")
    evidence = evidence_for(tmp_path)
    assert statuses(await check_api(target, evidence)) == (0, "completed")
    # The same host-held placement as the behavioral lane: the map's API-key header, no Bearer.
    headers = {key.lower(): value for key, value in requests[0]["headers"].items()}
    assert headers["x-inventory-key"] == "synthetic-target-credential" and "authorization" not in headers
