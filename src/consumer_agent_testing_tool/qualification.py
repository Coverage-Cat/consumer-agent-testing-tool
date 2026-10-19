"""External qualification completeness; provenance remains operator-attested."""
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator

from .calibration import evaluate, timestamp
from .calibration_observations import DIMENSIONS
from .config import ModelProfile, Strict, exact_identifier, is_loopback, validate_url
from .errors import LabError
from .serialization import digest, file_digest, loads
from .surface import inside

MODEL_LIVE = {
    "native-tool-call", "nested-arguments", "call-result-correlation",
    "tool-result-continuation", "local-tool-execution", "model-identity",
}
MODEL_NEGATIVE = {
    "secret-isolation", "invalid-configuration", "unsupported-capability",
    "timeout-rate-limit", "fallback-disabled", "offline-rejection",
}
BROWSER_PROBES = {
    "restricted-dispatch", "direct-tcp", "direct-udp", "dns", "dns-rebinding",
    "redirects", "fetch", "websockets", "service-workers", "webrtc",
    "child-processes", "host-aliases", "privileged-destinations",
    "positive-controls", "bypass-controls", "destination-observations",
    "enforcing-layer", "secret-isolation", "runtime-identity",
}
# False means deterministic test-report evidence is also permitted. It does
# not waive the check or turn a test report into a live observation.
REQUIREMENTS = {
    "local-glimmer-offline": {
        **dict.fromkeys(MODEL_LIVE | {
            "artifact-runtime-identity", "reasoning-separation", "fixture-adapters",
            "representative-memory", "sustained-workflow", "offline-enforcement",
        }, True),
        **dict.fromkeys({"secret-isolation", "fallback-disabled"}, False),
    },
    "bedrock-bearer": {
        **dict.fromkeys(MODEL_LIVE | {"bearer-without-access-key-pair"}, True),
        **dict.fromkeys(MODEL_NEGATIVE, False),
    },
    "bedrock-iam": {
        **dict.fromkeys(MODEL_LIVE | {"credential-chain-session"}, True),
        **dict.fromkeys(MODEL_NEGATIVE, False),
    },
    "browser-isolation": {
        **dict.fromkeys(BROWSER_PROBES, False), "agent-directed-attempt": True,
    },
    "cloud-worker-lifecycle": dict.fromkeys({
        "provision", "ready", "seed", "run", "export-verified", "cancel",
        "destroy", "volume-cleanup", "failed-run-cleanup", "coordinator-loss",
        "independent-resource-expiry",
    }, True),
}


class Reference(Strict):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or str(path) != value or "\\" in value or ".." in path.parts:
            raise ValueError("Qualification references must be canonical project-relative paths")
        return value


class Versioned(Strict):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Qualification version must be an integer")
        return value


class Check(Strict):
    id: str = Field(min_length=1)
    status: Literal["passed"]
    basis: Literal["live", "test-report"]
    artifacts: list[Reference] = Field(min_length=1)


class Scope(Strict):
    id: str = Field(min_length=1)
    configuration: Reference
    run_lock: Reference
    checks: list[Check] = Field(min_length=1)


class LiveQualification(Versioned):
    observed_at: str
    scopes: list[Scope] = Field(min_length=1)


class MuseQualification(Versioned):
    observed_at: str
    cohort: Reference
    profile: Reference
    surface_revision: str = Field(min_length=1)
    study_plan: Reference
    comparison: Reference


class References:
    def __init__(self, root):
        self.root = Path(root)
        self.seen = {}

    def check(self, reference):
        if reference.path in self.seen and self.seen[reference.path] != reference.sha256:
            raise ValueError("One qualification path cannot identify different evidence versions")
        path = inside(self.root, reference.path)
        if not path.is_file() or not path.stat().st_size or file_digest(path) != reference.sha256:
            raise ValueError("Qualification evidence is missing, empty or changed")
        self.seen[reference.path] = reference.sha256
        return path

    def document(self, reference):
        path = self.check(reference)
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError("Qualification document is oversized or changed")
        document = loads(raw.decode("utf-8"), json_only=True)
        if not isinstance(document, dict) or not document:
            raise ValueError("Qualification documents must be nonempty JSON objects")
        return document

    def recheck(self):
        for path, sha256 in self.seen.items():
            self.check(Reference(path=path, sha256=sha256))


def text(mapping, key):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Qualification configuration is incomplete")
    return value


def configuration_valid(gate, configuration):
    """Validate declared scope without contacting providers or loading credentials."""
    text(configuration, "platform")
    backend = configuration["execution"]["backend"]
    if gate == "cloud-worker-lifecycle":
        cloud = configuration["cloud"]
        if backend != "worker" or cloud["provider"] != "maritime":
            raise ValueError("Cloud qualification requires an implemented named provider")
        exact_identifier(text(cloud, "template_id"))
        text(cloud, "namespace")
        endpoint = text(cloud, "api_url")
        validate_url(endpoint)
        if is_loopback(endpoint):
            raise ValueError("A loopback cloud fixture is not live provider evidence")
        return
    model = ModelProfile.model_validate(configuration["model"])
    if backend != "local":
        raise ValueError("These qualification scopes require local execution")
    if model.provider != "local-glimmer" and any(
            url and is_loopback(url) for url in (model.base_url, model.endpoint)):
        raise ValueError("A loopback inference fixture is not hosted model evidence")
    if gate == "local-glimmer-offline":
        if model.provider != "local-glimmer" or configuration.get("offline") is not True:
            raise ValueError("Offline qualification requires the local Glimmer configuration")
        if not model.runtime_build or not model.runtime_pid or not model.memory_budget_mb:
            raise ValueError("Local runtime identity and memory budget are required")
        for key in ("weights", "runtime"):
            artifact = model.artifacts[key]
            text(artifact, "path")
            if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
                raise ValueError("Local model artifact hashes are required")
    elif gate.startswith("bedrock-"):
        mode = "api_key" if gate == "bedrock-bearer" else "aws_credentials"
        if model.provider != "bedrock" or model.auth.mode != mode:
            raise ValueError("Model provider or authentication does not match this gate")
    elif gate == "browser-isolation":
        runtime = configuration["browser_runtime"]
        for key in ("kernel", "architecture", "chromium", "boundary"):
            text(runtime, key)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", text(runtime, "image")):
            raise ValueError("Browser qualification requires its pinned worker image")


def live_valid(root, gate, document, now, expires):
    qualification = LiveQualification.model_validate(document)
    observed = timestamp(qualification.observed_at)
    if observed > now or observed >= expires:
        raise ValueError("Invalid qualification observation date")
    references = References(root)
    scopes = set()
    for scope in qualification.scopes:
        text({"id": scope.id}, "id")
        if scope.id in scopes:
            raise ValueError("Duplicate qualification scope")
        scopes.add(scope.id)
        configuration = references.document(scope.configuration)
        configuration_valid(gate, configuration)
        lock = references.document(scope.run_lock)
        if not all(key in lock for key in ("harness", "platform", "model", "dependencies", "source_hashes")) \
                or not isinstance(lock["dependencies"], list) or not lock["dependencies"] \
                or not isinstance(lock["source_hashes"], dict) or not lock["source_hashes"] \
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in lock["source_hashes"].values()) \
                or not isinstance(lock.get("dependency_lock_sha256"), str) \
                or not re.fullmatch(r"[0-9a-f]{64}", lock["dependency_lock_sha256"]) \
                or lock["platform"] != configuration["platform"] \
                or lock["execution"]["backend"] != configuration["execution"]["backend"]:
            raise ValueError("Qualification requires the original run lock")
        text(lock, "harness")
        for dependency in lock["dependencies"]:
            text(dependency, "name")
            text(dependency, "version")
        if gate == "cloud-worker-lifecycle":
            if lock["execution"].get("provider") != configuration["cloud"]["provider"]:
                raise ValueError("Cloud provider differs from the recorded execution")
        else:
            declared = ModelProfile.model_validate(configuration["model"]).model_dump(mode="json")
            recorded = ModelProfile.model_validate(lock["model"]).model_dump(mode="json")
            if digest(declared) != digest(recorded):
                raise ValueError("Qualification configuration differs from the recorded model")
        checks = {check.id: check for check in scope.checks}
        if len(checks) != len(scope.checks) or set(checks) != set(REQUIREMENTS[gate]):
            raise ValueError("Missing, duplicate or unknown qualification checks")
        for name, live_required in REQUIREMENTS[gate].items():
            check = checks[name]
            if live_required and check.basis != "live":
                raise ValueError("Deterministic checks cannot replace this live observation")
            for artifact in check.artifacts:
                references.check(artifact)
    references.recheck()


def muse_valid(root, document, now, expires):
    qualification = MuseQualification.model_validate(document)
    references = References(root)
    cohort = references.document(qualification.cohort)
    profile = references.document(qualification.profile)
    references.check(qualification.study_plan)
    saved = references.document(qualification.comparison)
    if cohort.get("schema_version") != 2 or cohort.get("evidence_kind") != "muse-observed":
        raise ValueError("Release calibration requires version 2 Muse observations")
    observed = timestamp(qualification.observed_at)
    if observed != timestamp(cohort["observed_at"]) or observed > now or observed >= expires:
        raise ValueError("Qualification and cohort dates do not match")
    actual = evaluate(root, qualification.cohort.path, profile_sha256=digest(profile),
                      surface_revision=qualification.surface_revision, now=now)
    if digest(saved) != digest(actual) or not actual["usable_for_comparison"]:
        raise ValueError("Saved comparison is stale, substituted or ineligible")
    for split in ("calibration", "held-out"):
        group = actual["groups"][split]
        if not group["paired_trials"] or not all(
                group["dimensions"][name]["compared_pairs"] for name in DIMENSIONS):
            raise ValueError("Both scenario splits need all required observable dimensions")
    references.recheck()


def external_qualification(root, gate, evidence, now, expires):
    """Return a safe diagnostic; never echo potentially sensitive evidence contents."""
    kind = "muse-observed" if gate == "muse-calibration" else "live"
    if evidence.get("evidence_kind") != kind:
        return False, "External qualification requires explicit nonsynthetic evidence"
    try:
        if gate == "muse-calibration":
            muse_valid(root, evidence.get("qualification"), now, expires)
        else:
            live_valid(root, gate, evidence.get("qualification"), now, expires)
    except (LabError, ValueError, TypeError, KeyError, AttributeError, OSError):
        return False, "External qualification scope, required observations or artifact bindings are incomplete or invalid"
    return True, "External evidence scope, required observations, hashes and dates verified; provenance is operator-attested"
