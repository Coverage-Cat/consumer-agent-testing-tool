"""Direct contract checks for explicitly mapped developer HTTP APIs."""
import httpx
from mcp import types

from .api_mapping import read_mapping
from .api_transport import dispatch
from .config import is_loopback
from .destinations import target_credential
from .errors import LabError, Unsupported
from .lint import usability_checks
from .serialization import digest


def expose(mapping, target, evidence, *, behavioral=False):
    record = mapping.record()
    evidence.profile.update(adapter=record["adapter"], mapping_provenance=record["provenance"])
    if "profile" in evidence.manifest["run_lock"]:
        evidence.manifest["run_lock"]["profile"] = dict(evidence.profile)
    evidence.manifest["run_lock"]["api_mapping"] = record
    evidence.manifest["locations"].update(
        browser="none", target="local" if is_loopback(target.base_url) else "remote")
    if not behavioral:
        evidence.manifest["locations"]["inference"] = "none"
    if "offline_policy" not in evidence.manifest["run_lock"]:
        evidence.manifest["locations"]["egress"] = (
            "declared direct HTTP target; no target OS isolation; " +
            ("separately selected inference" if behavioral else "inference disabled"))
    evidence.manifest["observation_gaps"].extend([
        "Curated developer schema declarations are not independently established service documentation",
        "No Spectral, Prism, Schemathesis or generated OpenAPI baseline was executed",
    ])
    if not behavioral:
        evidence.manifest["observation_gaps"].append(
            "Contract responses do not establish committed backend state; no independent oracle configured")
    else:
        evidence.manifest["observation_gaps"].append(
            "Developer control adapter is trusted to isolate/reset test state and enforce consent atomically; "
            "no OS isolation or real-Muse fidelity proof")
    evidence.event("surface_discovered", record["source"], provenance="developer-maintained curated HTTP map")
    evidence.event("surface_exposed", record["effective"], provenance=record["adapter"])
    evidence.event("inspection_views", {
        "published_surface": record["source"], "exposed_surface": record["effective"],
        "observed_input": {"status": "not generated",
                           "reason": "Before behavioral inference" if behavioral else "No inference requested"},
        "visibility": {"installed": True, "discoverable": True,
                       "selected_into_context": False, "invoked": False},
        "developer_diagnostics": {"response_projection": {
            name: {status: reply.project for status, reply in operation.responses.items()}
            for name, operation in mapping.operations.items()}},
    })
    tools = [types.Tool(name=name, description=row.description, input_schema=row.input_schema)
             for name, row in mapping.operations.items()]
    return [{"id": "api-mapping", "status": "passed", "class": "schema error",
             "detail": "Validated explicit request mapping and source/effective JSON schemas"},
            *usability_checks(tools)]


async def inspect_api(target, evidence):
    if target.openapi:
        if target.openapi.adapter == "fastmcp-generated":
            from .generated_api import check_generated
            return await check_generated(target, evidence, static=True)
        from .openapi_contract import check_openapi
        return await check_openapi(target, evidence, static=True)
    return expose(read_mapping(target), target, evidence)


async def check_api(target, evidence):
    if target.openapi:
        if target.openapi.adapter == "fastmcp-generated":
            from .generated_api import check_generated
            return await check_generated(target, evidence)
        from .openapi_contract import check_openapi
        return await check_openapi(target, evidence)
    mapping = read_mapping(target)
    checks = expose(mapping, target, evidence)
    if not target.contract_cases:
        return checks + [{"id": "api-cases", "status": "observation-gap",
                          "detail": "No explicit developer contract requests declared"}]
    # Prepare the complete case list before dispatch: a late invalid case cannot
    # cause earlier declared mutations to run under a partially valid test.
    prepared = []
    for case in target.contract_cases:
        operation = mapping.operations.get(case.tool)
        if operation is None or operation.effect != case.effect:
            raise LabError("Contract case must match a declared tool and business effect")
        if operation.effect not in ("read", "draft"):
            raise Unsupported("Committing API contracts require a domain consent adapter")
        prepared.append((case, operation, operation.request_for(case.arguments)))
    secret = target_credential(target)
    if secret:
        evidence.redactor.secrets.append(secret)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    # Host-held credential only: Bearer by default, or the map's API-key header, as in the behavioral lane.
    headers.update(mapping.credential_headers(secret))
    evidence.manifest["run_lock"]["api_transport"] = {
        "base_url": target.base_url, "follow_redirects": False, "trust_env": False,
        "retries": 0, "timeout_seconds": target.timeout_seconds,
        "max_response_bytes": target.max_response_bytes, "auth": target.auth.model_dump(mode="json"),
        "cases_sha256": digest([case.model_dump(mode="json") for case in target.contract_cases]),
        "scope": "declared direct HTTP cases; no automatic discovery, retry, reset or backend oracle",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=False, trust_env=False,
                                 timeout=target.timeout_seconds) as client:
        for number, (case, operation, request) in enumerate(prepared, 1):
            identity = f"api-case:{number}:{case.tool}"
            evidence.event("action_proposed", {"case": identity, "operation": operation.operation,
                "effect": operation.effect, "arguments": case.arguments, "request": request},
                provenance="explicit developer contract case; not model consent")
            try:
                response = await dispatch(client, target, operation, request, evidence, identity)
                evidence.event("tool_result", {"case": identity, "result": response["result"]},
                               provenance="declared curated response projection; no model invoked")
                if (response["status"] >= 400) != case.expect_error:
                    raise LabError("HTTP success/error differs from the declared case expectation",
                                   code=1, category="tool/API")
                checks.append({"id": identity, "status": "passed", "class": "documented contract violation"})
            except (LabError, httpx.HTTPError, TimeoutError) as error:
                if isinstance(error, (httpx.HTTPError, TimeoutError)):
                    evidence.event("api_transport_failure", {"case": identity,
                        "error": type(error).__name__, "effect": operation.effect},
                        provenance="HTTP client; outcome unknown, no automatic retry")
                    if operation.effect != "read":
                        evidence.manifest["observation_gaps"].append(
                            "Uncertain API draft outcome; no retry or subsequent case was dispatched")
                    raise LabError("API transport failed: " + type(error).__name__,
                                   category="setup/infrastructure") from error
                checks.append({"id": identity, "status": "failed",
                    "class": error.category if isinstance(error, LabError) else "setup/infrastructure",
                    "detail": str(error) if isinstance(error, LabError) else type(error).__name__})
                # A timed-out write may have committed. Stop instead of executing
                # the rest of a dependent test plan or retrying the mutation.
                if operation.effect != "read":
                    evidence.manifest["observation_gaps"].append(
                        "Failed draft response: backend effect is unverified; remaining requests were not sent")
                    break
    return checks
