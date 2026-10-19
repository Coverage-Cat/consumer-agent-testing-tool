"""P0 redaction: ordinary settings are not secrets, short tokens cannot corrupt seals,
personal data stays out of exports, and adversarial nesting cannot crash evidence writing."""
import copy
import json
import os
import pickle
import threading
import time

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import (
    NESTING_MARKER,
    SECRET_KEYS,
    Evidence,
    Redactor,
    SecretList,
    environment_secrets,
    export_redacted,
    private_secrets,
)
from consumer_agent_testing_tool.replay import export_public_run, playback
from consumer_agent_testing_tool.sealed_report import finalize
from consumer_agent_testing_tool.surface import SurfaceRun

# Split so secret scanners on a public repository do not mistake the synthetic token for a real one.
TOKEN = "ghp_" + "9f2Kq7LmW3xZ8rT1vB6nC4yH0dJ5sA2eGu"


def test_environment_settings_are_not_secrets_but_credentials_are(monkeypatch, tmp_path):
    for name in list(__import__("os").environ):
        if any(word in name.upper() for word in ("TOKEN", "KEY", "SECRET", "PASSWORD", "COOKIE", "EMAIL",
                                                 "PHONE", "CONTACT", "AUTHORIZATION", "STORAGE")):
            monkeypatch.delenv(name)
    settings = {"TOKENIZERS_PARALLELISM": "false", "MAX_TOKENS": "4096", "TOKEN_LIMIT": "4096",
                "COOKIE_SECURE": "true", "SESSION_TOKEN_TTL": "3600", "SECRET_ROTATION": "on",
                "HF_TOKEN_PATH": str(tmp_path), "API_KEY_DOCS": "https://platform.example/docs/keys",
                "MASKED_PASSWORD": "********", "SHORT_TOKEN": "abc123",
                # Plain paths are settings whether or not they exist on this machine.
                "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
                "TOKEN_CACHE_PATH": "/nonexistent/path/to/token", "STORAGE_STATE": "./storage-state.json",
                "SECRETS_DIR": "C:\\Users\\x\\token.txt"}
    for name, value in {**settings, "GITHUB_TOKEN": TOKEN}.items():
        monkeypatch.setenv(name, value)
    redactor = Redactor.environment()
    assert list(redactor.secrets) == [TOKEN]
    schema = {"type": "object", "additionalProperties": False, "max_tokens": 4096,
              "text": 'result: {"ok": false, "limit": 4096} and TOKENIZERS_PARALLELISM=false'}
    assert redactor.clean(schema) == schema
    assert TOKEN not in json.dumps(redactor.clean({"note": f"token {TOKEN} used"}))


def test_credentials_embedded_in_environment_urls_and_schemes_are_learned():
    found = environment_secrets({
        "DATABASE_PASSWORD_URL": "postgres://reporter:hunter2-long-pass@db.internal:5432/app",
        "GIT_TOKEN_REMOTE": f"https://{TOKEN}@github.com/org/repo.git",
        "AUTHORIZATION_HEADER": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.signature-part",
        "PLAIN_TOKEN_ENDPOINT": "https://auth.example/oauth/token",
        "UNRELATED": "s3cr3t-value-without-a-secret-name"})
    assert "hunter2-long-pass" in found and TOKEN in found
    assert "eyJhbGciOiJIUzI1NiJ9.e30.signature-part" in found
    assert not any("auth.example" in item for item in found)
    assert "s3cr3t-value-without-a-secret-name" not in found


def test_credentials_in_url_paths_and_opaque_file_names_are_learned():
    slack = "https://hooks.slack.com/services/T024BE7LD/B01234567/aB3dE6gH9jK2mN5pQ8sT1vW4"
    bot = "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/sendMessage"
    found = environment_secrets({"SLACK_WEBHOOK_SECRET": slack, "TELEGRAM_TOKEN_URL": bot,
                                 "SECRET_BLOB": "/9j/4AAQSkZJRgABAQEASABIAAD",
                                 "TOKEN_DOCS": "https://example.com/docs/getting-started-with-api-keys"})
    assert slack in found and "aB3dE6gH9jK2mN5pQ8sT1vW4" in found
    assert bot in found and "bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" in found
    assert "/9j/4AAQSkZJRgABAQEASABIAAD" in found
    assert not any("getting-started" in item for item in found)


async def test_tokenizers_parallelism_false_no_longer_blocks_agent_requests(tmp_path, monkeypatch):
    from test_surface_agent import driver
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    monkeypatch.setenv("MAX_TOKENS", "4096")
    planner, api = driver()
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"},
                     planner=planner)
    assert await run.run() == 0
    assert len(api.inputs) >= 1  # The credential guard no longer refuses the first request.
    assert "false" not in Redactor.environment().secrets


def test_secret_list_guards_every_mutator_and_keeps_longest_first():
    redactor = Redactor()
    secrets = redactor.secrets
    secrets.append("")
    secrets.append("2")
    secrets.append("Bearer")
    secrets.append("false")
    secrets.extend(["abcd", "abcd", "  abcdefgh-long-token  "])
    secrets.insert(0, "xy")
    secrets += ["mnop-qrst"]
    secrets.add("[REDACTED]")
    secrets[0] = secrets[0]
    secrets.sort(key=len)
    secrets.reverse()
    assert list(secrets) == ["abcdefgh-long-token", "mnop-qrst", "abcd"]
    assert "abcd" in secrets and " abcd " in secrets and "" not in secrets
    assert redactor.text("x abcdefgh-long-token y") == "x [REDACTED] y"  # No "efgh-long-token" remainder.
    redactor.secrets = sorted({"", "1", "token-value-1234"}, key=len)
    assert isinstance(redactor.secrets, SecretList) and list(redactor.secrets) == ["token-value-1234"]
    assert Redactor(["abcdefgh", "efghijkl"]).text("abcdefghijkl") == "[REDACTED]"  # Overlaps merge.
    assert json.loads(json.dumps(redactor.secrets)) == ["token-value-1234"]


def test_secret_list_admits_numbers_and_rejects_unknown_types():
    # Declared values keep their decimal text; null, booleans and blanks hide nothing.
    redactor = Redactor({"pin": 482913, "rate": 0.875, "unset": None, "flag": True}.values())
    redactor.secrets.extend([12345678, "  "])
    assert list(redactor.secrets) == ["12345678", "482913", "0.875"]
    assert redactor.text("PIN 482913, ID 12345678") == "PIN [REDACTED], ID [REDACTED]"
    with pytest.raises(TypeError):
        redactor.secrets.append(b"bytes-secret")


def test_secret_list_is_fast_atomic_and_survives_copies():
    redactor = Redactor()
    started = time.perf_counter()
    for index in range(3000):
        redactor.secrets.append(f"secret-{index:06d}")
    assert time.perf_counter() - started < 1  # Insertion, not a full rebuild per append.

    def register(offset):
        for index in range(500):
            redactor.secrets.append(f"thread-secret-{offset}-{index:05d}")
            redactor.secrets.append(f"shared-{index:05d}")

    threads = [threading.Thread(target=register, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    secrets = list(redactor.secrets)
    assert len(secrets) == 3000 + 4 * 500 + 500 == len(set(secrets))
    assert secrets == sorted(secrets, key=lambda item: (-len(item), item))
    redactor.protect("preferences")
    for clone in (copy.deepcopy(redactor), pickle.loads(pickle.dumps(redactor))):
        assert list(clone.secrets) == secrets
        clone.secrets.append("preferences")
        assert "preferences" not in clone.secrets and clone.clean("preferences") == "preferences"


def test_redaction_is_idempotent_and_never_rewrites_its_own_markers():
    redactor = Redactor(["EDAC", "MAIL"])
    once = redactor.clean({"text": "EDAC alice@example.com [REDACTED]"})
    assert once == {"text": "[REDACTED] [EMAIL] [REDACTED]"}
    assert redactor.clean(once) == once
    # A secret that itself contains a marker still matches where it literally occurs.
    spanning = Redactor(["abc[EMAIL]def", "key-[REDACTED]-x"])
    assert spanning.text("x abc[EMAIL]def y key-[REDACTED]-x") == "x [REDACTED] y [REDACTED]"
    assert spanning.text("x [REDACTED] y") == "x [REDACTED] y"


class PagedHost(FakeHost):
    def __init__(self, token):
        super().__init__()
        self.token = token

    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        result["structuredContent"].update(next_page_token=self.token, page_token=self.token,
                                           token_type="Bearer")
        return result


@pytest.mark.parametrize("token", ["", "2", "Bearer"])
async def test_empty_short_or_type_tokens_cannot_corrupt_results_or_prevent_a_seal(tmp_path, token):
    run = SurfaceRun(tmp_path, scenario(), PagedHost(token), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    record = playback(run.path)
    result = record["result"]
    assert result["run_id"] == run.path.name and result["scenario_id"] == "preferences"
    assert result["status"] == "completed" and result["checks"][0]["status"] == "passed"
    assert "[REDACTED]" not in json.dumps({key: result[key] for key in (
        "run_id", "scenario_id", "interface_profile", "run_lock", "dependency_notices", "status")})
    assert (run.path / "report.html").is_file()
    assert all(secret not in ("", "2", "Bearer") for secret in run.redactor.secrets)


PROFILE_NAME = "consumer-surface-public-constraint-surrogate"


@pytest.mark.parametrize("token", ["preferences", PROFILE_NAME, "consumer surface surrogate", "completed"])
async def test_tokens_equal_to_identity_or_status_words_never_break_a_seal(tmp_path, token):
    """A page token equal to the scenario ID, the profile name or label, or a status word."""
    run = SurfaceRun(tmp_path, scenario(), PagedHost(token), secrets={"access": "original-secret-canary"})
    run.redactor.secrets.append(token)  # Also as if declared or learned before the first event.
    assert await run.run() == 0
    result = playback(run.path)["result"]
    assert result["scenario_id"] == "preferences" and result["status"] == "completed"
    assert result["interface_profile"]["name"] == PROFILE_NAME
    assert result["fidelity"] == "consumer surface surrogate" and result["checks"][0]["status"] == "passed"
    assert token not in run.redactor.secrets and (run.path / "report.html").is_file()
    responses = [row for row in playback(run.path)["events"] if row["kind"] == "service_response"]
    assert all(row["result"]["structuredContent"]["next_page_token"] == "[REDACTED]" for row in responses)
    exported = playback(export_public_run(run.path, tmp_path / "export"))["result"]
    assert exported["scenario_id"] == "preferences" and exported["interface_profile"]["name"] == PROFILE_NAME


class CoincidentHost(FakeHost):
    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        # Learned credentials that happen to occur inside the run ID ("surface-"), the
        # profile name, the scenario ID ("preferences") and check statuses ("passed").
        result["structuredContent"].update(access_token="face", refresh_token="pref", password="pass")
        return result


async def test_short_learned_secrets_never_corrupt_identity_locks_or_statuses(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), CoincidentHost(), secrets={"access": "original-secret-canary"})
    run.redactor.add([run.path.name[8:12], run.run_lock["scenario"][:6]])  # Hex slices of identities.
    assert await run.run() == 0
    result = playback(run.path)["result"]
    assert result["run_id"] == run.path.name and result["scenario_id"] == "preferences"
    assert result["interface_profile"]["name"] == "consumer-surface-public-constraint-surrogate"
    assert result["run_lock"]["scenario"] == run.run_lock["scenario"]
    assert result["dependency_notices"]["path"].endswith(result["dependency_notices"]["sha256"] + ".zip")
    assert result["checks"][0]["status"] == "passed" and result["status"] == "completed"


def test_identity_fields_still_redact_exact_and_long_secrets():
    """Machine values (digests, run IDs, timestamps, references) lose only exact or long secrets."""
    digest = __import__("hashlib").sha256(b"receipt").hexdigest()
    redactor = Redactor([digest[30:42], "surf", "2026"])
    record = {"payload": {"run_id": "surface-" + "ab12" * 8, "receipt_sha256": digest,
                          "at": "2026-10-16T12:00:00.123456+00:00", "path": f"artifacts/{digest}.json",
                          "note": "surface 2026"}}
    cleaned = redactor.clean(record)["payload"]
    assert {key: cleaned[key] for key in ("run_id", "receipt_sha256", "at", "path")} == {
        key: record["payload"][key] for key in ("run_id", "receipt_sha256", "at", "path")}
    assert cleaned["note"] == "[REDACTED]ace [REDACTED]"  # Free text is not a machine value.
    redactor.add([digest[:20]])
    assert redactor.clean(digest) == "[REDACTED]" + digest[20:]  # 16+ characters still match.
    assert Redactor([digest]).clean({"run_id": digest}) == {"run_id": "[REDACTED]"}  # Exact match.
    # A key name confers nothing: a root "run_id" holding free text is redacted as usual.
    assert Redactor(["lease-7f3a"]).clean({"run_id": "lease lease-7f3a"}) == {"run_id": "lease [REDACTED]"}


def test_registered_identity_is_never_a_secret_and_never_rewritten():
    redactor = Redactor(["preferences", "pref", "consumer surface surrogate"])
    redactor.protect("preferences", "consumer surface surrogate", "alice@example.com", "/tmp/run",
                     "https://x.test/?code=1", "Bearer abcdefgh", None, 7)
    assert list(redactor.secrets) == ["pref"]  # Equal secrets dropped; other values never registered.
    for value in ("preferences", "consumer surface surrogate", "completed", "developer", "redacted"):
        redactor.secrets.append(value)
        assert value not in redactor.secrets
        assert redactor.clean({"any": {"depth": value}}) == {"any": {"depth": value}}
    assert redactor.clean("preferences page") == "[REDACTED]erences page"  # Only the exact value.
    assert redactor.clean("alice@example.com") == "[EMAIL]"
    assert redactor.mask_credentials({"scenario": "preferences"}) == {"scenario": "preferences"}


class ServiceRootHost(FakeHost):
    """A service whose result fields share names with harness record fields."""

    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        result["structuredContent"].update(lease="lease-7f3a")
        return result


def service_root_payload():
    return {"status": "lease lease-7f3a active", "kind": "user#profile",
            "profile": {"raw": '{"password": "pw-plain-1", "ssn": "123-45-6789"}', "notes": "session abc12345"},
            "steps": ["Login with code abc12345"], "metrics": {"detail": "code abc12345"},
            "diagnostics": "lease-7f3a", "limits": ["lease-7f3a"], "artifacts": "lease-7f3a"}


def test_service_payloads_at_the_record_root_get_full_redaction(tmp_path):
    hidden = ("lease-7f3a", "abc12345", "pw-plain-1", "123-45-6789")
    run = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                   profile={"name": "synthetic", "surface": "mcp", "label": "synthetic fixture"},
                   lock={}, redactor=Redactor(["lease-7f3a", "abc12345"]))
    run.event("tool_result", service_root_payload())
    run.finish("completed")
    artifacts = "".join(path.read_text() for path in (run.path / "artifacts").glob("*.json"))
    assert not any(value in artifacts for value in hidden) and "user#profile" in artifacts
    output = export_redacted(run.path, tmp_path / "export")
    exported = "".join(path.read_text(errors="ignore") for path in output.rglob("*") if path.is_file())
    assert not any(value in exported for value in hidden)
    # Sealed events: the payload root is service data, recorded and exported the same way.
    broker = BrowserBroker(tmp_path, BrowserPolicy(url="http://127.0.0.1:8765",
        origins=["http://127.0.0.1:8765"], rules=[]), host=FakeHost(), secrets=["lease-7f3a", "abc12345"])
    broker.record("snapshot", service_root_payload())
    broker.finish("completed")
    events = playback(broker.path)["events"]
    assert not any(value in json.dumps(events) for value in hidden)
    target = export_public_run(broker.path, tmp_path / "broker-export")
    assert not any(value in (target / name).read_text() for value in hidden
                   for name in ("events.jsonl", "result.json", "report.html"))


PERSONAL = {
    "full_name": "Taylor Sandbox Example", "first_name": "Taylor", "last_name": "Example",
    "given_name": "Taylor", "family_name": "Example", "phone_number": "+12065550100",
    "mobile": "206-555-0100", "birthday": "1990-04-01", "date_of_birth": "1990-04-01", "dob": "1990-04-01",
    "license_number": "WA1234567", "drivers_license": "WA7654321", "ssn": "123-45-6789",
    "social_security_number": "987-65-4321", "passport_number": "X1234567", "tax_id": "12-3456789",
    "address": "123 Main St", "street": "123 Main St", "street_address": "500 Pine St",
    "address_line1": "1 Infinite Loop", "address_line2": "Suite 100", "postal_code": "98101",
    "zip": "98101", "zip_code": "98101",
}


@pytest.mark.parametrize("key", sorted(PERSONAL))
def test_personal_fields_are_redacted_by_normalized_key(key):
    camel = key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:])
    record = {key: PERSONAL[key], camel: PERSONAL[key], key.upper(): PERSONAL[key]}
    assert set(Redactor().clean_evidence(record).values()) == {"[REDACTED]"}
    # Model-facing ``clean`` keeps the user's own data; other lanes still build observations with it.
    assert Redactor().clean(record) == record


def test_private_values_of_any_type_and_whole_subtrees_are_redacted():
    cleaned = Redactor().clean_evidence({
        "card_number": 4242424242424242, "password": 12345678, "address": {"street": "1 Main", "zip": "98101"},
        "set-cookie": ["sid=abc123def456; HttpOnly"], "token": {"value": "nested-secret-value"},
        "accessToken": "eyJhbGciOiJIUzI1NiJ9.e30.sig", "phone": None, "email": False,
        "name": "Public Display Name", "id": "draft-1", "max_tokens": 4096, "token_type": "Bearer",
        "note": "Reach alice@example.com with Bearer abc.def at https://x.test/cb?code=c3&id_token=t4&view=1",
    })
    assert all(cleaned[key] == "[REDACTED]" for key in (
        "card_number", "password", "address", "set-cookie", "token", "accessToken"))
    assert cleaned["phone"] is None and cleaned["email"] is False
    assert cleaned["name"] == "Public Display Name" and cleaned["id"] == "draft-1"
    assert cleaned["max_tokens"] == 4096 and cleaned["token_type"] == "Bearer"
    assert cleaned["note"] == ("Reach [EMAIL] with Bearer [REDACTED] at "
                               "https://x.test/cb?code=[REDACTED]&id_token=[REDACTED]&view=1")


def test_schema_property_names_are_not_data_and_reprs_are_scrubbed():
    schema = {"type": "object", "required": ["full_name", "address"], "properties": {
        "full_name": {"type": "string"}, "address": {"type": "object", "properties": {
            "zip": {"type": "string", "pattern": "^[0-9]{5}$"}}}, "password": True}}
    assert Redactor().clean_evidence({"inputSchema": schema}) == {"inputSchema": schema}
    data = Redactor().clean_evidence({"properties": {"address": "123 Main St"}})
    assert data == {"properties": {"address": "[REDACTED]"}}
    # A data map that is merely called "properties" is not a schema.
    record = {"properties": {"password": {"value": "hunter2-plain"}, "api_key": {}, "email": {"type": "string"}}}
    assert Redactor().clean(record) == {"properties": {"password": "[REDACTED]", "api_key": {},
                                                       "email": {"type": "string"}}}
    text = Redactor().clean_evidence("{'password': 'hunter2-plain', 'zip': 98101, 'note': 'ok'} and "
                                     "{\"ssn\": 123, oops}")
    assert "hunter2-plain" not in text and "98101" not in text and "123," not in text and "'note': 'ok'" in text


def test_masked_credentials_keep_the_users_own_data_visible():
    redactor = Redactor(["registered-host-credential"])
    view = redactor.mask_credentials({"full_name": "Taylor Example", "email": "taylor@example.com",
        "intake_access_token": "minted-token-value", "note": "use registered-host-credential",
        "headers": {"Authorization": "Bearer abc.def"}})
    assert view["full_name"] == "Taylor Example" and view["email"] == "taylor@example.com"
    assert view["intake_access_token"] == "[REDACTED]" and view["headers"]["Authorization"] == "[REDACTED]"
    assert view["note"] == "use [REDACTED]"


def test_learn_is_iterative_and_skips_counts_and_types():
    deep = value = {}
    for _ in range(5000):
        value["a"] = {}
        value = value["a"]
    value.update(next_page_token="cursor-0001", token_type="Bearer", max_tokens="4096",
                 authorization={"scheme": "Bearer", "credentials": "nested-credential-1"})
    redactor = Redactor()
    redactor.learn(deep)
    assert sorted(redactor.secrets) == ["cursor-0001", "nested-credential-1"]


def test_adversarial_nesting_is_redacted_not_raised_and_stays_fast():
    redactor = Redactor(["secret-in-the-text"])
    deep = value = {}
    for _ in range(3000):
        value["a"] = {}
        value = value["a"]
    value["password"] = "never-visible"
    cleaned = json.dumps(redactor.clean({"result": deep}))
    assert NESTING_MARKER in cleaned and "never-visible" not in cleaned
    for depth in (1000, 20000):
        assert NESTING_MARKER in redactor.text("result: " + "[" * depth + "]" * depth)
    started = time.perf_counter()
    redactor.text('{"a": [' * 8000 + " secret-in-the-text")
    snapshot = "\n".join(f'- button "Item {index}" [ref=e{index}] [cursor=pointer]' for index in range(32000))
    assert "secret-in-the-text" not in redactor.text(snapshot + " secret-in-the-text")
    assert time.perf_counter() - started < 5


def test_deep_payload_is_sealed_with_one_redaction_pass(tmp_path):
    broker = BrowserBroker(tmp_path, BrowserPolicy(url="http://127.0.0.1:8765",
        origins=["http://127.0.0.1:8765"], rules=[]), host=FakeHost(), secrets=["EDAC"])
    deep = value = {}
    for _ in range(2000):
        value["a"] = {}
        value = value["a"]
    broker.record("snapshot", {"text": "[REDACTED] EDAC", "details": deep})
    broker.finish("completed")
    events = playback(broker.path)["events"]
    assert NESTING_MARKER in json.dumps(events[0]) and events[0]["text"] == "[REDACTED] [REDACTED]"


def sealed_run_with_private_state(path, leaked):
    path.mkdir()
    finalize(path, {"status": "completed", "checks": [], "run_lock": {"scenario": "synthetic"}})
    (path / "events.jsonl").write_text(json.dumps({"kind": "service_response", "sequence": 1,
                                                   "result": {"session": {"handle": leaked}}}) + "\n")
    (path / "checkpoint.private.json").write_text(json.dumps({"secrets": {"session": leaked}}))
    finalize(path, json.loads((path / "result.json").read_text()))


def test_export_applies_the_runs_own_captured_values_without_exporting_them(tmp_path):
    leaked = "sess-handle-9f8e7d6c5b4a"
    source = tmp_path / "source"
    sealed_run_with_private_state(source, leaked)
    assert leaked in (source / "events.jsonl").read_text()
    target = export_public_run(source, tmp_path / "export")
    exported = "".join(path.read_text(errors="ignore") for path in target.rglob("*") if path.is_file())
    assert leaked not in exported and "[REDACTED]" in (target / "events.jsonl").read_text()
    assert not list(target.rglob("*.private.json"))
    report = export_public_run(source, tmp_path / "report-copy").joinpath("report.html").read_text()
    assert leaked not in report


def test_unstructured_private_state_is_itself_treated_as_a_secret(tmp_path):
    source = tmp_path / "source"
    sealed_run_with_private_state(source, "raw-private-checkpoint-value")
    (source / "checkpoint.private.json").write_text("raw-private-checkpoint-value")
    target = export_public_run(source, tmp_path / "export")
    assert "raw-private-checkpoint-value" not in (target / "events.jsonl").read_text()
    (source / "checkpoint.private.json").unlink()
    (source / "checkpoint.private.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(LabError, match="link"):
        export_public_run(source, tmp_path / "linked-export")
    assert not (tmp_path / "linked-export").exists()


def test_private_state_that_cannot_be_applied_refuses_the_export(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.evidence as evidence_module
    source = tmp_path / "source"
    sealed_run_with_private_state(source, "sess-handle-9f8e7d6c5b4a")
    (source / "private").mkdir()
    # Credentials under a data map called "properties" are still private values.
    (source / "private" / "trial.private.json").write_text(json.dumps(
        {"properties": {"api_key": "sk-in-a-properties-map-123"}, "secrets": {"pin": 482913}}))
    assert {"sk-in-a-properties-map-123", 482913} <= set(private_secrets(source))
    (source / "private" / "binary.private.json").write_bytes(b"\xff\xfe not text")
    with pytest.raises(LabError, match="unreadable"):
        export_public_run(source, tmp_path / "binary")
    (source / "private" / "binary.private.json").unlink()
    monkeypatch.setattr(evidence_module, "PRIVATE_STATE_LIMIT", 8)
    with pytest.raises(LabError, match="cannot be applied"):
        export_public_run(source, tmp_path / "oversized")
    monkeypatch.undo()
    (source / "private").rename(tmp_path / "elsewhere")
    (source / "private").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(LabError, match="link"):
        export_public_run(source, tmp_path / "linked")
    assert not any((tmp_path / name).exists() for name in ("binary", "oversized", "linked"))


async def test_surface_capture_is_absent_from_export(tmp_path, monkeypatch):
    """End-to-end guard. SurfaceRun registers captures before recording, so this passes even
    without export re-redaction; the synthetic historical run above is the real E3 check."""
    from consumer_agent_testing_tool.surface import SurfaceScenario
    captured = "sess-handle-9f8e7d6c5b4a"
    flow = SurfaceScenario.model_validate({
        "id": "capture", "goal": "Open a session and read status",
        "endpoint": "http://127.0.0.1:8765/mcp", "allowed_origins": ["http://127.0.0.1:8765"],
        "tools": {"open": {"effect": "write", "capture": {"session": "/structuredContent/session/handle"}},
                  "status": {"effect": "read", "host_arguments": {"session": {"$ref": "/secrets/session"}}}},
        "steps": [{"id": "open", "description": "Open session", "kind": "tool", "tool": "open"},
                  {"id": "status", "description": "Read status", "kind": "tool", "tool": "status",
                   "after": ["open"], "checks": [{"id": "ok", "layer": "service",
                   "path": "/status/result/structuredContent/ok", "expected": True}]}]})

    class Host(FakeHost):
        async def call(self, name, arguments):
            if name == "open":
                return {"structuredContent": {"session": {"handle": captured}}}
            return {"structuredContent": {"ok": arguments.get("session") == captured}}

    run = SurfaceRun(tmp_path, flow, Host())
    assert await run.run() == 0
    target = export_public_run(run.path, tmp_path / "export")
    assert all(captured not in path.read_text(errors="ignore") for path in target.rglob("*") if path.is_file())


def test_core_export_applies_private_trial_state(tmp_path):
    run = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                   profile={"name": "synthetic", "surface": "mcp", "label": "synthetic fixture"},
                   lock={}, redactor=Redactor([]))
    run.event("tool_result", {"content": "issued opaque-lease-identifier-77"})
    run.finish("completed")
    (run.path / "private").mkdir()
    (run.path / "private" / "api-lease.private.json").write_text(
        json.dumps({"lease_token": "opaque-lease-identifier-77"}))
    output = export_redacted(run.path, tmp_path / "export")
    assert all("opaque-lease-identifier-77" not in path.read_text(errors="ignore")
               for path in output.rglob("*") if path.is_file())
    assert not (output / "private").exists()


class ApplicantHost(FakeHost):
    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        result["structuredContent"]["applicant"] = {
            "fullName": PERSONAL["full_name"], "phone_number": PERSONAL["phone_number"],
            "birthday": PERSONAL["birthday"], "license_number": PERSONAL["license_number"],
            "address": {"street": PERSONAL["street"], "zip": PERSONAL["zip"]}, "plan": "umbrella"}
        return result


async def test_personal_fields_are_absent_from_exports(tmp_path):
    values = [PERSONAL[key] for key in ("full_name", "phone_number", "birthday", "license_number", "street")]
    run = SurfaceRun(tmp_path, scenario(), ApplicantHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    target = export_public_run(run.path, tmp_path / "export")
    exported = "".join(path.read_text(errors="ignore") for path in target.rglob("*") if path.is_file())
    assert "umbrella" in exported and not any(value in exported for value in values)
    # A raw event recorded before these rules is re-redacted at export time.
    source = tmp_path / "historical"
    sealed_run_with_private_state(source, "sess-handle-9f8e7d6c5b4a")
    (source / "events.jsonl").write_text(json.dumps({"kind": "service_response", "sequence": 1,
        "result": {"applicant": {"full_name": values[0], "dateOfBirth": values[2]}}}) + "\n")
    finalize(source, json.loads((source / "result.json").read_text()))
    historical = export_public_run(source, tmp_path / "historical-export")
    text = (historical / "events.jsonl").read_text() + (historical / "report.html").read_text()
    assert values[0] not in text and values[2] not in text


def test_model_views_hide_credentials_in_text_and_embedded_json_but_not_the_users_data():
    redactor = Redactor(["registered-host-credential"])
    view = redactor.mask_credentials({
        "link": "https://shop.test/review?review_token=minted-review-1&page=2",
        "raw": '{"session_token": "embedded-session-1", "email": "taylor@example.com"}',
        "note": "Mail taylor@example.com; use registered-host-credential",
        "contact": "Taylor Example", "address": {"street": "1 Main St"},
        "schema": {"properties": {"password": {"type": "string"}}}})
    assert view["link"] == "https://shop.test/review?review_token=[REDACTED]&page=2"
    assert json.loads(view["raw"]) == {"session_token": "[REDACTED]", "email": "taylor@example.com"}
    assert view["note"] == "Mail taylor@example.com; use [REDACTED]"
    assert view["contact"] == "Taylor Example" and view["address"] == {"street": "1 Main St"}
    assert view["schema"] == {"properties": {"password": {"type": "string"}}}  # A schema, not a password.
    # Evidence written to disk still applies every personal-data rule to the same values.
    recorded = redactor.clean_evidence(view)
    assert recorded["contact"] == recorded["address"] == "[REDACTED]" and "[EMAIL]" in recorded["note"]


def test_learn_never_registers_the_schema_of_a_credential_named_property():
    redactor = Redactor()
    redactor.learn({"structuredContent": {"inputSchema": {"type": "object", "properties": {
        "token": {"type": "string", "description": "Session token from login"},
        "password": {"type": "string", "minLength": 12}}}},
        "properties": {"api_key": "data-map-key-0001"}})  # A data map that is merely called "properties".
    assert list(redactor.secrets) == ["data-map-key-0001"]
    assert redactor.clean_evidence({"type": "string", "note": "a substring"}) == {
        "type": "string", "note": "a substring"}


def test_learn_takes_credential_names_anywhere_but_never_flattens_credential_objects():
    redactor = Redactor()
    learned = redactor.learn({"result": {
        "password_reset_url": "https://shop.test/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0",
        "secret_answer": "my first pet rex", "csrftoken": "csrf-value-0001", "backup_tokens": ["backup-0001"],
        "max_tokens": "4096", "token_type": "Bearer", "tokens_used": "1200", "token_status": "active",
        "tokenExpiresAt": "2026-10-16T00:00:00Z", "refresh_token_ttl": "86400",
        # Descriptive fields of a credential object are no credentials; its value field is.
        "credentials": {"kind": "host", "method": "action", "expires_in": "3600", "value": "nested-value-0001"},
        "cookies": [{"name": "session", "value": "cookie-value-0001", "domain": "shop.test"}]}})
    assert sorted(redactor.secrets) == sorted(learned) == sorted([
        "https://shop.test/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0", "my first pet rex", "csrf-value-0001",
        "backup-0001", "nested-value-0001", "cookie-value-0001"])
    assert redactor.clean_evidence({"event_type": "host_action", "domain": "shop.test"}) == {
        "event_type": "host_action", "domain": "shop.test"}


def test_environment_contact_details_are_hidden_in_evidence_but_are_no_credentials(monkeypatch):
    for name in list(os.environ):
        if SECRET_KEYS.search(name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("EMAIL", "taylor.dev@example.com")
    monkeypatch.setenv("CONTACT_PHONE", "+1 415 555 0123")
    monkeypatch.setenv("EMAIL_PASSWORD", "hunter2-long-pass")  # A credential word wins.
    redactor = Redactor.environment()
    assert redactor.credentials == ["hunter2-long-pass"]
    view = {"email": "taylor.dev@example.com", "note": "Call +1 415 555 0123; hunter2-long-pass"}
    assert redactor.mask_credentials(view) == {**view, "note": "Call +1 415 555 0123; [REDACTED]"}
    recorded = json.dumps(redactor.clean_evidence(view))
    assert "taylor.dev" not in recorded and "555 0123" not in recorded and "hunter2" not in recorded
    # Lanes copy environment secrets into agent redactors: copies keep the distinction, and the
    # same text registered as a credential stays one.
    for clone in (Redactor(redactor.secrets), pickle.loads(pickle.dumps(redactor))):
        assert clone.credentials == ["hunter2-long-pass"] and clone.mask_credentials(view)["email"] == view["email"]
    assert Redactor([*redactor.secrets, "+1 415 555 0123"]).credentials == ["hunter2-long-pass", "+1 415 555 0123"]


def test_model_views_keep_local_paths_and_ordinary_url_parameters(monkeypatch):
    url = "https://shop.test/review/1?code=SAVE10&review_token=minted-review-1"
    monkeypatch.setenv("HOME", "/")  # A root home directory must not turn every "/" into "~".
    redactor = Redactor()
    assert redactor.mask_credentials({"url": url}) == {"url": url.replace("minted-review-1", "[REDACTED]")}
    assert redactor.clean_evidence(url) == "https://shop.test/review/1?code=[REDACTED]&review_token=[REDACTED]"
    monkeypatch.setenv("HOME", "/Users/gab")
    paths = "/Users/gab/Downloads/receipt.pdf /Users/gabriel/notes /Users/gab.bak /Users/gab"
    assert redactor.clean_evidence(paths) == "~/Downloads/receipt.pdf /Users/gabriel/notes /Users/gab.bak ~"
    assert redactor.mask_credentials(paths) == paths


async def test_export_keeps_tool_schemas_whose_property_names_look_like_credentials(tmp_path):
    """Schemas are not secrets: the exported Inspect log revalidates with every schema keyword."""
    import uuid

    from inspect_ai import Task, eval_async
    from inspect_ai.agent import react
    from inspect_ai.dataset import Sample
    from inspect_ai.log import read_eval_log
    from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
    from inspect_ai.tool import ToolDef, ToolParams

    from consumer_agent_testing_tool.inspect_logs import export_logs
    from consumer_agent_testing_tool.providers import CapturingAPI

    schema = {"type": "object", "properties": {
        "key": {"type": "string", "description": "Idempotency key."},
        "token": {"type": "string", "description": "Session token."},
        "password": {"type": "string", "description": "Account password."}},
        "required": ["key", "token", "password"], "additionalProperties": False}

    async def login(key: str, token: str, password: str):
        return "accepted"

    class Scripted(ModelAPI):
        turns = 0

        async def generate(self, input, tools, tool_choice, config):
            self.turns += 1
            if self.turns == 1:
                return ModelOutput.for_tool_call("test/schema", "login", {
                    "key": "order-7", "token": "tok-plain-value-1", "password": "pw-plain-value-1"})
            return ModelOutput.from_content("test/schema", "Logged in.")

    run = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                   profile={"name": "synthetic", "surface": "mcp", "label": "synthetic fixture"}, lock={})
    model = Model(CapturingAPI(Scripted("test/schema"), run), GenerateConfig(max_retries=0))
    tool = ToolDef(login, name="login", description="Log in.", parameters=ToolParams.model_validate(schema))
    logs = await eval_async(Task(dataset=[Sample(input="Log in.")], model=model,
                                 solver=react(tools=[tool], model=model, submit=False)),
                            model=model, log_dir=f"memory://schema-export-{uuid.uuid4().hex}",
                            log_format="json", log_model_api=False)
    export_logs(logs, run)
    run.finish("completed")
    exported = export_redacted(run.path, tmp_path / "export")  # EvalLog.model_validate on the redacted log
    log = read_eval_log(str(exported / "inspect/trial-0.eval"))
    tools = [item for event in log.samples[0].events if event.event == "model" for item in event.tools]
    assert tools and all({name: (row.type, row.description) for name, row in item.parameters.properties.items()}
                         == {name: (row["type"], row["description"]) for name, row in schema["properties"].items()}
                         and item.parameters.required == schema["required"] for item in tools)
    # Credential values are still redacted; the schemas that name them are not.
    proposed = next(message for message in log.samples[0].messages if message.role == "assistant").tool_calls[0]
    assert proposed.arguments == {"key": "order-7", "token": "[REDACTED]", "password": "[REDACTED]"}


def test_nesting_past_the_parsers_own_limit_is_replaced_whole(monkeypatch):
    # Before Python 3.14 the JSON parser itself stops near 1,000 levels; the redactor then replaces the
    # balanced value whole instead of leaving it as unparsed text.
    native = json.JSONDecoder.raw_decode

    def shallow(self, text, index=0):
        if text.startswith("[" * 100, index):
            raise RecursionError("maximum recursion depth exceeded")
        return native(self, text, index)
    monkeypatch.setattr(json.JSONDecoder, "raw_decode", shallow)
    redactor = Redactor(["secret-in-the-text"])
    deep = "[" * 3000 + '"never-visible"' + "]" * 3000
    cleaned = redactor.text(f'before {deep} after {{"password": "hidden-value"}} secret-in-the-text')
    assert NESTING_MARKER in cleaned and "never-visible" not in cleaned
    assert cleaned.startswith("before ") and " after " in cleaned
    assert "hidden-value" not in cleaned and "secret-in-the-text" not in cleaned
    # An unbalanced run keeps the budgeted plain-text path (no marker, still fast).
    assert NESTING_MARKER not in redactor.text("[" * 3000 + " tail")
