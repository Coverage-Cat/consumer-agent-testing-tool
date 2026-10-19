"""P0 notices: one shared copy of the public notices archive instead of 3.3 MB per evidence directory,
held in a harness-owned store so that regenerating the distribution archive cannot touch evidence."""
import errno
import os
import shutil
import stat

import pytest
from test_surface import FakeHost, scenario

import consumer_agent_testing_tool.evidence as evidence_module
from consumer_agent_testing_tool.evidence import (
    Evidence,
    dependency_notice,
    notice_store,
    notices_source,
    share_public_file,
)
from consumer_agent_testing_tool.replay import export_public_run, playback
from consumer_agent_testing_tool.serialization import file_digest
from consumer_agent_testing_tool.surface import SurfaceRun

PROFILE = {"name": "synthetic", "surface": "mcp", "label": "synthetic fixture"}


def source_notices():
    return notices_source()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """A private stand-in for the distribution archive that tests may rewrite in place."""
    path = tmp_path / "third_party" / "notices.zip"
    path.parent.mkdir()
    shutil.copyfile(source_notices(), path)
    monkeypatch.setattr(evidence_module, "notices_source", lambda: path)
    return path


def test_notices_are_hard_linked_with_unchanged_reference(tmp_path):
    original = source_notices()
    before = file_digest(original)
    run = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=PROFILE, lock={})
    reference = run.manifest["dependency_notices"]
    copy = run.path / reference["path"]
    stored = tmp_path / ".cache/notices" / f"{before}.zip"
    assert reference["sha256"] == before and reference["path"] == f"artifacts/{before}.zip"
    assert notice_store(run.path) == tmp_path / ".cache/notices"
    assert not os.path.samefile(original, copy)  # Never linked to the mutable distribution archive.
    assert os.path.samefile(stored, copy)  # No second 3.3 MB copy on this filesystem.
    assert stat.S_IMODE(os.stat(stored).st_mode) == 0o444
    assert file_digest(copy) == before
    assert dependency_notice(run.path) == reference  # Repeated calls replace, never write through.
    assert file_digest(original) == before
    # Sub-runs and browser segments share the store of their outermost run.
    child = Evidence(run.path / "agent", lane="model preflight", label="probe", profile=PROFILE, lock={})
    assert notice_store(child.path) == notice_store(run.path / "browser") == tmp_path / ".cache/notices"
    assert os.path.samefile(stored, child.path / reference["path"])


async def test_rewriting_the_source_archive_in_place_cannot_change_sealed_runs(tmp_path, archive):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    notice = playback(run.path)["result"]["dependency_notices"]
    replacement = tmp_path / "regenerated.zip"
    replacement.write_bytes(b"regenerated notices")
    shutil.copyfile(replacement, archive)  # Opens the existing archive and truncates it in place.
    assert playback(run.path)["integrity"] == "verified"
    assert file_digest(run.path / notice["path"]) == notice["sha256"] != file_digest(archive)
    target = export_public_run(run.path, tmp_path / "export")
    assert playback(target)["result"]["dependency_notices"]["sha256"] == notice["sha256"]
    # New runs reference the regenerated archive through a new store entry.
    later = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=PROFILE, lock={})
    assert later.manifest["dependency_notices"]["sha256"] == file_digest(archive)
    assert playback(run.path)["integrity"] == "verified"


def test_a_damaged_store_entry_is_replaced_not_reused(tmp_path, archive):
    first = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=PROFILE, lock={})
    reference = first.manifest["dependency_notices"]
    stored = notice_store(first.path) / f"{reference['sha256']}.zip"
    stored.chmod(0o644)
    stored.write_bytes(b"damaged")  # Damages every run linked to it, never the source.
    assert file_digest(archive) == reference["sha256"]
    second = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=PROFILE, lock={})
    copy = second.path / second.manifest["dependency_notices"]["path"]
    assert file_digest(copy) == reference["sha256"] and not os.path.samefile(copy, first.path / reference["path"])


def test_an_unavailable_store_copies_instead_of_linking_the_source(tmp_path, archive, monkeypatch):
    def unavailable(*args):
        raise PermissionError("read-only workspace")

    monkeypatch.setattr(evidence_module, "stored_public_file", unavailable)
    run = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=PROFILE, lock={})
    copy = run.path / run.manifest["dependency_notices"]["path"]
    assert not os.path.samefile(archive, copy) and os.stat(copy).st_nlink == 1
    assert file_digest(copy) == file_digest(archive) and stat.S_IMODE(os.stat(copy).st_mode) == 0o444


def test_notices_fall_back_to_a_copy_across_filesystems(tmp_path, monkeypatch):
    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(evidence_module.os, "link", cross_device)
    run = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested", profile=PROFILE, lock={})
    copy = run.path / run.manifest["dependency_notices"]["path"]
    assert not os.path.samefile(source_notices(), copy)
    assert file_digest(copy) == run.manifest["dependency_notices"]["sha256"]
    assert not list(copy.parent.glob(".*.tmp"))


def test_sharing_replaces_an_existing_destination_without_writing_through(tmp_path):
    first, second, shared = tmp_path / "first", tmp_path / "second", tmp_path / "shared"
    first.write_bytes(b"first public file")
    second.write_bytes(b"second public file")
    share_public_file(first, shared)
    share_public_file(second, shared)
    assert first.read_bytes() == b"first public file" and shared.read_bytes() == b"second public file"


async def test_sealed_run_and_export_share_the_notice(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    notice = playback(run.path)["result"]["dependency_notices"]
    target = export_public_run(run.path, tmp_path / "export")
    assert playback(target)["result"]["dependency_notices"]["sha256"] == notice["sha256"]
    assert os.path.samefile(run.path / notice["path"], target / notice["path"])


def test_notice_store_stays_in_the_workspace_and_out_of_the_runs_directory(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "artifacts").mkdir(parents=True)  # A project directory, not a run.
    run = workspace / "runs" / ("surface-" + "a" * 32)
    (run / "agent").mkdir(parents=True)
    (run / "checkpoint.private.json").write_text("{}")
    for path in (run, run / "agent" / ("r-" + "b" * 32), run / "browser", run / "browser-attempt-2"):
        assert notice_store(path) == workspace / ".cache/notices"
    (workspace / "consumer-agent-testing-tool.yaml").write_text("{}")
    assert notice_store(workspace / ("r-" + "c" * 32)) == workspace / ".cache/notices"  # --output .


def test_inventory_replaces_the_notices_archive_without_writing_through_links(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import inventory
    archive = tmp_path / "third_party" / "notices.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"previous notices")
    linked = tmp_path / "runs" / "artifacts" / "notices.zip"
    linked.parent.mkdir(parents=True)
    os.link(archive, linked)  # Older evidence shares the archive's inode.
    generated = tmp_path / "stage" / "notices.zip"
    generated.parent.mkdir()
    generated.write_bytes(b"regenerated notices")
    inventory.replace_file(generated, archive)
    assert archive.read_bytes() == b"regenerated notices"
    assert linked.read_bytes() == b"previous notices" and not os.path.samefile(archive, linked)
    assert [path.name for path in archive.parent.iterdir()] == ["notices.zip"]  # No temporary left behind.
