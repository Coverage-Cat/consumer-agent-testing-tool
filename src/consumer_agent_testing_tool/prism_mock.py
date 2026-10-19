"""Stateless upstream mock responses; no HTTP client or service credentials."""
import base64
import hashlib

from .errors import LabError
from .evidence import PRIVATE_VALUE_KEYS
from .surface import pointer


def register_private_examples(document, evidence):
    """Source examples can reappear as unstructured upstream diagnostics."""
    values = set()

    def strings(value):
        if isinstance(value, str) and len(value) >= 4:
            values.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                strings(item)
        elif isinstance(value, list):
            for item in value:
                strings(item)

    def examples(value, seen):
        if not isinstance(value, dict):
            return
        ref = value.get("$ref")
        if ref and ref not in seen:
            examples(pointer(document, ref[1:]), seen | {ref})
        for name in ("example", "examples", "default", "enum"):
            if name in value:
                strings(value[name])
        if isinstance(value.get("schema"), dict):
            examples(value["schema"], seen)

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in PRIVATE_VALUE_KEYS and isinstance(item, dict):
                    examples(item, set())
                walk(item)
            if str(value.get("name", "")).lower() in PRIVATE_VALUE_KEYS:
                examples(value, set())
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    evidence.redactor.secrets = sorted(set(evidence.redactor.secrets) | values, key=len, reverse=True)


async def mock_case(node, case, request, target, evidence):
    raw = base64.b64decode(case["body"], validate=True) if case["body"] is not None else None
    text = raw.decode("utf-8", errors="replace") if raw is not None else None
    request_event = evidence.event("prism_mock_request", {
        "case_id": case["id"], "operation": case["operation"], "mode": case["mode"],
        "serialized": {"method": case["method"], "url": case["url"], "headers": case["headers"],
            "body_utf8": text, "body_bytes": len(raw) if raw is not None else 0,
            "body_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "body_utf8_lossy": raw is not None and text.encode("utf-8") != raw},
        "prism_input": {key: value for key, value in request.items() if key != "body"},
        "body_projection": "Redacted UTF-8 view; Prism parses JSON media types before validation"},
        provenance="Schemathesis serialized case and Prism URL/query projection; no HTTP dispatched")
    result = await node.request("mock", method=case["method"], path=case["path"], request=request,
                                max_response_bytes=target.max_response_bytes)
    if "error" in result:
        evidence.event("prism_mock_error", {"case_id": case["id"], "error": result["error"]},
                       parent_id=request_event,
                       provenance="Prism could not synthesize a response; no HTTP fallback")
        raise LabError("Prism could not synthesize the selected response; see prism_mock_error",
                       code=1, category="tool/API")
    response = result["response"]
    data = base64.b64decode(response["body"], validate=True)
    if type(response["status"]) is not int or not 100 <= response["status"] <= 599:
        raise LabError("Prism returned an invalid mock status", code=1, category="tool/API")
    if len(data) > target.max_response_bytes:
        raise LabError("Prism mock response exceeds byte budget", code=1, category="tool/API")
    for key, value in response["header_items"]:
        if key.lower() in PRIVATE_VALUE_KEYS and isinstance(value, str) and len(value) >= 4:
            evidence.redactor.secrets.append(value)
    evidence.redactor.secrets.sort(key=len, reverse=True)
    evidence.event("prism_mock_response", {"case_id": case["id"], "operation": case["operation"],
        "status": response["status"], "headers": response["headers"],
        "body": data.decode("utf-8"), "validations": result["validations"]},
        parent_id=request_event,
        provenance="Prism static example/schema synthesis; no HTTP, backend state or business effect")
    return response, result["validations"]
