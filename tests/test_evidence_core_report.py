"""Core run reports (evidence.write_report) and exports (check:reports follow-ups): the failing assertion's
step, the viewers, a sealed sub-run's report kept as sealed, and the standalone export's last-line scan."""
import html
import json
import re
import zipfile

import pytest
from test_api_scenario import confirm, done, draft_call, run
from test_mapped_scenario import buy, call
from test_mapped_scenario import inventory as inventory_fixture
from test_mapped_scenario import run as mapped_run
from test_scenario_runtime import scenario as api_scenario
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import export_redacted, write_report
from consumer_agent_testing_tool.replay import exported_private_values, playback, seal
from consumer_agent_testing_tool.surface import SurfaceRun

PRIVATE = "private-core-export-canary-3b9d"
inventory = inventory_fixture


async def failed_api_run(tmp_path):
    spec = api_scenario(assertions=[{"scorer": "backend.confirmed_count", "expected": 5}])
    evidence, _, checks = await run(tmp_path, [draft_call(), confirm, done()], spec)
    assert evidence.manifest["execution_status"] == "failed"
    assert {"id": "backend.confirmed_count", "status": "failed"}.items() <= next(
        check for check in checks if check["id"] == "backend.confirmed_count").items()
    return evidence


async def test_core_report_names_the_step_of_the_failed_assertion_and_the_viewers(tmp_path):
    evidence = await failed_api_run(tmp_path)
    page = (evidence.path / "report.html").read_text()
    verdict = page[page.index("<h2>Verdict"):page.index("<h2>Checks")]
    found = re.search(r"<dt>Failing step</dt><dd>Step (\d+) · (.*?)</dd>", verdict)
    # The api-tools lane records its checks as state_asserted assertions, not as check events.
    assert found and html.unescape(found[2]).endswith("; check backend.confirmed_count failed")
    section = re.search(rf'<section class="step failed" id="step-{found[1]}">.*?</section>', page, re.DOTALL)
    assert section and "<code>state_asserted</code>" in section[0] and page.count('class="step failed"') == 1
    viewers = page[page.index("<h2>Viewers and linked evidence</h2>"):]
    assert "<li>Inspect View: <code>inspect view --log-dir &lt;run&gt;/inspect</code></li>" in viewers
    assert '<a href="inspect/trial-0.eval">' in viewers


async def test_mapped_core_report_names_the_step_of_the_failed_assertion(inventory, tmp_path):
    inventory.fault = "false_success"  # The service says it booked; the independent oracle counts none.
    evidence, _, _ = await mapped_run(inventory, tmp_path, [call("get_price"), buy, done()])
    assert evidence.manifest["execution_status"] == "failed"
    page = (evidence.path / "report.html").read_text()
    found = re.search(r"<dt>Failing step</dt><dd>Step (\d+) · (.*?)</dd>", page)
    assert found and html.unescape(found[2]).endswith("; check backend.confirmed_count failed")
    assert re.findall(r'<section class="step failed" id="step-(\d+)">', page) == [found[1]]


async def agent_run(tmp_path):
    from test_surface_agent import driver
    planner, _ = driver()
    run_ = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"}, planner=planner)
    assert await run_.run() == 0
    return run_.path, run_.path / run_.state["agent_runs"][-1]


async def test_report_of_a_sealed_sub_run_returns_its_sealed_report_and_keeps_the_parent_seal(tmp_path, capsys):
    parent, child = await agent_run(tmp_path)
    # A run sealed by an earlier renderer: its sub-run report differs from what this version renders.
    earlier = (child / "report.html").read_text().replace("<h2>Timeline</h2>", "<h2>Evidence timeline</h2>")
    (child / "report.html").write_text(earlier)
    seal(parent)
    assert await execute(parser().parse_args(["report", str(child)])) == 0
    assert capsys.readouterr().out.strip() == str(child / "report.html")
    assert (child / "report.html").read_text() == earlier  # Not re-rendered in place.
    assert playback(parent)["integrity"] == "verified"
    (child / "report.html").write_text(earlier + "<!-- changed -->")  # Changed after sealing: fails closed.
    with pytest.raises(LabError, match="integrity check failed"):
        write_report(child)
    assert (child / "report.html").read_text().endswith("<!-- changed -->")
    (child / "report.html").unlink()  # Sealed without a report: nothing is written into the sealed run.
    seal(parent)
    with pytest.raises(LabError, match="sealed in .* without a report") as raised:
        write_report(child)
    assert raised.value.category == "reporting" and not (child / "report.html").exists()
    assert playback(parent)["integrity"] == "verified"


async def test_standalone_core_export_refuses_a_private_value_left_in_its_inspect_log(tmp_path, monkeypatch):
    import inspect_ai.log
    evidence = await failed_api_run(tmp_path)
    (evidence.path / "private" / "canary.private.json").write_text(json.dumps({"secrets": {"canary": PRIVATE}}))
    clean = export_redacted(evidence.path, tmp_path / "clean")
    assert not exported_private_values(clean, [PRIVATE]) and (clean / "inspect" / "trial-0.eval").is_file()
    original = inspect_ai.log.write_eval_log

    def leaky(log, location, *args, **kwargs):  # A redaction gap inside the re-written Inspect log only.
        original(log, location, *args, **kwargs)
        with zipfile.ZipFile(location, "a", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("samples/planted.json", json.dumps({"system": f"Use {PRIVATE} for the account"}))
    monkeypatch.setattr(inspect_ai.log, "write_eval_log", leaky)
    with pytest.raises(LabError, match="private values; nothing was exported") as raised:
        export_redacted(evidence.path, tmp_path / "public")
    assert raised.value.category == "reporting" and PRIVATE not in str(raised.value)
    assert not (tmp_path / "public").exists() and not list(tmp_path.glob(".surface-export-*"))
