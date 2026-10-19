"""check suites that read no configuration (poppy, gadget, muse-guidelines) under --offline, and the options
muse-guidelines refuses instead of ignoring."""
import json
import sys
from types import ModuleType

import pytest

from consumer_agent_testing_tool import offline
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError, Unsupported

URL = "https://cedar.example/mcp"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # No consumer-agent-testing-tool.yaml here.
    return tmp_path


async def test_offline_network_suites_give_their_own_message_without_a_configuration(workspace):
    for suite, message in (("poppy", "--offline does not apply to --suite poppy"),
                           ("muse-guidelines", "--offline does not apply to --suite muse-guidelines")):
        args = parser().parse_args(["check", URL, "--suite", suite, "--offline"])
        assert offline.bootstrap(args) is None  # Nothing read: there is no configuration to read.
        with pytest.raises(Unsupported, match=message):
            await execute(args)


def test_offline_gadget_runs_confined_without_a_configuration(workspace, monkeypatch):
    launched = []

    def run(command, env, check):
        launched.append((command, json.loads(env["CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY"])))
        return type("Done", (), {"returncode": 7})()
    monkeypatch.setattr(offline.subprocess, "run", run)
    monkeypatch.setattr(offline.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(offline, "kernel_denies_default_egress", lambda pid=None: False)
    args = parser().parse_args(["check", "gadget.json", "--suite", "gadget", "--offline"])
    assert offline.bootstrap(args) == 7  # The confined child's exit code.
    [(command, policy)] = launched
    assert command[:3] == ["/usr/bin/sandbox-exec", "-p", offline.policy_for([])]
    assert policy["loopback_ports"] == [] and policy["default_outbound"] == "deny"
    # Inside the guard the suite itself runs; elsewhere than macOS it is refused, never run unconfined.
    monkeypatch.setenv("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY", "{}")
    monkeypatch.setattr(offline, "kernel_denies_default_egress", lambda pid=None: True)
    assert offline.bootstrap(args) is None
    monkeypatch.delenv("CONSUMER_AGENT_TESTING_TOOL_OFFLINE_POLICY")
    monkeypatch.setattr(offline.platform, "system", lambda: "Linux")
    with pytest.raises(Unsupported, match="requires macOS sandbox-exec"):
        offline.bootstrap(args)


async def test_muse_guidelines_refuses_options_it_would_ignore(workspace, monkeypatch):
    checked = []
    module = ModuleType("consumer_agent_testing_tool.guidelines")

    async def run_check(endpoint, **options):
        checked.append(endpoint)
        return 0
    module.run_check = run_check
    monkeypatch.setitem(sys.modules, "consumer_agent_testing_tool.guidelines", module)
    for extra in (["--model-profile", "agent"], ["--execution", "worker"], ["--interface-profile", "api-tools"],
                  ["--profile", "legacy"], ["--model-profile", "agent", "--offline"],
                  ["--profile", "x", "--drift-check"]):
        with pytest.raises(LabError, match="does not apply to --suite muse-guidelines") as caught:
            await execute(parser().parse_args(["check", URL, "--suite", "muse-guidelines", *extra]))
        assert caught.value.code == 2
    assert checked == []
    assert await execute(parser().parse_args(["check", URL, "--suite", "muse-guidelines", "--execution", "local"])) == 0
    assert checked == [URL]
