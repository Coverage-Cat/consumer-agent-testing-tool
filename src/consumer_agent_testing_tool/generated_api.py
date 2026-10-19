"""Explicit FastMCP-generated OpenAPI baseline; distinct from direct API checks."""
import asyncio
import base64
import difflib
import json
from urllib.parse import urlsplit

import httpx
from jsonschema import validators

from .api_tools import ValidatorWorker
from .destinations import target_credential
from .errors import LabError, Unsupported
from .evidence import PRIVATE_VALUE_KEYS
from .openapi_contract import prepare_case, read_document, request_case
from .serialization import digest


def admit_request(proposal, association, target):
    request = dict(proposal)
    if request["method"] != association["method"]:
        raise Unsupported("Generated tool changed its declared HTTP method")
    headers = dict(request["headers"])
    body = base64.b64decode(request["body"], validate=True) if request["body"] is not None else b""
    for key, value in list(headers.items()):
        if key.lower() == "host":
            if value.lower() != urlsplit(request["url"]).netloc.lower():
                raise Unsupported("Generated Host header differs from its URL")
            del headers[key]
        elif key.lower() == "content-length":
            if value != str(len(body)):
                raise Unsupported("Generated framing does not match the request body")
            del headers[key]
    request.update(path=association["path"], headers=headers)
    return prepare_case(request, target)


def expose_generated(document, metadata, target, evidence, *, behavioral=False):
    for tool in metadata["tools"]:
        schema = tool["inputSchema"]
        validators.validator_for(schema).check_schema(schema)
    evidence.profile.update(adapter=metadata["adapter"], mapping_provenance="workbench-generated FastMCP OpenAPI")
    if "profile" in evidence.manifest["run_lock"]:
        evidence.manifest["run_lock"]["profile"] = dict(evidence.profile)
    evidence.manifest["run_lock"]["generated_api"] = {
        "source": document, "source_sha256": digest(document), **metadata,
        "settings": target.openapi.model_dump(mode="json"),
        "case_selection": "Inspect model proposals" if behavioral else
                          "explicit developer contract_cases; no Schemathesis generation in this adapter",
        "transport": {"base_url": target.base_url, "retries": 0, "follow_redirects": False,
                      "trust_env": False, "timeout_seconds": target.timeout_seconds,
                      "max_response_bytes": target.max_response_bytes},
        "transformations": [
            "FastMCP generates tool names, descriptions, flattened argument and output schemas",
            "Only explicitly selected source operations are generated; remaining routes excluded",
            "Source servers ignored; separately declared target base_url used",
            "Host verifies generated method/route and controls Host, Content-Length, Connection and Accept-Encoding",
            "Host inserts the target bearer; private response headers withheld from the worker",
            "Generated client session cookies discarded; no authentication retry or session workflow",
        ],
    }
    evidence.manifest["locations"].update(browser="none", target="local",
        egress="FastMCP worker: kernel-denied; host: declared loopback target; target OS isolation unverified")
    if not behavioral:
        evidence.manifest["locations"]["inference"] = "none"
    evidence.manifest["observation_gaps"].extend([
        "Workbench-generated surface, not evidence of native developer tools or a Muse API wrapper",
        "Private developer oracle, retained state and atomic target grant enforcement are trusted"
        if behavioral else "No behavioral inference, backend oracle, transaction consent or continuation in this contract run",
        "Direct API validation and MCP wire conformance are separate lanes",
        "FastMCP schema transformations are recorded, not certified lossless",
        "Developer business-effect declarations and target outbound isolation are not independently proven",
    ])
    diff = list(difflib.unified_diff(json.dumps(document, indent=2).splitlines(),
        json.dumps(metadata["tools"], indent=2).splitlines(), fromfile="source OpenAPI", tofile="generated MCP tools"))
    evidence.event("surface_discovered", document, provenance="developer-supplied OpenAPI source")
    evidence.event("surface_exposed", metadata["tools"], provenance="FastMCP generated MCP tools, before inference")
    evidence.event("projection_diff", diff, provenance="source OpenAPI versus native generated MCP catalog")
    evidence.event("inspection_views", {
        "published_surface": document, "exposed_surface": metadata["tools"],
        "observed_input": {"status": "not generated",
                           "reason": "Before behavioral inference" if behavioral else "No behavioral inference requested"},
        "developer_diagnostics": {"mapping": metadata["mapping"], "projection_diff": diff},
        "visibility": {"installed": True, "discoverable": True, "selected_into_context": False, "invoked": False},
    })


async def check_generated(target, evidence, *, static=False):
    document = read_document(target)
    if not static and any(effect not in ("read", "draft") for effect in target.openapi.operations.values()):
        raise Unsupported("Generated committing tools require a separate consent/reconciliation profile")
    worker = ValidatorWorker("fastmcp", evidence, target.openapi.wall_seconds)
    checks = []
    try:
        async with asyncio.timeout(target.openapi.wall_seconds):
            await worker.start()
            metadata = await worker.request("init", document=document, operations=target.openapi.operations,
                base_url=target.base_url, timeout_seconds=target.timeout_seconds)
            expose_generated(document, metadata, target, evidence)
            checks.append({"id": "fastmcp-generated", "status": "passed", "class": "schema error",
                           "detail": "Native FastMCP catalog and complete source-operation associations recorded"})
            if static:
                return checks
            if not target.contract_cases:
                return checks + [{"id": "generated-cases", "status": "observation-gap",
                                  "detail": "Declare contract_cases against the inspected generated tool names"}]
            for case in target.contract_cases:
                association = metadata["mapping"].get(case.tool)
                if association is None or association["effect"] != case.effect:
                    raise LabError("Generated contract case must match an exposed tool and its declared effect")
            schemas = {tool["name"]: tool["inputSchema"] for tool in metadata["tools"]}
            secret = target_credential(target)
            if secret:
                evidence.redactor.secrets.append(secret)
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                         timeout=target.timeout_seconds) as client:
                for index, case in enumerate(target.contract_cases, 1):
                    association = metadata["mapping"][case.tool]
                    identity = f"generated-case:{index}:{case.tool}"
                    evidence.event("action_proposed", {"case": identity, "tool": case.tool,
                        "arguments": case.arguments, "effect": case.effect},
                        provenance="explicit developer case through native generated MCP tools")
                    reply = await worker.request("prepare", tool=case.tool, arguments=case.arguments)
                    response = None
                    if "request" in reply:
                        schema = schemas[case.tool]
                        invalid = list(validators.validator_for(schema)(schema).iter_errors(case.arguments))
                        if invalid:
                            evidence.event("generated_input_enforcement_failure", {
                                "case": identity, "arguments": case.arguments,
                                "failures": [error.message for error in invalid],
                                "proposed_request": {key: value for key, value in reply["request"].items()
                                                     if key != "body"},
                                "disposition": "blocked before target dispatch"},
                                provenance="native generated tool proposed HTTP for input outside its exposed schema")
                            checks.append({"id": identity, "status": "failed", "class": "documented contract violation",
                                "http_status": None, "detail": "Generated tool did not enforce its advertised input schema"})
                            break
                        request = admit_request(reply["request"], association, target)
                        wire_case = {"id": identity, **association, "tool": case.tool, **reply["request"]}
                        if secret:
                            request["headers"]["Authorization"] = "Bearer " + secret
                        response = await request_case(client, request, target, evidence, wire_case)
                        client.cookies.clear()
                        if case.effect != "read" and response["status"] >= 500:
                            raise LabError("Generated draft outcome uncertain after server error",
                                           code=1, category="tool/API")
                        private_free = {**response, "headers": {key: value for key, value in response["headers"].items()
                                                              if key.lower() not in PRIVATE_VALUE_KEYS}}
                        # Send only fields required by FastMCP's HTTP response adapter.
                        try:
                            reply = await worker.request("finish", call_id=reply["call_id"],
                                response={key: private_free[key] for key in ("status", "headers", "body")})
                        except LabError as error:
                            if error.code != 1:
                                raise
                            evidence.event("generated_result_rejected", {"case": identity, "detail": str(error)},
                                           provenance="native FastMCP/MCP client response validation")
                            checks.append({"id": identity, "status": "failed", "class": "documented contract violation",
                                "http_status": response["status"], "detail": str(error)})
                            evidence.manifest["observation_gaps"].append(
                                "Native client rejected the result; no parsed CallToolResult is available")
                            break
                    result = reply["completed"]
                    failed = bool(result.get("isError", False)) != case.expect_error
                    if response is not None and response["status"] >= 400 and not result.get("isError", False):
                        failed = True
                    evidence.event("tool_result", {"case": identity, "result": result},
                                   provenance="native FastMCP CallToolResult; distinct from direct HTTP response")
                    checks.append({"id": identity, "status": "failed" if failed else "passed",
                        "class": "documented contract violation", "http_status": response["status"] if response else None,
                        "detail": "Generated MCP result compared with the declared error expectation"})
                    if failed and case.effect != "read":
                        evidence.manifest["observation_gaps"].append("Failed generated draft; no later cases dispatched")
                        break
    except (httpx.HTTPError, TimeoutError) as error:
        evidence.manifest["observation_gaps"].append(
            "Generated API request interrupted; outcome unknown, no retry or reconciliation performed")
        raise LabError("Generated API transport failed: " + type(error).__name__,
                       category="setup/infrastructure") from error
    finally:
        await worker.close()
    return checks
