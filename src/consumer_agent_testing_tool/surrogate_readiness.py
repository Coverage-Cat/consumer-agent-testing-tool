"""Catalog-only readiness against CATT's shared surrogate contract, never vendor admission rules."""
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from .emulation import policy_record, surrogate_contract, validate_instinct
from .errors import LabError, normalize_error
from .evidence import Redactor
from .permissions import classify_tool
from .projection import ConnectorView, Projection, credential_shapes
from .serialization import digest, load

SUITE = "surrogate"
LABEL = "surrogate contract, not hosted-assistant compatibility"
BASIS = "assumed: CATT connector-surrogate-v1"


class ReadinessManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    kind: Literal["surrogate-readiness"] = "surrogate-readiness"
    allow: list[str] | None = Field(None, min_length=1, max_length=1000)
    classes: dict[str, Literal["read", "write", "sensitive-write"]] = Field(default_factory=dict)
    host_arguments: dict[str, list[str]] = Field(default_factory=dict)


def check(identifier, status, detail, *, required=True):
    return {"id": identifier, "layer": "surrogate", "status": status, "required": required,
            "detail": detail, "basis": BASIS}


def catalog_checks(tools, manifest, *, target, projection, secrets=(), instructions=None):
    """Check the actual projection as well as catalog syntax; never retrieve schema references."""
    checks, classified = [], []
    named = isinstance(tools, list) and all(
        isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]
        and not any(ord(char) < 32 or ord(char) == 127 or char in "\u2028\u2029"
                    for char in tool["name"]) for tool in tools)
    unique = named and len({tool["name"] for tool in tools}) == len(tools)
    checks.append(check("catalog.names", "passed" if unique and tools else "failed",
                        "Tools need unique, nonempty names without control characters, and a nonempty catalog."))
    # Scan before recording catalog evidence, so newly found credentials are also registered by the caller.
    text = json.dumps([tools, instructions], ensure_ascii=False)
    leaked = any(secret and secret in text for secret in secrets) or credential_shapes([tools, instructions])
    checks.append(check("catalog.credentials", "failed" if leaked else "passed",
                        "A credential appears in catalog text." if leaked else
                        "No linked credential or recognized bearer credential appears in catalog text."))
    if not named:
        return checks, classified
    names = {tool["name"] for tool in tools}
    manifest = manifest or ReadinessManifest()
    declared = set(manifest.classes) | set(manifest.host_arguments) | set(manifest.allow or [])
    unknown = sorted(declared - names)
    checks.append(check("catalog.allowlist", "failed" if unknown else "passed" if manifest.allow else "not-evaluated",
                        f"Manifest names tools absent from the catalog: {', '.join(unknown)}" if unknown else
                        "The reviewed allowlist exists in the catalog." if manifest.allow else
                        "Supply manifest.allow to check a reviewed allowlist.", required=bool(unknown or manifest.allow)))
    selected = [tool for tool in tools if manifest.allow is None or tool["name"] in manifest.allow]
    invalid = []
    for tool in selected:
        schema = tool.get("inputSchema")
        try:
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("inputSchema must describe an object")
            Draft202012Validator.check_schema(schema)
            if tool.get("outputSchema") is not None:
                Draft202012Validator.check_schema(tool["outputSchema"])
        except Exception:  # noqa: BLE001 — untrusted schema details may contain a credential
            invalid.append(tool["name"])
            continue
        classified.append(classify_tool(
            tool, manifest.classes.get(tool["name"]), emulation_target=target).model_dump(mode="json"))
    checks.append(check("catalog.schemas", "failed" if invalid else "passed",
                        f"Invalid JSON Schema for: {', '.join(invalid)}" if invalid else
                        "Selected tools have valid input/output schema syntax; references were not retrieved."))
    if unique and not invalid and not unknown:
        try:
            surface = Projection(projection, "in-prompt", [ConnectorView(
                "service", selected, host_owned=manifest.host_arguments)], emulation_target=target)
            surface.specs()
        except LabError as error:
            checks.append(check("catalog.projection", "unsupported", str(error)))
        else:
            checks.append(check("catalog.projection", "passed",
                                f"The selected {projection} projection can expose {len(selected)} tools."))
    checks.append(check("catalog.classifications", "passed" if set(manifest.classes) >= {
        tool["name"] for tool in selected} else "not-evaluated",
        "Review the recorded classes and declare each selected tool's class in manifest.classes.",
        required=False))
    return checks, classified


async def run_check(endpoint, *, auth, manifest_path, output, profile, emulation_target="muse",
                    projection="native", services=None, store=None, sealed=None):
    from .connector_run import default_services
    from .sealed_events import metadata, record
    from .sealed_report import finalize

    contract = surrogate_contract(emulation_target)
    if projection not in ("native", "cli", "muse-cli"):
        raise LabError(f"Unknown projection {projection!r}")
    if emulation_target == "instinct":
        try:
            validate_instinct(projection, [profile.name], {})
        except ValueError as error:
            raise LabError(str(error)) from None
    services = services or default_services()
    interface = {"name": f"{emulation_target}-surrogate-readiness-v1", "surface": "mcp-tools-list", "label": LABEL}
    run_id, clock_id = f"surrogate-{uuid.uuid4().hex}", uuid.uuid4().hex
    path = Path(output) / run_id
    path.mkdir(parents=True, mode=0o700)
    redactor = Redactor.environment()
    redactor.protect(run_id, SUITE, emulation_target, projection, *interface.values())
    sequence = 0

    def event(kind, payload):
        nonlocal sequence
        sequence += 1
        row = record(path, redactor, sequence, kind, payload, scenario_id=SUITE, profile=interface,
                     clock_id=clock_id, driver="static", run_id=run_id)
        with (path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    stage, code, detail, category = "manifest", 0, None, None
    checks, classified, server, linked, manifest = [], [], {}, {}, None
    started = datetime.now(UTC).isoformat()
    try:
        if manifest_path:
            from pydantic import ValidationError

            from .connector_scenario import validation_message
            try:
                manifest = ReadinessManifest.model_validate(load(manifest_path))
            except ValidationError as error:
                raise LabError(f"Invalid surrogate readiness manifest: {validation_message(error)}") from None
        event("surrogate_contract", contract)
        stage = "link"
        store = Path(store) if store is not None else Path.cwd() / ".cache" / "connector-links"
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential = await services.link(auth, endpoint, store=store, http_policy=profile, redactor=redactor)
        redactor.add(credential.secrets)
        linked = credential.describe()
        stage = "connect"
        client = services.make_client(profile, endpoint, credential, allowed_tools=[])
        try:
            await client.connect()
            stage = "tools/list"
            tools = await client.list_tools()
        finally:
            try:
                await client.close()
            except Exception as error:  # noqa: BLE001 — close errors do not erase catalog observations
                event("cleanup_failure", {"detail": str(error)})
        stage = "catalog"
        instructions = getattr(client, "instructions", None)
        checks, classified = catalog_checks(tools, manifest, target=emulation_target, projection=projection,
                                             secrets=credential.secrets, instructions=instructions)
        redactor.add(credential_shapes([tools, instructions]))
        server = {"server_info": client.server_info, "protocol_version": client.protocol_version,
                  "tools_sha256": digest(redactor.clean_evidence(tools)),
                  "tool_count": len(tools) if isinstance(tools, list) else None,
                  "digest_basis": "sha256 of redacted catalog evidence"}
        event("catalog", {**server, "tools": tools, "instructions": instructions})
        event("readiness_checks", {"checks": checks, "classifications": classified})
        code = 1 if any(row["status"] == "failed" for row in checks) else \
            3 if any(row["required"] and row["status"] == "unsupported" for row in checks) else 0
        stage = "catalog" if code else None
        category = "tool/API" if code == 1 else "unsupported" if code == 3 else None
        detail = next((row["detail"] for row in checks if row["status"] in ("failed", "unsupported")), None)
    except Exception as error:  # noqa: BLE001 — setup failures still produce sealed evidence
        failure = normalize_error(error)
        code, category, detail = 3 if failure.code == 3 else 2, failure.category, str(failure)
        event("setup_error", {"stage": stage, "class": category, "detail": detail})
    status = "completed" if code == 0 else "blocked" if code == 3 else "failed"
    event("run_finished", {"status": status, "exit_code": code})
    report = {
        **metadata(path, scenario_id=SUITE, profile=interface, driver="static", run_id=run_id, locations={
            "inference": "none", "browser": "none", "target": redactor.text(endpoint), "telemetry": "disabled",
            "egress": "the MCP endpoint and its authorization server only"}),
        "schema_version": 1, "kind": "surrogate-readiness-check", "lane": "readiness", "suite": SUITE,
        "evidence_label": LABEL, "fidelity": "catalog-only review against CATT's surrogate contract",
        "emulation_target": emulation_target, "projection": projection, "status": status, "exit_code": code,
        "execution_status": "failed" if code == 2 else "blocked" if code == 3 else "completed",
        "failure_stage": stage, "failure_class": category, "detail": detail, "started_at": started,
        "compatibility_pass": False, "server": server, "checks": checks, "classifications": classified,
        "target": {"endpoint": endpoint, "client_profile": profile.name, "auth": linked},
        "run_lock": {"emulation_target": emulation_target, "projection": projection,
                     "surrogate_contract": contract, "tools_sha256": server.get("tools_sha256"),
                     "manifest_sha256": digest(manifest.model_dump(mode="json")) if manifest else None,
                     **({"emulation_policy": policy_record(emulation_target)} if emulation_target == "instinct" else {})},
        "evidence_limits": [
            "No model or service tool was called. Run scripted connector scenarios to check behavior.",
            "Schema syntax and projection checks do not prove runtime instance validation or resolve references.",
            "Tool classes are declarations or heuristics. Review side effects and approval rules in a scenario.",
            "This is CATT's shared contract, not Instinct or Muse admission rules or real-host calibration.",
        ],
    }
    result = finalize(path, redactor.clean_evidence(report))
    if result["reporting_status"] != "completed":
        code = 2
    print(path)
    print(f"{emulation_target} surrogate readiness: {result['status']}; {LABEL}. Evidence: {path}", file=sys.stderr)
    if sealed:
        sealed(path, code)
    return code
