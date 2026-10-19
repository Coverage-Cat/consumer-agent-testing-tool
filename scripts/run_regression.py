"""Run the complete suite with workspace-local state and a source-bound exit record."""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from consumer_agent_testing_tool.release import source_digest
from consumer_agent_testing_tool.release_scope import RELEASE_SCOPE
from consumer_agent_testing_tool.serialization import file_digest
from consumer_agent_testing_tool.surface import inside


def portable(text, root):
    """Published evidence names the workspace as "." and the home directory as "~", never a user's path."""
    return text.replace(str(root), ".").replace(str(Path.home()), "~")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    report = inside(root, args.junit)
    if report.exists() or report.with_suffix(".run.json").exists():
        raise ValueError("Use a new report path; prior regression evidence is preserved")
    report.parent.mkdir(parents=True, exist_ok=True)
    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="regression-run-", dir=cache))
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    for variable, name in (("HOME", "home"), ("TMPDIR", "tmp"),
                           ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
        directory = work / name
        directory.mkdir()
        env[variable] = str(directory)
    for name in ("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER", "SURFACE_BROWSER_WORKER_TEST"):
        env[name] = os.environ.get(name, "0")
    if env["CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER"] == "1":
        host = os.environ.get("DOCKER_HOST", "")
        if not host.startswith("unix://"):
            raise ValueError("Local regression requires an explicit workspace Docker socket")
        socket = inside(root, host.removeprefix("unix://"))
        config = inside(root, os.environ["DOCKER_CONFIG"])
        if not socket.is_socket() or not config.is_dir():
            raise ValueError("Workspace Docker connection is unavailable")
        env.update(DOCKER_HOST="unix://" + str(socket), DOCKER_CONFIG=str(config))
    command = [sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
               "--basetemp=" + str(work / "tests"), "--junitxml=" + str(report), "tests"]
    record_path = report.with_suffix(".run.json")
    log = report.with_suffix(".log")
    record = {"schema_version": 1, "release_scope": RELEASE_SCOPE,
              "status": "running", "source_before": source_digest(root),
              "started_at": datetime.now(UTC).isoformat(), "work": str(work.relative_to(root)),
              "junit": str(report.relative_to(root)), "log": str(log.relative_to(root)),
              "opt_in_tests": {name: env[name] for name in (
                  "CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER", "SURFACE_BROWSER_WORKER_TEST")},
              "command": [portable(part, root) for part in command], "returncode": None}
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    try:
        with log.open("x") as stream:
            log.chmod(0o600)
            process = subprocess.Popen(command, cwd=root, env=env, stdout=stream,
                stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
            try:
                # The complete integration suite can exceed half an hour on
                # slower hosts. Individual scenario and worker deadlines remain
                # independent; this only bounds the whole regression process.
                record["returncode"] = process.wait(timeout=3600)
            except BaseException:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
                raise
    finally:
        if log.is_file():
            log.write_text(portable(log.read_text(errors="surrogateescape"), root), errors="surrogateescape")
        record["source_after"] = source_digest(root)
        record["finished_at"] = datetime.now(UTC).isoformat()
        record["junit_sha256"] = file_digest(report) if report.is_file() else None
        record["log_sha256"] = file_digest(log) if log.is_file() else None
        record["status"] = "passed" if record["returncode"] == 0 and record["junit_sha256"] \
            and record["source_before"] == record["source_after"] else "failed"
        record_path.write_text(json.dumps(record, indent=2) + "\n")
        print(f"Regression {record['status']}; record: {record_path.relative_to(root)}", flush=True)
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
