"""Report recovery must never become a second execution or launder changed evidence."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import export_public_run, playback, seal
from consumer_agent_testing_tool.sealed_report import finalize, view
from consumer_agent_testing_tool.serialization import file_digest
from consumer_agent_testing_tool.surface import SurfaceRun


def broker_for(tmp_path, **options):
    return BrowserBroker(tmp_path, BrowserPolicy(
        url="http://localhost:8765", origins=["http://localhost:8765"], rules=[], **options),
        host=SimpleNamespace(), secrets=["private-report-canary"])


def fail_render(*args):
    raise OSError("private-report-canary: failed report destination")


def sealed_bytes(path):
    names = json.loads((path / "evidence.sha256.json").read_text())
    return {name: (path / name).read_bytes() for name in [*names, "evidence.sha256.json"]}


@pytest.mark.parametrize("fault", ["render", "write"])
def test_browser_report_failure_preserves_sealed_execution_and_verification(tmp_path, monkeypatch, fault):
    import consumer_agent_testing_tool.sealed_report as reports
    broker = broker_for(tmp_path)
    broker.assert_backend_count("confirmations", 1, 1, source="independent synthetic oracle")
    if fault == "render":
        monkeypatch.setattr(reports, "render", fail_render)
    else:
        original = reports.atomic_text

        def fail_html(path, text):
            if path.name == "report.html":
                fail_render()
            return original(path, text)

        monkeypatch.setattr(reports, "atomic_text", fail_html)
    broker.finish("completed")
    record = playback(broker.path)
    result = record["result"]
    assert result["execution_status"] == result["verification_status"] == "completed"
    assert result["status"] == result["reporting_status"] == "failed"
    assert result["verified_service_outcome"] == {"confirmations": 1}
    assert result["html_report"]["error_class"] == "OSError"
    assert "private-report-canary" not in json.dumps(record)
    assert not (broker.path / "report.html").exists()
    assert not list(broker.path.glob(".report-*.tmp"))


async def test_consumer_report_recovery_is_offline_and_does_not_repeat_saved_effect(tmp_path, monkeypatch, capsys):
    import consumer_agent_testing_tool.sealed_report as reports
    host = FakeHost()
    run = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "private-report-canary"})
    with monkeypatch.context() as patch:
        patch.setattr(reports, "render", fail_render)
        assert await run.run() == 2
    before = sealed_bytes(run.path)
    calls = list(host.calls)
    source = playback(run.path)["result"]
    assert source["execution_status"] == "completed" and source["reporting_status"] == "failed"
    assert not source["compatibility_pass"]
    assert source["checks"][0]["status"] == "passed"
    assert await execute(parser().parse_args(["report", str(run.path)])) == 0
    report = Path(capsys.readouterr().out.strip())
    assert report.is_file() and not report.is_relative_to(run.path)
    assert view(run.path) == report
    assert view(report.parent) == report
    copy = playback(report.parent)["result"]
    assert copy["export"]["reporting_status"] == "completed"
    assert copy["export"]["rendering_harness"]["implementation"]
    assert copy["export"]["source_seal_sha256"] == file_digest(run.path / "evidence.sha256.json")
    assert copy["reporting_status"] == "failed"  # Historical run status is not rewritten.
    assert copy["execution_status"] == "completed" and not copy["compatibility_pass"]
    assert sealed_bytes(run.path) == before and host.calls == calls
    assert host.rendered == 1 and sum(name == "save" for name, _ in calls) == 1
    assert not list(report.parent.rglob("*.private.json"))


def test_process_exit_during_render_retains_a_verified_pending_report(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    program = (
        "import os,sys; from pathlib import Path; import consumer_agent_testing_tool.sealed_report as r; "
        "r.render=lambda *args: os._exit(23); "
        "r.finalize(Path(sys.argv[1]), {'status':'completed','checks':[{'actual':1}],'compatibility_pass':False})"
    )
    process = subprocess.run([sys.executable, "-c", program, str(output)],
        env={name: value for name, value in os.environ.items()
             if name in ("PATH", "SYSTEMROOT", "WINDIR")},
        capture_output=True, timeout=15, check=False)
    assert process.returncode == 23, process.stderr.decode()
    before = sealed_bytes(output)
    source = playback(output)["result"]
    assert source["execution_status"] == "completed" and source["reporting_status"] == "pending"
    assert source["status"] == "blocked" and source["checks"] == [{"actual": 1}]
    assert view(output).is_file()
    assert sealed_bytes(output) == before


@pytest.mark.parametrize("fault", ["result", "events", "report", "descriptor"])
def test_report_recovery_rejects_tampering_or_unrecorded_missing_html(tmp_path, monkeypatch, fault):
    import consumer_agent_testing_tool.sealed_report as reports
    broker = broker_for(tmp_path)
    if fault != "report":
        monkeypatch.setattr(reports, "render", fail_render)
    broker.finish("completed")
    if fault == "events":
        (broker.path / "events.jsonl").write_text('{"sequence":1}\n')
    elif fault in ("result", "descriptor"):
        path = broker.path / "result.json"
        result = json.loads(path.read_text())
        if fault == "result":
            result["verified_service_outcome"] = {"invented": 1}
        else:
            del result["html_report"]
        path.write_text(json.dumps(result))
        if fault == "descriptor":
            seal(broker.path)  # Even a self-consistent old-style seal needs its report.
    else:
        (broker.path / "report.html").unlink()
    with pytest.raises(LabError, match="integrity|incomplete"):
        view(broker.path)
    assert not (broker.path.parent / f"{broker.path.name}.reports").exists()


def test_report_copy_failure_is_retryable_and_does_not_modify_source(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.sealed_report as reports
    broker = broker_for(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(reports, "render", fail_render)
        broker.finish("completed")
    before = sealed_bytes(broker.path)
    original = Path.write_text

    def fail_copy(path, *args, **kwargs):
        if path.name == "report.html":
            fail_render()
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_copy)
        with pytest.raises(OSError):
            view(broker.path)
    assert sealed_bytes(broker.path) == before
    copies = broker.path.parent / f"{broker.path.name}.reports"
    assert not list(copies.iterdir())
    assert view(broker.path).is_file()


def test_report_copy_rejects_links_and_corrupted_existing_copy(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.sealed_report as reports
    broker = broker_for(tmp_path)
    monkeypatch.setattr(reports, "render", fail_render)
    broker.finish("completed")
    copies = broker.path.parent / f"{broker.path.name}.reports"
    other = tmp_path / "other"
    other.mkdir()
    copies.symlink_to(other, target_is_directory=True)
    with pytest.raises(LabError, match="links"):
        view(broker.path)
    copies.unlink()
    report = view(broker.path)
    report.write_text("Changed cached report")
    with pytest.raises(LabError, match="integrity"):
        view(broker.path)


def test_report_recovery_does_not_hide_failed_diagnostics_or_failed_backend(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.sealed_report as reports
    broker = broker_for(tmp_path, diagnostics={"enabled": True})
    broker.diagnostics.prepare()
    broker.assert_backend_count("confirmations", 0, 1, source="independent synthetic oracle")
    monkeypatch.setattr(reports, "render", fail_render)
    broker.finish("completed")
    source = playback(broker.path)["result"]
    assert source["execution_status"] == "completed" and source["verification_status"] == "failed"
    assert source["diagnostic_reporting_status"] == "failed"
    copy = export_public_run(broker.path, tmp_path / "copy")
    result = playback(copy)["result"]
    assert result["status"] == result["reporting_status"] == "failed"
    assert result["verified_service_outcome"] is None
    assert result["export"]["reporting_status"] == "completed"
    assert not (copy / "diagnostics.private").exists()


def test_successful_report_replaces_files_atomically_and_escapes_text(tmp_path):
    result = finalize(tmp_path, {"status": "completed", "detail": "<script>bad()</script>",
                                "compatibility_pass": True})
    assert result["status"] == result["reporting_status"] == "completed"
    assert result["compatibility_pass"]
    assert "<script>" not in (tmp_path / "report.html").read_text()
    assert playback(tmp_path)["result"] == result
    assert view(tmp_path) == tmp_path / "report.html"


def test_failed_new_report_does_not_serve_a_stale_successful_html(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.sealed_report as reports
    finalize(tmp_path, {"status": "completed", "detail": "First execution"})
    old_html = (tmp_path / "report.html").read_bytes()
    monkeypatch.setattr(reports, "render", fail_render)
    finalize(tmp_path, {"status": "blocked", "detail": "Second execution paused"})
    assert (tmp_path / "report.html").read_bytes() == old_html
    report = view(tmp_path)
    assert report != tmp_path / "report.html"
    assert "Second execution paused" in report.read_text()
    assert playback(tmp_path)["result"]["execution_status"] == "blocked"


@pytest.mark.parametrize("fault,code", [("html", 2), ("backend", 1)])
async def test_browser_cli_propagates_reporting_and_backend_failure_codes(tmp_path, monkeypatch, fault, code):
    import consumer_agent_testing_tool.sealed_report as reports
    from consumer_agent_testing_tool import browser
    broker = broker_for(tmp_path)
    broker.assert_backend_count("confirmations", 0 if fault == "backend" else 1, 1,
                                source="independent synthetic oracle")

    async def open_page():
        return {"generation": 1, "text": "Synthetic page"}

    async def close_page():
        pass

    monkeypatch.setattr(broker, "open", open_page)
    monkeypatch.setattr(broker, "close", close_page)
    monkeypatch.setattr(browser, "BrowserBroker", lambda *args, **kwargs: broker)
    if fault == "html":
        monkeypatch.setattr(reports, "render", fail_render)
    policy = tmp_path / "policy.json"
    policy.write_text(broker.policy.model_dump_json())
    assert await execute(parser().parse_args(["browse", str(policy)])) == code
    assert playback(broker.path)["result"]["execution_status"] == "completed"


@pytest.mark.parametrize("reseal", [False, True])
def test_export_rejects_source_changes_during_rendering(tmp_path, monkeypatch, reseal):
    source, destination = tmp_path / "source", tmp_path / "export"
    source.mkdir()
    finalize(source, {"status": "completed"})
    original = Path.write_text

    def change_during_export(path, *args, **kwargs):
        if path.name == "report.html" and path.parent != source:
            result = json.loads((source / "result.json").read_text())
            result["detail"] = "Changed by another writer"
            original(source / "result.json", json.dumps(result))
            if reseal:
                seal(source)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", change_during_export)
    with pytest.raises(LabError, match="integrity|changed during export"):
        export_public_run(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".surface-export-*"))


def test_export_requires_the_callers_expected_source_seal(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    finalize(source, {"status": "completed"})
    with pytest.raises(LabError, match="changed before export"):
        export_public_run(source, tmp_path / "export", source_seal_sha256="old-seal")
    assert not (tmp_path / "export").exists()


def test_successful_report_copy_cannot_omit_its_html_even_with_a_new_seal(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.sealed_report as reports
    broker = broker_for(tmp_path)
    monkeypatch.setattr(reports, "render", fail_render)
    broker.finish("completed")
    report = view(broker.path)
    report.unlink()
    seal(report.parent)
    with pytest.raises(LabError, match="incomplete"):
        playback(report.parent)
