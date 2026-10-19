"""Offline playback, strict fixture request matching, and milestone-based comparisons."""
import json
import re
from pathlib import Path

from .errors import LabError
from .evidence import normalized_key
from .serialization import digest, file_digest, implementation_lock, load


def evidence_path(root, name):
    """Resolve only relative evidence names without following filesystem links."""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise LabError("Invalid evidence path")
    candidate = root
    if candidate.is_symlink():
        raise LabError("Evidence links are not supported")
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise LabError("Evidence links are not supported")
    return candidate


def public_evidence_files(path):
    names = {name for name in ("result.json", "report.html", "events.jsonl")
             if evidence_path(path, name).is_file()}
    result = load(evidence_path(path, "result.json"))
    from .sealed_events import public_files
    events_path = evidence_path(path, "events.jsonl")
    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.is_file() else []
    names.update(public_files(path, result, events))
    agents = result.get("agent_runs", [])
    if not isinstance(agents, list) or any(not isinstance(name, str) for name in agents):
        raise LabError("Invalid agent evidence references")
    if result.get("agent_evidence"):
        agents = [*agents, result["agent_evidence"]]
    for name in agents:
        if not isinstance(name, str) or len(Path(name).parts) != 2 or Path(name).parts[0] != "agent":
            raise LabError("Invalid agent evidence reference")
        agent = evidence_path(path, name)
        manifest = agent / "manifest.json"
        if not agent.is_dir() or not evidence_path(path, str(manifest.relative_to(path))).is_file():
            raise LabError("Linked agent evidence is missing")
        # Only the redacted, public evidence layout is included. Private checkpoints
        # and other host files are never traversed.
        for filename in ("manifest.json", "events.jsonl", "report.html"):
            relative = str((agent / filename).relative_to(path))
            if evidence_path(path, relative).is_file():
                names.add(relative)
        for directory in ("artifacts", "inspect"):
            location = evidence_path(path, str((agent / directory).relative_to(path)))
            if not location.exists():
                continue
            for item in location.iterdir():
                relative = str(item.relative_to(path))
                if not evidence_path(path, relative).is_file():
                    raise LabError("Unexpected directory in public agent evidence")
                names.add(relative)
    return names


def seal(path):
    from .sealed_report import atomic_text
    path = Path(path)
    record = {name: file_digest(evidence_path(path, name))
              for name in sorted(public_evidence_files(path))}
    atomic_text(evidence_path(path, "evidence.sha256.json"), json.dumps(record, indent=2))


def playback(path):
    path = Path(path)
    manifest = evidence_path(path, "evidence.sha256.json")
    if not manifest.exists():
        raise LabError("Run has no evidence seal; historical unsealed runs cannot be verified playback")
    hashes = load(manifest)
    if not {"result.json", "events.jsonl"}.issubset(hashes):
        raise LabError("Evidence seal is incomplete")
    for name, expected in hashes.items():
        item = evidence_path(path, name)
        if not item.is_file() or file_digest(item) != expected:
            raise LabError(f"Evidence integrity check failed: {name}")
    if set(hashes) != public_evidence_files(path):
        raise LabError("Evidence seal is incomplete or contains unlinked artifacts")
    result = load(path / "result.json")
    if "report.html" not in hashes:
        from .sealed_report import report_unavailable
        if not report_unavailable(result):
            raise LabError("Evidence seal is incomplete: report missing without a recorded rendering failure")
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    sequences = [event["sequence"] for event in events]
    if sequences != sorted(set(sequences)):
        raise LabError("Event ordering is invalid")
    return {"mode": "playback", "execution": False, "result": result,
            "events": events, "integrity": "verified", "provenance": "recorded surrogate run"}


def export_public_run(source, destination, *, source_seal_sha256=None, secrets=()):
    """Rebuild sealed surface/browser evidence without private images, traces or state.

    Re-redaction applies environment credentials and the run's own private
    values (captures, --secrets-file values), which are never exported.
    """
    import tempfile

    from .evidence import export_redacted, export_redactor, private_secrets, record_identity
    from .sealed_report import document
    source, destination = Path(source).absolute(), Path(destination).absolute()
    source_seal = evidence_path(source, "evidence.sha256.json")
    if not source_seal.is_file():
        raise LabError("Run has no evidence seal; unsealed runs cannot be exported")
    identity = file_digest(source_seal)
    if source_seal_sha256 is not None and source_seal_sha256 != identity:
        raise LabError("Source evidence seal changed before export")
    original = playback(source)
    if file_digest(source / "evidence.sha256.json") != identity:
        raise LabError("Source evidence seal changed during verification")
    if destination.exists() or destination.is_symlink() \
            or destination.resolve().is_relative_to(source.resolve()):
        raise LabError("Export destination must be new and outside the source run")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".surface-export-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "public"
        staged.mkdir(mode=0o700)
        redactor = export_redactor(source, secrets, record_identity(original["result"], *original["events"]))
        result = redactor.clean_evidence(original["result"])
        if "diagnostics" in result:
            result["diagnostics"] = {"status": "excluded", "audience": "developer-only",
                                     "reason": "Pixels and raw browser traces are not export-redacted"}
        result["export"] = {"redacted": True, "private_artifacts": "excluded", "reporting_status": "completed",
                            "source_seal_sha256": identity, "rendering_harness": implementation_lock(Path.cwd())}
        if original["result"].get("event_schema_version") == 2 and (
                notice := original["result"].get("dependency_notices")):
            # These are verified public content addresses, not captured target
            # strings. A coincidental secret substring must not corrupt their
            # binding to the unmodified public notice archive.
            result["dependency_notices"].update(path=notice["path"], sha256=notice["sha256"])
        agents = set(original["result"].get("agent_runs", []))
        if original["result"].get("agent_evidence"):
            agents.add(original["result"]["agent_evidence"])
        for name in sorted(agents):
            export_redacted(evidence_path(source, name), evidence_path(staged, name),
                            secrets=list(redactor.secrets))
        (staged / "result.json").write_text(json.dumps(result, indent=2))
        from .sealed_events import export_events
        (staged / "events.jsonl").write_text(
            export_events(source, staged, original["events"], original["result"], redactor))
        (staged / "report.html").write_text(document(staged, result))
        # Last line of defense: redaction works on structure; a private value that
        # survived it anywhere in the staged files refuses the whole export.
        if leaked := exported_private_values(staged, [*private_secrets(source), *secrets]):
            raise LabError(f"Export still contains {len(leaked)} of the run's private values; "
                           "nothing was exported", category="reporting")
        seal(staged)
        playback(staged)
        playback(source)
        if file_digest(source / "evidence.sha256.json") != identity:
            raise LabError("Source evidence seal changed during export")
        staged.rename(destination)
    return destination


# Digests and ISO timestamps are harness values: a short private value inside one is not a leak.
EXPORT_MACHINE_VALUES = re.compile(r"(?<![0-9a-f])[0-9a-f]{32,64}(?![0-9a-f])"
                                   r"|\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def exported_private_values(directory, secrets):
    """Private values a staged export still contains, as whole words outside digests and
    timestamps (JSON- and HTML-escaped forms included). Long numbers count; short ones do not.
    Archive members count too (staged_texts): an archive that cannot be scanned raises LabError."""
    import html
    forms = {}
    for secret in secrets:
        if isinstance(secret, bool) or not isinstance(secret, (str, int)):
            continue
        text = str(secret)
        if len(text) < (6 if isinstance(secret, int) or text.isdigit() else 4):
            continue
        for form in (text, json.dumps(text)[1:-1], html.escape(text)):
            forms.setdefault(form, text)
    if not forms:
        return set()
    pattern = re.compile("|".join(
        ("(?<![0-9A-Za-z])" if form[0].isalnum() else "") + re.escape(form)
        + ("(?![0-9A-Za-z])" if form[-1].isalnum() else "") for form in sorted(forms, key=len, reverse=True)))
    found = set()
    for _, text in staged_texts(directory):
        found.update(forms[match[0]] for match in pattern.finditer(EXPORT_MACHINE_VALUES.sub(" ", text)))
    return found


# Archives are scanned member by member, nested ones included: Inspect .eval logs hold system prompts
# and provider payloads that no other exported file repeats. Bounded; an archive outside these bounds,
# unreadable or encrypted refuses the export instead of going unscanned.
ARCHIVE_SUFFIXES = (".zip", ".eval")
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
ARCHIVE_MEMBERS = 10_000
ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
ARCHIVE_BYTES = 256 * 1024 * 1024
ARCHIVE_DEPTH = 2


def declared_notices(directory):
    """The dependency-notice archives the directory's records (result.json, manifest.json) name by path and
    digest, where the file still has that digest: unmodified public license texts, never run content."""
    from .serialization import file_digest
    notices = set()
    for record in sorted(Path(directory).rglob("*.json")):
        if record.name not in ("result.json", "manifest.json") or record.is_symlink() or not record.is_file():
            continue
        try:
            reference = json.loads(record.read_text()).get("dependency_notices")
        except (ValueError, AttributeError, OSError):
            continue
        key = reference.get("sha256") if isinstance(reference, dict) else None
        if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) \
                and reference.get("path") == f"artifacts/{key}.zip":
            candidate = record.parent / "artifacts" / f"{key}.zip"
            if candidate.is_file() and not candidate.is_symlink() and file_digest(candidate) == key:
                notices.add(candidate)
    return notices


def archive_texts(source, name, budget=None, depth=0):
    """(member name, decoded text) for each file of a zip archive (a path or bytes), nested archives
    included, within the ARCHIVE_* bounds (``budget`` counts one archive's uncompressed bytes)."""
    import io
    import zipfile
    import zlib
    budget = {"bytes": 0} if budget is None else budget

    def refused(reason):
        # Only the staged file's own name: member names are archive content.
        return LabError(f"Archive {name.split('!', 1)[0]} cannot be scanned for private values ({reason}); "
                        "nothing was exported", category="reporting")
    try:
        archive = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError) as error:
        raise refused("not a readable zip archive") from error
    with archive:
        members = archive.infolist()
        if len(members) > ARCHIVE_MEMBERS:
            raise refused(f"more than {ARCHIVE_MEMBERS} members")
        for member in members:
            if member.is_dir():
                continue
            if member.flag_bits & 0x1:
                raise refused("an encrypted member")
            if member.file_size > ARCHIVE_MEMBER_BYTES or budget["bytes"] + member.file_size > ARCHIVE_BYTES:
                raise refused("larger than the scan bounds")
            try:
                with archive.open(member) as stream:  # Reads stop at the declared, bounded size.
                    data = stream.read(ARCHIVE_MEMBER_BYTES + 1)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError, ValueError,
                    zlib.error) as error:
                raise refused("an unreadable member") from error
            budget["bytes"] += len(data)
            if len(data) > ARCHIVE_MEMBER_BYTES or budget["bytes"] > ARCHIVE_BYTES:
                raise refused("larger than the scan bounds")
            label = f"{name}!{member.filename}"
            if member.filename.lower().endswith(ARCHIVE_SUFFIXES) or data[:4] in ZIP_MAGIC:
                if depth >= ARCHIVE_DEPTH:
                    raise refused("archives nested too deeply")
                yield from archive_texts(data, label, budget, depth + 1)
            else:
                yield label, data.decode(errors="ignore")


def staged_texts(directory):
    """(name, decoded text) for every regular file under ``directory``, and for each member of its zip
    archives (.zip, .eval or zip content; archive_texts), except the declared dependency notices."""
    directory = Path(directory)
    public = declared_notices(directory)
    for item in sorted(directory.rglob("*")):
        if not item.is_file() or item.is_symlink() or item in public:
            continue
        name = item.relative_to(directory).as_posix()
        with item.open("rb") as stream:
            head = stream.read(4)
        if item.suffix.lower() in ARCHIVE_SUFFIXES or head in ZIP_MAGIC:
            yield from archive_texts(item, name)
        else:
            yield name, item.read_bytes().decode(errors="ignore")


class ContractTape:
    """An isolated deterministic fixture: mismatches never fall through to a network."""
    def __init__(self, entries, fixture_version):
        self.entries, self.fixture_version, self.position = entries, fixture_version, 0

    def request(self, *, step, operation, arguments, headers=None):
        if self.position >= len(self.entries):
            raise LabError("Replay exhausted; no live fallback exists")
        actual = {"step": step, "operation": operation, "arguments": arguments,
                  "headers": headers or {}, "body_sha256": digest(arguments),
                  "fixture_version": self.fixture_version}
        entry = self.entries[self.position]
        if actual != entry["request"]:
            differences = [key for key in actual if actual[key] != entry["request"].get(key)]
            raise LabError(f"Replay request mismatch at {self.position}: {', '.join(differences)}")
        self.position += 1
        return entry["response"]

    def complete(self):
        if self.position != len(self.entries):
            raise LabError("Replay ended before all expected requests were consumed")


def contract_replay(tape, requests):
    document = load(Path(tape))
    fixture = ContractTape(document["entries"], document["fixture_version"])
    results = [fixture.request(**request) for request in load(Path(requests))]
    fixture.complete()
    return {"mode": "contract", "execution": "isolated tape fixture", "live_fallback": False,
            "results": results}


def pointer_segment(key):
    """RFC 6901 escaping, so reported paths can be passed back as --ignore pointers."""
    return str(key).replace("~", "~0").replace("/", "~1")


def differences(left, right, prefix=""):
    if isinstance(left, dict) and isinstance(right, dict):
        changes = []
        for key in sorted(left.keys() | right.keys(), key=str):
            path = f"{prefix}/{pointer_segment(key)}"
            if key not in left or key not in right:
                changes.append({"path": path, "change": "added" if key in right else "removed",
                                "baseline": left.get(key), "candidate": right.get(key)})
                continue
            changes.extend(differences(left[key], right[key], path))
        return changes
    if isinstance(left, list) and isinstance(right, list):
        # Element-wise, so one changed entry is not reported as a whole new list.
        changes = []
        for index in range(max(len(left), len(right))):
            path = f"{prefix}/{index}"
            if index >= len(left):
                changes.append({"path": path, "change": "added", "baseline": None, "candidate": right[index]})
            elif index >= len(right):
                changes.append({"path": path, "change": "removed", "baseline": left[index], "candidate": None})
            else:
                changes.extend(differences(left[index], right[index], path))
        return changes
    return [] if left == right and type(left) is type(right) else [
        {"path": prefix, "baseline": left, "candidate": right}]


MILESTONE_KINDS = frozenset({
    "step_started", "agent_action", "assertion", "ui_callback", "host_action", "consent_consumed",
    "service_response", "agent_observation", "browser_action", "snapshot", "network_denied",
    "network_authorized", "agent_final", "agent_proposal"})
DIFF_SECTIONS = ("outcomes", "capabilities", "milestones")
UUID_VALUE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
# Generated identifiers: whole hex tokens of 16+ characters mixing digits and letters.
HEX_IDENTIFIER = re.compile(r"(?i)(?<![0-9a-z])(?=[0-9a-z]*\d)(?=[0-9a-z]*[a-f])[0-9a-f]{16,}(?![0-9a-z])")
# Digests are compared exactly: a changed receipt or resource hash is a real difference.
DIGEST_KEY = re.compile(r"(?:^|_)(?:sha\d*|digest|hash|checksum|etag|integrity)$")
# In outcomes, hex IDs are normalized only under identifier keys; other values are exact.
ID_KEY = re.compile(r"(?:^|_)(?:id|ids|uid|uuid|guid)$")
# Under identifier keys only: other generated tokens (ULIDs, base62 IDs) mixing letters and digits.
OPAQUE_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}"
                               r"(?![A-Za-z0-9])")
# Service clocks, not behavior. Booking times such as starts_at are deliberately absent.
VOLATILE_TIME_KEYS = frozenset({
    "created_at", "updated_at", "modified_at", "expires_at", "expires", "expiry", "expiration",
    "valid_until", "estimated_at", "issued_at", "generated_at", "retrieved_at", "fetched_at",
    "observed_at", "received_at", "sent_at", "requested_at", "started_at", "finished_at",
    "completed_at", "last_modified", "last_updated", "timestamp", "wall_timestamp", "server_time",
    "iat", "exp", "nbf"})
STATUS_RANK = {"completed": 0, "passed": 0, "blocked": 1, "paused": 1, "failed": 2}
IGNORED = "<ignored>"


def parse_pointer(pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise LabError("Diff --ignore values must be JSON pointers such as /milestones/STEP/KIND/0/FIELD")
    segments = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    if segments[0] not in DIFF_SECTIONS:
        raise LabError("Diff --ignore pointers start with /outcomes, /capabilities or /milestones; "
                       "declare run lock differences with --treatment")
    return segments


def keyed_checks(checks):
    """Checks keyed by id (repeats as ``id#2``; rows without an id as ``#index``), for alignment."""
    if not isinstance(checks, list):
        return checks
    keyed = {}
    for index, row in enumerate(checks):
        name = row.get("id") if isinstance(row, dict) else None
        base = name if isinstance(name, str) and name else f"#{index}"
        key, occurrence = base, 1
        while key in keyed:
            occurrence += 1
            key = f"{base}#{occurrence}"
        keyed[key] = row
    return keyed


class Normalization:
    """Explicit, recorded rules applied identically to both runs before comparison.

    Run locks are never normalized: a changed digest there is a configuration change.
    """
    RULES = (
        "uuid: RFC 4122-shaped identifiers become <uuid>",
        ("hex_id: hexadecimal tokens of 16+ characters with digits and letters become <hex-id>; in "
         "outcomes only under identifier keys (id, *_id, uid, ids)"),
        ("opaque_id: under identifier keys, other alphanumeric tokens of 16+ characters mixing letters "
         "and digits (ULIDs, base62 IDs) become <id>"),
        "timestamp: values under " + ", ".join(sorted(VOLATILE_TIME_KEYS)) + " become <timestamp>",
        "exact: values under digest keys (*sha256, *digest, *hash, checksum, etag, integrity) are never normalized",
        ("checks are aligned by their recorded id (repeats as id#2) and milestone events by step, then kind "
         "and occurrence"),
    )

    def __init__(self, ignore=()):
        self.ignore = list(ignore)
        self.pointers = [parse_pointer(pointer) for pointer in self.ignore]
        self.counts = {"uuid": 0, "hex_id": 0, "opaque_id": 0, "timestamp": 0, "ignored": 0}

    def text(self, value, hex_ids=True, identifier=False):
        value, count = UUID_VALUE.subn("<uuid>", value)
        self.counts["uuid"] += count
        if hex_ids or identifier:
            value, count = HEX_IDENTIFIER.subn("<hex-id>", value)
            self.counts["hex_id"] += count
        if identifier:
            value, count = OPAQUE_IDENTIFIER.subn("<id>", value)
            self.counts["opaque_id"] += count
        return value

    def value(self, item, key=None, outcome=False):
        name = normalized_key(key) if key is not None else None
        if name is not None and DIGEST_KEY.search(name):
            return item
        if isinstance(item, dict):
            return {child: self.value(value, child, outcome) for child, value in item.items()}
        if isinstance(item, list):
            # Items inherit their key: entries of "ids" are identifiers, of "expires" clocks.
            return [self.value(child, key, outcome) for child in item]
        if name in VOLATILE_TIME_KEYS and isinstance(item, (str, int, float)) and not isinstance(item, bool):
            self.counts["timestamp"] += 1
            return "<timestamp>"
        if isinstance(item, str):
            return self.text(item, hex_ids=not outcome, identifier=bool(name and ID_KEY.search(name)))
        return item

    def checks(self, checks):
        keyed = keyed_checks(checks)
        if isinstance(keyed, dict):
            return {name: self.value(row, outcome=True) for name, row in keyed.items()}
        return self.value(keyed, outcome=True)

    def milestones(self, record, root):
        selected = {}
        for event in record["events"]:
            if event.get("event_schema_version") == 2:
                event = load(evidence_path(Path(root), event["artifacts"][0]["path"]))
            if event.get("kind") not in MILESTONE_KINDS:
                continue
            step = event.get("step") or event.get("id") or event.get("operation") or "context"
            payload = self.value({key: value for key, value in event.items() if key != "sequence"})
            selected.setdefault(self.text(str(step)), {}).setdefault(event["kind"], []).append(payload)
        return selected

    def document(self, record, root):
        result = record["result"]
        view = {"outcomes": {"status": result.get("status"), "checks": self.checks(result.get("checks")),
                             "verified_service_outcome": self.value(result.get("verified_service_outcome"),
                                                                    outcome=True)},
                "capabilities": self.value(result.get("capabilities", {})),
                "milestones": self.milestones(record, root)}
        for segments in self.pointers:
            view = self._without(view, segments)
        return view

    def _without(self, item, segments):
        if not segments:
            self.counts["ignored"] += 1
            return IGNORED
        head, rest = segments[0], segments[1:]
        if isinstance(item, dict):
            return {key: self._without(child, rest) if head in ("*", key) else child
                    for key, child in item.items()}
        if isinstance(item, list):
            return [self._without(child, rest) if head in ("*", str(index)) else child
                    for index, child in enumerate(item)]
        return item

    def record(self):
        return {"rules": list(self.RULES), "ignored": self.ignore, "replacements": self.counts,
                "scope": "outcomes, capabilities and milestones of both runs; run locks are compared exactly"}


def regressions(before, after):
    """Outcome regressions: a worse run status, or a passing check that no longer passes.

    Ignored parts (``--ignore``) never count, whether a whole section, a check or one field.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []  # The outcomes section itself was ignored.
    found = []
    left, right = before.get("status"), after.get("status")
    if IGNORED not in (left, right) and STATUS_RANK.get(right, 2) > STATUS_RANK.get(left, 2):
        found.append({"path": "/outcomes/status", "baseline": left, "candidate": right})
    baseline, candidate = before.get("checks"), after.get("checks")
    if not isinstance(baseline, dict) or candidate == IGNORED:
        return found  # No baseline checks, or either run's checks were ignored.
    candidate = candidate if isinstance(candidate, dict) else {}  # A run without checks lost them all.
    for name, check in baseline.items():
        other = candidate.get(name)
        if not isinstance(check, dict) or check.get("status") != "passed" or other == IGNORED:
            continue
        status = other.get("status") if isinstance(other, dict) else "missing"
        if status != "passed" and status != IGNORED:
            found.append({"path": f"/outcomes/checks/{pointer_segment(name)}",
                          "baseline": "passed", "candidate": status})
    return found


def compare(baseline, candidate, treatments=(), *, ignore=()):
    """Semantic diff of two sealed runs; ``regressions`` drives the CLI exit code."""
    normalization = Normalization(ignore)
    try:
        before, after = playback(baseline), playback(candidate)
    except LabError as error:
        raise LabError(f"Runs are not comparable: {error}", code=3, category="unsupported") from error
    if not before["result"].get("run_lock") or not after["result"].get("run_lock"):
        raise LabError("Runs are not comparable: comparison requires explicit run locks",
                       code=3, category="unsupported")
    lock_changes = differences(before["result"].get("run_lock", {}),
                               after["result"].get("run_lock", {}))
    unexpected = [item for item in lock_changes if not any(
        item["path"] == field or item["path"].startswith(field + "/") for field in treatments)]
    left, right = normalization.document(before, baseline), normalization.document(after, candidate)
    return {"mode": "diff", "comparison": "declared treatment" if treatments else "same-configuration",
            "lock_compatible": not unexpected, "unexplained_lock_changes": unexpected,
            "declared_treatments": list(treatments),
            "outcomes": differences(left["outcomes"], right["outcomes"]),
            "capabilities": differences(left["capabilities"], right["capabilities"]),
            "milestones": differences(left["milestones"], right["milestones"]),
            "regressions": regressions(left["outcomes"], right["outcomes"]),
            "normalization": normalization.record(),
            "causal_claim": None}
