import asyncio
import copy
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from consumer_agent_testing_tool.api_contract import check_api, inspect_api
from consumer_agent_testing_tool.api_tools import ValidatorWorker
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Target, interface_profile
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.offline import kernel_denies_default_egress
from consumer_agent_testing_tool.openapi_contract import prepare_case
from consumer_agent_testing_tool.surface import pointer


def test_spectral_source_globs_use_patched_minimatch_with_the_callable_interface():
    root = Path(__file__).resolve().parents[1]
    # Exercise Spectral's actual source selector: a bare major-version override passes
    # an audit but breaks its old callable CommonJS import.
    script = """
const assert = require('node:assert/strict');
const {createRequire} = require('node:module');
const {dirname, join} = require('node:path');
const req = createRequire(process.cwd() + '/tooling/api/package.json');
const match = req(join(dirname(req.resolve('@stoplight/spectral-core')), 'ruleset/utils/minimatch.js')).minimatch;
assert.equal(match('schemas/purchase.yaml', '*.yaml'), true);
assert.equal(match('schemas/purchase.yaml', '*.json'), false);
assert.equal(match('schemas/purchase.yaml', '**/{purchase,policy}.yaml'), true);
assert.equal(match('schemas/private/purchase.yaml', '!**/private/**'), false);
assert.equal(req('minimatch-patched/package.json').version, '11.0.3');
"""
    result = subprocess.run(["node", "-e", script], cwd=root, text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


def document():
    return {"openapi": "3.0.3", "info": {"title": "Synthetic inventory", "version": "1"},
        "servers": [{"url": "https://must-not-be-contacted.invalid"}],
        "paths": {"/items": {"get": {
            "operationId": "listItems", "description": "Read inventory. Invalid counts return 400.",
            "x-consumer-agent-testing-tool-effect": "read",
            "parameters": [{"in": "query", "name": "count", "required": True,
                "description": "Number of items to return.",
                "schema": {"type": "integer", "minimum": 1, "maximum": 2}}],
            "responses": {
                "200": {"description": "Items", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"count": {"type": "integer"}},
                    "required": ["count"], "additionalProperties": False}}}},
                "400": {"description": "Invalid count", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"error": {"type": "string"}},
                    "required": ["error"], "additionalProperties": False}}}},
            }}}}}


@pytest.fixture
def service():
    requests, state = [], {"fault": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"method": self.command, "url": self.path, "headers": dict(self.headers)})
            query = parse_qs(urlsplit(self.path).query)
            status = 400
            try:
                count = int(query["count"][0])
                if count in (1, 2):
                    status = 200
            except (ValueError, KeyError):
                pass
            result = {"count": count} if status == 200 else {"error": "invalid count"}
            if state["fault"] == "schema":
                result = {"count": "wrong type"}
            if state["fault"] == "accept_negative":
                status, result = 200, {"count": 1}
            if state["fault"] == "redirect":
                status = 302
            if state["fault"] == "status":
                status = 503
            if state["fault"] == "secret":
                status, result = 400, {"error": "Bearer synthetic-openapi-secret"}
            body = json.dumps(result).encode()
            if state["fault"] == "malformed":
                body = b'{"unfinished":'
            if state["fault"] == "binary":
                body = b"\xff"
            self.send_response(status)
            if state["fault"] != "missing-content-type":
                self.send_header("Content-Type", "text/plain" if state["fault"] == "content-type" else "application/json")
            self.send_header("Content-Length", str(len(body)))
            if status == 302:
                self.send_header("Location", "/forbidden")
            if state["fault"] == "cookies":
                self.send_header("Set-Cookie", "first=synthetic-minted-cookie-one; HttpOnly")
                self.send_header("Set-Cookie", "second=synthetic-minted-cookie-two; HttpOnly")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    from consumer_agent_testing_tool.fixture_http import FixtureHTTPServer
    http = FixtureHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", requests, state
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def target_for(tmp_path, url, *, source=None, **settings):
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(source or document()))
    return Target(kind="api", transport="http", source_revision="openapi-test-v1",
        base_url=url, permitted_destinations=[url],
        openapi={"path": str(path), "operations": {"listItems": "read"}, "examples": 2, **settings})


def evidence_for(tmp_path):
    return Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                    profile=interface_profile("api-tools-surrogate-v1"), lock={})


async def test_real_upstream_generation_and_validation(service, tmp_path):
    url, requests, _ = service
    target, evidence = target_for(tmp_path, url), evidence_for(tmp_path)
    checks = await check_api(target, evidence)
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks)[0] == 0, checks
    assert 2 <= len(requests) <= 4
    assert all(row["url"].startswith("/v1/items") for row in requests)
    assert {row["mode"] for row in checks if "mode" in row} == {"positive", "negative"}
    record = evidence.manifest["run_lock"]["openapi"]
    assert record["node"]["@stoplight/prism-http"] == "5.16.0"
    assert record["schemathesis"]["schemathesis"] == "4.32.6"


async def test_static_openapi_never_calls_target_and_style_is_a_warning(service, tmp_path):
    url, requests, _ = service
    source = document()
    del source["paths"]["/items"]["get"]["description"]
    checks = await inspect_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    assert statuses(checks)[0] == 0, checks
    assert any(row["id"] == "spectral:surface-agent-purpose" and row["status"] == "warning" for row in checks)
    assert requests == []


async def test_spectral_reports_all_wording_categories_with_source_locations(service, tmp_path):
    url, requests, _ = service
    source = document()
    operation = source["paths"]["/items"]["get"]
    operation["operationId"] = "execute"
    operation["summary"] = "Guaranteed to always succeed."
    del operation["description"]
    del operation["x-consumer-agent-testing-tool-effect"]
    operation["parameters"] += [
        {"name": "amount", "in": "query", "required": True,
         "description": "Optional amount.", "schema": {"type": "integer"}},
        {"name": "value", "in": "query", "description": "Required value.",
         "schema": {"type": "string"}},
    ]
    checks = await inspect_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    warnings = [row for row in checks if row["id"].startswith("spectral:surface-agent-")]
    assert {row["id"].removeprefix("spectral:surface-agent-") for row in warnings} == {
        "purpose", "ambiguous-name", "units", "required-conflict", "recovery",
        "preconditions", "unsupported-claim", "side-effects"}
    assert all(row["status"] == "warning" and row["required"] is False
               and row["class"] == "heuristic usability warning" for row in warnings)
    assert all(row["path"][:3] == ["paths", "/items", "get"] for row in warnings)
    assert any(row["id"].endswith("required-conflict") and row["path"][-3:]
               == ["parameters", "1", "description"] for row in warnings)
    assert statuses(checks)[0] == 0, checks
    assert requests == []


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
async def test_spectral_checks_inherited_parameters_refs_and_nested_body_fields(service, tmp_path, version):
    url, requests, _ = service
    source = document()
    source["openapi"] = version
    item = source["paths"]["/items"]
    item["parameters"] = [{"in": "query", "name": "timeout", "schema": {"type": "integer"}}]
    item["get"]["requestBody"] = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Input"}}}}
    source["components"] = {"schemas": {"Input": {"type": "object", "properties": {
        "entries": {"type": "array", "items": {"type": "object", "required": ["amount"], "properties": {
            "amount": {"type": "integer", "description": "Optional amount."},
            "createdAt": {"type": "string", "format": "date-time"}}}}}}}}
    checks = await inspect_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    warnings = [row for row in checks if row["id"].startswith("spectral:surface-agent-")]
    assert any(row["id"].endswith("units") and row["path"] ==
               ["paths", "/items", "parameters", "0"] for row in warnings)
    assert any(row["id"].endswith("required-conflict") and row["path"][:3] ==
               ["components", "schemas", "Input"] for row in warnings), warnings
    assert any(row["id"].endswith("units") and "createdAt" in row["path"] for row in warnings)
    for row in warnings:
        # Every diagnostic must point into the published document, not an
        # imaginary expanded reference at its use site.
        pointer(source, "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                      for part in row["path"]))
    assert requests == []


async def test_specific_metadata_is_clean_and_composed_presence_is_not_guessed(service, tmp_path):
    url, requests, _ = service
    source = document()
    operation = source["paths"]["/items"]["get"]
    operation["description"] = (
        "Read current inventory. No prerequisites. For invalid counts, correct the count and retry.")
    operation["parameters"].extend([
        {"name": "amountMinor", "in": "query", "description": "Optional amount in minor USD units.",
         "schema": {"type": "integer"}},
        {"name": "timeoutSeconds", "in": "query", "schema": {"type": "integer"}},
        {"name": "since", "in": "query", "description": "Timestamp with a UTC offset.",
         "schema": {"type": "string", "format": "date-time"}},
    ])
    operation["requestBody"] = {"content": {"application/json": {"schema": {"allOf": [
        {"type": "object", "properties": {"itemId": {"type": "string", "description": "Required identifier."}}},
        {"type": "object", "required": ["itemId"]},
    ]}}}}
    checks = await inspect_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    assert not [row for row in checks if row["id"].startswith("spectral:surface-agent-")], checks
    assert statuses(checks)[0] == 0, checks
    assert requests == []


async def test_invalid_metadata_types_are_schema_failures_not_worker_crashes(service, tmp_path):
    url, requests, _ = service
    source = document()
    source["paths"]["/items"]["get"]["description"] = 123
    checks = await inspect_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    assert any(row["id"] == "spectral:oas3-schema" and row["status"] == "failed" for row in checks)
    assert requests == []


@pytest.mark.parametrize("fault", ["schema", "content-type", "status", "accept_negative", "redirect",
                                  "malformed", "missing-content-type"])
async def test_broken_contract_is_rejected_by_upstream_validators(service, tmp_path, fault):
    url, requests, state = service
    state["fault"] = fault
    checks = await check_api(target_for(tmp_path, url), evidence_for(tmp_path))
    assert statuses(checks)[0] == 1, checks
    assert requests
    assert not any(row["url"] == "/forbidden" for row in requests)


@pytest.mark.parametrize("reference", ["https://forbidden.invalid/schema", "file:///forbidden", "../schema.json"])
async def test_external_references_are_rejected_before_any_worker_or_http(service, tmp_path, reference):
    url, requests, _ = service
    source = document()
    source["paths"]["/items"]["get"]["parameters"][0]["schema"] = {"$ref": reference}
    with pytest.raises(Unsupported, match="document"):
        await check_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    assert requests == []


async def test_local_refs_are_validated_by_real_tools(service, tmp_path):
    url, _, _ = service
    source = document()
    field = source["paths"]["/items"]["get"]["parameters"][0]
    source["components"] = {"schemas": {"Count": copy.deepcopy(field["schema"])}}
    field["schema"] = {"$ref": "#/components/schemas/Count"}
    checks = await check_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    assert statuses(checks)[0] == 0, checks


async def test_explicit_committing_get_or_unknown_operation_makes_no_request(service, tmp_path):
    url, requests, _ = service
    with pytest.raises(Unsupported, match="committing"):
        await check_api(target_for(tmp_path, url, operations={"listItems": "commit"}), evidence_for(tmp_path))
    with pytest.raises(LabError, match="uniquely"):
        await check_api(target_for(tmp_path, url, operations={"missing": "read"}), evidence_for(tmp_path))
    assert requests == []


async def test_invalid_source_schema_stops_before_request(service, tmp_path):
    url, requests, _ = service
    source = document()
    source["paths"]["/items"]["get"]["parameters"][0]["schema"]["type"] = "not-a-type"
    checks = await check_api(target_for(tmp_path, url, source=source), evidence_for(tmp_path))
    assert any(row["id"] == "spectral:oas3-schema" and row["status"] == "failed" for row in checks)
    assert requests == []


async def test_separate_target_credential_is_not_in_export(service, tmp_path, monkeypatch):
    url, requests, state = service
    state["fault"] = "secret"
    monkeypatch.setenv("OPENAPI_TEST_TOKEN", "synthetic-openapi-secret")
    target = target_for(tmp_path, url)
    from consumer_agent_testing_tool.config import Auth
    target.auth = Auth(mode="api_key", env="OPENAPI_TEST_TOKEN")
    evidence = evidence_for(tmp_path)
    checks = await check_api(target, evidence)
    evidence.finish(statuses(checks)[1], checks)
    assert all(row["headers"]["Authorization"] == "Bearer synthetic-openapi-secret" for row in requests)
    exported = export_redacted(evidence.path, tmp_path / "export")
    for path in exported.rglob("*"):
        if path.is_file():
            assert b"synthetic-openapi-secret" not in path.read_bytes()


@pytest.mark.parametrize("path", ["../forbidden", "%2e%2e", "a/b", "%252f", "a%2fb"])
def test_generated_path_cannot_change_the_authorized_route(tmp_path, path):
    target = target_for(tmp_path, "http://127.0.0.1:8765/v1")
    case = {"method": "GET", "path": "/items/{id}", "url": target.base_url + "/items/" + path,
            "headers": {}, "body": None}
    with pytest.raises(Unsupported):
        prepare_case(case, target)


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
async def test_draft_failure_stops_without_retry_or_shrinking(service, tmp_path, version):
    url, requests, state = service
    state["fault"] = "schema"
    source = document()
    source["openapi"] = version
    checks = await check_api(target_for(tmp_path, url, source=source,
                                       operations={"listItems": "draft"}), evidence_for(tmp_path))
    assert statuses(checks)[0] == 1
    assert len(requests) == 1


async def test_non_utf8_body_is_unsupported_not_silently_rewritten(service, tmp_path):
    url, requests, state = service
    state["fault"] = "binary"
    checks = await check_api(target_for(tmp_path, url), evidence_for(tmp_path))
    assert statuses(checks)[0] == 3
    assert any(row["status"] == "unsupported" and "UTF-8" in row["detail"] for row in checks)
    assert len(requests) == 1


async def test_worker_death_stops_before_any_later_http_request(service, tmp_path, monkeypatch):
    url, requests, _ = service
    original = ValidatorWorker.request

    async def die(self, command, **arguments):
        if command == "validate":
            await self.close()
        return await original(self, command, **arguments)

    monkeypatch.setattr(ValidatorWorker, "request", die)
    checks = await check_api(target_for(tmp_path, url), evidence_for(tmp_path))
    assert statuses(checks)[0] == 1
    assert len(requests) == 1


async def test_real_worker_kernel_boundaries_and_environment(tmp_path, monkeypatch):
    evidence = evidence_for(tmp_path)
    captured = []
    original = asyncio.create_subprocess_exec
    for name in ("OPENROUTER_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "OPENAPI_TEST_TOKEN",
                 "NODE_OPTIONS", "PYTHONPATH"):
        monkeypatch.setenv(name, "synthetic-inherited-setting")

    async def capture(*args, **kwargs):
        captured.append(kwargs["env"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    for kind in ("node", "python"):
        worker = ValidatorWorker(kind, evidence, 10)
        try:
            await worker.start()
            await worker.request("init", document=document())
            assert kernel_denies_default_egress(worker.process.pid)
        finally:
            await worker.close()
        assert worker.process.returncode is not None
    assert len(captured) == 2
    assert all("synthetic-inherited-setting" not in env.values() for env in captured)


async def test_missing_negative_cases_are_an_observation_gap(service, tmp_path):
    url, requests, _ = service
    source = document()
    del source["paths"]["/items"]["get"]["parameters"]
    checks = await check_api(target_for(tmp_path, url, source=source, modes=["negative"]), evidence_for(tmp_path))
    assert statuses(checks)[0] == 3
    assert requests == []
    assert any(row["status"] == "observation-gap" for row in checks)


async def test_network_sandboxed_host_gets_an_explicit_capability_failure(service, tmp_path, monkeypatch):
    import consumer_agent_testing_tool.api_tools as workers
    url, requests, _ = service
    monkeypatch.setattr(workers, "kernel_denies_default_egress", lambda: True)
    with pytest.raises(Unsupported, match="nested Seatbelt"):
        await check_api(target_for(tmp_path, url), evidence_for(tmp_path))
    assert requests == []


@pytest.mark.parametrize("invalid_header", [False, True])
async def test_repeated_response_cookies_are_preserved_but_values_never_exported(service, tmp_path, invalid_header):
    url, _, state = service
    state["fault"] = "cookies"
    evidence = evidence_for(tmp_path)
    source = document()
    if invalid_header:
        source["paths"]["/items"]["get"]["responses"]["200"]["headers"] = {
            "Set-Cookie": {"description": "Deliberately invalid synthetic response contract",
                           "schema": {"type": "integer"}}}
    checks = await check_api(target_for(tmp_path, url, source=source), evidence)
    assert statuses(checks)[0] == (1 if invalid_header else 0)
    evidence.finish(statuses(checks)[1], checks)
    event = next(json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()
                 if json.loads(line)["event_type"] == "api_source_response")
    body = json.loads((evidence.path / event["artifacts"][0]["path"]).read_text())
    cookies = [value for item in body["header_items"] for key, value in item.items() if key == "set-cookie"]
    assert cookies == ["[REDACTED]", "[REDACTED]"]
    exported = export_redacted(evidence.path, tmp_path / "export")
    for directory in (evidence.path, exported):
        for path in directory.rglob("*"):
            if path.is_file():
                assert b"synthetic-minted-cookie-" not in path.read_bytes()
