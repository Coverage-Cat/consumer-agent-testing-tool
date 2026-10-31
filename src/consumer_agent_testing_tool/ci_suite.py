"""Offline CI suite contracts and deterministic plans. Planning has no service or provider boundary."""
import copy
import itertools
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .connector_scenario import (
    IDENTIFIER,
    LoadedScenario,
    Strict,
    check_url,
    is_loopback,
    parse_scenario,
    published_digest,
    substitute,
)
from .emulation import SHARED_EXTENSIONS, EmulationTarget, ProjectionMode
from .errors import LabError
from .evidence import Redactor
from .serialization import digest, load

MAX_CASES = 512
MAX_INPUT_BYTES = 4 * 1024 * 1024
PLAN_PORT = "49152"
Identifier = Annotated[str, Field(pattern=IDENTIFIER)]
CheckStatus = Literal["failed", "blocked", "unsupported", "observation-gap", "not-evaluated"]


class PlanError(LabError):
    """A public diagnostic with no exception input, source snippet, credential, or private value."""

    def __init__(self, identifier, location, message):
        self.diagnostic = {"code": identifier, "location": location, "message": message}
        super().__init__(f"{location}: {message}")


class ServiceSpec(Strict):
    command: list[str] | None = Field(default=None, min_length=1, max_length=100)
    ready_url: str | None = None
    startup_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    # These are declarations by the service author, not an inference from a loopback address.
    state: Literal["process-local", "case-isolated", "shared"]

    @model_validator(mode="after")
    def ownership(self):
        if self.command is None:
            if self.state != "shared" or self.ready_url is not None:
                raise ValueError("An existing service has state: shared and no ready_url")
        elif not self.ready_url or not self.command[0] or any("\x00" in part for part in self.command):
            raise ValueError("An owned service needs a command and ready_url")
        if self.ready_url:
            check_url(self.ready_url.replace("{port}", PLAN_PORT).replace("{case}", "ci-case"),
                      "service.ready_url")
        return self


class Matrix(Strict):
    emulation_target: list[EmulationTarget] = Field(min_length=1, max_length=2)
    projection: list[ProjectionMode] = Field(default_factory=lambda: ["native"], min_length=1, max_length=3)
    exposure: list[Literal["in-prompt", "search-only"]] = Field(
        default_factory=lambda: ["in-prompt"], min_length=1, max_length=2)
    client_profile: list[Literal["generic-sdk", "muse-template-unofficial-2026-10"]] = Field(
        default_factory=lambda: ["generic-sdk"], min_length=1, max_length=2)
    output_schema: list[Literal["lenient", "strict"]] = Field(
        default_factory=lambda: ["strict"], min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique(self):
        if any(len(set(values)) != len(values) for values in self.model_dump().values()):
            raise ValueError("Matrix values must be distinct")
        return self


class Treatment(Strict):
    emulation_target: EmulationTarget
    projection: ProjectionMode
    exposure: Literal["in-prompt", "search-only"]
    client_profile: Literal["generic-sdk", "muse-template-unofficial-2026-10"]
    output_schema: Literal["lenient", "strict"]


class Expectation(Strict):
    exit_code: Literal[0, 1, 3] = 0
    checks: dict[str, CheckStatus] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def specific(self):
        if bool(self.checks) != (self.exit_code != 0):
            raise ValueError("A negative expectation needs an exit code and exact nonpassing check IDs")
        if any(not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", key) for key in self.checks):
            raise ValueError("Expected checks name exact check IDs; patterns are not supported")
        return self


class CaseSpec(Strict):
    id: Identifier
    kind: Literal["connector", "readiness"] = "connector"
    scenario: str
    script: str | None = None
    manifest: str | None = None
    connector: str | None = None
    service: Identifier
    matrix: Matrix
    tags: list[Identifier] = Field(default_factory=list, max_length=20)
    timeout_seconds: float = Field(default=120, gt=0, le=7200, allow_inf_nan=False)
    network: Literal["loopback", "remote"] = "loopback"
    expect: Expectation = Field(default_factory=Expectation)

    @model_validator(mode="after")
    def lane(self):
        if self.kind == "connector" and (not self.script or self.manifest or self.connector):
            raise ValueError("Connector cases need a script; manifest and connector select readiness inputs")
        if self.kind == "readiness" and (self.script or not self.manifest):
            raise ValueError("Readiness cases need a manifest and cannot have a script")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("Tags must be distinct")
        return self


class SuiteSpec(Strict):
    version: Literal[1]
    kind: Literal["ci-suite"]
    id: Identifier
    services: dict[Identifier, ServiceSpec] = Field(min_length=1, max_length=64)
    cases: list[CaseSpec] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def references(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        if any(case.service not in self.services for case in self.cases):
            raise ValueError("Each case must name a declared service")
        return self


class PlanCase(Strict):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,299}$")
    source_id: Identifier
    kind: Literal["connector", "readiness"]
    definition_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    treatment: Treatment
    service: Identifier
    state: Literal["process-local", "case-isolated", "shared"]
    network: Literal["loopback", "remote"]
    tags: list[str]
    timeout_seconds: float
    expect: Expectation


class SuitePlan(Strict):
    schema_version: Literal[1] = 1
    kind: Literal["catt-ci-plan"] = "catt-ci-plan"
    suite_id: Identifier
    suite_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_label: Literal["scripted surrogate compatibility; no model inference"] = (
        "scripted surrogate compatibility; no model inference")
    cases: list[PlanCase] = Field(min_length=1, max_length=MAX_CASES)
    selected: list[str] = Field(min_length=1, max_length=MAX_CASES)
    scheduled: list[str] = Field(min_length=1, max_length=MAX_CASES)
    shard_index: int = Field(ge=1, le=MAX_CASES)
    shard_count: int = Field(ge=1, le=MAX_CASES)


@dataclass
class PreparedCase:
    public: PlanCase
    spec: CaseSpec
    service: ServiceSpec
    scenario_document: dict
    script: list | None
    manifest: dict | None
    loaded: LoadedScenario
    readiness_connector: str | None


@dataclass
class PreparedSuite:
    plan: SuitePlan
    cases: dict[str, PreparedCase]
    root: Path
    redactor: Redactor


def read_document(path, *, location):
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise PlanError("input.size", location, "Input exceeds the 4 MiB limit.")
        return load(path)
    except PlanError:
        raise
    except (LabError, OSError, UnicodeError):
        # YAML exceptions quote the source, and even a duplicate key can be a private value.
        raise PlanError("input.document", location, "Cannot read valid JSON/YAML; inspect the input locally.") from None


def input_path(root, base, value, location):
    path = (base / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise PlanError("input.path", location, "Input must name a file inside the working directory.")
    return path


def validate_model(model, document, location):
    try:
        return model.model_validate(document)
    except ValidationError as error:
        # Validation messages from user-defined validators can contain their original input.
        types = ", ".join(sorted({item["type"] for item in error.errors(
            include_input=False, include_context=False, include_url=False)}))
        raise PlanError("input.schema", location, f"Invalid {model.__name__} ({types}); check its JSON Schema.") from None


def _urls(document):
    if isinstance(document, dict):
        for item in document.values():
            yield from _urls(item)
    elif isinstance(document, list):
        for item in document:
            yield from _urls(item)
    elif isinstance(document, str) and document.startswith(("http://", "https://")):
        yield document


def _validate_script_refs(value, index):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            pointer = value["$ref"]
            match = re.fullmatch(r"/(results|responses)/([0-9]+)(/.*)?", pointer) if isinstance(pointer, str) else None
            if not match or int(match[2]) >= index or re.search(r"~(?![01])", pointer):
                raise PlanError("script.reference", f"script.steps.{index}",
                                "A reference must point into an earlier /results/N or /responses/N value.")
        else:
            for item in value.values():
                _validate_script_refs(item, index)
    elif isinstance(value, list):
        for item in value:
            _validate_script_refs(item, index)


def _prepared(case, treatment, service, *, root, base, redactor, location):
    from .connector_run import ScriptedDriver, effective_scenario, profile_named
    from .surrogate_readiness import ReadinessManifest

    path = input_path(root, base, case.scenario, location + ".scenario")
    document = read_document(path, location=location + ".scenario")
    if not isinstance(document, dict):
        raise PlanError("scenario.mapping", location, "A scenario must be an object.")
    redactor.add(document.get("private") or {})
    extensions = document.get("extensions") or {}
    if not isinstance(extensions, dict) or set(extensions) - SHARED_EXTENSIONS:
        raise PlanError("scenario.inference", location,
                        "CI accepts only shipped shared extensions; user_simulator and custom factories are excluded.")
    try:
        loaded = parse_scenario({**document, "emulation_target": document.get("emulation_target", "muse")},
                                base=path.parent, path=path, variables={"PORT": PLAN_PORT})
        effective = effective_scenario(loaded.scenario, **treatment.model_dump())
        loaded = replace(loaded, scenario=effective)
    except (LabError, ValueError, TypeError):
        raise PlanError("scenario.invalid", location,
                        "The scenario or treatment is invalid; validate the scenario and target capabilities locally.") from None
    if case.kind == "connector" and not any(row.required for row in effective.checks):
        raise PlanError("scenario.checks", location, "A CI connector case needs a required independent outcome check.")
    script = manifest = readiness_connector = None
    if case.script:
        script = read_document(input_path(root, base, case.script, location + ".script"), location=location + ".script")
        try:
            ScriptedDriver(substitute(script, {"PORT": PLAN_PORT}))
        except LabError:
            raise PlanError("script.invalid", location, "Invalid scripted steps; review the calls file locally.") from None
        for index, step in enumerate(script):
            _validate_script_refs(step, index)
            if "connector" in step and step["connector"] not in {item.name for item in effective.connectors}:
                raise PlanError("script.connector", location, "A portable call names an undeclared connector.")
    if case.kind == "readiness":
        choices = [item for item in effective.connectors if case.connector in (None, item.name)]
        if len(choices) != 1:
            raise PlanError("readiness.connector", location,
                            "Readiness needs one connector; set connector when the scenario declares several.")
        readiness_connector = choices[0].name
        manifest = read_document(input_path(root, base, case.manifest, location + ".manifest"),
                                 location=location + ".manifest")
        checked = validate_model(ReadinessManifest, manifest, location + ".manifest")
        if not checked.allow:
            raise PlanError("readiness.allow", location, "CI readiness needs a reviewed manifest.allow list.")
        if checked.protocol_revision not in (None, *profile_named(treatment.client_profile).protocol_versions):
            raise PlanError("readiness.protocol", location, "The selected client cannot offer the manifest's protocol.")
        if treatment.exposure != "in-prompt" or treatment.output_schema != "strict":
            raise PlanError("readiness.treatment", location,
                            "Readiness has no exposure or result-schema treatment; leave those matrix defaults.")
    if case.network == "loopback":
        urls = [*_urls(effective.model_dump(mode="json")), *_urls(substitute(script, {"PORT": PLAN_PORT}))]
        if service.ready_url:
            urls.append(service.ready_url.replace("{port}", PLAN_PORT).replace("{case}", "ci-case"))
        if any(not is_loopback(url) for url in urls):
            raise PlanError("network.remote", location,
                            "A declared URL is remote; use a test endpoint or explicitly set network: remote.")
    identifier = "--".join([case.id, *treatment.model_dump().values()])
    # Private values are masked before hashing; public evidence must not allow offline guesses of them.
    definition = {
        "case": case.model_dump(exclude={"matrix", "scenario", "script", "manifest"}),
        "treatment": treatment.model_dump(), "scenario": published_digest(effective),
        "script": redactor.clean(script), "manifest": redactor.clean(manifest),
        "docs": redactor.clean(loaded.docs), "service": redactor.clean(service.model_dump()),
    }
    public = PlanCase(id=identifier, source_id=case.id, kind=case.kind, definition_sha256=digest(definition),
                      treatment=treatment, service=case.service, state=service.state, network=case.network,
                      tags=sorted(case.tags), timeout_seconds=case.timeout_seconds, expect=case.expect)
    return PreparedCase(public, case, service, copy.deepcopy(document), copy.deepcopy(script), manifest, loaded,
                        readiness_connector)


def parse_shard(text):
    if not isinstance(text, str) or not re.fullmatch(r"[1-9][0-9]*/[1-9][0-9]*", text):
        raise PlanError("selection.shard", "shard", "Use a one-based shard I/N, for example 1/3.")
    index, count = map(int, text.split("/"))
    if not 1 <= index <= count <= MAX_CASES:
        raise PlanError("selection.shard", "shard", f"Require 1 <= I <= N <= {MAX_CASES}.")
    return index, count


def prepare_suite(path, *, root=None, cases=(), tags=(), shard="1/1", jobs=1):
    root = Path(root or Path.cwd()).resolve()
    path = input_path(root, root, str(path), "suite")
    spec = validate_model(SuiteSpec, read_document(path, location="suite"), "suite")
    if type(jobs) is not int or not 1 <= jobs <= 64:
        raise PlanError("execution.jobs", "jobs", "Use between 1 and 64 workers.")
    redactor = Redactor.environment()
    redactor.numbers = True
    prepared = {}
    for index, case in enumerate(spec.cases):
        matrix = case.matrix.model_dump()
        for values in itertools.product(*matrix.values()):
            if len(prepared) >= MAX_CASES:
                raise PlanError("matrix.limit", "cases", f"A suite expands to at most {MAX_CASES} cases.")
            treatment = Treatment.model_validate(dict(zip(matrix, values, strict=True)))
            item = _prepared(case, treatment, spec.services[case.service], root=root, base=path.parent,
                             redactor=redactor, location=f"cases.{index}")
            if item.public.id in prepared:
                raise PlanError("matrix.duplicate", "cases", "Expanded case IDs must be unique.")
            prepared[item.public.id] = item
    ordered = [prepared[key].public for key in sorted(prepared)]
    for name in cases:
        if not any(name in (item.id, item.source_id) for item in ordered):
            raise PlanError("selection.case", "case", "A requested case ID is absent from this suite.")
    for tag in tags:
        if not any(tag in item.tags for item in ordered):
            raise PlanError("selection.tag", "tag", "A requested tag is absent from this suite.")
    selected = [item.id for item in ordered if (not cases or item.id in cases or item.source_id in cases)
                and (not tags or set(tags).intersection(item.tags))]
    if not selected:
        raise PlanError("selection.empty", "selection", "No cases match the complete selection.")
    index, count = parse_shard(shard)
    if count > len(selected):
        raise PlanError("selection.empty-shard", "shard", "Shard count exceeds selected case count.")
    if (jobs > 1 or count > 1) and any(prepared[key].service.state == "shared" for key in selected):
        raise PlanError("service.shared", "selection",
                        "Shared state requires one worker and one shard; declare and provide case isolation to parallelize.")
    identity = {item.id: item.definition_sha256 for item in ordered}
    plan = SuitePlan(suite_id=spec.id, suite_sha256=digest(identity),
                     scope_sha256=digest({key: identity[key] for key in selected}), cases=ordered,
                     selected=selected, scheduled=selected[index - 1::count], shard_index=index, shard_count=count)
    if redactor.clean(plan.model_dump()) != plan.model_dump():
        raise PlanError("privacy.identity", "suite",
                        "A public case identifier or plan field contains a private value; change the identifier.")
    return PreparedSuite(plan, prepared, root, redactor)
