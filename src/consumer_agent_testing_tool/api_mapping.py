"""Explicit curated HTTP mappings; no OpenAPI generation or schema rewriting."""
import copy
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic import Field, ValidationError, model_serializer, model_validator

from .config import RecoveryMap, Strict, validation_message
from .consent import digest as consent_digest
from .errors import LabError
from .serialization import digest, loads
from .surface import inside

VERSION = "curated-http-v1"
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
# Credential, framing, range, routing/rewrite, client-address and host-controlled
# negotiation headers; the host owns these and a model argument never sets them.
RESERVED_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "host", "transfer-encoding",
    "connection", "keep-alive", "upgrade", "te", "trailer", "expect", "accept", "range", "if-range",
    "max-forwards", "forwarded", "via", "origin", "referer", "x-forwarded", "x-real-ip",
    "x-http-method-override", "x-http-method", "x-method-override", "x-original-url", "x-original-uri",
    "x-rewrite-url", "x-original-host", "x-host", "x-http-host-override", "x-original-method",
    "true-client-ip", "x-client-ip", "x-cluster-client-ip", "x-remote-ip", "x-remote-addr",
    "x-originating-ip", "client-ip", "cf-connecting-ip", "fastly-client-ip",
}
# content-* also covers Content-Length/Type/Encoding/Range; accept-* covers negotiation.
RESERVED_PREFIXES = ("proxy-", "sec-", "x-forwarded-", "content-", "accept-", "x-envoy-")
# Credential-like names; "key" as a name part except the idempotency key.
CREDENTIAL_WORDS = re.compile(r"auth|token|secret|passw|credential|session|cookie|signature|csrf|xsrf|jwt"
                              r"|api.?key|access.?key|(?<![a-z])key(?![a-z])", re.IGNORECASE)
IDEMPOTENCY_HEADERS = {"idempotency-key", "x-idempotency-key"}


def header_name(name, *, credential=False):
    """Validate an RFC 9110 field name that this map may set."""
    lowered = name.lower()
    if not TOKEN.fullmatch(name) or lowered in RESERVED_HEADERS or lowered.startswith(RESERVED_PREFIXES):
        raise ValueError(f"Header {name!r} is reserved for the host or not a valid field name")
    if not credential and lowered not in IDEMPOTENCY_HEADERS and CREDENTIAL_WORDS.search(name):
        raise ValueError(f"Header {name!r} looks like a credential; credentials come only from the host")
    return lowered


def validate_schema(schema):
    """Keep union/reference semantics; never resolve a reference over the network."""
    Draft202012Validator.check_schema(schema)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("$id", "$dynamicRef", "$dynamicAnchor", "$anchor"):
                    raise ValueError(f"{key} is not supported by curated-http-v1")
                if key == "$schema" and value != "https://json-schema.org/draft/2020-12/schema":
                    raise ValueError("Curated schemas use JSON Schema 2020-12")
                if key == "$ref" and (not isinstance(value, str) or not value.startswith("#/$defs/")):
                    raise ValueError("Schema references must be document-local $defs pointers")
                if key == "$ref":
                    resolved = schema
                    try:
                        for part in value[2:].split("/"):
                            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
                    except (KeyError, TypeError) as error:
                        raise ValueError("Unresolved document-local schema reference") from error
                    if not isinstance(resolved, (dict, bool)):
                        raise ValueError("Reference must resolve to a JSON schema")
                if key == "format" and value not in FormatChecker.checkers:
                    raise ValueError("Unknown schema format cannot be silently ignored")
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)


def validate_value(schema, value):
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        # Do not put potentially credential-bearing instance values in exceptions.
        raise LabError(f"API value violates schema ({errors[0].validator})", code=1, category="tool/API")


def parameter_types(schema, field):
    """Require an explicit supported wire type, without simplifying unions."""
    node, seen = schema["properties"][field], set()
    while isinstance(node, dict) and "type" not in node and "$ref" in node:
        reference = node["$ref"]
        if reference in seen:
            raise ValueError("Parameter reference must resolve to an explicit scalar type")
        seen.add(reference)
        node = schema
        for part in reference[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
    value = node.get("type") if isinstance(node, dict) else None
    if value is None:
        raise ValueError("Path/query parameters require an explicit scalar type")
    return {value} if isinstance(value, str) else set(value)


class RequestMap(Strict):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str
    path_parameters: dict[str, str] = Field(default_factory=dict)
    query_parameters: dict[str, str] = Field(default_factory=dict)
    body_parameters: dict[str, str] = Field(default_factory=dict)
    # Explicit request headers from model arguments, e.g. Idempotency-Key or If-Match.
    header_parameters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def route(self):
        if self.method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            raise ValueError("Unsupported HTTP method")
        names = [header_name(name) for name in self.header_parameters]
        if len(names) != len(set(names)):
            raise ValueError("Header parameters must be unique case-insensitively")
        if not re.fullmatch(r"/[A-Za-z0-9_./{}~-]*", self.path) or "//" in self.path \
                or any(part in (".", "..") for part in self.path.split("/")):
            raise ValueError("HTTP path must be a relative route without escaping or dot segments")
        parameters = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", self.path)
        if set(parameters) != set(self.path_parameters) or len(parameters) != len(set(parameters)) \
                or "{" in re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "", self.path) \
                or "}" in re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "", self.path):
            raise ValueError("Path placeholders must match unique path_parameters")
        if self.method == "HEAD" and self.body_parameters:
            raise ValueError("HEAD cannot carry a mapped JSON body")
        return self

    @model_serializer(mode="wrap")
    def unchanged_digest(self, handler):
        # Maps without headers keep their published mapping_sha256.
        data = handler(self)
        if not data.get("header_parameters"):
            data.pop("header_parameters", None)
        return data


class ResponseMap(Strict):
    schema_: dict | bool | None = Field(alias="schema")
    project: list[str] | None = None

    @model_validator(mode="after")
    def contract(self):
        if self.schema_ is not None:
            validate_schema(self.schema_)
        if self.project is not None:
            schema = self.schema_
            if not self.project or len(self.project) != len(set(self.project)) \
                    or not isinstance(schema, dict) or schema.get("type") != "object" \
                    or set(self.project) - schema.get("properties", {}).keys():
                raise ValueError("Projection names must select explicit object properties")
            # Root relational constraints cannot be faithfully moved to a projected object.
            if set(schema) - {"$schema", "$defs", "type", "properties", "required",
                              "additionalProperties", "description", "title"}:
                raise ValueError("Root schema constraints cannot be silently removed by projection")
        return self

    def effective_schema(self):
        if self.project is None:
            return copy.deepcopy(self.schema_)
        source = self.schema_
        return {"type": "object",
                **({"$defs": copy.deepcopy(source["$defs"])} if "$defs" in source else {}),
                "properties": {key: copy.deepcopy(source["properties"][key]) for key in self.project},
                "required": [key for key in source.get("required", []) if key in self.project],
                "additionalProperties": False}

    def project_result(self, value):
        if self.schema_ is None:
            if value is not None:
                raise LabError("Expected an empty HTTP response", code=1, category="tool/API")
            return None
        validate_value(self.schema_, value)
        projected = {key: value[key] for key in self.project if key in value} if self.project else value
        validate_value(self.effective_schema(), projected)
        return projected


class OperationMap(Strict):
    operation: str = Field(min_length=1)
    description: str = Field(min_length=1)
    effect: Literal["read", "draft", "commit", "cancel"]
    input_schema: dict
    request: RequestMap
    responses: dict[str, ResponseMap]

    @model_validator(mode="after")
    def mapping(self):
        if self.effect not in ("read", "draft", "commit", "cancel"):
            raise ValueError("Every operation requires an explicit business effect")
        validate_schema(self.input_schema)
        schema = self.input_schema
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("Mapped inputs require an explicitly closed object schema")
        if set(schema.get("required", [])) - set(schema.get("properties", {})):
            raise ValueError("Required inputs must have mapped property schemas")
        parameters = [value for group in (self.request.path_parameters, self.request.query_parameters,
                                         self.request.body_parameters, self.request.header_parameters)
                      for value in group.values()]
        if len(parameters) != len(set(parameters)) or set(parameters) != set(schema.get("properties", {})):
            raise ValueError("Map every input property exactly once; no hidden or dropped arguments")
        if set(self.request.path_parameters.values()) - set(schema.get("required", [])):
            raise ValueError("Path parameters must be required inputs")
        for field in self.request.path_parameters.values():
            if parameter_types(schema, field) - {"string", "integer"}:
                raise ValueError("Path schemas must allow only strings or integers")
        for field in self.request.query_parameters.values():
            if parameter_types(schema, field) - {"string", "integer", "number", "boolean"}:
                raise ValueError("Query schemas must allow only non-null scalar types")
        for field in self.request.header_parameters.values():
            if parameter_types(schema, field) - {"string", "integer"}:
                raise ValueError("Header schemas must allow only strings or integers")
        if self.effect in ("commit", "cancel"):
            try:
                consent_digest(schema)  # A consent-bound schema must be canonicalizable before any trial.
            except LabError as error:
                raise ValueError(f"Consent-bound input schema cannot be canonicalized: {error}") from error
        if not self.responses or any(not re.fullmatch(r"[1-5][0-9]{2}", key) for key in self.responses):
            raise ValueError("Declare response contracts by exact HTTP status")
        return self

    def request_for(self, arguments):
        validate_value(self.input_schema, arguments)
        # Roundtrip rejects non-finite numbers and unsupported Python objects.
        arguments = loads(json.dumps(arguments, allow_nan=False), json_only=True)
        path = self.request.path
        for placeholder, key in self.request.path_parameters.items():
            value = arguments[key]
            if type(value) not in (str, int, float) or str(value) in ("", ".", "..") \
                    or any(c in str(value) for c in "/\\%?#"):
                raise LabError("Unsupported path parameter value")
            path = path.replace("{" + placeholder + "}", quote(str(value), safe=""))
        query = {}
        for parameter, key in self.request.query_parameters.items():
            if key not in arguments:
                continue
            value = arguments[key]
            if type(value) not in (str, int, float, bool):
                raise LabError("Query mapping requires scalar non-null values")
            query[parameter] = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
        body = {parameter: arguments[key] for parameter, key in self.request.body_parameters.items()
                if key in arguments}
        headers = {}
        for name, key in self.request.header_parameters.items():
            if key not in arguments:
                continue
            value = arguments[key]
            text = value if isinstance(value, str) else str(value) if type(value) is int else None
            # Visible ASCII only: no CR/LF/NUL header splitting or obs-text.
            if text is None or not text or len(text) > 1024 or not re.fullmatch(r"[\x20-\x7e]+", text) \
                    or text != text.strip():
                raise LabError("Unsupported header parameter value")
            headers[name] = text
        return {"method": self.request.method,
                "path": path + ("?" + urlencode(query) if query else ""),
                "body": body if self.request.body_parameters else None,
                **({"headers": headers} if headers else {})}


class ApiMapping(Strict):
    schema_version: Literal[1]
    provenance: str = Field(min_length=1)
    operations: dict[str, OperationMap] = Field(min_length=1)
    recovery: dict[str, RecoveryMap] = Field(default_factory=dict)
    # API-key header name for the host credential (e.g. X-API-Key). The value is
    # always the target's host-held credential; the default is Authorization: Bearer.
    credential_header: str | None = None

    @model_validator(mode="after")
    def version(self):
        if self.schema_version != 1 or not self.operations:
            raise ValueError("A v1 curated map requires operations")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) for name in self.operations):
            raise ValueError("Operation tool names must be native function identifiers")
        if len({row.operation for row in self.operations.values()}) != len(self.operations):
            raise ValueError("Operation identities must be unique")
        if self.credential_header is not None:
            credential = header_name(self.credential_header, credential=True)
            if any(name.lower() == credential for row in self.operations.values()
                   for name in row.request.header_parameters):
                raise ValueError("A model header parameter cannot set the host credential header")
        for name, recovery in self.recovery.items():
            operation = self.operations.get(name)
            lookup = self.operations.get(recovery.lookup_tool)
            if operation is None or operation.effect == "read" or lookup is None or lookup.effect != "read":
                raise ValueError("Recovery must map a declared write to a declared read-only tool")
            if set(recovery.arguments) != set(lookup.input_schema.get("properties", {})) \
                    or set(recovery.arguments.values()) - set(operation.input_schema.get("required", [])):
                raise ValueError("Recovery lookup inputs must map exactly to required original arguments")
            if "200" not in lookup.responses or lookup.responses["200"].project is not None:
                raise ValueError("Recovery requires an unprojected HTTP 200 receipt")
            if recovery.poll and ("202" not in lookup.responses or lookup.responses["202"].project is not None):
                raise ValueError("Polling recovery requires an unprojected HTTP 202 pending receipt")
        return self

    @model_serializer(mode="wrap")
    def unchanged_digest(self, handler):
        data = handler(self)
        if data.get("credential_header") is None:
            data.pop("credential_header", None)
        return data

    def credential_headers(self, credential):
        """Host credential placement; never derived from a model argument."""
        if not credential:
            return {}
        if self.credential_header:
            return {self.credential_header: credential}
        return {"Authorization": "Bearer " + credential}

    def catalog(self):
        return {name: {"name": name, "operation": row.operation, "description": row.description,
                       "effect": row.effect, "input_schema": row.input_schema,
                       "responses": {status: reply.effective_schema() for status, reply in row.responses.items()}}
                for name, row in self.operations.items()}

    def record(self):
        return {"adapter": VERSION, "mapping_sha256": digest(self.model_dump(mode="json", by_alias=True)),
                "source": self.model_dump(mode="json", by_alias=True), "effective": self.catalog(),
                "defaults": "JSON Schema defaults are annotations; missing optional arguments stay omitted",
                "provenance": self.provenance}


def read_mapping(target):
    path = inside(Path.cwd(), target.api_mapping)
    if path.stat().st_size > 1024 * 1024:
        raise LabError("API mapping exceeds 1 MiB")
    try:
        return ApiMapping.model_validate(loads(path.read_text()))
    except SchemaError as error:
        raise LabError("Invalid JSON schema in curated API mapping") from error
    except (ValidationError, ValueError) as error:
        detail = validation_message(error) if isinstance(error, ValidationError) else str(error)
        raise LabError("Invalid curated API mapping: " + detail) from error
