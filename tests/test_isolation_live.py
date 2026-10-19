"""Opt-in real boundary tests. A skipped test is not release evidence."""
import json
import os
from pathlib import Path

import pytest

from consumer_agent_testing_tool.sandbox import Sandbox


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                    reason="Unverified: set CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER=1 with the prebuilt image")
async def test_actual_boundary_and_intentionally_bypassed_canary():
    async with Sandbox("consumer-agent-testing-tool-sandbox:0.1.0", "isolation-self-test") as sandbox:
        probe = await sandbox.isolation_probe()
        assert probe["passed"], probe
        result = await sandbox.shell("booking slots.list")
        assert result["exit_status"] == 0
        assert "12500" in result["stdout"]
        from consumer_agent_testing_tool.serialization import file_digest
        identity = await sandbox.cli_identity()
        assert identity["executable_sha256"] == file_digest(Path("containers/booking"))
        # Owned disposable self-test only. The evaluated user has no NET_ADMIN.
        await sandbox.required("exec", "--user", "0", sandbox.agent,
                               "iptables", "-I", "OUTPUT", "1", "-p", "tcp", "--dport", "8766", "-j", "ACCEPT")
        broken = await sandbox.isolation_probe()
        assert not broken["passed"]
        assert broken["destination_canary_connections"]


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                    reason="Unverified: real container/Inspect skill integration requires Docker")
@pytest.mark.parametrize("custom,transient", [(False, False), (True, False), (False, True)])
async def test_inspect_loads_skill_and_runs_draft_in_actual_container(
        tmp_path, monkeypatch, custom, transient):
    from inspect_ai.model import Model, ModelAPI, ModelOutput

    from consumer_agent_testing_tool.config import Config, ModelProfile, Target, interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.providers import CapturingAPI, generation
    from consumer_agent_testing_tool.runner import run_scenario
    from consumer_agent_testing_tool.scenario import read_scenario

    class Scripted(ModelAPI):
        step = 0

        async def generate(self, input, tools, tool_choice, config):
            self.step += 1
            if self.step == 1:
                assert any(t.name == "skill" for t in tools)
                return ModelOutput.for_tool_call("test/scripted", "skill", {"command": "booking"},
                                                 tool_call_id="load-skill")
            if self.step == 2:
                result = input[-1]
                assert not result.error
                assert "does not reserve inventory" in result.text
                assert "/workspace/skills/booking" in result.text
                return ModelOutput.for_tool_call("test/scripted", "shell", {
                    "command": """booking reservations.create --args '{"slot_id":"afternoon","contact":"Fixture Alice","idempotency_key":"draft-test"}'"""
                }, tool_call_id="create-draft")
            if transient and self.step == 3:
                assert "429" in input[-1].text
                return ModelOutput.for_tool_call("test/scripted", "shell", {
                    "command": """booking reservations.create --args '{"slot_id":"afternoon","contact":"Fixture Alice","idempotency_key":"draft-test"}'"""
                }, tool_call_id="retry-draft")
            if self.step == (4 if transient else 3):
                result = input[-1]
                assert not result.error
                assert "draft-1" in result.text
                return ModelOutput.for_tool_call("test/scripted", "shell", {
                    "command": """booking reservations.get --args '{"id":"draft-1"}'"""
                }, tool_call_id="read-draft")
            return ModelOutput.for_tool_call("test/scripted", "submit",
                {"answer": "Created a nonbinding draft at $125."}, tool_call_id="submit-answer")

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="test",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={},
                        scenario_id="transient_recovery" if transient else "direct_draft")
    model = Model(CapturingAPI(Scripted("test/scripted"), evidence), generation(profile))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    name = "transient_recovery" if transient else "direct_draft"
    scenario = read_scenario(Path(f"src/consumer_agent_testing_tool/data/scenarios/{name}.yaml"))
    scenario["assertions"].append({"scorer": "exposure.required_capability", "expected": True})
    config = Config()
    if custom:
        config.targets["booking-service"] = Target(
            kind="cli-skill", source_revision="test-custom-cli-v1",
            container_image="consumer-agent-testing-tool-sandbox:0.1.0", service_image="consumer-agent-testing-tool-sandbox:0.1.0",
            command=["python", "-m", "consumer_agent_testing_tool.fixture_service", "serve", "--no-canary"],
            cli_command=["booking"], readiness=["test", "-S", "/control/oracle.sock"],
            oracle_command=["python", "-m", "consumer_agent_testing_tool.fixture_service", "control-stdin"],
            skill=str(Path("src/consumer_agent_testing_tool/data/skills/booking/SKILL.md").resolve()),
        )
    checks = await run_scenario(scenario, config, profile, evidence)
    assert all(c["status"] == "passed" for c in checks)
    from consumer_agent_testing_tool.serialization import file_digest
    assert evidence.manifest["run_lock"]["cli_identity"]["executable_sha256"] == file_digest(
        Path("containers/booking"))
    assert evidence.manifest["run_lock"]["skill_hashes"]["effective_sha256"] == file_digest(
        Path("src/consumer_agent_testing_tool/data/skills/booking/SKILL.md"))
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    assert any(e["event_type"] == "skill_loaded" for e in events)
    assert sum(e["event_type"] == "model_request" for e in events) == (5 if transient else 4)
    evidence.finish("completed", checks)
    assert evidence.manifest["metrics"]["exposure"]
    assert evidence.manifest["metrics"]["selection"]
    if transient:
        assert evidence.manifest["metrics"]["recovery_success"]["numerator"] == 1
    from consumer_agent_testing_tool.evidence import export_redacted
    exported = export_redacted(evidence.path, tmp_path / "export")
    assert list((exported / "inspect").glob("*.eval"))


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                    reason="Unverified: broken-skill behavioral detectors require Docker")
@pytest.mark.parametrize("variant", ["missing_argument", "hostile_skill"])
async def test_broken_skill_diagnostics_and_actual_hostile_attempt(tmp_path, monkeypatch, variant):
    from inspect_ai.model import Model, ModelAPI, ModelOutput

    from consumer_agent_testing_tool.config import Config, ModelProfile, Target, interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.providers import CapturingAPI, generation
    from consumer_agent_testing_tool.runner import run_scenario
    from consumer_agent_testing_tool.scenario import read_scenario

    class Scripted(ModelAPI):
        step = 0

        async def generate(self, input, tools, tool_choice, config):
            self.step += 1
            if self.step == 1:
                return ModelOutput.for_tool_call("test/scripted", "skill", {"command": "booking"},
                                                 tool_call_id="load")
            if self.step == 2:
                if variant == "missing_argument":
                    command = """booking reservations.create --args '{"slot_id":"afternoon","contact":"Fixture Alice"}'"""
                else:
                    command = """python -c 'import socket; socket.create_connection(("forbidden-canary.test",8766),timeout=.2)'"""
                return ModelOutput.for_tool_call("test/scripted", "shell", {"command": command},
                                                 tool_call_id="attempt")
            if variant == "hostile_skill" and self.step == 3:
                assert "timed out" in input[-1].text
                return ModelOutput.for_tool_call("test/scripted", "shell",
                    {"command": "booking slots.list"}, tool_call_id="read")
            return ModelOutput.for_tool_call("test/scripted", "submit",
                {"answer": "The attempted action failed."}, tool_call_id="answer")

    profile = ModelProfile(provider="local-glimmer", api="chat-completions", model_id="test",
                           base_url="http://127.0.0.1:8080/v1")
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    model = Model(CapturingAPI(Scripted("test/scripted"), evidence), generation(profile))
    monkeypatch.setattr("consumer_agent_testing_tool.runner.resolve_model", lambda *args: model)
    config = Config(targets={"booking-service": Target(variant=variant)})
    name = "direct_draft" if variant == "missing_argument" else "hostile_content"
    scenario = read_scenario(Path(f"src/consumer_agent_testing_tool/data/scenarios/{name}.yaml"))
    checks = await run_scenario(scenario, config, profile, evidence)
    if variant == "missing_argument":
        assert any(c["class"] == "discovery/exposure" for c in checks if c["status"] == "failed")
        assert any(c["id"] == "backend.draft_count" and c["actual"] == 0 for c in checks)
        assert evidence.manifest["target_attempts"][0]["arguments_valid"] == 0
    else:
        assert all(c["status"] == "passed" for c in checks)
        assert any(c["id"] == "isolation.no_forbidden_traffic" for c in checks)
        assert any("timed out" in p.read_text() for p in (evidence.path / "artifacts").glob("*.json"))


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                    reason="Unverified: custom stdio process isolation requires Docker")
async def test_custom_stdio_container_excludes_host_credentials_and_routes(tmp_path, monkeypatch):
    import asyncio
    import shutil
    import subprocess
    import uuid

    from consumer_agent_testing_tool.cli import statuses
    from consumer_agent_testing_tool.config import Target, interface_profile
    from consumer_agent_testing_tool.evidence import Evidence
    from consumer_agent_testing_tool.protocol import check_contract

    image = f"consumer-agent-testing-tool-stdio-test:{uuid.uuid4().hex}"
    (tmp_path / "Dockerfile").write_text(
        "FROM consumer-agent-testing-tool-sandbox:0.1.0\nCOPY server.py /probe/server.py\n")
    shutil.copyfile("tests/stdio_boundary_probe.py", tmp_path / "server.py")
    await asyncio.to_thread(subprocess.run,
        ["docker", "build", "--network=none", "-q", "-t", image, str(tmp_path)],
        check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "coordinator-only-secret")
    try:
        target = Target(kind="mcp", source_revision="boundary-probe-double-v1",
            command=["python", "/probe/server.py"], container_image=image,
            contract_cases=[{"tool": "probe", "arguments": {}, "effect": "read"}])
        evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                            profile=interface_profile("mcp-contract-2025-11-25"), lock={})
        checks = await check_contract(target, evidence)
        assert statuses(checks)[0] == 0
        assert any(c["id"] == "call:0:probe" and c["status"] == "passed" for c in checks)
    finally:
        await asyncio.to_thread(subprocess.run, ["docker", "image", "rm", "-f", image],
                                check=True, capture_output=True, timeout=15)


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1",
                    reason="Unverified: connector origin enforcement requires Docker")
async def test_connector_egress_is_separate_from_agent_egress(monkeypatch):
    import asyncio
    import socket
    import uuid

    from consumer_agent_testing_tool.config import Target

    control = Sandbox("consumer-agent-testing-tool-sandbox:0.1.0", "egress-self-test")
    bridge = json.loads(await control.required("network", "inspect", "bridge"))[0]
    gateway = bridge["IPAM"]["Config"][0]["Gateway"]
    remote = f"surface-staging-canary-{uuid.uuid4().hex[:10]}"
    # This is an owned TCP-boundary probe, not a claim to test a remote TLS API.
    program = """
import json,socketserver,threading
from pathlib import Path
hits=[]
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        hits.append(self.server.server_address[1])
        Path('/tmp/hits.json').write_text(json.dumps(hits))
        self.request.sendall(b'owned-staging-canary')
for port in (8767,8768):
    server=socketserver.ThreadingTCPServer(('0.0.0.0',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
Path('/tmp/ready').touch()
threading.Event().wait()
"""
    await control.required("run", "-d", "--name", remote, "--network=bridge", "--pull=never",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,size=1m", "-p", f"{gateway}::8767", "-p", f"{gateway}::8768",
        "--entrypoint", "python", "consumer-agent-testing-tool-sandbox:0.1.0", "-c", program)
    try:
        info = json.loads(await control.required("inspect", remote))[0]
        ports = info["NetworkSettings"]["Ports"]
        allowed = int(ports["8767/tcp"][0]["HostPort"])
        forbidden = int(ports["8768/tcp"][0]["HostPort"])
        for _ in range(30):
            ready = await control.docker("exec", remote, "test", "-f", "/tmp/ready")
            if ready["exit_status"] == 0:
                break
            await asyncio.sleep(.05)
        original = socket.getaddrinfo

        def resolve(host, port, **kwargs):
            if host == "staging.fixture.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (gateway, port))]
            return original(host, port, **kwargs)

        monkeypatch.setattr("consumer_agent_testing_tool.destinations.socket.getaddrinfo", resolve)
        monkeypatch.setenv("STAGING_FIXTURE_CREDENTIAL", "connector-only-canary")
        url = f"https://staging.fixture.test:{allowed}"
        descriptor = Target(kind="cli-skill", source_revision="owned-egress-probe-v1",
            container_image="consumer-agent-testing-tool-sandbox:0.1.0", service_image="consumer-agent-testing-tool-sandbox:0.1.0",
            command=["python", "-m", "consumer_agent_testing_tool.fixture_service", "serve", "--no-canary"],
            cli_command=["booking"], readiness=["test", "-S", "/control/oracle.sock"],
            oracle_command=["python", "-m", "consumer_agent_testing_tool.fixture_service", "control-stdin"],
            skill=str(Path("src/consumer_agent_testing_tool/data/skills/booking/SKILL.md").resolve()),
            base_url=url, permitted_destinations=[url],
            auth={"mode": "api_key", "env": "STAGING_FIXTURE_CREDENTIAL"})
        async with Sandbox("consumer-agent-testing-tool-sandbox:0.1.0", "egress-self-test", descriptor=descriptor) as boundary:
            await boundary.oracle("reset")
            assert (await boundary.isolation_probe())["passed"]
            script = f"""
import json,os,socket
results={{'credential_available':'CONSUMER_AGENT_TESTING_TOOL_TARGET_TOKEN' in os.environ}}
for name,port in [('allowed',{allowed}),('forbidden',{forbidden})]:
    try:
        with socket.create_connection(('staging.fixture.test',port),timeout=.3) as connection:
            results[name]=connection.recv(100).decode()
    except OSError:
        results[name]='blocked'
print(json.dumps(results))
"""
            output = await boundary.required("exec", "--user", "1000:1000",
                                             boundary.service, "python", "-c", script)
            assert json.loads(output) == {"credential_available": True,
                "allowed": "owned-staging-canary", "forbidden": "blocked"}
            agent_script = script.replace("'staging.fixture.test'", repr(gateway))
            agent = await boundary.exec_agent(["python", "-c", agent_script])
            assert json.loads(agent["stdout"]) == {
                "credential_available": False, "allowed": "blocked", "forbidden": "blocked"}
            hits = json.loads(await control.required("exec", remote, "cat", "/tmp/hits.json"))
            assert hits == [8767]
    finally:
        await control.required("rm", "-f", remote)
