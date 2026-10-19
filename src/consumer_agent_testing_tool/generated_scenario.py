"""Inspect trials using the actual generated MCP catalog and HTTP serializer."""
import asyncio
import copy
import json
import re
from dataclasses import dataclass
from functools import partial

from .api_mapping import validate_value
from .api_tools import ValidatorWorker
from .errors import LabError, Unsupported
from .evidence import PRIVATE_VALUE_KEYS
from .generated_api import admit_request, expose_generated
from .generated_resume import GeneratedRecovery
from .mapped_scenario import MappedAdapter, mapped_catalog, run_with_mapping
from .openapi_contract import read_document, request_case
from .serialization import digest, load


@dataclass(frozen=True)
class GeneratedOperation:
    operation: str
    effect: str
    description: str
    input_schema: dict


class GeneratedMapping:
    """The control adapter pins this record; it is not a curated request map."""
    generated = True

    def __init__(self, document, metadata, recovery=None):
        self.document, self.metadata = copy.deepcopy(document), copy.deepcopy(metadata)
        self.recovery = copy.deepcopy(recovery or {})
        self.operations = {}
        for tool in metadata["tools"]:
            name = tool["name"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                raise Unsupported("Generated tool name cannot be used by this native function profile")
            association = metadata["mapping"][name]
            schema = copy.deepcopy(tool["inputSchema"])
            # JSON Schema's omitted additionalProperties means true, whereas
            # Inspect ToolParams defaults it to false. Preserve semantics and
            # record this explicit normalization beside the untouched catalog.
            schema.setdefault("additionalProperties", True)
            self.operations[name] = GeneratedOperation(association["operation"], association["effect"],
                tool.get("description", ""), schema)
        for name, rule in self.recovery.items():
            original, lookup = self.operations.get(name), self.operations.get(rule.lookup_tool)
            if original is None or original.effect == "read" or lookup is None or lookup.effect != "read":
                raise Unsupported("Generated recovery must map a declared write to a read-only native tool")
            if set(rule.arguments) != set(lookup.input_schema.get("properties", {})) \
                    or set(rule.arguments.values()) - set(original.input_schema.get("required", [])):
                raise Unsupported("Generated recovery inputs must map exactly to required original arguments")
            if rule.poll:
                route = metadata["mapping"][rule.lookup_tool]
                responses = document["paths"][route["path"]][route["method"].lower()]["responses"]
                if not {"200", "202"} <= set(responses):
                    raise Unsupported("Generated polling recovery requires declared HTTP 200 and 202 receipts")

    def model_dump(self, **_kwargs):
        return {"schema_version": 1, "adapter": "fastmcp-generated-v1",
                "source": copy.deepcopy(self.document), "generator": copy.deepcopy(self.metadata),
                "effective": self.catalog(),
                "recovery": {key: value.model_dump(mode="json") for key, value in self.recovery.items()}}

    def catalog(self):
        return {name: {"name": name, "operation": row.operation, "description": row.description,
                      "effect": row.effect, "input_schema": copy.deepcopy(row.input_schema)}
                for name, row in self.operations.items()}

    def record(self):
        return {"adapter": "fastmcp-generated-v1", "mapping_sha256": digest(self.model_dump()),
                "source": self.model_dump(), "effective": self.catalog(),
                "provenance": "FastMCP-generated OpenAPI surface; no lossless-mapping claim"}


async def initialize_mapping(worker, target):
    document = read_document(target)
    await worker.start()
    metadata = await worker.request("init", document=document, operations=target.openapi.operations,
                                    base_url=target.base_url, timeout_seconds=target.timeout_seconds)
    return GeneratedMapping(document, metadata, target.openapi.recovery)


def read_retained_mapping(target, evidence):
    path = evidence.path / "private/generated-mapping.private.json"
    if not path.is_file() or path.is_symlink():
        raise LabError("Generated continuation requires its original private mapping")
    saved = load(path)
    if not isinstance(saved, dict) or set(saved) != {"document", "metadata"} \
            or digest(saved["document"]) != digest(read_document(target)):
        raise LabError("Generated OpenAPI source changed since the original trial")
    return GeneratedMapping(saved["document"], saved["metadata"], target.openapi.recovery)


def expose_mapping(mapping, target, evidence, *, behavioral):
    expose_generated(mapping.document, mapping.metadata, target, evidence, behavioral=behavioral)
    evidence.manifest["run_lock"]["api_mapping"] = mapping.record()
    evidence.manifest["run_lock"]["generated_api"]["transformations"].append(
        "Inspect schemas make an omitted top-level additionalProperties explicitly true to preserve "
        "JSON Schema semantics; other constraints must survive serialization unchanged. Tool returns "
        "a JSON envelope containing the native MCP result and HTTP status, not raw MCP transport messages")
    evidence.manifest["run_lock"]["generated_api"]["request_binding"] = (
        "Serialized request frozen before private consent; identical admitted bytes dispatched once")


class GeneratedAdapter(GeneratedRecovery, MappedAdapter):
    result_provenance = "native generated MCP result through host-mediated HTTP"

    def __init__(self, *args, worker):
        super().__init__(*args)
        self.worker, self.prepared = worker, None

    def arguments(self, name, arguments):
        validate_value(self.catalog[name]["schema"], arguments)
        return json.loads(json.dumps(arguments, allow_nan=False))

    async def approve(self, checkpoint, runtime, name, arguments):
        if name == "ask_user":
            return "approve_once"
        if self.prepared is not None:
            raise LabError("A generated request is already pending")
        reply = await self.worker.request("prepare", tool=name, arguments=arguments)
        if "request" not in reply and not reply.get("completed", {}).get("isError"):
            raise LabError("Generated tool returned success without an HTTP request", code=1, category="tool/API")
        association = self.mapping.metadata["mapping"][name]
        if "request" in reply:
            # Validate before asking for authority, without sending HTTP.
            admit_request(reply["request"], association, self.target)
        proposal = {"checkpoint": checkpoint, "name": name, "arguments": copy.deepcopy(arguments),
                    "reply": copy.deepcopy(reply), "association": copy.deepcopy(association)}
        self.prepared = {"proposal": proposal, "sha256": digest(proposal)}
        self.retain_prepared(proposal)
        self.evidence.event("generated_request_prepared", {
            "checkpoint": checkpoint, "tool": name, "proposal_sha256": self.prepared["sha256"],
            "http_prepared": "request" in reply},
            provenance="upstream serialization frozen; no target request sent")
        # A native serialization error needs no transaction grant.
        if "request" not in reply:
            return "approve_once"
        decision = await super().approve(checkpoint, runtime, name, arguments)
        if decision == "deny":
            result = await self.worker.request("abort", call_id=reply["call_id"])
            if result != {"call_id": reply["call_id"], "discarded_before_dispatch": True}:
                raise LabError("Generated request discard was not confirmed")
            self.evidence.event("generated_request_discarded", {"checkpoint": checkpoint},
                                provenance="denied proposal cancelled before host HTTP dispatch")
            self.prepared = None
        return decision

    async def dispatch(self, name, arguments, checkpoint):
        from .measurements import measure
        with measure(self.evidence, "tool", identity=checkpoint):
            return await self._dispatch(name, arguments, checkpoint)

    async def _dispatch(self, name, arguments, checkpoint):
        if self.prepared is None or digest(self.prepared["proposal"]) != self.prepared["sha256"]:
            raise LabError("Generated request changed after preparation")
        proposal = self.prepared["proposal"]
        if (name, arguments, checkpoint) != (proposal["name"], proposal["arguments"], proposal["checkpoint"]):
            raise LabError("Generated dispatch differs from its approved proposal")
        reply, association = proposal["reply"], proposal["association"]
        status = None
        if "request" in reply:
            request = admit_request(reply["request"], association, self.target)
            response = await request_case(self.client, request, self.target, self.evidence,
                {"id": checkpoint, **association, "tool": name, **reply["request"]})
            self.client.cookies.clear()
            status = response["status"]
            native_response = {"status": status, "body": response["body"],
                "headers": {key: value for key, value in response["headers"].items()
                            if key.lower() not in PRIVATE_VALUE_KEYS}}
            self.response_bytes(native_response)
            self.retain_response(name, arguments, checkpoint, native_response)
            if association["effect"] != "read" and status >= 500:
                raise LabError("Generated write outcome is uncertain after server error", code=1, category="tool/API")
            reply = await self.worker.request("finish", call_id=reply["call_id"], response=native_response)
        self.prepared = None
        return {"status": status, "result": reply["completed"]}


async def run_generated_scenario(scenario, target, model_profile, evidence, *, model=None,
                                 resume=False, resumable=False, existing_session=None):
    if resume and existing_session is None:
        from .mapped_scenario import continuation_contract
        from .scenario_session import ScenarioSession
        mapping = read_retained_mapping(target, evidence)
        contract = continuation_contract(target, model_profile, evidence, model, mapping)
        with ScenarioSession(evidence, scenario, contract, resume=True, storage="api") as session:
            return await run_generated_scenario(scenario, target, model_profile, evidence, model=model,
                                                existing_session=session)
    worker = ValidatorWorker("fastmcp", evidence, target.openapi.wall_seconds)
    try:
        if existing_session:
            worker.timeout = min(worker.timeout, existing_session.remaining_seconds())
            async with asyncio.timeout(existing_session.remaining_seconds()):
                mapping = await initialize_mapping(worker, target)
        else:
            mapping = await initialize_mapping(worker, target)
        # Exact Inspect schema support is checked before any private reset or model call.
        mapped_catalog(mapping, scenario)
        return await run_with_mapping(scenario, target, mapping, model_profile, evidence, model=model,
            adapter_factory=partial(GeneratedAdapter, worker=worker), expose_mapping=expose_mapping,
            resume=resume, resumable=resumable, existing_session=existing_session)
    finally:
        await worker.close()
