"""Evidence identity, artifact integrity and export for standalone workflows."""
import json
from datetime import datetime

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, export_redacted
from consumer_agent_testing_tool.replay import compare, export_public_run, playback, seal
from consumer_agent_testing_tool.sealed_events import ENVELOPE_FIELDS, recovery_metadata
from consumer_agent_testing_tool.sealed_report import finalize
from consumer_agent_testing_tool.serialization import file_digest
from consumer_agent_testing_tool.surface import SurfaceRun


async def make_run(root, kind="consumer"):
    if kind == "consumer":
        run = SurfaceRun(root, scenario(), FakeHost(), secrets={"access": "fixture-private-value"})
        assert await run.run() == 0
    else:
        run = BrowserBroker(root, BrowserPolicy(
            url="http://127.0.0.1:8765", origins=["http://127.0.0.1:8765"], rules=[]), host=FakeHost())
        run.record("snapshot", {"text": "synthetic snapshot"})
        run.finish("completed")
    return run


@pytest.mark.parametrize("kind", ["consumer", "browser"])
async def test_standalone_events_retain_clock_identity_provenance_and_notices(tmp_path, kind):
    run = await make_run(tmp_path, kind)
    saved = playback(run.path)
    result = saved["result"]
    assert result["run_id"] == run.path.name
    assert result["lane"] == "contract" and result["evidence_label"] == "protocol-tested"
    assert set(result["locations"]) == {"inference", "browser", "target", "telemetry", "egress"}
    hashes = json.loads((run.path / "evidence.sha256.json").read_text())
    assert result["dependency_notices"]["path"] in hashes
    stamps = []
    for event in saved["events"]:
        assert ENVELOPE_FIELDS.issubset(event)
        assert event["event_id"] == f'{run.path.name}:{event["sequence"]}'
        assert event["run_id"] == run.path.name
        assert event["scenario_id"] == result["scenario_id"]
        assert datetime.fromisoformat(event["wall_timestamp"]).tzinfo is not None
        assert event["parent_id"] is None  # No invented causal attribution.
        assert event["visibility"] == "developer"
        stamps.append(event["monotonic_ns"])
        reference = event["artifacts"][0]
        assert hashes[reference["path"]] == reference["sha256"]
        assert file_digest(run.path / reference["path"]) == reference["sha256"]
        assert b"fixture-private-value" not in (run.path / reference["path"]).read_bytes()
    assert stamps == sorted(stamps)


@pytest.mark.parametrize("damage", [
    "payload", "missing-payload", "notice", "identity", "clock", "naive-time",
    "profile", "flat-payload", "result-version", "result-lane", "added-payload", "invalid-time",
])
async def test_even_resealing_cannot_hide_broken_artifact_or_envelope(tmp_path, damage):
    run = await make_run(tmp_path)
    saved = playback(run.path)
    event, result = saved["events"][0], saved["result"]
    if damage == "payload":
        (run.path / event["artifacts"][0]["path"]).write_text("{}")
    elif damage == "missing-payload":
        (run.path / event["artifacts"][0]["path"]).unlink()
    elif damage == "notice":
        # Replace rather than write in place: the notice is a read-only hard link to the shared store.
        notice = run.path / result["dependency_notices"]["path"]
        notice.unlink()
        notice.write_bytes(b"wrong notice")
    elif damage == "identity":
        event["event_id"] = "another-run:1"
    elif damage == "clock":
        event["monotonic_ns"] = True
    elif damage == "naive-time":
        event["wall_timestamp"] = "2026-10-14T12:00:00"
    elif damage == "profile":
        event["profile"] = "unrelated"
    elif damage == "flat-payload":
        event["discovery"] = "failed"
    elif damage == "added-payload":
        event["unobserved_value"] = "invented"
    elif damage == "invalid-time":
        event["wall_timestamp"] = 17
    elif damage == "result-version":
        result.pop("event_schema_version")
    else:
        result.pop("lane")
    (run.path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in saved["events"]))
    (run.path / "result.json").write_text(json.dumps(result))
    with pytest.raises(LabError):
        seal(run.path)


async def test_export_rewrites_redacted_payload_hashes_but_preserves_original_notices(tmp_path, monkeypatch):
    run = await make_run(tmp_path, "browser")
    run.record("snapshot", {"text": "export-only-secret"})
    run.finish("completed")
    original = playback(run.path)
    source_hash = file_digest(run.path / "evidence.sha256.json")
    monkeypatch.setenv("TEST_EXPORT_SECRET", "export-only-secret")
    target = export_public_run(run.path, tmp_path / "export")
    exported = playback(target)
    assert exported["events"][-1]["artifacts"][0]["sha256"] != original["events"][-1]["artifacts"][0]["sha256"]
    for name in json.loads((target / "evidence.sha256.json").read_text()):
        assert b"export-only-secret" not in (target / name).read_bytes()
    notice = original["result"]["dependency_notices"]["path"]
    assert (target / notice).read_bytes() == (run.path / notice).read_bytes()
    assert file_digest(run.path / "evidence.sha256.json") == source_hash


async def test_resume_keeps_run_identity_and_separates_monotonic_epochs(tmp_path):
    run = await make_run(tmp_path)
    before = playback(run.path)
    resumed = SurfaceRun(tmp_path, scenario(), FakeHost(), resume=run.path)
    assert await resumed.run() == 0
    after = playback(run.path)
    assert after["events"][:len(before["events"])] == before["events"]
    assert after["events"][-1]["run_id"] == before["result"]["run_id"]
    assert after["events"][-1]["clock_id"] != before["events"][-1]["clock_id"]
    assert after["events"][-1]["sequence"] > before["events"][-1]["sequence"]


async def test_diff_ignores_envelope_timestamps_but_keeps_payload_changes(tmp_path):
    first, second = await make_run(tmp_path), await make_run(tmp_path)
    assert compare(first.path, second.path)["milestones"] == []
    second.event("service_response", {"operation": "status", "result": {"changed": True}})
    second.report("completed", None)
    assert compare(first.path, second.path)["milestones"]


async def test_interrupted_browser_retains_observed_metadata_without_guessing_locations(tmp_path):
    run = await make_run(tmp_path, "browser")
    events = playback(run.path)["events"]
    details = recovery_metadata(run.path, events, driver="agent")
    assert all("unknown" in value for value in details["locations"].values())
    finalize(run.path, {**details, "status": "paused", "execution_status": "interrupted"})
    assert playback(run.path)["events"] == events


async def test_payload_cannot_replace_host_metadata_and_remains_visible_to_diff(tmp_path):
    first, second = await make_run(tmp_path, "browser"), await make_run(tmp_path, "browser")
    for run, name in [(first, "pretended-one"), (second, "pretended-two")]:
        run.record("snapshot", {"run_id": name, "event_id": "spoofed", "monotonic_ns": -1})
        run.finish("completed")
        event = playback(run.path)["events"][-1]
        assert event["run_id"] == run.path.name and event["event_id"] != "spoofed"
        assert event["monotonic_ns"] >= 0
    changes = compare(first.path, second.path)["milestones"]
    assert changes and "pretended-one" in json.dumps(changes) and "pretended-two" in json.dumps(changes)


def test_nested_runs_do_not_reuse_directory_basename_as_event_identity(tmp_path):
    identifiers = []
    for group in ["first", "second"]:
        run = BrowserBroker(tmp_path, BrowserPolicy(
            url="http://127.0.0.1:8765", origins=["http://127.0.0.1:8765"], rules=[]),
            host=FakeHost(), path=tmp_path / group / "browser")
        run.record("snapshot", {"text": "fixture"})
        run.finish("completed")
        saved = playback(run.path)
        identifiers.append(saved["events"][0]["run_id"])
        assert saved["result"]["run_id"] == identifiers[-1]
    assert identifiers[0] != identifiers[1]


async def test_public_notice_reference_survives_coincidental_secret_substring(tmp_path, monkeypatch):
    run = await make_run(tmp_path)
    original = playback(run.path)
    notice = original["result"]["dependency_notices"]
    monkeypatch.setenv("TEST_PUBLIC_HASH_SECRET", notice["sha256"][:8])
    target = export_public_run(run.path, tmp_path / "export")
    exported = playback(target)
    assert exported["result"]["dependency_notices"]["sha256"] == notice["sha256"]
    assert exported["result"]["dependency_notices"]["path"] == notice["path"]
    assert (target / notice["path"]).read_bytes() == (run.path / notice["path"]).read_bytes()


@pytest.mark.parametrize("field", ["notice", "payload"])
def test_core_export_reads_original_references_before_redaction(tmp_path, monkeypatch, field):
    run = Evidence(tmp_path, lane="contract", label="protocol-tested",
        profile={"name": "synthetic", "surface": "mcp", "label": "synthetic fixture"}, lock={})
    run.event("observation_emitted", {"text": "fixture"})
    run.finish("completed")
    event = json.loads((run.path / "events.jsonl").read_text().splitlines()[0])
    notice = run.manifest["dependency_notices"]
    selected = notice if field == "notice" else event["artifacts"][0]
    monkeypatch.setenv("TEST_PUBLIC_HASH_SECRET", selected["sha256"][:8])
    target = export_redacted(run.path, tmp_path / "export")
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["dependency_notices"]["sha256"] == notice["sha256"]
    for line in (target / "events.jsonl").read_text().splitlines():
        for reference in json.loads(line)["artifacts"]:
            assert file_digest(target / reference["path"]) == reference["sha256"]
