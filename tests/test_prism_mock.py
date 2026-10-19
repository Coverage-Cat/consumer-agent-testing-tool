import base64
import copy
import hashlib
import http.client
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from test_openapi_contract import document, evidence_for, target_for
from test_openapi_contract import service as service  # noqa: PLC0414

from consumer_agent_testing_tool.api_contract import check_api, inspect_api
from consumer_agent_testing_tool.api_tools import ValidatorWorker
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Target
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.metrics import recorded_events
from consumer_agent_testing_tool.protocol import child_environment


def mock_target(tmp_path, url, **kwargs):
    return target_for(tmp_path, url, adapter="prism-mock", **kwargs)


async def test_prism_mock_generates_schema_aware_responses_without_http(service, tmp_path, monkeypatch):
    url, requests, _ = service
    evidence = evidence_for(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Mock profile tried to create HTTP or resolve target credentials")

    monkeypatch.setattr("consumer_agent_testing_tool.openapi_contract.httpx.AsyncClient", forbidden)
    monkeypatch.setattr("consumer_agent_testing_tool.openapi_contract.target_credential", forbidden)
    checks = await check_api(mock_target(tmp_path, url), evidence)
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks)[0] == 0, checks
    responses = [payload for event, payload in recorded_events(evidence.path)
                 if event["event_type"] == "prism_mock_response"]
    assert {row["status"] for row in responses} == {200, 400}
    assert all(json.loads(row["body"]).keys() == ({"count"} if row["status"] == 200 else {"error"})
               for row in responses)
    inputs = {event["event_id"]: payload for event, payload in recorded_events(evidence.path)
              if event["event_type"] == "prism_mock_request"}
    assert len(inputs) == len(responses)
    for event, payload in recorded_events(evidence.path):
        if event["event_type"] == "prism_mock_response":
            request = inputs[event["parent_id"]]
            assert request["case_id"] == payload["case_id"]
            assert urlsplit(request["serialized"]["url"]).path == "/v1/items"
            assert request["prism_input"]["url"]["path"] == "/items"
            assert request["serialized"]["body_utf8"] is None
    assert requests == []
    assert not any(event["event_type"] in ("network_request", "api_source_response", "model_request")
                   for event, _ in recorded_events(evidence.path))
    lock = evidence.manifest["run_lock"]["openapi"]
    assert lock["adapter"] == "prism-stateless-mock-v1"
    assert lock["mock"]["dynamic"] is False
    assert "no real service" in evidence.manifest["fidelity"]
    assert any("not the developer's running API" in gap for gap in evidence.manifest["observation_gaps"])


async def test_prism_mock_static_inspection_never_synthesizes_responses(service, tmp_path):
    url, requests, _ = service
    evidence = evidence_for(tmp_path)
    checks = await inspect_api(mock_target(tmp_path, url), evidence)
    assert statuses(checks)[0] == 0, checks
    assert not any(event["event_type"] == "prism_mock_response" for event, _ in recorded_events(evidence.path))
    assert requests == []


async def test_prism_mock_retains_request_body_identity_without_exporting_private_body(service, tmp_path, monkeypatch):
    url, requests, _ = service
    source = document()
    operation = source["paths"]["/items"].pop("get")
    source["paths"]["/items"]["post"] = operation
    operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "properties": {
            "email": {"type": "string", "enum": ["synthetic-mock-request@example.test"]},
            "amount_minor": {"type": "integer", "enum": [12500]}},
        "required": ["email", "amount_minor"], "additionalProperties": False}}}}
    cases = {}
    original = ValidatorWorker.request

    async def capture(worker, command, **arguments):
        result = await original(worker, command, **arguments)
        if command == "generate":
            cases.update({case["id"]: case for case in result["cases"]})
        return result

    monkeypatch.setattr(ValidatorWorker, "request", capture)
    evidence = evidence_for(tmp_path)
    checks = await check_api(mock_target(tmp_path, url, source=source, modes=["positive"]), evidence)
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks)[0] == 0, checks
    inputs = [payload for event, payload in recorded_events(evidence.path)
              if event["event_type"] == "prism_mock_request"]
    assert inputs and len(inputs) == len(cases)
    for record in inputs:
        raw = base64.b64decode(cases[record["case_id"]]["body"])
        assert record["serialized"]["body_sha256"] == hashlib.sha256(raw).hexdigest()
        assert record["serialized"]["body_bytes"] == len(raw)
        assert record["serialized"]["body_utf8_lossy"] is False
        assert json.loads(record["serialized"]["body_utf8"]) == {
            "email": "[REDACTED]", "amount_minor": 12500}
        assert record["serialized"]["method"] == "POST"
    exported = export_redacted(evidence.path, tmp_path / "export")
    for path in exported.rglob("*"):
        if path.is_file() and path.suffix != ".zip":
            assert b"synthetic-mock-request@example.test" not in path.read_bytes()
    assert requests == []


async def test_prism_mock_uses_examples_and_preserves_invalid_example_failure(service, tmp_path):
    url, requests, _ = service
    for value, expected in ((7, 0), ("wrong type", 1)):
        source = document()
        source["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["example"] = {
            "count": value}
        folder = tmp_path / str(expected)
        folder.mkdir()
        evidence = evidence_for(folder)
        checks = await check_api(mock_target(folder, url, source=source, modes=["positive"]), evidence)
        assert statuses(checks)[0] == expected, checks
        responses = [payload for event, payload in recorded_events(evidence.path)
                     if event["event_type"] == "prism_mock_response"]
        if expected == 0:
            assert responses and all(json.loads(row["body"]) == {"count": value} for row in responses)
        else:
            assert any(row["status"] == "failed" and row["id"].startswith("spectral:")
                       and row["path"][-2:] == ["example", "count"] for row in checks), checks
    assert requests == []


async def test_prism_mock_declared_commit_has_no_state_or_network_effect(service, tmp_path):
    url, requests, _ = service
    source = document()
    operation = source["paths"]["/items"].pop("get")
    source["paths"]["/items"]["post"] = operation
    operation.update(operationId="commitOrder", **{"x-consumer-agent-testing-tool-effect": "commit"})
    evidence = evidence_for(tmp_path)
    checks = await check_api(mock_target(tmp_path, url, source=source,
        operations={"commitOrder": "commit"}), evidence)
    assert statuses(checks)[0] == 0, checks
    assert requests == []
    assert any("No persistent state" in gap for gap in evidence.manifest["observation_gaps"])
    assert not evidence.manifest.get("backend_metrics")


async def test_prism_mock_rejects_callbacks_without_forwarding(service, tmp_path):
    url, requests, _ = service
    source = document()
    source["paths"]["/items"]["get"]["callbacks"] = {"notice": {
        "{$request.query.callbackUrl}": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"type": "object"}}}}, "responses": {"200": {"description": "Delivered"}}}}}}
    evidence = evidence_for(tmp_path)
    checks = await check_api(mock_target(tmp_path, url, source=source, modes=["positive"]), evidence)
    assert any(row["status"] == "unsupported" and "callbacks" in row["detail"] for row in checks), checks
    assert requests == []


async def test_prism_mock_response_budget_has_no_network_fallback(service, tmp_path):
    url, requests, _ = service
    target = mock_target(tmp_path, url, modes=["positive"])
    target.max_response_bytes = 1
    checks = await check_api(target, evidence_for(tmp_path))
    assert any(row["status"] == "failed" and "byte budget" in row["detail"] for row in checks), checks
    assert requests == []


async def test_prism_mock_missing_validation_response_is_not_fabricated_http(service, tmp_path):
    url, requests, _ = service
    source = document()
    del source["paths"]["/items"]["get"]["responses"]["400"]
    evidence = evidence_for(tmp_path)
    checks = await check_api(mock_target(tmp_path, url, source=source, modes=["negative"]), evidence)
    assert statuses(checks)[0] == 1, checks
    errors = [payload["error"] for event, payload in recorded_events(evidence.path)
              if event["event_type"] == "prism_mock_error"]
    assert errors and all(error["status"] == 422 for error in errors)
    assert not any(event["event_type"] in ("network_request", "prism_mock_response")
                   for event, _ in recorded_events(evidence.path))
    assert requests == []


@pytest.mark.parametrize("referenced_header", [False, True])
async def test_prism_mock_new_synthetic_secrets_are_redacted(service, tmp_path, referenced_header):
    url, requests, _ = service
    source = document()
    response = source["paths"]["/items"]["get"]["responses"]["200"]
    response["headers"] = {"Set-Cookie": {"schema": {"type": "string"},
                                        "example": "session=synthetic-mock-private; HttpOnly"}}
    if referenced_header:
        source["components"] = {"headers": {"Session": response["headers"]["Set-Cookie"]}}
        response["headers"]["Set-Cookie"] = {"$ref": "#/components/headers/Session"}
    response["content"]["application/json"] = {"schema": {
        "type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
        "example": {"token": "synthetic-mock-body-token"}}
    evidence = evidence_for(tmp_path)
    checks = await check_api(mock_target(tmp_path, url, source=source, modes=["positive"]), evidence)
    evidence.finish(statuses(checks)[1], checks)
    assert statuses(checks)[0] == 0, checks
    assert any(event["event_type"] == "prism_mock_response" for event, _ in recorded_events(evidence.path))
    exported = export_redacted(evidence.path, tmp_path / "export")
    for path in exported.rglob("*"):
        if path.is_file() and path.suffix != ".zip":
            # The source example is public input but newly emitted secret fields
            # and typed cookie headers must also be scrubbed.
            assert b"synthetic-mock-body-token" not in path.read_bytes()
            assert b"synthetic-mock-private" not in path.read_bytes()
    assert requests == []


@pytest.mark.parametrize("setting", ["auth", "api_control", "contract_cases"])
def test_prism_mock_cannot_accept_target_authority(tmp_path, setting):
    target = mock_target(tmp_path, "http://127.0.0.1:8765").model_dump()
    target[setting] = copy.deepcopy({
        "auth": {"mode": "api_key", "env": "UNUSED_TARGET_SECRET"},
        "api_control": {"socket": ".cache/private.sock", "principal": "customer"},
        "contract_cases": [{"tool": "listItems", "arguments": {"count": 1}, "effect": "read"}],
    }[setting])
    with pytest.raises(ValidationError):
        Target(**target)


@pytest.mark.parametrize("command", ["inspect", "check"])
def test_prism_mock_offline_cli_seals_host_and_workers_before_execution(service, tmp_path, command):
    url, requests, _ = service
    port = urlsplit(url).port
    baseline = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    baseline.request("GET", "/v1/items?count=1")
    assert baseline.getresponse().status == 200
    baseline.close()
    assert requests
    requests.clear()
    target = mock_target(tmp_path, url)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": {"mock": target.model_dump(mode="json")}}))
    probe = """
import asyncio,json,socket,sys
from consumer_agent_testing_tool.cli import parser,execute
from consumer_agent_testing_tool.offline import bootstrap,kernel_denies_default_egress
from consumer_agent_testing_tool.api_tools import ValidatorWorker
original_request=ValidatorWorker.request
async def checked(worker,command,**arguments):
    result=await original_request(worker,command,**arguments)
    if command == "init":
        assert kernel_denies_default_egress(worker.process.pid)
    return result
ValidatorWorker.request=checked
args=parser().parse_args(sys.argv[2:])
assert bootstrap(args) is None
assert asyncio.run(execute(args)) == 0
assert kernel_denies_default_egress()
for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):
    with socket.socket(socket.AF_INET,kind) as connection:
        connection.settimeout(2)
        try:
            if kind == socket.SOCK_STREAM:
                connection.connect(("127.0.0.1",int(sys.argv[1])))
                connection.sendall(b"GET /forbidden HTTP/1.0\\r\\n\\r\\n")
            else:
                connection.sendto(b"forbidden",("127.0.0.1",int(sys.argv[1])))
        except PermissionError:
            continue
        raise AssertionError("Offline host direct egress was not denied by the kernel")
print("kernel-probes-denied")
"""
    result = subprocess.run([sys.executable, "-c", probe, str(port), "--development-extensions", command, "mock",
        "--config", str(config), "--output", str(tmp_path / "runs"), "--offline",
        *(["--suite", "contract"] if command == "check" else [])],
        env=child_environment({"HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                              "PATH": str(Path(shutil.which("node")).parent) + ":/usr/bin:/bin"}),
        capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "kernel-probes-denied" in result.stdout
    assert requests == []
    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["run_lock"]["offline_policy"]["state"] == "enforced"
    assert manifest["run_lock"]["offline_policy"]["loopback_ports"] == []
    assert manifest["locations"]["egress"].startswith("kernel-denied")
    assert all(row["status"] == "passed" for row in manifest["checks"] if row.get("required", True))
