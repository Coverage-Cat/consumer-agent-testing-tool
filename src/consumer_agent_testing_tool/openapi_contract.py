"""Explicit generated API contracts using Spectral, Prism and Schemathesis."""
import asyncio
import base64
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .api_tools import ValidatorWorker
from .destinations import target_credential
from .errors import LabError, Unsupported
from .evidence import PRIVATE_VALUE_KEYS
from .serialization import digest, load
from .surface import inside, pointer

RESERVED_HEADERS = {"authorization", "proxy-authorization", "cookie", "host", "content-length",
                    "transfer-encoding", "connection", "accept-encoding", "forwarded", "x-api-key"}


def read_document(target):
    path = inside(Path.cwd(), target.openapi.path)
    if path.stat().st_size > 1024 * 1024:
        raise Unsupported("OpenAPI source exceeds 1 MiB")
    document = load(path)
    if not isinstance(document, dict) or not re.fullmatch(r"3\.[01]\.\d+", str(document.get("openapi", ""))):
        raise Unsupported("The OpenAPI validator profile currently accepts OpenAPI 3.0/3.1")

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$id", "$dynamicRef", "$dynamicAnchor", "$anchor", "externalValue"):
                    raise Unsupported("OpenAPI checks cannot fetch external examples or dynamic schema scopes")
                if key == "$ref":
                    if not isinstance(item, str) or not item.startswith("#/"):
                        raise Unsupported("OpenAPI references must stay inside this document")
                    try:
                        pointer(document, item[1:])
                    except (KeyError, IndexError, ValueError, TypeError) as error:
                        raise LabError("OpenAPI contains an unresolved local reference") from error
                if value.get("in") == "header" and isinstance(value.get("name"), str):
                    name = value["name"].lower()
                    if name in RESERVED_HEADERS or name.startswith("x-forwarded-"):
                        raise Unsupported("Generated cases cannot control credential, routing or framing headers")
                if value.get("in") == "cookie":
                    raise Unsupported("Generated cookie credentials/session state require a separate profile")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(document)
    return document


def prepare_case(case, target):
    """Keep upstream serialization; reject routes that escape the declared operation."""
    parsed, base = urlsplit(case["url"]), urlsplit(target.base_url)
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc \
            or parsed.username or parsed.password or parsed.fragment:
        raise Unsupported("Generated request escapes the declared target origin")
    route = case["path"]
    if not route.startswith("/") or "//" in route or any(s in (".", "..") for s in route.split("/")) \
            or any(char in route for char in "\\%?#"):
        raise Unsupported("OpenAPI path cannot escape the declared API base")
    fragments = re.split(r"(\{[^{}]+\})", route)
    pattern = re.escape(base.path.rstrip("/")) + "".join(
        r"([^/]+)" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in fragments)
    match = re.fullmatch(pattern, parsed.path)
    if not match or any(unquote(value) in (".", "..") or any(
            character in unquote(value) for character in "/\\%?#\x00\r\n") for value in match.groups()):
        raise Unsupported("Generated path parameter would change the declared route")
    if len(case["url"].encode()) > 16384:
        raise Unsupported("Generated URL exceeds the 16 KiB request budget")
    body = base64.b64decode(case["body"], validate=True) if case["body"] is not None else None
    if body is not None and len(body) > 1024 * 1024:
        raise Unsupported("Generated request body exceeds 1 MiB")
    headers = dict(case["headers"])
    if any(name.lower() in RESERVED_HEADERS - {"connection", "accept-encoding", "content-length"}
           or name.lower().startswith("x-forwarded-") for name in headers):
        raise Unsupported("Generated request attempted to supply a private credential or routing header")
    headers = {key: value for key, value in headers.items()
               if key.lower() not in ("connection", "accept-encoding")}
    headers["Accept-Encoding"] = "identity"
    # Construct without dispatch so invalid encodings or headers cannot cause a
    # partially validated plan to start.
    httpx.Request(case["method"], case["url"], headers=headers, content=body)
    return {"method": case["method"], "url": case["url"], "headers": headers, "content": body}


async def request_case(client, request, target, evidence, case):
    started = time.monotonic()
    body = request["content"]
    evidence.event("action_proposed", {
        "case": {key: value for key, value in case.items() if key != "body"},
        "body_text": body.decode("utf-8", errors="replace") if body is not None else None,
        "effect": target.openapi.operations[case["operation"]]},
                   provenance="Schemathesis generated case; explicitly declared developer effect")
    async with asyncio.timeout(target.timeout_seconds):
        async with client.stream(**request) as response:
            # Upstream diagnostics may quote a header value without its field name.
            # Register newly minted private values before recording any response or diagnostic.
            for key, value in response.headers.multi_items():
                if key.lower() in PRIVATE_VALUE_KEYS and len(value) >= 4 and value not in evidence.redactor.secrets:
                    evidence.redactor.secrets.append(value)
            evidence.redactor.secrets.sort(key=len, reverse=True)
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                if len(data) + len(chunk) > target.max_response_bytes:
                    raise LabError("API response exceeds configured byte budget", code=1, category="tool/API")
                data.extend(chunk)
            record = {"status": response.status_code, "headers": dict(response.headers),
                      "header_items": list(response.headers.multi_items()),
                      "elapsed": time.monotonic() - started, "body": base64.b64encode(data).decode()}
            evidence.event("network_request", {"case_id": case["id"], "operation": case["operation"],
                "method": case["method"], "url": case["url"], "status": response.status_code,
                "seconds": time.monotonic() - started},
                provenance="host HTTP transport; no redirects/proxies/retries")
            # Keep readable source content for normal text redaction, not an
            # unredactable base64 copy of credentials or personal data.
            evidence.event("api_source_response", {"case_id": case["id"], "status": response.status_code,
                "headers": dict(response.headers),
                "header_items": [{key: value} for key, value in record["header_items"]],
                "body": data.decode("utf-8", errors="replace")},
                provenance="direct API response; no backend outcome inference")
            if 300 <= response.status_code < 400:
                raise LabError("Redirect blocked; no follow-up request sent", code=1, category="tool/API")
            return record


async def check_openapi(target, evidence, *, static=False):
    document = read_document(target)
    checks = []
    settings = target.openapi
    mocked = settings.adapter == "prism-mock"
    if not static and not mocked and any(effect in ("commit", "cancel") for effect in settings.operations.values()):
        raise Unsupported("Generated committing operations require a separate consent/reconciliation profile")
    evidence.manifest["run_lock"].update(openapi={"source": document, "source_sha256": digest(document),
        "settings": settings.model_dump(mode="json"), "adapter": "openapi-contract-v1",
        "transport": {"base_url": target.base_url, "retries": 0, "follow_redirects": False, "trust_env": False,
            "max_response_bytes": target.max_response_bytes, "timeout_seconds": target.timeout_seconds},
        "transformations": ["Source servers ignored in favor of declared target base_url",
            "Generated authentication disabled; host inserts the separately referenced target bearer",
            "Connection and Accept-Encoding controlled by bounded host transport"]})
    evidence.manifest["locations"].update(inference="none", browser="none", target="local",
        egress="validator workers: kernel-denied; host: declared loopback target; target OS isolation unverified")
    evidence.manifest["observation_gaps"].extend([
        "Generated contract cases do not verify committed backend state or absence of side effects",
        "No authentication retries, stateful links, generated agent wrapper or real-Muse calibration",
        "Developer effect declarations and the target's own outbound behavior are not independently proven",
    ])
    if mocked:
        from .prism_mock import register_private_examples
        register_private_examples(document, evidence)
        evidence.manifest["fidelity"] = "stateless Prism mock; no real service execution"
        evidence.profile["adapter"] = "prism-stateless-mock-v1"
        evidence.manifest["run_lock"]["openapi"].update(adapter="prism-stateless-mock-v1",
            transport={"execution": "disabled; no HTTP client created", "virtual_base_url": target.base_url,
                       "max_response_bytes": target.max_response_bytes},
            mock={"engine": "@stoplight/prism-http", "dynamic": False, "isProxy": False,
                  "validateRequest": True, "validateResponse": True, "checkSecurity": True,
                  "callbacks": "unsupported", "elapsed_seconds": "synthetic zero; not API latency"},
            transformations=["Source servers ignored; base_url used only for request serialization",
                "Prism static examples/schema sampling; no request forwarding or HTTP fallback",
                "No real authentication, persistent state, grants or business effects"])
        evidence.manifest["locations"].update(target="local stateless Prism mock",
            egress="kernel-denied validator workers; coordinator creates no HTTP client")
        evidence.manifest["observation_gaps"].extend([
            "Mock success validates synthesized response contracts, not the developer's running API",
            "Authentication is schema-only; no principal, credential or session verification",
            "No persistent state, backend oracle, transaction consent or business effects",
            "Static synthesis may not support every schema; failures never fall back to HTTP",
        ])
    evidence.event("surface_discovered", document, provenance="developer-supplied OpenAPI source")
    node = ValidatorWorker("node", evidence, settings.wall_seconds)
    python = ValidatorWorker("python", evidence, settings.wall_seconds)
    try:
        async with asyncio.timeout(settings.wall_seconds):
            await node.start()
            deferred_offline = evidence.manifest["run_lock"].get("offline_policy", {}).get(
                "state") == "pending-mock-workers"
            if deferred_offline:
                if not mocked:
                    raise Unsupported("Deferred offline worker startup is only available for stateless Prism mocks")
                if not static:
                    await python.start()
                from .offline import finish_mock_worker_startup
                finish_mock_worker_startup(evidence)
            metadata = await node.request("init", document=document)
            evidence.manifest["run_lock"]["openapi"]["node"] = metadata["versions"]
            evidence.event("surface_exposed", metadata["prism_operations"],
                           provenance="Prism normalized validation contracts; not model tools")
            for row in metadata["spectral"]:
                checks.append({"id": "spectral:" + str(row["code"]), "status": "failed" if row["severity"] == 0 else "warning",
                    "class": "schema error" if row["severity"] == 0 else "heuristic usability warning",
                    "detail": row["message"], "path": row.get("path"), "required": row["severity"] == 0})
            if not any(row["status"] == "failed" for row in checks):
                checks.append({"id": "spectral", "status": "passed", "class": "schema error"})
            evidence.event("inspection_views", {"published_surface": document,
                "exposed_surface": metadata["prism_operations"],
                "observed_input": {"status": "not generated", "reason":
                    "No inference; stateless Prism mock" if mocked else "No inference; this is direct API validation"},
                "developer_diagnostics": {"spectral": metadata["spectral"]},
                "visibility": {"installed": True, "discoverable": True, "selected_into_context": False, "invoked": False}})
            if static or any(row["status"] == "failed" for row in checks):
                return checks
            if python.process is None:
                await python.start()
            engine = await python.request("init", document=document)
            evidence.manifest["run_lock"]["openapi"]["schemathesis"] = engine
            candidates = {}
            for operation in engine["operations"]:
                name = operation["operation"]
                if name:
                    candidates.setdefault(name, []).append(operation)
            if any(len(candidates.get(name, [])) != 1 for name in settings.operations):
                raise LabError("Each selected OpenAPI operationId must resolve uniquely")
            operations = [candidates[name][0] for name in sorted(settings.operations)]
            if not operations:
                return checks + [{"id": "openapi-cases", "status": "observation-gap",
                                  "detail": "Declare operations and their effects before generated HTTP execution"}]
            plan = await python.request("generate", operations=operations, base_url=target.base_url,
                                        seed=settings.seed, examples=settings.examples, modes=settings.modes)
            for gap in plan["gaps"]:
                checks.append({"id": "generation:" + gap["operation"] + ":" + gap["mode"],
                               "status": "observation-gap", "detail": gap["reason"]})
            prepared = []
            for case in plan["cases"]:
                try:
                    prepared.append((case, prepare_case(case, target)))
                except (Unsupported, ValueError, UnicodeError) as error:
                    checks.append({"id": case["id"], "status": "unsupported",
                                   "class": "policy/approval", "detail": str(error)})
            # Never log the binary wire container without text-aware redaction.
            evidence.event("openapi_generation", {"seed": settings.seed, "examples_per_mode": settings.examples,
                "generated": len(plan["cases"]), "admitted": len(prepared), "gaps": plan["gaps"]},
                provenance="Schemathesis/Hypothesis generation; no shrinking or automatic mutation replay")
            secret = None if mocked else target_credential(target)
            if secret:
                evidence.redactor.secrets.append(secret)
            async with AsyncExitStack() as stack:
                client = None if mocked else await stack.enter_async_context(httpx.AsyncClient(
                    follow_redirects=False, trust_env=False, timeout=target.timeout_seconds))
                for case, request in prepared:
                    effect = settings.operations[case["operation"]]
                    if secret:
                        request["headers"]["Authorization"] = "Bearer " + secret
                    try:
                        parsed = urlsplit(case["url"])
                        prism_request = {"method": case["method"].lower(),
                            "url": {"path": parsed.path[len(urlsplit(target.base_url).path.rstrip("/")):],
                                "query": {k: v[0] if len(v) == 1 else v for k, v in parse_qs(
                                    parsed.query, keep_blank_values=True).items()}},
                            "headers": case["headers"], "body": case["body"]}
                        if mocked:
                            from .prism_mock import mock_case
                            response, prism = await mock_case(node, case, prism_request, target, evidence)
                        else:
                            response = await request_case(client, request, target, evidence, case)
                        # A server error after a write cannot be treated as a safe terminal outcome.
                        if not mocked and effect != "read" and response["status"] >= 500:
                            raise LabError("Draft outcome uncertain after server error", code=1, category="tool/API")
                        if not mocked:
                            prism = await node.request("validate", method=case["method"], path=case["path"],
                                request=prism_request,
                                response={"statusCode": response["status"], "headers": response["headers"],
                                          "body": response["body"]})
                        native = await python.request("validate", id=case["id"], **response)
                        evidence.event("openapi_validated", {"case_id": case["id"], "mode": case["mode"],
                            "prism": prism, "schemathesis": native},
                            provenance="separate upstream validators; " +
                                ("synthesized mock contracts only" if mocked else "public response contracts only"))
                        failed = bool(prism["output"] or native["failures"] or
                                      case["mode"] == "positive" and prism["input"])
                        checks.append({"id": case["id"], "status": "failed" if failed else "passed",
                            "class": "documented contract violation",
                            "detail": ("Prism stateless mock" if mocked else "Prism response validation") +
                                " and explicit Schemathesis checks",
                            "operation": case["operation"], "mode": case["mode"]})
                        if failed and effect != "read" and not mocked:
                            evidence.manifest["observation_gaps"].append("Failed draft contract; later cases not dispatched")
                            break
                    except (LabError, httpx.HTTPError, TimeoutError) as error:
                        checks.append({"id": case["id"], "status": "unsupported" if isinstance(error, Unsupported) else "failed",
                            "class": error.category if isinstance(error, LabError) else "setup/infrastructure",
                            "detail": str(error) if isinstance(error, LabError) else type(error).__name__})
                        if (effect != "read" and not mocked) or not isinstance(error, LabError) or \
                                any(worker.process is None or worker.process.returncode is not None
                                    for worker in (node, python)):
                            evidence.manifest["observation_gaps"].append("Uncertain request outcome; no retry or later case")
                            break
            return checks
    except TimeoutError as error:
        raise LabError("OpenAPI suite deadline reached; no automatic retry") from error
    finally:
        await node.close()
        await python.close()
