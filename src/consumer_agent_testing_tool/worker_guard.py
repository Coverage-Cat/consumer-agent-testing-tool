"""POSIX reference-job lease. Runs separately from the lifecycle RPC supervisor."""
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def write_record(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main():
    job, alive_fd, lease_fd = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    finished_fd = int(sys.argv[4])
    # Only this guard retains the lease/control descriptors. The evaluated
    # command and its children get stdio and a scoped environment.
    os.set_inheritable(alive_fd, False)
    os.set_inheritable(lease_fd, False)
    os.set_inheritable(finished_fd, False)
    state = json.loads((job / "state.json").read_text())
    deadline = min(state["deadline_monotonic"],
                   time.monotonic() + max(0, state["expires_at"] - time.time()))
    lock = json.loads((job / "bundle-lock.json").read_text())
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
    private = sys.stdin.buffer.read(16 * 1024 + 1)
    if len(private) > 16 * 1024:
        raise ValueError("Worker coordinator credential input exceeds limit")
    credentials = json.loads(private or b"{}")
    command = lock["command"]
    if lock.get("coordinator"):
        invocation = json.loads((job / "invocation.worker.json").read_text())
        if command != ["consumer-agent-testing-tool-worker-run"] \
                or invocation.get("format") != "consumer-agent-testing-tool-worker-run-v1" \
                or not isinstance(credentials, dict) \
                or set(credentials) != set(invocation.get("credential_env", [])) \
                or any(not isinstance(value, str) for value in credentials.values()):
            raise ValueError("Invalid scoped coordinator credentials")
        # The child is the installed trusted CLI, not a bundled arbitrary command.
        # Its existing tool/browser launchers retain their separate allowlists.
        runtime = invocation.get("environment", {})
        if not isinstance(runtime, dict) or set(runtime) - {
                "DOCKER_HOST", "DOCKER_CONFIG", "CONSUMER_AGENT_TESTING_TOOL_TOOLING", "AWS_EC2_METADATA_DISABLED"}:
            raise ValueError("Invalid coordinator runtime environment")
        if set(credentials) & {"PATH", "HOME", "SYSTEMROOT", "TMPDIR", "DOCKER_HOST", "DOCKER_CONFIG",
                               "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CONSUMER_AGENT_TESTING_TOOL_TOOLING",
                               "CONSUMER_AGENT_TESTING_TOOL_RUNTIME", *runtime} \
                or any(name.startswith(("PYTHON", "LD_", "DYLD_", "NODE_")) for name in credentials):
            raise ValueError("Credentials cannot override coordinator runtime controls")
        env.update(runtime)
        env.update(credentials)
        for name, relative in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
            location = job / ".coordinator" / relative
            location.mkdir(parents=True, exist_ok=True)
            env[name] = str(location)
        command = [sys.executable, "-I", "-m", "consumer_agent_testing_tool.worker_run"]
    elif credentials:
        raise ValueError("Arbitrary worker commands cannot receive coordinator credentials")
    temporary = job / "tmp"
    temporary.mkdir(exist_ok=True)
    env["TMPDIR"] = str(temporary)
    child = None
    outcome = {"exit_code": -signal.SIGKILL, "reason": "guard_error",
               "scope": "reference-process-group", "trial_id": job.name}
    try:
        write_record(job / "guard.json", {"pid": os.getpid(), "trial_id": job.name})
        with (job / "stdout.log").open("w") as output, (job / "stderr.log").open("w") as error:
            # Cancellation and expiry are checked before starting the command too.
            if time.monotonic() >= deadline or (job / "cancelled").exists():
                outcome["reason"] = "expired" if time.monotonic() >= deadline else "cancelled"
            elif select.select([alive_fd], [], [], 0)[0] and os.read(alive_fd, 1) == b"":
                outcome["reason"] = "supervisor_lost"
            else:
                child = subprocess.Popen(command, cwd=job, env=env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=error)
                while True:
                    code = child.poll()
                    if code is not None:
                        outcome.update(exit_code=code, reason="exited")
                        break
                    if time.monotonic() >= deadline or time.time() >= state["expires_at"]:
                        outcome["reason"] = "expired"
                        break
                    if (job / "cancelled").exists():
                        outcome["reason"] = "cancelled"
                        break
                    if select.select([alive_fd], [], [], .025)[0] and os.read(alive_fd, 1) == b"":
                        outcome["reason"] = "supervisor_lost"
                        break
    except Exception as error:  # noqa: BLE001 — every failure must stop the owned process group
        outcome["detail"] = str(error)
    finally:
        try:
            # This is a pre-termination receipt. Readers must also acquire the
            # kernel lease after the guard exits; the receipt alone is not finality.
            write_record(job / "exit.json", outcome)
            # Let a live RPC supervisor kill/reap this still-live group leader.
            # If it is dead or unresponsive, the guard kills the group itself.
            os.write(finished_fd, b"1")
            select.select([alive_fd], [], [], .1)
        finally:
            # The guard is the live session/group leader, so this cannot target a
            # recycled process-group ID. Also stops ordinary background children.
            os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
