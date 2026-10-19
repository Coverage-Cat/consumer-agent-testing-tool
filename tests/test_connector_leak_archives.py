"""The leak scan reads zip archives (Inspect .eval logs) member by member within bounds: a secret registered after
an archive was written is found in it, numbers there do not count, the harness's public dependency notices are
exempt, and an archive the scan cannot read whole makes the run unshareable."""
import json
import zipfile
from typing import ClassVar

from test_connector_support import (
    BaseExtension,
    agent,
    booking_responses,
    result_of,
    run_connector,
    scenario,
)

from consumer_agent_testing_tool import connector_run
from consumer_agent_testing_tool.connector_run import EXTENSIONS, NoConfig

LATE = "late-secret-value-91c7e2"  # registered after the archives were written


def archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, text in members.items():
            handle.writestr(name, text)
    return path


class Archiver(BaseExtension):
    """Writes an archive into the run at the end, holding a value it registers only afterwards."""
    config_model = NoConfig
    members: ClassVar[dict] = {}

    async def on_final(self, run, final_text):
        archive(run.path / "extension-log" / "trace.eval", Archiver.members)
        run.register_private("late_value", LATE)
        return []


async def test_a_secret_inside_an_archive_member_makes_the_run_unshareable(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-archiver", Archiver)
    monkeypatch.setattr(Archiver, "members", {"samples/1.json": json.dumps({"messages": [{"text": f"key {LATE}"}]})})
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-archiver": {}}))
    assert code == 2
    result = result_of(run)
    assert result["reporting_status"] == "failed" and result["failure_class"] == "reporting"
    assert result["detail"].startswith("1 registered secret value(s) appear in this run's public evidence")
    assert LATE in json.loads((run.path / "private" / "connector.private.json").read_text())["secrets"]


async def test_numbers_in_archive_members_are_the_logs_own_and_text_files_count_text_only(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-archiver", Archiver)
    # 4321 is a private number of this scenario: as an Inspect log's token count or plain text it is no leak.
    monkeypatch.setattr(Archiver, "members", {"samples/1.json": json.dumps({"usage": {"input_tokens": 4321}}),
                                              "notes.txt": "4321 tokens"})
    document = scenario(extensions={"probe-archiver": {}})
    document["private"]["pin"] = 4321
    run, code, _ = await run_connector(tmp_path, document)
    assert code == 0, run.summary()
    assert result_of(run)["reporting_status"] == "completed" and run.unscanned == []


async def test_an_archive_beyond_the_scan_s_bounds_is_never_called_clean(tmp_path, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-archiver", Archiver)
    monkeypatch.setattr(Archiver, "members", {"samples/1.json": json.dumps({"text": "x" * 4096})})
    monkeypatch.setattr(connector_run, "ARCHIVE_BYTES", 1024)
    run, code, _ = await run_connector(tmp_path, scenario(extensions={"probe-archiver": {}}))
    assert code == 2
    detail = result_of(run)["detail"]
    assert "1 archive(s) in its public evidence could not be scanned for registered secrets" in detail
    assert "extension-log/trace.eval" in detail and "registered secret value(s) appear" not in detail


async def test_a_corrupt_archive_is_unscannable(tmp_path):
    run, code, _ = await run_connector(tmp_path)
    assert code == 0, run.summary()
    (run.path / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")
    assert run.leaked() == set() and run.unscanned == ["broken.zip"]


async def test_agent_inspect_logs_are_scanned_and_the_dependency_notices_are_exempt(tmp_path):
    driver, _ = agent(booking_responses())
    run, code, _ = await run_connector(tmp_path, driver=driver)
    assert code == 0, run.summary()
    logs = list(run.path.rglob("*.eval"))
    notices = [path for path in run.path.rglob("*.zip") if path.parent.name == "artifacts"]
    assert logs and notices  # An agent run keeps its Inspect log, and runs keep the public notices.
    assert run.leaked() == set() and run.unscanned == []
    # A value the model saw that becomes a secret later is found inside the log, not only in events.
    run.redactor.add(["Garden double"])
    found = run.leaked()
    assert "Garden double" in found
    for path in run.path.rglob("*"):
        if path.is_file() and path.suffix not in (".eval", ".zip"):
            path.write_text(path.read_text(errors="ignore").replace("Garden double", "room"))
    assert run.leaked() == {"Garden double"}  # Only the archive holds it now.
