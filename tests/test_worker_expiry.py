"""Real process/pipe/lock probes for the POSIX reference worker's deadline."""
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from consumer_agent_testing_tool.workers import WorkerConfig, WorkerLifecycle, bundle, reap

GATEWAY = Path(__file__).resolve().parents[1] / "src/consumer_agent_testing_tool/worker_gateway.py"
WRITER = """import os,time
from pathlib import Path
end=time.monotonic()+10
while time.monotonic()<end:
 Path('child-heartbeat').write_text(str(time.monotonic()))
 time.sleep(.025)
"""
PROGRAM = f"""import os,subprocess,sys,time
from pathlib import Path
with Path('executions').open('a') as out: out.write('started\\n')
child=subprocess.Popen([sys.executable,'-c',{WRITER!r}])
print('synthetic process tree running',flush=True)
end=time.monotonic()+10
while time.monotonic()<end:
 Path('heartbeat').write_text(str(time.monotonic()))
 time.sleep(.025)
"""


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError("Owned worker did not reach the expected state")


class LocalGateway:
    def __init__(self, root, *, ttl=2, embedded=False, program=PROGRAM):
        self.root = root
        (root / "main.py").write_text(program)
        (root / "manifest.json").write_text(json.dumps({
            "files": ["main.py"], "command": [sys.executable, "main.py"]}))
        self.package = bundle(root, "manifest.json", "bundle.zip")
        self.trial = "worker-" + uuid.uuid4().hex
        self.job = root / "jobs" / self.trial
        self.expiry = time.time() + ttl
        if embedded:
            source = "WORKER_GUARD_SOURCE = " + repr(
                GATEWAY.with_name("worker_guard.py").read_text()) + "\n" + GATEWAY.read_text()
            self.command = [sys.executable, "-I", "-c", source, str(root / "jobs")]
        else:
            self.command = [sys.executable, "-I", str(GATEWAY), str(root / "jobs")]
        self.rpc = None
        assert self.call("provision")["trial_id"] == self.trial
        seed = {"bundle_base64": base64.b64encode(Path(self.package["bundle"]).read_bytes()).decode()} \
            if embedded else {"bundle": self.package["bundle"]}
        assert self.call("seed", sha256=self.package["sha256"], **seed)["seeded"]

    def spawn(self, operation, **args):
        child = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        child.stdin.write(json.dumps({
            "operation": operation, "trial_id": self.trial, "expires_at": self.expiry, **args}))
        child.stdin.close()
        child.stdin = None
        return child

    def call(self, operation, **args):
        child = self.spawn(operation, **args)
        output, error = child.communicate(timeout=12)
        assert child.returncode == 0, error
        return json.loads(output)

    def start(self):
        self.rpc = self.spawn("run")
        wait_for(lambda: all((self.job / name).is_file()
                            for name in ("heartbeat", "child-heartbeat", "guard.json")))
        first = self.heartbeats()
        time.sleep(.1)
        assert self.heartbeats() != first, "Positive control: the actual task must be progressing"
        return self.rpc

    def heartbeats(self):
        return tuple((self.job / name).read_text() for name in ("heartbeat", "child-heartbeat"))

    def stopped(self):
        wait_for(lambda: (self.job / "exit.json").is_file())
        # Allow queued SIGKILL delivery/stdio close, then observe both destinations.
        time.sleep(.15)
        previous = self.heartbeats()
        time.sleep(.15)
        assert self.heartbeats() == previous
        assert (self.job / "executions").read_text() == "started\n"

    def close(self):
        if self.rpc is not None and self.rpc.poll() is None:
            os.kill(self.rpc.pid, signal.SIGCONT)
        self.call("cancel")
        if self.rpc is not None:
            self.rpc.communicate(timeout=5)
        self.call("destroy")


@pytest.mark.parametrize("fault", ["expiry", "rpc_killed", "rpc_stopped", "guard_killed", "cancel"])
def test_reference_worker_stops_actual_process_tree_after_supervision_failure(tmp_path, fault):
    target = LocalGateway(tmp_path)
    rpc = target.start()
    try:
        if fault == "rpc_killed":
            rpc.kill()
            rpc.wait(timeout=3)
            assert rpc.returncode == -signal.SIGKILL
        elif fault == "rpc_stopped":
            os.kill(rpc.pid, signal.SIGSTOP)
        elif fault == "guard_killed":
            guard = json.loads((target.job / "guard.json").read_text())
            os.kill(guard["pid"], signal.SIGKILL)
        elif fault == "cancel":
            assert target.call("cancel")["cancelled"] is True
        target.stopped()
        if fault == "rpc_stopped":
            # A suspended supervisor still holds its kernel lease. A receipt and
            # elapsed deadline cannot by themselves authorize terminal collection.
            assert "running" in target.call("export")["error"]
            assert "running" in target.call("destroy")["error"]
            os.kill(rpc.pid, signal.SIGCONT)
        rpc.communicate(timeout=5)
        exported = target.call("export")
        assert exported["complete"] is True, exported
        outcome = json.loads(exported["files"]["exit.json"])
        assert outcome["exit_code"] != 0
        assert outcome["reason"] == {
            "expiry": "expired", "rpc_killed": "supervisor_lost", "rpc_stopped": "expired",
            "guard_killed": "guard_lost", "cancel": "cancelled"}[fault]
        assert target.call("destroy")["destroyed"] is True
        assert not target.job.exists()
        assert "retired" in target.call("provision")["error"].lower()
        assert "retired" in target.call("run")["error"].lower()
    finally:
        target.close()


def test_reference_worker_normal_completion_stops_background_children_and_cannot_rerun(tmp_path):
    program = ("import subprocess,sys,time\nfrom pathlib import Path\n"
        "with Path('executions').open('a') as out: out.write('started\\n')\n"
        f"subprocess.Popen([sys.executable,'-c',{WRITER!r}])\n"
        "while not Path('child-heartbeat').exists(): time.sleep(.01)\n")
    target = LocalGateway(tmp_path, ttl=5, program=program)
    try:
        first = target.call("run")
        assert first["exit_code"] == 0 and first["reason"] == "exited"
        heartbeat = (target.job / "child-heartbeat").read_text()
        time.sleep(.15)
        assert (target.job / "child-heartbeat").read_text() == heartbeat
        assert target.call("run") == first
        assert (target.job / "executions").read_text() == "started\n"
        assert "reseeded" in target.call("seed", bundle=target.package["bundle"],
                                       sha256=target.package["sha256"])["error"]
        assert target.call("export")["complete"] is True
    finally:
        target.close()


def test_reference_worker_concurrent_run_requests_execute_only_once(tmp_path):
    target = LocalGateway(tmp_path, ttl=1)
    first, second = target.spawn("run"), target.spawn("run")
    target.rpc = first
    try:
        outcomes = [json.loads(child.communicate(timeout=5)[0]) for child in (first, second)]
        assert sum("error" not in row for row in outcomes) == 1
        assert (target.job / "executions").read_text() == "started\n"
        assert target.call("export")["complete"] is True
    finally:
        second.communicate(timeout=5)
        target.close()


def test_reference_worker_embedded_ssh_program_keeps_original_deadline(tmp_path):
    # Actual python -c/base64 transport layout, without opening an SSH connection.
    target = LocalGateway(tmp_path, ttl=1, embedded=True)
    try:
        target.call("provision", expires_at=target.expiry + 3600)
        assert json.loads((target.job / "state.json").read_text())["expires_at"] == target.expiry
        result = target.call("run")
        assert result["reason"] == "expired" and result["exit_code"] != 0
        assert target.call("export")["complete"] is True
    finally:
        target.close()


def test_reference_worker_coordinator_sigkill_preserves_expiry_evidence_and_reaping(tmp_path):
    target = LocalGateway(tmp_path, ttl=2)
    # Use a fresh controller-generated identity for the actual coordinator run.
    config = WorkerConfig(provider="local-process-reference", gateway=target.command,
        ttl_seconds=2, expected_harness_sha256=target.package["sha256"])
    (tmp_path / "worker-config.json").write_text(config.model_dump_json())
    code = """import asyncio,json
from pathlib import Path
from consumer_agent_testing_tool.workers import WorkerConfig,WorkerLifecycle
root=Path.cwd()
config=WorkerConfig.model_validate_json((root/'worker-config.json').read_text())
controller=WorkerLifecycle(root,config)
(root/'coordinator.json').write_text(json.dumps({'trial':controller.path.name}))
asyncio.run(controller.run(root/'bundle.zip'))
"""
    coordinator = subprocess.Popen([sys.executable, "-c", code], cwd=tmp_path,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    try:
        wait_for(lambda: (tmp_path / "coordinator.json").is_file())
        trial = json.loads((tmp_path / "coordinator.json").read_text())["trial"]
        job = tmp_path / "jobs" / trial
        wait_for(lambda: (job / "child-heartbeat").is_file())
        coordinator.kill()
        coordinator.wait(timeout=3)
        assert coordinator.returncode == -signal.SIGKILL
        wait_for(lambda: (job / "exit.json").is_file())
        time.sleep(.2)
        heartbeat = (job / "child-heartbeat").read_text()
        time.sleep(.15)
        assert (job / "child-heartbeat").read_text() == heartbeat
        assert asyncio.run(reap(tmp_path, config, now=time.time() + 3)) == [trial]
        saved = json.loads((tmp_path / "runs" / trial / "worker.json").read_text())
        assert saved["collection"] == "complete" and saved["cleanup"] == "confirmed"
        assert json.loads(saved["artifacts"]["files"]["exit.json"])["reason"] == "expired"
        assert sum(event.get("mode") == "start" for event in saved["events"]) == 1
        assert not job.exists()
    finally:
        if coordinator.poll() is None:
            coordinator.kill()
        coordinator.communicate(timeout=5)
        target.close()


async def test_reference_worker_real_rpc_timeout_stops_and_collects_the_job(tmp_path):
    target = LocalGateway(tmp_path, ttl=5)
    config = WorkerConfig(provider="local-process-reference", gateway=target.command, run_mode="blocking",
        ttl_seconds=5, call_timeout_seconds=1, expected_harness_sha256=target.package["sha256"])
    try:
        result = await WorkerLifecycle(tmp_path, config).run(target.package["bundle"])
        assert result["status"] == "failed"
        assert result["collection"] == "complete" and result["cleanup"] == "confirmed"
        outcome = json.loads(result["artifacts"]["files"]["exit.json"])
        assert outcome["reason"] == "supervisor_lost"
        assert "synthetic process tree running" in result["artifacts"]["files"]["stdout.log"]
    finally:
        target.close()


@pytest.mark.parametrize("name", ["state.json", "active.lock", "supervisor.lock", "exit.json", "started"])
def test_reference_worker_seed_rejects_lifecycle_file_collisions(tmp_path, name):
    target = LocalGateway(tmp_path, ttl=5)
    try:
        (tmp_path / name).write_text("{}")
        (tmp_path / "bad.json").write_text(json.dumps({
            "files": [name], "command": [sys.executable, "main.py"]}))
        package = bundle(tmp_path, "bad.json", "bad.zip")
        assert "private lifecycle" in target.call(
            "seed", bundle=package["bundle"], sha256=package["sha256"])["error"]
    finally:
        target.close()


def test_reference_worker_rejected_seed_cannot_replace_pinned_program(tmp_path):
    target = LocalGateway(tmp_path, ttl=5, program="print('original pinned program')")
    try:
        original = (target.job / "main.py").read_bytes()
        (tmp_path / "main.py").write_text("print('wrong replacement program')")
        (tmp_path / "state.json").write_text("{}")
        (tmp_path / "bad.json").write_text(json.dumps({
            "files": ["main.py", "state.json"], "command": [sys.executable, "main.py"]}))
        invalid = bundle(tmp_path, "bad.json", "invalid.zip")
        assert "private lifecycle" in target.call("seed",
            bundle=invalid["bundle"], sha256=invalid["sha256"])["error"]
        (tmp_path / "bad.json").write_text(json.dumps({
            "files": ["main.py"], "command": [sys.executable, "main.py"]}))
        changed = bundle(tmp_path, "bad.json", "changed.zip")
        assert "another pinned bundle" in target.call("seed",
            bundle=changed["bundle"], sha256=changed["sha256"])["error"]
        assert (target.job / "main.py").read_bytes() == original
        assert target.call("seed", bundle=target.package["bundle"],
                           sha256=target.package["sha256"])["seeded"] is True
        assert target.call("run")["exit_code"] == 0
        assert target.call("export")["files"]["stdout.log"] == "original pinned program\n"
    finally:
        target.close()


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                   reason="Unverified: reference-worker Linux proof requires the owned Docker runtime")
@pytest.mark.parametrize("setup_delay", [0, 1.0])
def test_reference_worker_actual_linux_expiry_and_supervisor_loss(tmp_path, setup_delay):
    root = Path(__file__).resolve().parents[1]
    files = {name: (root / path).read_text() for name, path in {
        "worker_gateway.py": "src/consumer_agent_testing_tool/worker_gateway.py",
        "worker_guard.py": "src/consumer_agent_testing_tool/worker_guard.py",
        "probe.py": "scripts/worker_expiry_probe.py"}.items()}
    if setup_delay:
        # Delay readiness, before any evaluated process exists. The real guard
        # and its immutable provision-time deadline remain unchanged.
        files["worker_gateway.py"] = files["worker_gateway.py"].replace(
            'if operation == "ready":\n', f'if operation == "ready":\n        time.sleep({setup_delay!r})\n', 1)
    image = subprocess.run(["docker", "image", "inspect", "consumer-agent-testing-tool-sandbox:0.1.0",
        "--format", "{{.Id}}"], capture_output=True, text=True, check=True).stdout.strip()
    assert image.startswith("sha256:")
    name = "surface-worker-expiry-" + uuid.uuid4().hex[:12]
    bootstrap = (
        "import json,sys,runpy\nfrom pathlib import Path\nroot=Path('/work')\n"
        "for name,text in json.loads(sys.argv[1]).items(): (root/name).write_text(text)\n"
        "sys.argv=['/work/probe.py','/work/worker_gateway.py','/work/proof']\n"
        "runpy.run_path('/work/probe.py',run_name='__main__')\n")
    created = False
    try:
        subprocess.run(["docker", "create", "--name", name, "--init", "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user=1000:1000", "--memory=256m", "--pids-limit=128",
            "--tmpfs=/work:rw,noexec,nosuid,size=64m,uid=1000,gid=1000",
            "--entrypoint=python", image, "-I", "-c", bootstrap, json.dumps(files)],
            capture_output=True, text=True, check=True, timeout=15)
        created = True
        result = subprocess.run(["docker", "start", "-a", name],
                                capture_output=True, text=True, check=False, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads(result.stdout)
        assert record["status"] == "passed" and len(record["cases"]) == 5
        assert all(row["runtime"]["platform"] == "linux" for row in record["cases"])
        assert all(row["process_tree_stopped"] and row["evidence_collected"] for row in record["cases"])
        assert all(row["positive_control"] and row["deadline_seconds"] == 5 for row in record["cases"])
        (tmp_path / "linux-proof.json").write_text(json.dumps({
            "image": image, "setup_delay": setup_delay, **record}, indent=2))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=created, timeout=15)
