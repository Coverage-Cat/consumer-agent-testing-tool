"""Evidence-driven audit of the explicitly scoped local consumer release."""
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

from .release_scope import EXCLUDED, GATES, RELEASE_SCOPE
from .serialization import digest, file_digest, load
from .surface import inside

# The committed policy names the gates, the scope and the exclusions. Recorded results are generated: the
# recorders write them under RESULTS_DIR (git-ignored), and the release workflow attaches them to a release.
POLICY_LEDGER = "docs/release-gates.json"
RESULTS_DIR = "release"
RESULTS_LEDGER = f"{RESULTS_DIR}/release-gates.json"
EVIDENCE_DIR = f"{RESULTS_DIR}/evidence"


def results_ledger(root):
    """The ledger the recorders update, started from the committed policy when nothing is recorded yet."""
    root = Path(root)
    path = root / RESULTS_LEDGER
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((root / POLICY_LEDGER).read_text())
    return path


def default_ledger(root):
    """release-audit reads recorded results when present; the bare policy reports every gate unverified."""
    return RESULTS_LEDGER if (Path(root) / RESULTS_LEDGER).is_file() else POLICY_LEDGER


def source_digest(root):
    root = Path(root)
    paths = [path for directory in ("src/consumer_agent_testing_tool", "scripts", "tests", "containers")
             for path in (root / directory).rglob("*")
             if path.is_file() and path.suffix not in (".pyc", ".pyo")
             and "__pycache__" not in path.parts]
    paths += [root / name for name in ("pyproject.toml", "uv.lock", "tooling/package-lock.json",
        "tooling/payment-ui/package-lock.json", "tooling/api/package-lock.json", "tooling/build-python/uv.lock",
        "third_party/notices.zip", "third_party/supplemental-notices.json")]
    return digest({str(path.relative_to(root)): file_digest(path) for path in sorted(paths) if path.is_file()})


def audit(root, ledger, now=None):
    root = Path(root).resolve()
    entries = load(inside(root, ledger))["gates"]
    now = now or datetime.now(UTC)
    outcomes = {}
    for name, basis in GATES.items():
        entry = entries.get(name, {})
        if not isinstance(entry, dict):
            entry = {}
        state, detail = "unverified", "No qualifying evidence attached"
        if isinstance(entry.get("reason"), str):
            detail = entry["reason"]
        if entry.get("status") == "passed":
            try:
                artifact = inside(root, entry["artifact"])
                evidence = load(artifact)
                if not isinstance(evidence, dict):
                    raise TypeError("Release evidence must be an object")
                expires = datetime.fromisoformat(entry["expires_at"])
                checks = evidence.get("checks", [])
                valid = (entry["basis"] == basis and file_digest(artifact) == entry["sha256"]
                         and expires.tzinfo is not None and expires > now
                         and evidence.get("gate") == name and evidence.get("status") == "passed"
                         and isinstance(checks, list) and bool(checks)
                         and all(isinstance(check, dict) and check.get("status") == "passed" for check in checks)
                         and evidence.get("basis") == basis
                         and evidence.get("source_sha256") == source_digest(root))
                state, detail = ("passed", "Evidence hash, scope, checks and expiry verified") if valid \
                    else ("unverified", "Evidence is stale, mismatched, incomplete, or has failing checks")
            except (KeyError, TypeError, ValueError, OSError):
                detail = "Missing or invalid evidence"
        outcomes[name] = {"status": state, "basis_required": basis, "detail": detail}
    return {"release_scope": RELEASE_SCOPE, "excluded_capabilities": EXCLUDED,
            "release_qualified": all(row["status"] == "passed" for row in outcomes.values()),
            "gates": outcomes, "checked_at": now.isoformat(),
            "limits": "Local consumer testing and the verified distribution only. Excluded "
                      "capabilities are not passed gates; live provider behavior, real Muse, "
                      "cloud lifecycle and OS isolation are not qualified."}


def native_inventory_review(root, evidence):
    """Package notice checks cannot substitute for an explicit runtime review."""
    reference = evidence.get("inventory", {})
    if not isinstance(reference, dict) or not {"path", "sha256"} <= reference.keys():
        return False
    path = inside(root, reference["path"])
    if file_digest(path) != reference["sha256"]:
        return False
    inventory = load(path)
    observation = native_observation(root, evidence.get("observation"))
    if observation is None or not isinstance(inventory, dict):
        return False
    # A review can add newly discovered components, but cannot discard or
    # substitute payloads and provenance already known to the distribution.
    reviewed = inventory.get("components")
    if not isinstance(reviewed, dict):
        return False
    for identity, original in observation["components"].items():
        candidate = reviewed.get(identity)
        if not isinstance(original, dict) or not isinstance(candidate, dict):
            return False
        for field, value in original.items():
            if field not in ("dependency_coverage", "notice_coverage") \
                    and (field not in candidate or candidate[field] != value):
                return False
    required = {"core-python", "optional-api-python", "node-protocol",
                "node-browser", "node-api", "browser-runtime", "host-runtime", "worker-images"}
    if not isinstance(inventory, dict):
        return False
    groups = inventory.get("observed_groups")
    if inventory.get("schema_version") != 1 or inventory.get("status") != "reviewed" \
            or inventory.get("gaps") != [] or not isinstance(groups, list) \
            or any(not isinstance(group, str) for group in groups) or set(groups) != required \
            or len(groups) != len(required):
        return False
    components = inventory.get("components")
    if not isinstance(components, dict) or not components:
        return False
    scopes = set()
    for component in components.values():
        if not isinstance(component, dict) or not isinstance(component.get("scope"), str) \
                or component["scope"] not in required:
            return False
        scopes.add(component["scope"])
        if not isinstance(component.get("artifacts"), dict) or not component["artifacts"]:
            return False
        for coverage in ("dependency_coverage", "notice_coverage"):
            review = component.get(coverage, {})
            if not isinstance(review, dict) or review.get("status") != "reviewed" \
                    or not isinstance(review.get("evidence"), list) or not review["evidence"]:
                return False
            for item in review["evidence"]:
                if not isinstance(item, dict) or not {"path", "sha256"} <= item.keys() \
                        or file_digest(inside(root, item["path"])) != item["sha256"]:
                    return False
        for artifact in component["artifacts"].values():
            if not isinstance(artifact, dict) or type(artifact.get("bytes")) is not int \
                    or artifact["bytes"] <= 0 or artifact.get("kind") not in ("binary", "font") \
                    or not isinstance(artifact.get("sha256"), str) or len(artifact["sha256"]) != 64 \
                    or any(char not in "0123456789abcdef" for char in artifact["sha256"]):
                return False
    return {"browser-runtime", "host-runtime", "worker-images"} <= scopes


def native_observation(root, reference):
    """Bind reviews to the generated inventory sealed into the source fingerprint."""
    name = "third_party/native-runtime.json"
    if not isinstance(reference, dict) or reference.get("path") != name:
        return None
    try:
        path = inside(root, name)
        if path.stat().st_size > 8 * 1024 * 1024 or file_digest(path) != reference.get("sha256"):
            return None
        with zipfile.ZipFile(inside(root, "third_party/notices.zip")) as archive:
            member = "native-runtime.json"
            if archive.namelist().count(member) != 1 \
                    or archive.getinfo(member).file_size != path.stat().st_size:
                return None
            with archive.open(member) as stream:
                if stream.read(8 * 1024 * 1024 + 1) != path.read_bytes():
                    return None
        observation = load(path)
        if not isinstance(observation, dict) or observation.get("schema_version") != 1 \
                or observation.get("status") != "unverified" \
                or not isinstance(observation.get("components"), dict) or not observation["components"]:
            return None
        return observation
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error):
        return None
