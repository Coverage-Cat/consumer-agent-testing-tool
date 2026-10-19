import asyncio
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_browser_portals import portal as portal_fixture

from consumer_agent_testing_tool.browser import BrowserBroker, BrowserPolicy
from consumer_agent_testing_tool.browser_diagnostics import (
    DiagnosticPolicy,
    diagnostic_command,
    verify_diagnostics,
)
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import export_public_run, playback

portal = portal_fixture


def child_environment(tmp_path):
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    env.update(HOME=str(tmp_path), TMPDIR=".cache/surface-host",
               PLAYWRIGHT_BROWSERS_PATH=str(Path.cwd() / ".cache/ms-playwright"),
               PWTEST_UNDER_TEST="1")
    return env


async def test_private_screenshots_trace_pairing_and_public_export(portal, tmp_path, capsys):
    snapshot = await portal.open("validation", diagnostics={"enabled": True, "retain_raw_trace": True})
    result = await portal.action(snapshot, "Submit")
    assert "Contact is required" not in result["text"]
    assert "diagnostics" not in result and "trace" not in result
    assert len(portal.broker.diagnostics.captures) == 2
    assert all(row["status"] == "captured" for row in portal.broker.diagnostics.captures)
    # The canvas changes visually while its validation message remains absent from
    # the accessibility snapshot. These are independently captured observations.
    assert len({row["sha256"] for row in portal.broker.diagnostics.captures}) == 2
    assert (await portal.oracle())["effects"] == []
    await portal.broker.close()
    portal.broker.finish("completed")
    directory, manifest, public = verify_diagnostics(portal.broker.path)
    assert public["execution_status"] == public["reporting_status"] == "completed"
    assert manifest["trace"]["status"] == "captured"
    assert not (directory / ".work").exists()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for item in directory.iterdir():
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    with zipfile.ZipFile(directory / manifest["trace"]["path"]) as trace:
        names = trace.namelist()
        assert any(name.endswith(".trace") for name in names)
        assert any(name.startswith("resources/") for name in names)
        assert not any(name.startswith("resources/src@") for name in names)
    assert "Generation 1" in (directory / "index.html").read_text()
    assert "Content-Security-Policy" in (directory / "index.html").read_text()
    command = diagnostic_command(Path.cwd(), portal.broker.path, trace=True)["argv"]
    assert command[0] == "node" and "npx" not in command and command[-4:] == [
        "--host", "127.0.0.1", "--port", "0"]
    viewer = await asyncio.create_subprocess_exec(*command, env=child_environment(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        for _ in range(10):
            line = await asyncio.wait_for(viewer.stdout.readline(), 15)
            if match := re.search(rb"Listening on (http://127\.0\.0\.1:\d+)", line):
                break
            if not line:
                pytest.fail((await viewer.stderr.read()).decode())
        else:
            pytest.fail("Playwright Trace Viewer did not announce its loopback listener")
        probe = await asyncio.create_subprocess_exec(
            "node", "tests/browser_diagnostics_probe.mjs", "viewer", match[1].decode(),
            env=child_environment(tmp_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(probe.communicate(), 25)
        finally:
            if probe.returncode is None:
                probe.terminate()
                await asyncio.wait_for(probe.wait(), 5)
        assert probe.returncode == 0, error.decode()
        view = json.loads(output)
        (tmp_path / "viewer-snapshot.txt").write_text(view["text"])
        (tmp_path / "viewer-requests.json").write_text(json.dumps(view["responses"], indent=2))
        assert view["loaded_trace"] and "Actions" in view["text"], view
    finally:
        if viewer.returncode is None:
            viewer.terminate()
        await asyncio.wait_for(viewer.wait(), 5)
    await execute(parser().parse_args(["browser-diagnostics", str(portal.broker.path)]))
    assert json.loads(capsys.readouterr().out)["status"] == "captured"
    output = tmp_path / "public"
    await execute(parser().parse_args(["export", str(portal.broker.path), "--output", str(output)]))
    exported = playback(output)["result"]
    assert exported["diagnostics"]["status"] == "excluded"
    assert not (output / "diagnostics.private").exists()
    assert "diagnostics.private/" not in (output / "report.html").read_text()
    assert not list(output.rglob("*.png"))
    # The sole public ZIP is the verified dependency notice archive; the
    # independently retained Playwright trace must still be excluded.
    assert list(output.rglob("*.zip")) == [output / exported["dependency_notices"]["path"]]
    assert not (output / "diagnostics.private" / manifest["trace"]["path"]).exists()
    screenshot = directory / manifest["captures"][0]["path"]
    screenshot.write_bytes(b"changed")
    with pytest.raises(LabError, match="integrity"):
        verify_diagnostics(portal.broker.path)
    # Private-data corruption is visible to diagnostic verification, not a
    # reason to claim previously sealed public observations changed.
    assert playback(portal.broker.path)["integrity"] == "verified"


async def test_pixel_masks_cover_form_fields_sensitive_regions_and_iframes(portal, tmp_path):
    await portal.open("diagnostic-masks", reads=["/mask-frame"], diagnostics={"enabled": True})
    row = portal.broker.diagnostics.captures[0]
    assert row["status"] == "captured"
    image = portal.broker.diagnostics.directory / row["path"]
    probe = await asyncio.to_thread(subprocess.run,
        ["node", "tests/browser_diagnostics_probe.mjs", "pixels", str(image)],
        env=child_environment(tmp_path), text=True, capture_output=True, timeout=15, check=False)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == [[255, 0, 255, 255]] * 3


async def test_capture_limits_preserve_execution_and_report_missing_evidence(portal):
    snapshot = await portal.open("dialog", diagnostics={"enabled": True, "max_screenshots": 1})
    await portal.action(snapshot, "Open dialog")
    await portal.broker.close()
    portal.broker.finish("completed")
    directory, manifest, result = verify_diagnostics(portal.broker.path)
    assert result["execution_status"] == "completed"
    assert result["reporting_status"] == "failed" and result["status"] == "failed"
    assert manifest["captures"][1]["status"] == "limit-exceeded"
    assert manifest["trace"]["status"] == "disabled"
    assert len(list(directory.glob("*.png"))) == 1
    assert not list(directory.glob("*.zip"))


async def test_diagnostic_failure_is_not_returned_to_agent(portal, monkeypatch):
    snapshot = await portal.open("dialog", diagnostics={"enabled": True})
    request = portal.broker.host.request

    async def failed(method, arguments):
        if method == "browse_diagnostics_capture":
            raise OSError("Synthetic private diagnostic detail")
        return await request(method, arguments)

    monkeypatch.setattr(portal.broker.host, "request", failed)
    result = await portal.action(snapshot, "Open dialog")
    assert 'dialog "Confirm reservation"' in result["text"]
    assert "Synthetic private diagnostic detail" not in json.dumps(result)
    for action in ({"kind": "screenshot"}, {"kind": "browse_diagnostics_capture"},
                   {"kind": "snapshot", "mask_selectors": []}):
        with pytest.raises(LabError):
            await portal.broker.action(action)
    await portal.broker.close()
    portal.broker.finish("completed")
    assert playback(portal.broker.path)["result"]["reporting_status"] == "failed"


def test_default_diagnostics_are_disabled_and_raw_trace_requires_explicit_capture(tmp_path):
    assert not DiagnosticPolicy().enabled
    for values in ({"retain_raw_trace": True}, {"mask_selectors": ["input"]}, {"enabled": "true"}):
        with pytest.raises(ValueError):
            DiagnosticPolicy(**values)
    policy = BrowserPolicy(url="http://localhost:8000", origins=["http://localhost:8000"], rules=[])
    broker = BrowserBroker(tmp_path, policy, host=SimpleNamespace())
    broker.finish("completed")
    assert not broker.diagnostics.directory.exists()
    assert playback(broker.path)["result"]["diagnostics"]["status"] == "disabled"
    with pytest.raises(LabError, match="no retained"):
        verify_diagnostics(broker.path)
    output = export_public_run(broker.path, tmp_path / "export")
    assert playback(output)["result"]["diagnostics"]["status"] == "excluded"
    alias = tmp_path / "alias"
    alias.symlink_to(broker.path, target_is_directory=True)
    with pytest.raises(LabError, match="links"):
        export_public_run(alias, tmp_path / "alias-export")
    with pytest.raises(LabError, match="outside"):
        export_public_run(broker.path, alias / "nested")


def test_unstarted_or_unfinalized_capture_is_not_a_diagnostic_pass(tmp_path):
    policy = BrowserPolicy(url="http://localhost:8000", origins=["http://localhost:8000"], rules=[],
                           diagnostics={"enabled": True, "retain_raw_trace": True})
    broker = BrowserBroker(tmp_path, policy, host=SimpleNamespace())
    broker.diagnostics.prepare()
    broker.finish("completed")
    _, manifest, result = verify_diagnostics(broker.path)
    assert result["execution_status"] == "completed" and result["reporting_status"] == "failed"
    assert {row["category"] for row in manifest["errors"]} == {"NotFinalized", "NoObservations"}


async def test_retained_media_budget_and_invalid_masks_preserve_observation(portal):
    snapshot = await portal.open("dialog", diagnostics={"enabled": True, "max_bytes": 1024})
    assert 'button "Open dialog"' in snapshot["text"]
    assert portal.broker.diagnostics.captures[0]["status"] == "limit-exceeded"
    assert not list(portal.broker.diagnostics.directory.glob("*.png"))
    await portal.broker.close()
    snapshot = await portal.open("dialog", diagnostics={"enabled": True, "mask_selectors": ["["]})
    assert 'button "Open dialog"' in snapshot["text"]
    assert portal.broker.diagnostics.errors
    assert not list(portal.broker.diagnostics.directory.glob("*.png"))


def test_duplicate_media_does_not_consume_the_retention_budget_twice(tmp_path):
    directory = tmp_path / "diagnostics.private"
    directory.mkdir(mode=0o700)
    result = subprocess.run(["node", "tests/browser_diagnostics_probe.mjs", "quota", str(directory)],
        env=child_environment(tmp_path), capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "statuses": ["captured", "captured", "limit-exceeded"], "bytes": 1024, "files": 1}
