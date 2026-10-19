"""connector-run --start: the tool starts the service under test on a free loopback port, waits until it
answers, runs the trials and stops it however they end; the bundled fixture end to end, a service that exits
at once, and one that never becomes ready."""
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_connector_e2e_support import EXAMPLES

from consumer_agent_testing_tool import service_start
from consumer_agent_testing_tool.ci_outputs import SUMMARY_SCHEMA
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError

PYTHON = shlex.quote(sys.executable)
FIXTURE = f"{PYTHON} -m consumer_agent_testing_tool.connector_fixture --port {{port}} --variant good --auth none"
SCENARIO, SCRIPT = "examples/fixture-booking.yaml", "examples/fixture-booking.calls.json"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the workspace.
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    return tmp_path


async def cli(*arguments):
    return await execute(parser().parse_args([str(argument) for argument in arguments]))


def scripted(*extra):
    return ["connector-run", SCENARIO, "--driver", "scripted", "--script", SCRIPT, *extra]


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def listening(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def python_command(code):
    return f"{PYTHON} -c {shlex.quote(code)}"


def test_start_runs_the_bundled_fixture_end_to_end_and_stops_it(workspace):
    # The real CLI in a process of its own: stdout must be exactly the summary document.
    done = subprocess.run([sys.executable, "-m", "consumer_agent_testing_tool.cli", *scripted(
        "--start", FIXTURE, "--trials", "2", "--summary-json", "-")], cwd=workspace, capture_output=True,
        text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stderr
    summary = json.loads(done.stdout)
    assert not list(Draft202012Validator(SUMMARY_SCHEMA).iter_errors(summary))
    service = summary["service"]
    assert summary["status"] == "completed" and [trial["exit_code"] for trial in summary["trials"]] == [0, 0]
    assert service["command"][-6:] == ["--port", str(service["port"]), "--variant", "good", "--auth", "none"]
    assert service["ready_url"] == f"http://127.0.0.1:{service['port']}/mcp"  # The scenario's endpoint.
    assert gone(service["pid"]) and not listening(service["port"])  # Stopped afterwards.
    log = Path(service["log"])
    assert log.parent == workspace / "runs" and log.is_file()  # Beside the sealed runs, never inside one.
    assert json.loads(log.read_text().splitlines()[0])["port"] == service["port"]
    assert f"Started service (pid {service['pid']}, port {service['port']})" in done.stderr
    for trial in summary["trials"]:
        lock = json.loads(Path(trial["run"], "result.json").read_text())["run_lock"]
        # The scenario's ${PORT:-8765} became the started service's port; the lock says who started it.
        assert lock["service"] == {"started_by": "consumer-agent-testing-tool --start",
                                   "command": service["command"], "port": service["port"],
                                   "ready_url": service["ready_url"], "log": log.name}
        assert "PORT" in lock["variables"]
    manifest = json.loads(Path(summary["summary_run"], "manifest.json").read_text())
    assert manifest["run_lock"]["service"]["port"] == service["port"]


async def test_start_stops_the_service_when_the_trial_fails(workspace, capsys):
    # The scenario's endpoint and the service's port differ: setup fails, and the service is still stopped.
    code = await cli(*scripted("--start", FIXTURE, "--set", "PORT=1", "--ready-url", "http://127.0.0.1:{port}/mcp",
                               "--summary-json", "-"))
    summary = json.loads(capsys.readouterr().out)
    assert code == 2 and summary["trials"][0]["failure_stage"] == "reset"  # Port 1 answers nothing.
    assert gone(summary["service"]["pid"]) and not listening(summary["service"]["port"])


async def test_start_reports_a_service_that_exits_before_it_is_ready(workspace):
    command = python_command("print('fixture-boom: no database'); raise SystemExit(4)")
    with pytest.raises(LabError) as raised:
        await cli(*scripted("--start", command, "--junit", "junit.xml"))
    message = str(raised.value)
    assert raised.value.code == 2 and "exited with code 4 before it was ready" in message
    assert "fixture-boom: no database" in message  # The log tail.
    assert not list(workspace.glob("runs/connector-*"))  # No trial ran.
    assert "exited with code 4" in (workspace / "junit.xml").read_text()


def test_start_exit_code_and_message_through_the_cli(workspace):
    command = python_command("import sys; print('fixture-boom', file=sys.stderr); raise SystemExit(4)")
    done = subprocess.run([sys.executable, "-m", "consumer_agent_testing_tool.cli", *scripted("--start", command)],
                          cwd=workspace, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 2 and done.stdout == ""
    assert "setup/infrastructure: The --start service exited with code 4" in done.stderr
    assert "fixture-boom" in done.stderr


async def test_start_kills_a_service_that_never_becomes_ready(workspace, monkeypatch):
    monkeypatch.setattr(service_start, "STOP_SECONDS", 0.5)
    pid_file = workspace / "service.pid"
    # Ignores SIGTERM, so stopping it takes the SIGKILL.
    command = python_command(f"import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                             f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(600)")
    started = time.monotonic()
    with pytest.raises(LabError) as raised:
        await cli(*scripted("--start", command, "--ready-url", "http://127.0.0.1:{port}/ready",
                            "--ready-timeout", "1.5"))
    assert raised.value.code == 2 and "did not answer http://127.0.0.1:" in str(raised.value)
    assert "within 1.5 s" in str(raised.value) and time.monotonic() - started < 30
    assert gone(int(pid_file.read_text()))


async def test_ready_flags_need_start(workspace):
    with pytest.raises(LabError, match="only with --start"):
        await cli(*scripted("--ready-timeout", "5"))
    with pytest.raises(SystemExit):
        parser().parse_args(scripted("--start", "x", "--ready-timeout", "0"))


def test_recorded_command_hides_credentials():
    assert service_start.redacted_command(["serve", "--api-key", "abc123-not-public", "--token=xyz789-hidden",
                                           "--port", "4000", "http://127.0.0.1/?access_token=t0ken-value"]) == [
        "serve", "--api-key", "[REDACTED]", "--token=[REDACTED]", "--port", "4000",
        "http://127.0.0.1/?access_token=[REDACTED]"]
    assert service_start.start_argv("serve --port {port} '{port} x'", 4321) == ["serve", "--port", "4321", "4321 x"]


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_start_stops_the_service_when_the_tool_is_terminated_or_interrupted(workspace, number):
    pid_file = workspace / "service.pid"
    command = python_command(f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                             "time.sleep(600)")
    # SIGINT handled as a terminal's Ctrl-C is, even when pytest runs as a background job (which ignores it).
    process = subprocess.Popen(
        [sys.executable, "-c", ("import signal; signal.signal(signal.SIGINT, signal.default_int_handler); "
                                "from consumer_agent_testing_tool.cli import main; main()"), *scripted(
            "--start", command, "--ready-url", "http://127.0.0.1:{port}/", "--ready-timeout", "120",
            "--summary-json", "-")], cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not (pid_file.is_file() and pid_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        process.send_signal(number)
        out, err = process.communicate(timeout=60)
    finally:
        process.kill()
    summary = json.loads(out)
    assert summary["status"] == "error" and summary["error"]["class"] == "interrupted"
    assert gone(int(pid_file.read_text())) and gone(summary["service"]["pid"])
    if number == signal.SIGTERM:
        assert process.returncode == 3 and "interrupted: Stopped by SIGTERM" in err
