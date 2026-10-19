"""Docker/firewall boundary. No agent shell ever runs on the coordinator."""
import asyncio
import json
import shutil
import subprocess
import uuid

from .errors import LabError, Unsupported
from .process import communicate_bounded


def docker_available():
    if not shutil.which("docker"):
        raise Unsupported("Docker is required for the evaluated shell; install/start a Docker "
                          "engine and build the supplied sandbox image during setup")
    result = subprocess.run(["docker", "info", "--format", "{{json .ServerVersion}}"],
                            capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise Unsupported("Docker engine unavailable")


class Sandbox:
    def __init__(self, image: str, run_id: str, variant="good", descriptor=None):
        self.image, self.run_id, self.variant = image, run_id, variant
        suffix = uuid.uuid4().hex[:12]
        self.network = f"surface-{suffix}"
        self.target = f"surface-target-{suffix}"
        self.agent = f"surface-agent-{suffix}"
        self.created = []
        self.image_id = None
        self.descriptor = descriptor if descriptor and descriptor.kind == "cli-skill" else None
        self.custom_images = {}
        self.destinations = []
        self.service = f"surface-service-{suffix}"
        self.closing = False
        self.quiescence = None
        self.finalization_lock = asyncio.Lock()

    async def docker(self, *args, timeout=30, input=None):
        process = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input is not None else None)
        try:
            data = input.encode() if isinstance(input, str) else input
            return await communicate_bounded(process, input=data, timeout=timeout)
        except TimeoutError:
            raise LabError("Docker operation timed out")

    async def required(self, *args):
        result = await self.docker(*args)
        if result["exit_status"]:
            raise LabError(f"Sandbox setup failed: {result['stderr']}")
        return result["stdout"].strip()

    async def __aenter__(self):
        docker_available()
        try:
            self.image_id = await self.required("image", "inspect", self.image, "--format", "{{.Id}}")
            if self.descriptor:
                from .destinations import resolve_destinations, target_credential
                self.destinations = resolve_destinations(self.descriptor)
                credential = target_credential(self.descriptor)
                for name, image in (("cli", self.descriptor.container_image),
                                    ("service", self.descriptor.service_image)):
                    self.custom_images[name] = await self.required(
                        "image", "inspect", image, "--format", "{{.Id}}")
            await self.required("network", "create", *([] if self.destinations else ["--internal"]),
                                self.network)
            self.created.append(("network", self.network))
            common = ["--pull=never", "--network", self.network, "--read-only",
                      "--security-opt=no-new-privileges", "--cap-drop=ALL", "--pids-limit=64",
                      "--memory=256m", "--cpus=1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m"]
            port = self.descriptor.service_port if self.descriptor else 8765
            egress = " ".join(f"{ip}:{entry['port']}" for entry in self.destinations
                              for ip in entry["addresses"])
            hosts = [arg for entry in self.destinations for arg in (
                "--add-host", f"{entry['host']}:{entry['addresses'][0]}")]
            await self.required("run", "-d", "--name", self.target, *common,
                "--cap-add=NET_ADMIN", "--cap-add=NET_RAW", "--tmpfs", "/data:rw,size=16m",
                "--cap-add=SETPCAP",
                "--tmpfs", "/control:rw,size=1m", "--env", f"SERVICE_PORT={port}",
                "--env", f"CONNECTOR_EGRESS={egress}", *hosts, self.image_id,
                "target-gate" if self.descriptor else "target", self.run_id, self.variant)
            self.created.append(("container", self.target))
            details = json.loads(await self.required("inspect", self.target))[0]
            target_ip = details["NetworkSettings"]["Networks"][self.network]["IPAddress"]
            await self.required("run", "-d", "--name", self.agent, *common,
                "--cap-add=NET_ADMIN", "--cap-add=NET_RAW", "--cap-add=SETUID", "--cap-add=SETGID",
                "--cap-add=SETPCAP", "--tmpfs", "/workspace:rw,uid=1000,gid=1000,size=32m",
                "--add-host", f"forbidden-canary.test:{target_ip}",
                "--env", f"FIXTURE_IP={target_ip}", "--env", f"SERVICE_PORT={port}",
                self.image_id, "agent")
            self.created.append(("container", self.agent))
            for _ in range(30):
                result = await self.docker("exec", self.target, "python", "-m",
                    "consumer_agent_testing_tool.fixture_service", "control", "--request", '{"operation":"reset"}')
                if result["exit_status"] == 0:
                    break
                await asyncio.sleep(.1)
            else:
                raise LabError("Fixture control socket did not become ready")
            # Docker exec is available before entrypoint firewall setup completes.
            for _ in range(30):
                ready = await self.docker("exec", self.agent, "test", "-f", "/tmp/boundary-ready")
                if ready["exit_status"] == 0:
                    break
                await asyncio.sleep(.1)
            else:
                raise LabError("Agent firewall did not become ready")
            if self.descriptor:
                isolated = ["--pull=never", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                    "--cpus=1", "--user=1000:1000", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m"]
                service_env = []
                if self.descriptor.base_url:
                    service_env += ["--env", f"CONSUMER_AGENT_TESTING_TOOL_REMOTE_URL={self.descriptor.base_url}"]
                if credential:
                    service_env += ["--env", f"CONSUMER_AGENT_TESTING_TOOL_TARGET_TOKEN={credential}"]
                await self.required("run", "-d", "--name", self.service, *isolated, *service_env,
                    "--network", f"container:{self.target}",
                    "--tmpfs", "/data:rw,uid=1000,gid=1000,size=32m",
                    "--tmpfs", "/control:rw,uid=1000,gid=1000,size=1m",
                    "--env", f"CONSUMER_AGENT_TESTING_TOOL_RUN_ID={self.run_id}", "--env", f"SERVICE_PORT={port}",
                    "--entrypoint", self.descriptor.command[0], self.custom_images["service"],
                    *self.descriptor.command[1:])
                self.created.append(("container", self.service))
                agent_gate = self.agent
                self.agent = f"{agent_gate}-cli"
                await self.required("run", "-d", "--name", self.agent, *isolated,
                    "--network", f"container:{agent_gate}",
                    "--tmpfs", "/workspace:rw,uid=1000,gid=1000,size=32m",
                    "--env", f"FIXTURE_IP={target_ip}", "--env", f"SERVICE_PORT={port}",
                    "--env", f"CONSUMER_AGENT_TESTING_TOOL_TARGET_URL=http://{target_ip}:{port}",
                    "--entrypoint", "/bin/sleep", self.custom_images["cli"], "infinity")
                self.created.append(("container", self.agent))
                for _ in range(30):
                    result = await self.docker("exec", "--user", "1000:1000",
                                               self.service, *self.descriptor.readiness, timeout=3)
                    if result["exit_status"] == 0:
                        break
                    await asyncio.sleep(.1)
                else:
                    logs = await self.docker("logs", "--tail", "30", self.service)
                    raise LabError(f"Custom service readiness check failed: "
                                   f"{logs['stdout']} {logs['stderr']}")
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def shell(self, command: str, timeout=20):
        return await self.exec_agent(["/bin/sh", "-c", command], timeout=timeout)

    async def cli_identity(self):
        command = self.descriptor.cli_command if self.descriptor else ["booking"]
        probe = """
import hashlib, json, pathlib, shutil, sys
command = json.loads(sys.argv[1])
resolved = shutil.which(command[0])
if not resolved:
    raise SystemExit('CLI executable is not installed')
path = pathlib.Path(resolved).resolve()
with path.open('rb') as stream:
    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
print(json.dumps({'command': command, 'executable': str(path),
                  'executable_sha256': checksum, 'version': None,
                  'version_basis': 'No common CLI version protocol; immutable image and executable hashes recorded'}))
"""
        result = await self.exec_agent(["python", "-c", probe, json.dumps(command)])
        if result["exit_status"] or result["output_capture"]["stdout_truncated"]:
            raise LabError("Cannot identify the evaluated CLI executable")
        return json.loads(result["stdout"])

    async def exec_agent(self, command, *, input=None, cwd="/workspace", env=None, timeout=20):
        if self.closing:
            raise LabError("The evaluated container is finalizing; further execution is forbidden",
                           code=3, category="policy/approval")
        arguments = ["exec", "-i", "--user", "1000:1000", "--workdir", cwd]
        for key, value in (env or {}).items():
            arguments.extend(["--env", f"{key}={value}"])
        arguments.extend([self.agent, "/usr/bin/timeout", "--kill-after=1", str(timeout), *command])
        return await self.docker(*arguments, input=input, timeout=timeout + 3)

    async def quiesce(self):
        """Stop all evaluated processes, then drain the independently controlled services."""
        self.closing = True
        async with self.finalization_lock:
            return await self._quiesce()

    async def _quiesce(self):
        if self.quiescence is not None:
            return dict(self.quiescence)
        state = json.loads(await self.required("inspect", self.agent, "--format", "{{json .State}}"))
        if state.get("Running") is True:
            await self.required("kill", "--signal=KILL", self.agent)
            await self.required("wait", self.agent)
        state = json.loads(await self.required("inspect", self.agent, "--format", "{{json .State}}"))
        if (state.get("Running") is not False or state.get("Restarting") is not False
                or state.get("Paused") is not False or state.get("Status") != "exited"):
            raise LabError("Evaluated processes were not confirmed stopped", code=3,
                           category="observation gap")
        # A stopped client does not cancel work already accepted by its service.
        # The private service hook must close ingress and drain that work first.
        confirmations = [await self.oracle("quiesce")]
        if self.descriptor:
            confirmations.append(await self.canary_oracle("quiesce"))
        if any(not isinstance(row, dict) or row.get("quiesced") is not True
               or row.get("run_id") != self.run_id for row in confirmations):
            raise LabError("Service did not confirm quiescence for this trial", code=3,
                           category="observation gap")
        self.quiescence = {
            "run_id": self.run_id, "agent_stopped": True,
            "public_requests_drained": True, "canary_requests_drained": True,
            "scope": "Evaluated container stopped; private service hooks confirm closed ingress "
                     "and drained requests. Remote service finality depends on its oracle contract.",
        }
        return dict(self.quiescence)

    async def oracle(self, operation="snapshot", **parameters):
        if self.descriptor:
            result = await self.docker("exec", "-i", "--user", "1000:1000", self.service,
                *self.descriptor.oracle_command,
                input=json.dumps({"operation": operation, "run_id": self.run_id, **parameters}) + "\n")
            if result["exit_status"] or result["output_capture"]["stdout_truncated"]:
                raise LabError("Custom service oracle failed or exceeded the capture limit")
            try:
                snapshot = json.loads(result["stdout"])
            except ValueError as exc:
                raise LabError("Custom service oracle must return one JSON object") from exc
            if not isinstance(snapshot, dict) or snapshot.get("error"):
                raise LabError("Custom service oracle did not return a successful object")
            if operation == "snapshot":
                from .fixture import oracle_metrics
                try:
                    oracle_metrics(snapshot)
                except (KeyError, TypeError) as exc:
                    raise LabError("Custom oracle does not implement the read/draft snapshot contract") from exc
                snapshot["canary_connections"] = (await self.canary_oracle())["canary_connections"]
            return snapshot
        return await self.canary_oracle(operation, **parameters)

    async def canary_oracle(self, operation="snapshot", **parameters):
        output = await self.required("exec", self.target, "python", "-m",
            "consumer_agent_testing_tool.fixture_service", "control", "--request",
            json.dumps({"operation": operation, **parameters}))
        result = json.loads(output)
        if "error" in result:
            raise LabError(result["error"])
        return result

    async def isolation_probe(self):
        script = """python - <<'PY'
import json, os, socket
results = {}
for host,port in [(os.environ['FIXTURE_IP'],8766),('host.docker.internal',80),('172.17.0.1',80)]:
    try:
        with socket.create_connection((host,port),timeout=1):
            results[host] = 'REACHABLE'
    except OSError:
        results[host] = 'blocked'
results['control_socket'] = os.path.exists('/control/oracle.sock')
results['docker_socket'] = os.path.exists('/var/run/docker.sock')
results['aws_credentials'] = os.path.exists('/root/.aws/credentials')
results['provider_variables'] = [k for k in os.environ if k.startswith(('AWS_', 'OPENAI_', 'ANTHROPIC_', 'META_', 'MODEL_API'))]
print(json.dumps(results))
PY"""
        result = await self.shell(script)
        if result["exit_status"]:
            raise LabError("Isolation probe failed to execute")
        observations = json.loads(result["stdout"])
        state = await self.oracle()
        passed = all(value == "blocked" if key not in ("control_socket", "docker_socket", "aws_credentials", "provider_variables")
                     else not value for key, value in observations.items())
        passed = passed and not state["canary_connections"]
        return {"passed": passed, "observations": observations,
                "enforcing_layer": "container network namespace iptables OUTPUT default DROP",
                "destination_canary_connections": state["canary_connections"]}

    async def __aexit__(self, *args):
        for kind, name in reversed(self.created):
            await self.docker("rm", "-f", name) if kind == "container" else await self.docker(
                "network", "rm", name)
        self.created.clear()
