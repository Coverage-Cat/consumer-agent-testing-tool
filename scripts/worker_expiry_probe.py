"""Standalone stdlib probe for an installed reference gateway, also runnable in Linux."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path


def probe(gateway, root):
    root.mkdir(parents=True)
    observed = []
    for fault in ("expiry", "rpc_killed", "guard_killed", "async_expiry", "async_supervisor_killed"):
        trial = "worker-" + uuid.uuid4().hex
        job = root / "jobs" / trial
        # Includes provision/ready/seed plus both Python process startups.
        # A too-short allowance can expire before the positive control exists,
        # proving no supervision behavior at all on a slower worker.
        deadline_seconds = 5
        deadline = time.time() + deadline_seconds
        child_code = (
            "import time\nfrom pathlib import Path\nend=time.monotonic()+20\n"
            "while time.monotonic()<end:\n"
            " Path('child-heartbeat').write_text(str(time.monotonic()))\n time.sleep(.025)\n")
        code = ("import subprocess,sys,time\nfrom pathlib import Path\n"
            f"subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
            "print('synthetic installed expiry probe',flush=True)\n"
            "end=time.monotonic()+20\n"
            "while time.monotonic()<end:\n"
            " Path('heartbeat').write_text(str(time.monotonic()))\n time.sleep(.025)\n")
        package = root / (trial + ".zip")
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("main.py", code)
            archive.writestr("bundle-lock.json", json.dumps({
                "files": {"main.py": hashlib.sha256(code.encode()).hexdigest()},
                "command": [sys.executable, "main.py"]}))

        def spawn(operation, _trial=trial, _deadline=deadline, **arguments):
            child = subprocess.Popen([sys.executable, "-I", str(gateway), str(root / "jobs")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            child.stdin.write(json.dumps({"operation": operation, "trial_id": _trial,
                                         "expires_at": _deadline, **arguments}))
            child.stdin.close()
            child.stdin = None
            return child

        def call(operation, **arguments):
            child = spawn(operation, **arguments)
            output, error = child.communicate(timeout=12)
            assert child.returncode == 0, error
            return json.loads(output)

        assert call("provision")["trial_id"] == trial
        runtime = call("ready")["runtime"]
        assert runtime["kind"] == "posix-process-group-lease-v1"
        assert call("seed", bundle=str(package), sha256=hashlib.sha256(
            package.read_bytes()).hexdigest())["seeded"] is True
        asynchronous = fault.startswith("async_")
        rpc = spawn("run", **({"mode": "start"} if asynchronous else {}))
        try:
            if asynchronous:
                started, error = rpc.communicate(timeout=1)
                assert rpc.returncode == 0 and json.loads(started)["run_state"] == "running", error
            end = time.monotonic() + 4
            while not (job / "child-heartbeat").is_file() and time.monotonic() < end:
                time.sleep(.025)
            if not (job / "child-heartbeat").is_file():
                # Keep the positive control mandatory, but preserve the reason
                # before the finally block destroys the job.
                diagnostic = {"fault": fault, "deadline_remaining": deadline - time.time(),
                              "rpc_returncode": rpc.poll()}
                for name in ("exit.json", "stdout.log", "stderr.log"):
                    if (job / name).is_file():
                        diagnostic[name] = (job / name).read_text()[-4096:]
                if rpc.poll() is not None:
                    diagnostic["rpc_reply"], diagnostic["rpc_error"] = rpc.communicate(timeout=1)
                raise AssertionError("Worker positive control never started: " + json.dumps(diagnostic))
            before = (job / "child-heartbeat").read_text()
            time.sleep(.1)
            assert (job / "child-heartbeat").read_text() != before
            if fault == "rpc_killed":
                rpc.kill()
                rpc.wait(timeout=3)
            elif fault == "guard_killed":
                guard = json.loads((job / "guard.json").read_text())
                os.kill(guard["pid"], signal.SIGKILL)
            elif fault == "async_supervisor_killed":
                supervisor = json.loads((job / "supervisor.json").read_text())
                os.kill(supervisor["pid"], signal.SIGKILL)
            _output, error = rpc.communicate(timeout=5)
            assert rpc.returncode in (0, -signal.SIGKILL), error
            # An asynchronous RPC has already returned; observation must cover
            # the declared execution deadline, not an unrelated three seconds.
            end = time.monotonic() + deadline_seconds + 1
            while not (job / "exit.json").is_file() and time.monotonic() < end:
                time.sleep(.025)
            assert (job / "exit.json").is_file(), f"{fault}: no terminal receipt after deadline"
            time.sleep(.15)
            before = [(job / name).read_text() for name in ("heartbeat", "child-heartbeat")]
            time.sleep(.15)
            assert before == [(job / name).read_text() for name in ("heartbeat", "child-heartbeat")]
            exported = call("export")
            assert exported["complete"] is True, exported
            result = json.loads(exported["files"]["exit.json"])
            assert result["reason"] == {
                "expiry": "expired", "rpc_killed": "supervisor_lost", "guard_killed": "guard_lost",
                "async_expiry": "expired", "async_supervisor_killed": "supervisor_lost"}[fault]
            assert result["exit_code"] != 0
            assert call("destroy")["destroyed"] is True
            assert not job.exists()
            observed.append({"fault": fault, "reason": result["reason"], "runtime": runtime,
                             "deadline_seconds": deadline_seconds,
                             "positive_control": True, "process_tree_stopped": True,
                             "evidence_collected": True, "cleanup": True})
        finally:
            call("cancel")
            rpc.communicate(timeout=5)
            call("destroy")
    return {"status": "passed", "cases": observed,
            "limits": "Reference process groups only; not hostile isolation or a cloud-provider lifecycle"}


if __name__ == "__main__":
    print(json.dumps(probe(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
