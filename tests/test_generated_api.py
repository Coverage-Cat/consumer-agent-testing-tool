import json

import pytest
from test_openapi_contract import document, evidence_for
from test_openapi_contract import service as service  # noqa: PLC0414 - pytest fixture re-export

from consumer_agent_testing_tool.api_contract import check_api, inspect_api
from consumer_agent_testing_tool.api_tools import ValidatorWorker
from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Auth, Config, Target
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import export_redacted
from consumer_agent_testing_tool.generated_api import admit_request


def generated(tmp_path, url, *, source=None, cases=None, **settings):
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(source or document()))
    return Target(kind="api", transport="http", source_revision="generated-test-v1",
        base_url=url, permitted_destinations=[url], contract_cases=cases or [],
        openapi={"path": str(path), "adapter": "fastmcp-generated", "operations": {"listItems": "read"}, **settings})


def case(**arguments):
    return {"tool": "listItems", "arguments": arguments, "effect": "read"}


async def test_generated_catalog_is_native_and_static_makes_no_request(service, tmp_path):
    url, received, _ = service
    source = document()
    source["paths"]["/unselected"] = {"get": {"operationId": "hiddenTool", "responses": {
        "200": {"description": "Not exposed"}}}}
    target, evidence = generated(tmp_path, url, source=source), evidence_for(tmp_path)
    checks = await inspect_api(target, evidence)
    assert statuses(checks)[0] == 0, checks
    lock = evidence.manifest["run_lock"]["generated_api"]
    assert lock["versions"]["fastmcp"] == "4.1.0"
    assert lock["runtime"] == {"telemetry": "off", "updates": "off"}
    assert [tool["name"] for tool in lock["tools"]] == ["listItems"]
    assert lock["tools"][0]["inputSchema"]["properties"]["count"]["minimum"] == 1
    assert lock["mapping"]["listItems"]["operation"] == "listItems"
    assert "/unselected" in lock["source"]["paths"]
    assert evidence.profile["mapping_provenance"] == "workbench-generated FastMCP OpenAPI"
    assert received == []


async def test_native_generated_call_and_invalid_arguments_are_distinct(service, tmp_path):
    url, received, _ = service
    target = generated(tmp_path, url, cases=[case(count=2), {**case(count=0), "expect_error": True}])
    evidence = evidence_for(tmp_path)
    checks = await check_api(target, evidence)
    assert statuses(checks)[0] == 1, checks
    assert len(received) == 1 and received[0]["url"] == "/v1/items?count=2"
    assert checks[-1]["http_status"] is None
    assert "did not enforce" in checks[-1]["detail"]
    evidence.finish(statuses(checks)[1], checks)
    rows = [json.loads(path.read_text()) for path in (evidence.path / "artifacts").glob("*.json")]
    assert any(row.get("result", {}).get("structuredContent") == {"count": 2}
               for row in rows if isinstance(row, dict))


@pytest.mark.parametrize("fault", ["schema", "redirect", "status"])
async def test_generated_broken_response_is_not_a_success(service, tmp_path, fault):
    url, received, state = service
    state["fault"] = fault
    target = generated(tmp_path, url, cases=[case(count=1)])
    if fault == "redirect":
        with pytest.raises(LabError, match="Redirect"):
            await check_api(target, evidence_for(tmp_path))
    else:
        checks = await check_api(target, evidence_for(tmp_path))
        assert statuses(checks)[0] == 1, checks
    assert len(received) == 1


async def test_generated_omission_null_and_nested_objects_keep_native_conversion(tmp_path):
    source = document()
    operation = source["paths"]["/items"]["get"]
    operation["parameters"] = [
        {"name": "optional", "in": "query", "schema": {"type": "string", "nullable": True}},
        {"name": "tags", "in": "query", "schema": {"type": "array", "items": {"type": "string"}},
         "style": "form", "explode": True},
    ]
    operation["requestBody"] = {"content": {"application/json": {"schema": {
        "type": "object", "properties": {"preferences": {"type": "object", "properties": {
            "quantity": {"type": "integer"}}}}}}}}
    worker = ValidatorWorker("fastmcp", evidence_for(tmp_path), 30)
    try:
        await worker.start()
        record = await worker.request("init", document=source, operations={"listItems": "read"},
                                      base_url="http://127.0.0.1:8765/v1", timeout_seconds=5)
        schema = record["tools"][0]["inputSchema"]
        assert schema["properties"]["preferences"]["properties"]["quantity"]["type"] == "integer"
        omitted = await worker.request("prepare", tool="listItems", arguments={})
        assert "optional" not in omitted["request"]["url"]
        await worker.request("finish", call_id=omitted["call_id"], response={
            "status": 200, "headers": {"content-type": "application/json"},
            "body": "eyJjb3VudCI6IDF9"})
        explicit = await worker.request("prepare", tool="listItems", arguments={
            "optional": None, "tags": ["one", "two"], "preferences": {"quantity": 2}})
        assert "tags=one&tags=two" in explicit["request"]["url"]
        # The baseline records the real conversion, even when null does not
        # survive as a distinct HTTP value. It makes no lossless-mapping claim.
        assert "optional" not in explicit["request"]["url"]
        import base64
        assert json.loads(base64.b64decode(explicit["request"]["body"])) == {"preferences": {"quantity": 2}}
    finally:
        await worker.close()


async def test_generated_authentication_stays_in_host_and_exports_redact(service, tmp_path, monkeypatch):
    url, received, state = service
    state["fault"] = "cookies"
    monkeypatch.setenv("GENERATED_TEST_TOKEN", "synthetic-generated-target-secret")
    target = generated(tmp_path, url, cases=[case(count=1), case(count=2)])
    target.auth = Auth(mode="api_key", env="GENERATED_TEST_TOKEN")
    evidence = evidence_for(tmp_path)
    checks = await check_api(target, evidence)
    assert statuses(checks)[0] == 0, checks
    assert len(received) == 2
    assert all(row["headers"]["Authorization"] == "Bearer synthetic-generated-target-secret" for row in received)
    assert all("Cookie" not in row["headers"] for row in received)
    evidence.finish(statuses(checks)[1], checks)
    exported = export_redacted(evidence.path, tmp_path / "export")
    for path in exported.rglob("*"):
        if path.is_file():
            value = path.read_bytes()
            assert b"synthetic-generated-target-secret" not in value
            assert b"synthetic-minted-cookie" not in value


async def test_generated_commit_unknown_tool_and_bad_plan_send_nothing(service, tmp_path):
    url, received, _ = service
    with pytest.raises(Unsupported, match="committing"):
        await check_api(generated(tmp_path, url, operations={"listItems": "commit"}), evidence_for(tmp_path))
    with pytest.raises(LabError, match="exposed tool"):
        await check_api(generated(tmp_path, url, cases=[
            case(count=1), {**case(count=2), "tool": "notExposed"}]), evidence_for(tmp_path))
    with pytest.raises(LabError, match="uniquely"):
        await inspect_api(generated(tmp_path, url, operations={"missing": "read"}), evidence_for(tmp_path))
    assert received == []


async def test_generated_cli_inspection_and_contract_with_config_roundtrip(service, tmp_path):
    url, received, _ = service
    target = generated(tmp_path, url, cases=[case(count=1)])
    target.auth = Auth(mode="api_key", env="MISSING_GENERATED_TARGET_KEY")
    config = Config(targets={"generated": target})
    path = tmp_path / "consumer-agent-testing-tool.json"
    path.write_text(config.model_dump_json())
    args = parser().parse_args(["inspect", "generated", "--config", str(path),
                               "--output", str(tmp_path / "inspect")])
    assert await execute(args) == 0
    assert received == []
    target.auth = Auth()
    path.write_text(config.model_dump_json())
    args = parser().parse_args(["check", "generated", "--config", str(path),
                               "--output", str(tmp_path / "contract")])
    assert await execute(args) == 0
    assert len(received) == 1


@pytest.mark.parametrize("settings", [{"examples": 2}, {"seed": 7}, {"modes": ["negative"]}])
def test_generated_profile_cannot_silently_ignore_generation_settings(tmp_path, settings):
    with pytest.raises(ValueError, match="explicit contract_cases"):
        generated(tmp_path, "http://127.0.0.1:8765", **settings)


async def test_generated_uncertain_draft_stops_without_retry_or_later_case(service, tmp_path):
    url, received, state = service
    state["fault"] = "status"
    target = generated(tmp_path, url, operations={"listItems": "draft"}, cases=[
        {**case(count=1), "effect": "draft"}, {**case(count=2), "effect": "draft"}])
    with pytest.raises(LabError, match="uncertain"):
        await check_api(target, evidence_for(tmp_path))
    assert len(received) == 1


@pytest.mark.parametrize("followup", ["second-call", "wrong-response"])
async def test_generated_pending_request_cannot_be_replaced_or_misassociated(tmp_path, followup):
    worker = ValidatorWorker("fastmcp", evidence_for(tmp_path), 30)
    try:
        await worker.start()
        await worker.request("init", document=document(), operations={"listItems": "read"},
                             base_url="http://127.0.0.1:8765/v1", timeout_seconds=5)
        proposal = await worker.request("prepare", tool="listItems", arguments={"count": 1})
        assert "request" in proposal
        with pytest.raises(LabError, match="pending"):
            if followup == "second-call":
                await worker.request("prepare", tool="listItems", arguments={"count": 2})
            else:
                await worker.request("finish", call_id=proposal["call_id"] + 1, response={
                    "status": 200, "headers": {}, "body": ""})
        assert worker.process.returncode is not None
    finally:
        await worker.close()


@pytest.mark.parametrize("change", [
    {"url": "https://forbidden.invalid/items"}, {"method": "POST"},
    {"url": "http://127.0.0.1:8765/v1/other"},
    {"headers": {"Host": "forbidden.invalid"}}, {"headers": {"Authorization": "not-authorized"}},
    {"headers": {"Content-Length": "1"}},
])
def test_generated_transport_rejects_changed_destination_and_private_headers(tmp_path, change):
    target = generated(tmp_path, "http://127.0.0.1:8765/v1")
    proposal = {"method": "GET", "url": target.base_url + "/items", "headers": {}, "body": None, **change}
    with pytest.raises(Unsupported):
        admit_request(proposal, {"method": "GET", "path": "/items"}, target)
