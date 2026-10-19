"""The export leak scan reads archive members (Inspect .eval logs, zips), bounded and fail-closed."""
import io
import json
import zipfile

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool import replay
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import export_public_run, exported_private_values, playback
from consumer_agent_testing_tool.serialization import file_digest
from consumer_agent_testing_tool.surface import SurfaceRun

PRIVATE = "private-archive-canary-5d2e"


def archive(members, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as output:
        for name, data in members.items():
            output.writestr(name, data)
    return buffer.getvalue()


async def test_export_refuses_a_private_value_planted_in_an_exported_inspect_log(tmp_path, monkeypatch):
    import inspect_ai.log
    from test_surface_agent import driver
    planner, _ = driver()
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": PRIVATE}, planner=planner)
    assert await run.run() == 0
    assert list(run.path.glob("agent/*/inspect/*.eval"))
    clean = export_public_run(run.path, tmp_path / "clean", secrets=[PRIVATE])
    assert not exported_private_values(clean, [PRIVATE])  # The real Inspect logs are scanned and clean.
    original, planted = inspect_ai.log.write_eval_log, []

    def leaky(log, location, *args, **kwargs):  # A redaction gap inside the re-written Inspect log only.
        original(log, location, *args, **kwargs)
        with zipfile.ZipFile(location, "a", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("samples/planted.json", json.dumps({"system": f"Use {PRIVATE} for the account"}))
        planted.append(location)
    monkeypatch.setattr(inspect_ai.log, "write_eval_log", leaky)
    with pytest.raises(LabError, match="private values") as raised:
        export_public_run(run.path, tmp_path / "public", secrets=[PRIVATE])
    assert raised.value.category == "reporting" and PRIVATE not in str(raised.value)
    assert planted and not (tmp_path / "public").exists()
    assert playback(run.path)["integrity"] == "verified"


def test_scan_reads_zip_and_eval_members_including_nested_archives(tmp_path):
    (tmp_path / "plain.json").write_text('{"ok": true}')
    (tmp_path / "trial-0.eval").write_bytes(archive({"header.json": "{}", "samples/1.json": json.dumps(
        {"input": f"token {PRIVATE}"})}))
    assert PRIVATE.encode() not in (tmp_path / "trial-0.eval").read_bytes()  # Deflated: invisible to a byte scan.
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}
    (tmp_path / "trial-0.eval").unlink()
    (tmp_path / "bundle.zip").write_bytes(archive({"inner/trace.zip": archive({"log.txt": f"{PRIVATE}\n"})}))
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}
    (tmp_path / "bundle.zip").unlink()
    (tmp_path / "unnamed.bin").write_bytes(archive({"note.txt": "4815162342 is a long number"}))  # Zip content.
    assert exported_private_values(tmp_path, [4815162342]) == {"4815162342"}
    assert not exported_private_values(tmp_path, [PRIVATE])


@pytest.mark.parametrize("fault", ["corrupt", "members", "member-size", "depth"])
def test_scan_refuses_an_archive_it_cannot_read_within_its_bounds(tmp_path, monkeypatch, fault):
    data = archive({"a.json": "{}", "b.json": "{}"})
    if fault == "corrupt":
        data = data[:30] + b"\0" * 10 + data[40:]
    elif fault == "members":
        monkeypatch.setattr(replay, "ARCHIVE_MEMBERS", 1)
    elif fault == "member-size":
        monkeypatch.setattr(replay, "ARCHIVE_MEMBER_BYTES", 1)
    else:  # Two nested levels are scanned; a third is refused.
        data = archive({"one.zip": archive({"two.zip": archive({"three.zip": archive({"three.txt": "deep"})})})})
    (tmp_path / "trial-0.eval").write_bytes(data)
    with pytest.raises(LabError, match="trial-0.eval cannot be scanned") as raised:
        exported_private_values(tmp_path, [PRIVATE])
    assert raised.value.category == "reporting"
    assert "a.json" not in str(raised.value) and "three.txt" not in str(raised.value)  # Member names stay out.


def test_scan_bounds_each_archive_by_its_total_uncompressed_size(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "ARCHIVE_BYTES", 10)  # Each member fits its own bound; together they do not.
    (tmp_path / "trial-0.eval").write_bytes(archive({"a.json": '"123456"', "b.json": '"654321"'}))
    with pytest.raises(LabError, match=r"trial-0.eval cannot be scanned .*\(larger than the scan bounds\)") as raised:
        exported_private_values(tmp_path, [PRIVATE])
    assert raised.value.category == "reporting"


def test_scan_refuses_an_encrypted_member_as_such(tmp_path):
    data = bytearray(archive({"a.json": "{}"}))
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # General purpose flag bit 0: encrypted.
        data[data.find(signature) + offset] |= 0x1
    (tmp_path / "trial-0.eval").write_bytes(bytes(data))
    with pytest.raises(LabError, match=r"trial-0.eval cannot be scanned .*\(an encrypted member\)"):
        exported_private_values(tmp_path, [PRIVATE])


def test_scan_follows_a_nested_archive_by_its_content_whatever_its_member_name(tmp_path):
    inner = archive({"log.txt": f"{PRIVATE}\n"})
    (tmp_path / "bundle.zip").write_bytes(archive({"inner/blob.bin": inner}, compression=zipfile.ZIP_STORED))
    assert PRIVATE.encode() not in (tmp_path / "bundle.zip").read_bytes()  # Only the nested member holds it.
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}


def test_scan_reads_an_archive_by_its_name_even_when_its_content_does_not_start_like_one(tmp_path):
    # A readable zip behind a prepended stub (as self-extracting archives are) is still read as an archive.
    data = b"#!synthetic stub\n" + archive({"samples/1.json": json.dumps({"input": f"token {PRIVATE}"})})
    assert data[:4] not in replay.ZIP_MAGIC and PRIVATE.encode() not in data
    (tmp_path / "trial-0.eval").write_bytes(data)
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}
    # An .eval or .zip name over content that is no archive is refused, never read as plain text.
    (tmp_path / "trial-0.eval").write_bytes(b"synthetic bytes that are no archive")
    with pytest.raises(LabError, match=r"trial-0.eval cannot be scanned .*\(not a readable zip archive\)"):
        exported_private_values(tmp_path, [PRIVATE])


def test_scan_skips_only_the_dependency_notices_a_record_declares_and_still_matches(tmp_path):
    notices = archive({"LICENSE.txt": f"Copyright {PRIVATE} contributors"})  # Coincidental public text.
    (tmp_path / "artifacts").mkdir()
    path = tmp_path / "artifacts" / "notices.zip"
    path.write_bytes(notices)
    key = file_digest(path)
    path = path.rename(tmp_path / "artifacts" / f"{key}.zip")
    record = {"dependency_notices": {"sha256": key, "path": f"artifacts/{key}.zip"}}
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}  # Undeclared: scanned like any archive.
    (tmp_path / "result.json").write_text(json.dumps(record))
    assert not exported_private_values(tmp_path, [PRIVATE])
    (tmp_path / "result.json").write_text(json.dumps({"dependency_notices": {**record["dependency_notices"],
                                                                             "sha256": "0" * 64}}))
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}  # A record that does not match is no basis.
    (tmp_path / "result.json").write_text(json.dumps(record))
    path.write_bytes(archive({"LICENSE.txt": f"Changed {PRIVATE}"}))
    assert exported_private_values(tmp_path, [PRIVATE]) == {PRIVATE}  # Changed since it was declared.
