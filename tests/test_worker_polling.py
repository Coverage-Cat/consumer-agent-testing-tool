"""Actual short RPCs observing one longer reference-worker execution."""
import asyncio
import json
import os
import signal
import sys
import time

import pytest
from test_worker_evidence import setup_job
from test_worker_expiry import GATEWAY, LocalGateway, wait_for

from consumer_agent_testing_tool.workers import CommandGateway, WorkerLifecycle


@pytest.mark.parametrize("embedded", [False, True])
def test_async_gateway_polling_never_restarts_and_outlives_start_rpc(tmp_path, embedded):
    target = LocalGateway(tmp_path, ttl=5, embedded=embedded,
        program="import time\nfrom pathlib import Path\n"
                "with Path('executions').open('a') as out: out.write('started\\n')\n"
                "print('synthetic delayed completion',flush=True)\ntime.sleep(1.5)\n")
    try:
        assert target.call("ready")["run_protocol"] == "poll-v1"
        assert target.call("run", mode="poll")["run_state"] == "not_started"
        assert not (target.job / "executions").exists()
        start = target.spawn("run", mode="start")
        reply = json.loads(start.communicate(timeout=1)[0])
        assert reply["run_state"] == "running"
        wait_for(lambda: (target.job / "executions").exists())
        assert target.call("run", mode="start")["run_state"] == "running"
        assert target.call("run", mode="poll")["run_state"] == "running"
        assert "running" in target.call("destroy")["error"]
        result = wait_for(lambda: (row if (row := target.call("run", mode="poll"))[
            "run_state"] == "completed" else None))
        assert result["exit_code"] == 0 and result["reason"] == "exited"
        assert target.call("run", mode="start") == result
        assert target.call("run", mode="poll") == result
        assert (target.job / "executions").read_text() == "started\n"
        assert target.call("export")["complete"] is True
    finally:
        target.close()


@pytest.mark.parametrize("fault", ["expiry", "supervisor_killed", "supervisor_stopped", "guard_killed", "cancel"])
def test_async_supervision_still_stops_owned_process_tree(tmp_path, fault):
    target = LocalGateway(tmp_path, ttl=2)
    supervisor = None
    try:
        assert target.call("run", mode="start")["run_state"] == "running"
        wait_for(lambda: all((target.job / name).is_file()
                            for name in ("child-heartbeat", "heartbeat", "supervisor.json", "guard.json")))
        before = target.heartbeats()
        time.sleep(.1)
        assert target.heartbeats() != before
        supervisor = json.loads((target.job / "supervisor.json").read_text())["pid"]
        if fault in ("supervisor_killed", "supervisor_stopped"):
            os.kill(supervisor, signal.SIGKILL if fault == "supervisor_killed" else signal.SIGSTOP)
        elif fault == "guard_killed":
            os.kill(json.loads((target.job / "guard.json").read_text())["pid"], signal.SIGKILL)
        elif fault == "cancel":
            assert target.call("cancel")["cancelled"]
        target.stopped()
        if fault == "supervisor_stopped":
            assert target.call("run", mode="poll")["run_state"] == "running"
            assert "running" in target.call("export")["error"]
            os.kill(supervisor, signal.SIGCONT)
        result = wait_for(lambda: (row if (row := target.call("run", mode="poll")).get(
            "run_state") == "completed" else None))
        assert result["exit_code"] != 0
        assert result["reason"] == {
            "expiry": "expired", "supervisor_killed": "supervisor_lost",
            "supervisor_stopped": "expired", "guard_killed": "guard_lost", "cancel": "cancelled"}[fault]
        assert target.call("export")["complete"] is True
    finally:
        if supervisor and fault == "supervisor_stopped":
            try:
                os.kill(supervisor, signal.SIGCONT)
            except ProcessLookupError:
                pass
        target.close()


@pytest.mark.parametrize("fault", [None, "start", "poll", "terminal"])
async def test_controller_job_outlasts_request_budget_and_recovers_lost_reply(tmp_path, fault):
    package, config = setup_job(tmp_path,
        "import time\nfrom pathlib import Path\n"
        "with Path('executions').open('a') as out: out.write('started\\n')\n"
        "print('synthetic long job',flush=True)\ntime.sleep(2)\n")
    config = config.model_copy(update={"run_mode": "auto", "ttl_seconds": 10,
        "call_timeout_seconds": 1, "poll_interval_seconds": .05})
    observed = []

    class Gateway(CommandGateway):
        failed = False

        async def call(self, operation, payload):
            result = await super().call(operation, payload)
            if operation == "run":
                observed.append((payload, result))
                selected_fault = payload.get("mode") == fault or (
                    fault == "terminal" and result["run_state"] == "completed")
                if selected_fault and not self.failed:
                    self.failed = True
                    raise TimeoutError("Synthetic lost reply from the actual worker")
            if operation == "destroy":
                saved = json.loads((tmp_path / "runs" / payload["trial_id"] / "worker.json").read_text())
                assert saved["collection"] == "complete"
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    result = await controller.run(package["bundle"])
    assert result["status"] == "completed" and result["execution"]["exit_code"] == 0
    assert result["collection"] == "complete" and result["cleanup"] == "confirmed"
    assert sum(payload["mode"] == "start" for payload, _ in observed) == 1
    assert any(reply["run_state"] == "running" for _, reply in observed)
    assert sum(event["status"] == "failed" for event in result["events"]) == bool(fault)
    assert "synthetic long job" in result["artifacts"]["files"]["stdout.log"]


async def test_controller_cancellation_cleans_async_execution(tmp_path):
    package, config = setup_job(tmp_path, "import time\nprint('synthetic waiting job',flush=True)\ntime.sleep(30)\n")
    config = config.model_copy(update={"run_mode": "polling", "ttl_seconds": 10,
                                      "poll_interval_seconds": .05})
    controller = WorkerLifecycle(tmp_path, config)
    task = asyncio.create_task(controller.run(package["bundle"]))
    try:
        for _ in range(100):
            if (tmp_path / "jobs" / controller.path.name / "stdout.log").exists():
                break
            await asyncio.sleep(.025)
        else:
            raise AssertionError("Actual asynchronous job did not start")
        task.cancel()
        result = await task
        assert result["status"] == "failed" and result["cleanup"] == "confirmed"
        assert result["collection"] == "complete"
        assert json.loads(result["artifacts"]["files"]["exit.json"])["reason"] == "cancelled"
    finally:
        if not task.done():
            task.cancel()
            await task


@pytest.mark.parametrize("fault", ["cancel", "lost"])
def test_async_startup_cancellation_and_loss_retire_admission(tmp_path, fault):
    target = LocalGateway(tmp_path, ttl=5)
    # Stop the actual supervisor immediately before run_job acquires control.
    # This deterministic lifecycle fault never changes the evaluated program.
    source = GATEWAY.read_text()
    original = "                    run_job(job, coordinator_environment, supervisor_fd=supervisor)"
    assert source.count(original) == 1
    source = source.replace(original,
        "                    os.kill(os.getpid(), signal.SIGSTOP)\n" + original)
    fixture = tmp_path / "startup_gateway.py"
    fixture.write_text("WORKER_GUARD_SOURCE = " + repr(
        GATEWAY.with_name("worker_guard.py").read_text()) + "\n" + source)
    target.command = [sys.executable, "-I", str(fixture), str(tmp_path / "jobs")]
    supervisor = cancellation = None
    try:
        assert target.call("run", mode="start")["run_state"] == "running"
        wait_for(lambda: (target.job / "supervisor.json").exists())
        supervisor = json.loads((target.job / "supervisor.json").read_text())["pid"]
        assert not (target.job / "started").exists()
        if fault == "cancel":
            cancellation = target.spawn("cancel")
            wait_for(lambda: (target.job / "cancelled").exists())
            os.kill(supervisor, signal.SIGCONT)
            assert json.loads(cancellation.communicate(timeout=2)[0])["cancelled"] is True
        else:
            os.kill(supervisor, signal.SIGKILL)
        result = wait_for(lambda: (row if (row := target.call("run", mode="poll")).get(
            "run_state") == "completed" else None))
        assert result["exit_code"] == 2
        assert result["reason"] == ("start_failed" if fault == "cancel" else "dispatch_lost")
        assert not (target.job / "executions").exists()
        assert target.call("run", mode="start") == result
        assert target.call("export")["complete"] is True
    finally:
        if supervisor:
            try:
                os.kill(supervisor, signal.SIGCONT)
            except ProcessLookupError:
                pass
        if cancellation:
            cancellation.communicate(timeout=12)
        target.close()


@pytest.mark.parametrize("response", [
    {"run_state": "running", "exit_code": 0},
    {"run_state": "completed", "exit_code": False},
    {"run_state": "not_started"},
    {"run_state": "unexpected"},
])
async def test_malformed_polling_acknowledgement_cannot_pass_or_restart(tmp_path, response):
    package, config = setup_job(tmp_path, "import time\ntime.sleep(10)\n")
    config = config.model_copy(update={"run_mode": "polling", "ttl_seconds": 5})

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            result = await super().call(operation, payload)
            if operation == "run":
                return {"trial_id": payload["trial_id"], **response}
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    result = await controller.run(package["bundle"])
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert "execution" not in result
    assert sum(event.get("mode") == "start" for event in result["events"]) == 1
    assert not any(event.get("mode") == "poll" for event in result["events"])


@pytest.mark.parametrize("protocol", [None, "unknown-version"])
async def test_required_polling_protocol_fails_before_execution(tmp_path, protocol):
    package, config = setup_job(tmp_path)
    config = config.model_copy(update={"run_mode": "polling"})

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            result = await super().call(operation, payload)
            if operation == "ready":
                result["run_protocol"] = protocol
            return result

    controller = WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config))
    result = await controller.run(package["bundle"])
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert "polling protocol" in result["detail"]
    assert not any(event["operation"] == "run" for event in result["events"])


async def test_lost_start_before_admission_does_not_turn_poll_into_start(tmp_path):
    package, config = setup_job(tmp_path,
        "from pathlib import Path\nPath('executions').write_text('must not execute')\n")
    config = config.model_copy(update={"run_mode": "polling", "poll_interval_seconds": .05})
    requests = []

    class Gateway(CommandGateway):
        async def call(self, operation, payload):
            if operation == "run":
                requests.append(payload["mode"])
                if payload["mode"] == "start":
                    raise TimeoutError("Synthetic request lost before reaching the worker")
            if operation == "destroy":
                assert not (tmp_path / "jobs" / payload["trial_id"] / "executions").exists()
            return await super().call(operation, payload)

    result = await WorkerLifecycle(tmp_path, config, Gateway(tmp_path, config)).run(package["bundle"])
    assert requests == ["start", "poll"]
    assert result["status"] == "failed" and result["cleanup"] == "confirmed"
    assert "did not admit" in result["detail"]
    assert "execution" not in result
