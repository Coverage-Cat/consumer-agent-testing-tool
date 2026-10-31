"""Real loopback suites, process ownership, interruption, and the public/private report boundary."""
import asyncio
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_ci_plan import suite_document
from test_connector_start import gone, listening

from consumer_agent_testing_tool.ci_results import SuiteReport, load_report
from consumer_agent_testing_tool.ci_run import run_suite, snapshot
from consumer_agent_testing_tool.ci_suite import prepare_suite
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.serialization import load

ROOT = Path(__file__).resolve().parents[1]


def fixture_inputs(root, *, count=2, command=None):
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "examples/connector/fixture-booking.yaml", root / "scenario.yaml")
    shutil.copyfile(ROOT / "examples/ci/booking.calls.yaml", root / "calls.yaml")
    suite = suite_document()
    suite["services"]["hotel"]["command"] = command or [
        sys.executable, "-m", "consumer_agent_testing_tool.connector_fixture",
        "--port", "{port}", "--variant", "good", "--auth", "none"]
    suite["services"]["hotel"]["ready_url"] = "http://127.0.0.1:{port}/mcp"
    suite["cases"] = [{"id": f"case-{index}", "scenario": "scenario.yaml", "script": "calls.yaml", "service": "hotel",
                       "matrix": {"emulation_target": ["muse" if index % 2 else "instinct"],
                                  "projection": ["native" if index % 2 else "cli"]}}
                      for index in range(count)]
    path = root / "suite.json"
    path.write_text(json.dumps(suite))
    return path, suite


def barrier_command(directory, *, participants=2, hang=False, descendants=False):
    # Each service records ownership before binding. Neither can proceed unless both are alive.
    program = f"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
directory.mkdir(exist_ok=True)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)']) if {descendants!r} else None
record = {{'pid': os.getpid(), 'port': int(os.environ['PORT']), 'namespace': os.environ['CATT_CASE_ID'],
           'run': os.environ['CATT_RUN_ID'], 'child': child.pid if child else None}}
(directory / (record['namespace'] + '.json')).write_text(json.dumps(record))
print('RAW-SERVICE-LOG-PRIVATE-7d0b4', flush=True)
deadline = time.monotonic() + 90
while len(list(directory.glob('*.json'))) < {participants} and time.monotonic() < deadline:
    time.sleep(0.05)
if len(list(directory.glob('*.json'))) < {participants}:
    raise SystemExit(42)
if {hang!r}:
    time.sleep(600)
else:
    sys.argv = ['fixture', '--port', os.environ['PORT'], '--variant', 'good', '--auth', 'none']
    from consumer_agent_testing_tool.connector_fixture import main
    main()
"""
    return [sys.executable, "-c", program, str(directory)]


async def test_real_cases_overlap_and_have_distinct_ports_state_and_private_namespaces(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, _ = fixture_inputs(tmp_path, command=barrier_command(tmp_path / "ownership", descendants=True))
    prepared = prepare_suite(path, root=tmp_path, jobs=2)
    report = await run_suite(prepared, public=tmp_path / "reports", private=tmp_path / "private", jobs=2)
    assert report.exit_code == 0, report.model_dump()
    records = [json.loads(item.read_text()) for item in (tmp_path / "ownership").glob("*.json")]
    assert len(records) == len({row["namespace"] for row in records}) == len({row["port"] for row in records}) == 2
    assert len({row["run"] for row in records}) == 1
    assert all(gone(row["pid"]) and gone(row["child"]) and not listening(row["port"]) for row in records)
    assert all(next(check for check in row.checks if check.id == "one-confirmed-booking").status == "passed"
               for row in report.cases)
    public = "\n".join(file.read_text() for file in (tmp_path / "reports").iterdir())
    assert "taylor@example.com" not in public and "RAW-SERVICE-LOG-PRIVATE-7d0b4" not in public
    assert {file.name for file in (tmp_path / "reports").iterdir()} == {"report.json", "junit.xml"}
    assert load_report(tmp_path / "reports/report.json") == report
    assert all((tmp_path / "private" / report.report_id / row.id).stat().st_mode & 0o077 == 0 for row in report.cases)


async def test_invalid_late_input_emits_an_error_report_and_starts_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path, suite = fixture_inputs(tmp_path, command=barrier_command(tmp_path / "ownership"))
    suite["cases"][-1]["script"] = "missing.json"
    path.write_text(json.dumps(suite))
    code = await execute(parser().parse_args(["ci", "run", str(path), "--jobs", "2", "--summary-json", "-"]))
    assert code == 2
    out, _ = capsys.readouterr()
    report = SuiteReport.model_validate_json(out)
    assert report.plan is None and report.cases == [] and report.status == "error"
    assert not (tmp_path / "ownership").exists()
    assert not (tmp_path / ".cache/catt-ci").exists()
    assert "<error" in (tmp_path / "reports/ci/junit.xml").read_text()


async def test_invalid_output_layout_still_returns_machine_readable_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path, _ = fixture_inputs(tmp_path, count=1)
    code = await execute(parser().parse_args([
        "ci", "run", str(path), "--report-dir", "reports", "--private-dir", "reports/private", "--summary-json", "-"]))
    assert code == 2
    report = SuiteReport.model_validate_json(capsys.readouterr().out)
    assert report.plan is None and report.status == "error"
    assert not (tmp_path / "reports").exists()


async def test_early_service_exit_keeps_raw_logs_out_of_public_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path, _ = fixture_inputs(tmp_path, count=1, command=[
        sys.executable, "-c", "print('RAW-UNREDACTABLE-SECRET-IN-LOG'); raise SystemExit(9)"])
    assert await execute(parser().parse_args(["ci", "run", str(path), "--summary-json", "-"])) == 2
    out, err = capsys.readouterr()
    report = SuiteReport.model_validate_json(out)
    assert report.cases[0].status == "error" and report.cases[0].attempts == 0
    public = out + err + (tmp_path / "reports/ci/junit.xml").read_text()
    assert "RAW-UNREDACTABLE-SECRET-IN-LOG" not in public
    assert any("RAW-UNREDACTABLE-SECRET-IN-LOG" in log.read_text()
               for log in (tmp_path / ".cache/catt-ci").rglob("*.log"))


async def test_deadline_stops_a_service_and_preserves_unobserved_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, suite = fixture_inputs(tmp_path, count=1, command=barrier_command(
        tmp_path / "ownership", participants=1, hang=True, descendants=True))
    suite["cases"][0]["timeout_seconds"] = 1.5
    path.write_text(json.dumps(suite))
    report = await run_suite(prepare_suite(path, root=tmp_path), public=tmp_path / "reports",
                             private=tmp_path / "private")
    record = json.loads(next((tmp_path / "ownership").glob("*.json")).read_text())
    assert report.exit_code == 3 and report.cases[0].status == "blocked"
    assert report.cases[0].outcome is None and report.cases[0].compatibility_pass is False
    assert gone(record["pid"]) and gone(record["child"])


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_cli_cancellation_cleans_concurrent_service_descendants_and_records_unrun_cases(tmp_path, number):
    path, _ = fixture_inputs(tmp_path, count=4, command=barrier_command(
        tmp_path / "ownership", participants=2, hang=True, descendants=True))
    process = subprocess.Popen(
        [sys.executable, "-m", "consumer_agent_testing_tool.cli", "ci", "run", str(path), "--jobs", "2",
         "--summary-json", "-"], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        deadline = time.monotonic() + 90
        while len(list((tmp_path / "ownership").glob("*.json"))) < 2 and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Suite stopped before services started: {process.communicate()[1]}")
            time.sleep(0.05)
        assert len(list((tmp_path / "ownership").glob("*.json"))) == 2
        process.send_signal(number)
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert process.returncode == 3, err
    report = SuiteReport.model_validate_json(out)
    assert sorted(row.status for row in report.cases) == ["cancelled", "cancelled", "not-run", "not-run"]
    assert report.compatibility_pass is False
    for file in (tmp_path / "ownership").glob("*.json"):
        record = json.loads(file.read_text())
        assert gone(record["pid"]) and gone(record["child"])
    assert "RAW-SERVICE-LOG-PRIVATE-7d0b4" not in out + err
    assert load_report(tmp_path / "reports/ci/report.json") == report


async def test_worker_runs_scripted_with_provider_entry_points_trapped(tmp_path, monkeypatch):
    from test_connector_support import Hotel, services

    from consumer_agent_testing_tool import connector_run
    from consumer_agent_testing_tool.ci_worker import run

    path, _ = fixture_inputs(tmp_path, count=1)
    prepared = prepare_suite(path, root=tmp_path)
    case = next(iter(prepared.cases.values()))
    workspace = tmp_path / "worker"
    snapshot(case, workspace, 8765, "ci-test-namespace")
    monkeypatch.chdir(workspace)

    def forbid(*args, **kwargs):
        pytest.fail("The CI worker tried to call a model provider")
    monkeypatch.setattr("consumer_agent_testing_tool.providers.resolve_model", forbid)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", forbid)
    monkeypatch.setattr(connector_run, "_agent_driver", forbid)
    monkeypatch.setattr(connector_run, "default_services", lambda: services(Hotel()))
    assert await run(load(workspace / "request.json")) == 0
    summary = json.loads((workspace / "summary.json").read_text())
    result = json.loads((Path(summary["trials"][0]["run"]) / "result.json").read_text())
    assert result["driver"] == "scripted" and not result["agent_runs"] and not result["model_preflight_run"]


async def test_inputs_are_frozen_before_worker_execution(tmp_path, monkeypatch):
    from consumer_agent_testing_tool import ci_run
    original = ci_run.snapshot
    path, _ = fixture_inputs(tmp_path, count=1)
    prepared = prepare_suite(path, root=tmp_path)

    def freeze_then_change(*args):
        original(*args)
        (tmp_path / "scenario.yaml").write_text("private: [malformed")
        (tmp_path / "calls.yaml").write_text("This file changed after the plan.")
    monkeypatch.setattr(ci_run, "snapshot", freeze_then_change)
    monkeypatch.chdir(tmp_path)
    report = await run_suite(prepared, public=tmp_path / "reports", private=tmp_path / "private")
    assert report.exit_code == 0


async def test_external_task_cancellation_stops_owned_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, suite = fixture_inputs(tmp_path, count=3, command=barrier_command(
        tmp_path / "ownership", participants=1, hang=True))
    for case in suite["cases"]:
        case["timeout_seconds"] = 2
    path.write_text(json.dumps(suite))
    task = asyncio.create_task(run_suite(prepare_suite(path, root=tmp_path),
                                         public=tmp_path / "reports", private=tmp_path / "private"))
    async with asyncio.timeout(60):
        while not list((tmp_path / "ownership").glob("*.json")):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    report = load_report(tmp_path / "reports/report.json")
    assert report.exit_code == 3
    assert [row.status for row in report.cases] == ["cancelled", "not-run", "not-run"]
    assert len(list((tmp_path / "ownership").glob("*.json"))) == 1
    record = json.loads(next((tmp_path / "ownership").glob("*.json")).read_text())
    assert gone(record["pid"])
