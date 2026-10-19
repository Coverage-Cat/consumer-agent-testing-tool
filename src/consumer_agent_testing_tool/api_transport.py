"""Bounded HTTP dispatch shared by mapped contracts and behavioral trials."""
import asyncio
import re
import time

from .errors import LabError
from .serialization import loads

# RFC 8259 JSON and RFC 6839 structured-syntax suffixes (problem+json, vnd.api+json, hal+json).
JSON_MEDIA = re.compile(r"application/(?:json|[a-z0-9!#$&^_.+-]+\+json)")


def json_media(content_type):
    return bool(JSON_MEDIA.fullmatch((content_type or "").split(";")[0].strip().lower()))


async def dispatch(client, target, operation, request, evidence, identity):
    # A compiled relative path cannot replace the origin or base path.
    url = target.base_url.rstrip("/") + request["path"]
    started = time.monotonic()
    options = {"json": request["body"]} if request["body"] is not None else {}
    if request.get("headers"):
        # Mapped, validated non-credential headers only; the client owns auth and framing.
        options["headers"] = request["headers"]
    async with asyncio.timeout(target.timeout_seconds):
        async with client.stream(request["method"], url, **options) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                if len(content) + len(chunk) > target.max_response_bytes:
                    raise LabError("API response exceeds configured byte budget", code=1, category="tool/API")
                content.extend(chunk)
            evidence.event("network_request", {"case": identity,
                "method": request["method"], "path": request["path"],
                "header_names": sorted(request.get("headers", {})),
                "status": response.status_code, "content_type": response.headers.get("content-type"),
                "retry_after": response.headers.get("retry-after"),
                "seconds": time.monotonic() - started},
                provenance="direct HTTP response; redirects and retries disabled")
            if 300 <= response.status_code < 400:
                raise LabError("API redirect blocked; no follow-up request sent", code=1, category="tool/API")
            contract = operation.responses.get(str(response.status_code))
            if contract is None:
                raise LabError("API returned an undeclared HTTP status", code=1, category="tool/API")
            if contract.schema_ is None:
                if content:
                    raise LabError("Expected empty response body", code=1, category="tool/API")
                source = None
            else:
                if not json_media(response.headers.get("content-type")):
                    raise LabError("Expected a JSON response (application/json or application/*+json)",
                                   code=1, category="tool/API")
                try:
                    source = loads(content.decode("utf-8"), json_only=True)
                except (UnicodeError, LabError) as error:
                    raise LabError("Invalid JSON response", code=1, category="tool/API") from error
            evidence.event("api_source_response", {"case": identity, "result": source},
                           provenance="direct HTTP JSON body before response projection")
            return {"status": response.status_code, "result": contract.project_result(source)}
