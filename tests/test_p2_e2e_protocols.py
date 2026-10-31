"""P3 protocols and hosts end to end through the installed CLI, run as a child process (main()'s exit code, its
stdout and stderr contract, and the sealed evidence it leaves):

- check --suite poppy against the loopback draft 0.1 test company (test_poppy_support.py), discovery only and
  with a probe manifest;
- check --suite gadget on a valid and an over-limit link.register;
- the contract lane's mcp-contract-2026-07-28 profile against the loopback connector fixture, an MCP SDK 2.3.0
  server, and against its handshake-only variant;
- surface-run with the shipped examples/surface-preferences.yaml against scripts/surface_fixture.mjs: the
  restrictive-default CSP, the sandbox proxy's own origin and _meta.ui.visibility.

Every document is synthetic and every service listens on 127.0.0.1; no model runs.
"""
import asyncio
import json
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from test_connector_e2e_support import STAY, fixture, oracle, public_text
from test_gadget_check import esp32_message, linux_params, message, spec
from test_poppy_support import ACCOUNT, company, probe_manifest
from test_protocol_modern import modern_server, serve

from consumer_agent_testing_tool import gadget, poppy
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.serialization import digest, file_digest

ROOT = Path(__file__).resolve().parents[1]
MODERN = "mcp-contract-2026-07-28"
# MCP Apps 2026-01-26 "Restrictive Default", plus the sandbox proxy's own directives (host.mjs DEFAULT_CSP).
DEFAULT_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; media-src 'self' data:; connect-src 'none'; frame-src 'none'; "
               "object-src 'none'; base-uri 'self'")
APP_ORIGIN, HOST_ORIGIN = "https://surface-app.invalid", "https://surface-host.invalid"
# What a child inherits: the workspace-local HOME/TMPDIR/XDG layout conftest set, never provider settings.
INHERITED = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "SYSTEMROOT")
POPPY_ENV = {"POPPY_TEST_EMAIL": ACCOUNT["email"], "POPPY_TEST_PASSWORD": ACCOUNT["password"]}


@dataclass
class Child:
    code: int
    out: str
    err: str
    cwd: Path

    def printed(self) -> Path:
        """The run the command printed last on stdout."""
        path = Path(self.out.splitlines()[-1])
        return path if path.is_absolute() else self.cwd / path


def command():
    script = Path(sys.executable).with_name("consumer-agent-testing-tool")
    return [str(script)] if script.is_file() else [sys.executable, "-m", "consumer_agent_testing_tool.cli"]


async def cli(*arguments, cwd, env=None, timeout=300) -> Child:
    """The installed console script in a child process, as a user runs it."""
    environment = {name: os.environ[name] for name in INHERITED if name in os.environ} | (env or {})
    process = await asyncio.create_subprocess_exec(*command(), *map(str, arguments), cwd=cwd, env=environment,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return Child(process.returncode, out.decode(), err.decode(), Path(cwd))


def sealed(child: Child) -> tuple[Path, dict, list]:
    """The printed run, verified against its seal."""
    path = child.printed()
    record = playback(path)
    return path, record["result"], record["events"]


def kinds(events, kind):
    return [event for event in events if event.get("kind") == kind]


@pytest.fixture
def workspace(tmp_path):
    return tmp_path  # The children run here: CLI paths stay inside the directory a command runs from.


async def report_and_export(run: Path, workspace: Path) -> dict:
    """report --format json shows the sealed result; export writes a verified, redacted copy elsewhere."""
    shown = await cli("report", run, "--format", "json", cwd=workspace)
    assert shown.code == 0, shown.err
    exported = await cli("export", run, "--output", workspace / "exports" / run.name, cwd=workspace)
    assert exported.code == 0, exported.err
    copy = playback(exported.printed())["result"]
    assert copy["export"]["redacted"] is True and copy["run_id"] == run.name
    return json.loads(shown.out)


# --- check --suite poppy ------------------------------------------------------------------------------

async def test_check_suite_poppy_discovery_reads_the_company_documents_and_nothing_else(workspace):
    with company() as (test_company, base):
        child = await cli("check", base, "--suite", "poppy", cwd=workspace)
        untouched = (test_company.sessions, test_company.tokens, test_company.fetched, test_company.effects)
    assert child.code == 0, child.err
    path, result, events = sealed(child)
    assert path.parent == (workspace / "runs").resolve() and path.name.startswith("poppy-")
    assert child.err.strip() == f"poppy completed: 0 fail, 0 warn, 0 info ({poppy.LABEL}). Evidence: {path}"
    assert (result["kind"], result["status"], result["exit_code"], result["label"]) == (
        "poppy-check", "completed", 0, poppy.LABEL)
    assert result["target"] == {"origin": base, "probes": False, "identity": None, "manifest": None}
    assert result["probes_run"] == [] and result["findings"] == [] and result["compatibility_pass"] is False
    document = result["company"]["poppy_json"]
    assert document["organization"]["domain"] == "127.0.0.1"
    assert result["run_lock"]["poppy_json_sha256"] == digest(document)
    assert result["company"]["authorization_server_metadata"]["issuer"] == base
    # Read-only: poppy.json, the issuer's RFC 8414 metadata and the MCP API's RFC 9728 metadata, all GETs.
    exchanges = kinds(events, "http_exchange")
    assert result["http_exchanges"] == len(exchanges) == 3
    assert {(row["request"]["method"], urlsplit(row["request"]["url"]).path) for row in exchanges} == {
        ("GET", "/.well-known/poppy.json"), ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/oauth-protected-resource/mcp")}
    assert untouched == ({}, {}, [], 0)  # No Session, no client-metadata fetch, no operation.
    assert all(row["status"] == "no-finding" for row in result["checks"])
    assert result["muse"]["basis"] == "unofficial" and result["specification"]["commit"] == poppy.SPEC_COMMIT
    shown = await report_and_export(path, workspace)
    assert (shown["run_id"], shown["status"]) == (path.name, "completed")


async def test_check_suite_poppy_with_a_probe_manifest_runs_every_probe_and_keeps_secrets_out(workspace):
    with company() as (test_company, base):
        (workspace / "probes.yaml").write_text(yaml.safe_dump(probe_manifest(base)))
        child = await cli("check", base, "--suite", "poppy", "--manifest", "probes.yaml", cwd=workspace,
                          env=POPPY_ENV)
        minted = [*test_company.tokens, *test_company.accounts]
        effects, fetched = test_company.effects, list(test_company.fetched)
        accounts = [(account["scope"], account["revoked"]) for account in test_company.accounts.values()]
    assert child.code == 0, child.err
    path, result, events = sealed(child)
    assert (result["status"], result["summary"]) == ("completed", {"fail": 0, "warn": 0, "info": 0, "blocked": 0})
    # Both sign-ins' Account Tokens are revoked: the graded sign-out's, then the operation's broader one.
    assert accounts == [("poppy:read", True), ("poppy:read poppy:write", True)]
    assert [row["probe"] for row in kinds(events, "http_exchange") if row["probe"].startswith("sign-out")] == [
        "sign-out", "sign-out:operations"]
    manifest = poppy.load_manifest(workspace / "probes.yaml")
    assert result["target"] == {"origin": base, "probes": True, "identity": "loopback", "manifest": {
        "path": str(workspace / "probes.yaml"), "sha256": digest(manifest.model_dump(mode="json"))}}
    exercised = {item.split(":")[0] for item in result["probes_run"]}
    assert {"4.2-start-session", "4.2-renew", "4.1-client-metadata", "4.3-proof-binding", "4.3-token-in-url",
            "5-browser-join", "5-browser-checks", "4.7-mediated", "6-insufficient-scope", "4.8-account-token",
            "4.9-sign-out", "ops-confirm", "ops-at-most-once", "ops-cancel"} <= exercised
    assert all(row["status"] == "no-finding" for row in result["checks"])
    assert any(row["detail"] == "no finding (exercised)" for row in result["checks"])
    assert effects == 1  # Three confirms, two of them concurrent: one exchange.
    assert any(url.endswith("/agent.json") for url in fetched)  # The company read the probe's client metadata.
    assert result["http_exchanges"] == len(kinds(events, "http_exchange")) > 30
    shown = await report_and_export(path, workspace)
    assert shown["probes_run"] == result["probes_run"]
    # No test-account credential, Session or Account Token, assertion or proof in the run or its export.
    for text in (public_text(path), public_text(workspace / "exports" / path.name)):
        assert ACCOUNT["password"] not in text and ACCOUNT["email"] not in text
        assert not [token for token in minted if token in text] and "eyJ" not in text


@pytest.mark.parametrize("defect,env,code,line", [
    ("double-perform", POPPY_ENV, 1, "poppy failed: 1 fail, 0 warn, 0 info ("),
    ("code-required", POPPY_ENV, 3, "poppy blocked: 0 fail, 0 warn, 0 info, 2 blocked ("),
    (None, {}, 2, f"poppy setup error ({poppy.LABEL}): "),  # The test account's credentials are unset.
], ids=["failed", "blocked", "setup"])
async def test_check_suite_poppy_exit_codes_reach_the_shell(workspace, defect, env, code, line):
    with company(*([defect] if defect else [])) as (_, base):
        (workspace / "probes.json").write_text(json.dumps(probe_manifest(base)))
        child = await cli("check", base, "--suite", "poppy", "--manifest", "probes.json", cwd=workspace, env=env)
    assert child.code == code, child.err
    path, result, events = sealed(child)
    assert result["exit_code"] == code and child.err.startswith(line) and str(path) in child.err
    if code == 1:
        assert "failing: ops-at-most-once (operations)" in child.err
    if code == 3:
        assert result["status"] == "blocked" and "one-time code" in result["detail"]
        # One sign-in, then none: each further attempt would text the person another one-time code.
        probes = [row["probe"] for row in kinds(events, "http_exchange")]
        assert [probe for probe in probes if probe.startswith("sign-in:")] == ["sign-in:account"]
        assert "session:operations" not in probes
    if code == 2:
        assert result["failure_class"] == "setup/infrastructure" and result["checks"] == []
        assert "POPPY_TEST_EMAIL" in result["detail"]


async def test_check_suite_poppy_signs_the_test_account_out_when_a_probe_fails(workspace):
    with company() as (test_company, base):
        manifest = probe_manifest(base)
        manifest["operations"]["effects"]["pointer"] = "/performed"  # an object: the probes stop with exit 2
        (workspace / "probes.json").write_text(json.dumps(manifest))
        child = await cli("check", base, "--suite", "poppy", "--manifest", "probes.json", cwd=workspace,
                          env=POPPY_ENV)
        accounts = [(account["scope"], account["revoked"]) for account in test_company.accounts.values()]
    assert child.code == 2, child.err
    _, result, events = sealed(child)
    assert result["failure_stage"] == "probes" and "gave no integer at /performed" in result["detail"]
    # The failure happened after both sign-ins; neither Account Token outlives the run.
    assert accounts == [("poppy:read", True), ("poppy:read poppy:write", True)]
    assert [row["probe"] for row in kinds(events, "http_exchange") if row["probe"].startswith("sign-out")] == [
        "sign-out:account", "sign-out:operations"]


async def test_check_suite_poppy_refuses_offline_before_reading_any_configuration(workspace):
    # No configuration file here: the suite's own refusal, never "Invalid configuration".
    child = await cli("check", "https://stormline.example", "--suite", "poppy", "--offline", cwd=workspace)
    assert (child.code, child.out) == (3, "")
    assert child.err.strip() == "unsupported: --offline does not apply to --suite poppy, which connects to the company"
    assert not (workspace / "runs").exists()


# --- check --suite gadget -----------------------------------------------------------------------------

async def test_check_suite_gadget_passes_a_valid_registration_through_the_cli(workspace):
    (workspace / "params.json").write_text(json.dumps(linux_params()))  # register_params() alone is wrapped
    (workspace / "esp32.yaml").write_text(yaml.safe_dump(esp32_message(), allow_unicode=True))
    for name, platform_name, assembled in (("params.json", "linux", True), ("esp32.yaml", "esp32", False)):
        child = await cli("check", name, "--suite", "gadget", cwd=workspace)
        assert child.code == 0, child.err
        path, result, events = sealed(child)
        assert (result["kind"], result["status"], result["label"]) == ("gadget-check", "completed", gadget.LABEL)
        assert (result["device"]["platform"], result["device"]["envelope_assembled"]) == (platform_name, assembled)
        assert result["summary"]["fail"] == 0 and result["run_lock"]["sdk_commit"] == gadget.SDK_COMMIT
        assert result["run_lock"]["document_sha256"] == file_digest(workspace / name)
        assert result["locations"]["egress"] == "none" and result["compatibility_pass"] is False
        assert [event["kind"] for event in events] == ["sdk", "document", "gadget_findings", "run_finished"]
        assert child.err.startswith("gadget completed: 0 fail, ") and str(path) in child.err
    limits = {row["id"]: row for row in result["limits"]}  # The ESP32 run: its own print buffer applies.
    assert limits["register-bytes-esp32"]["applies"] is True
    assert limits["register-bytes-esp32"]["measured"] == gadget.wire_bytes(esp32_message(), "esp32")
    shown = await report_and_export(path, workspace)
    assert shown["device"] == result["device"]


async def test_check_suite_gadget_fails_an_over_limit_registration_through_the_cli(workspace):
    # ESP32: printed into an 8 KB buffer. Linux: bound only by the VM's 256 KB per message.
    esp32 = esp32_message()
    esp32["params"]["commands_v2"]["device.health"]["description"] += " Battery history." * 500
    linux = message(linux_params(commands_v2={"system.run": spec("Run a command. " + "x" * gadget.MESSAGE_BYTES)}))
    for name, document, rule, limit in (("esp32.json", esp32, "register.size.esp32", "register-bytes-esp32"),
                                        ("linux.json", linux, "register.size", "message-bytes")):
        (workspace / name).write_text(json.dumps(document))
        child = await cli("check", name, "--suite", "gadget", cwd=workspace)
        assert child.code == 1, child.err
        path, result, _ = sealed(child)
        assert (result["status"], result["exit_code"], result["failure_class"]) == ("failed", 1, "gadget")
        assert [row["rule"] for row in result["findings"] if row["severity"] == "fail"] == [rule]
        assert any(row["id"] == rule and row["status"] == "failed" for row in result["checks"])
        row = next(row for row in result["limits"] if row["id"] == limit)
        assert row["applies"] is True
        assert row["measured"] == gadget.wire_bytes(document, document["params"]["platform"]) > row["limit"]
        assert f"failing: {rule}" in child.err and str(path) in child.err
    # The same oversized ESP32 bytes from a Linux device stay inside the VM's 256 KB limit.
    assert gadget.wire_bytes(esp32, "linux") < gadget.MESSAGE_BYTES


@pytest.mark.skipif(platform.system() != "Darwin", reason="--offline enforcement uses macOS sandbox-exec")
async def test_check_suite_gadget_offline_runs_confined_and_seals_its_boundary(workspace):
    (workspace / "device.json").write_text(json.dumps(message(linux_params())))
    child = await cli("check", "device.json", "--suite", "gadget", "--offline", cwd=workspace)
    assert child.code == 0, child.err
    _, result, _ = sealed(child)
    assert result["status"] == "completed"
    policy = result["run_lock"]["offline_policy"]
    assert (policy["enforcing_layer"], policy["default_outbound"], policy["loopback_ports"]) == (
        "macOS kernel Seatbelt", "deny", [])
    assert result["locations"]["egress"] == "kernel-enforced default-deny coordinator"


# --- the contract lane at MCP 2026-07-28 --------------------------------------------------------------

def contract_config(workspace, url, case=None):
    """One HTTP MCP target (by default the connector fixture) with one developer-declared read case."""
    (workspace / "config.json").write_text(json.dumps({"targets": {"cedar": {
        "kind": "mcp", "transport": "streamable-http", "base_url": url, "permitted_destinations": [url],
        "source_revision": "cedar-stays-fixture-1.0.0",
        "contract_cases": [case or {"tool": "search_rooms", "arguments": STAY, "effect": "read"}]}}}))
    return workspace / "config.json"


def recorded(child: Child) -> tuple[Path, dict, list]:
    """The contract lane's run: its manifest and events."""
    path = child.printed()
    return path, json.loads((path / "manifest.json").read_text()), [
        json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]


async def test_the_contract_lane_records_the_2026_07_28_era_of_an_sdk_server(workspace):
    async with fixture(variant="good", auth="none") as running:
        child = await cli("check", "cedar", "--config", contract_config(workspace, running.mcp_url),
                          "--interface-profile", MODERN, cwd=workspace)
        rpc = oracle(running)["rpc"]
    assert child.code == 0, child.err
    path, manifest, events = recorded(child)
    assert (manifest["execution_status"], manifest["reporting_status"]) == ("completed", "completed")
    lock = manifest["run_lock"]
    assert (lock["interface_profile"]["name"], lock["interface_profile"]["protocol_era"]) == (MODERN, "modern")
    assert (lock["negotiated_protocol_revision"], lock["negotiated_protocol_era"]) == ("2026-07-28", "modern")
    assert lock["server_info"]["name"] == "cedar-stays"  # The SDK server's _meta serverInfo stamp.
    assert manifest["protocol_negotiation"] == {
        "offered": "2026-07-28", "profile_revision": "2026-07-28", "method": "server/discover",
        "negotiated": "2026-07-28", "era": "modern", "supported_versions": ["2026-07-28"]}
    checks = {check["id"]: check for check in manifest["checks"]}
    assert checks["negotiation"] == {"id": "negotiation", "status": "passed", "revision": "2026-07-28",
                                     "offered": "2026-07-28"}
    assert checks["unsupported-version-rejection"]["actual"] == {"code": -32022, "supported": ["2026-07-28"]}
    assert checks["call:0:search_rooms"]["status"] == checks["stateless-http"]["status"] == "passed"
    assert checks["stateless-http"]["actual"]["session_ids_issued"] == 0 and "ping" not in checks
    upstream = checks["upstream-conformance"]
    assert (upstream["status"], upstream["required"], upstream["gap"]) == ("unsupported", False, "upstream")
    # Every exchange was one stateless POST stamped with its own version; no initialize ever went out.
    trace = manifest["http_transport"]
    assert {record["method"] for record in trace} == {"POST"} and not any(row["session_issued"] for row in trace)
    assert {record["protocol_version"] for record in trace} == {"2026-07-28", "2099-01-01"}
    assert "initialize" not in {record["mcp_method"] for record in trace} | set(rpc)
    assert "protocol_discovered" in {event["event_type"] for event in events}
    assert "protocol_initialized" not in {event["event_type"] for event in events}
    assert {event["profile"] for event in events} == {MODERN}
    coverage = manifest["protocol_coverage"]
    assert (coverage["era"], coverage["negotiation"], coverage["negotiated_revision"]) == (
        "modern", "server/discover", "2026-07-28")
    assert {"ping", "HTTP sessions"} <= set(coverage["not_applicable"])
    shown = await cli("report", path, "--format", "json", cwd=workspace)
    assert shown.code == 0 and json.loads(shown.out)["run_lock"]["negotiated_protocol_era"] == "modern"
    exported = await cli("export", path, "--output", workspace / "exports" / path.name, cwd=workspace)
    assert exported.code == 0, exported.err
    copy = json.loads((workspace / "exports" / path.name / "manifest.json").read_text())
    assert copy["protocol_negotiation"] == manifest["protocol_negotiation"]


async def test_the_modern_profile_names_the_era_a_handshake_only_sdk_server_speaks(workspace):
    # needs-session keeps state in its MCP session, a handshake-era server: server/discover is unknown there.
    async with fixture(variant="needs-session", auth="none") as running:
        child = await cli("check", "cedar", "--config", contract_config(workspace, running.mcp_url),
                          "--interface-profile", MODERN, cwd=workspace)
        rpc = oracle(running)["rpc"]
    assert child.code == 3, child.err
    assert child.err.startswith("unsupported: Server did not negotiate MCP 2026-07-28 with server/discover")
    assert "select --interface-profile mcp-contract-2025-11-25" in child.err
    _, manifest, _ = recorded(child)
    assert manifest["execution_status"] == "blocked" and manifest["checks"][0]["class"] == "unsupported"
    negotiation = manifest["protocol_negotiation"]
    assert negotiation["discover_error"]["code"] == -32601 and negotiation["fallback"] == "initialize"
    assert (negotiation["negotiated"], negotiation["era"]) == ("2025-11-25", "handshake")
    assert manifest["run_lock"]["negotiated_protocol_era"] == "handshake"
    assert "tools/call" not in rpc  # Refused before any revision-specific coverage ran.


async def test_the_default_profile_names_the_modern_one_a_2026_only_server_needs(workspace):
    # No --interface-profile: MCP targets default to mcp-contract-2025-11-25, which this server refuses.
    log = []
    runner, url = await serve(modern_server(), log)
    config = contract_config(workspace, url, {"tool": "lookup_record", "arguments": {"key": "a"}, "effect": "read"})
    try:
        refused = await cli("check", "cedar", "--config", config, cwd=workspace)
        named = await cli("check", "cedar", "--config", config, "--interface-profile", MODERN, cwd=workspace)
    finally:
        await runner.cleanup()
    assert refused.code == 3, refused.err
    assert refused.err.startswith("unsupported: Server refused the MCP 2025-11-25 initialize handshake (-32022")
    assert "select --interface-profile mcp-contract-2026-07-28" in refused.err
    _, manifest, _ = recorded(refused)
    assert (manifest["execution_status"], manifest["run_lock"]["interface_profile"]["name"]) == (
        "blocked", "mcp-contract-2025-11-25")
    assert manifest["protocol_negotiation"]["refusal"] == {"code": -32022, "supported": ["2026-07-28"]}
    assert "negotiated_protocol_era" not in manifest["run_lock"]
    # The profile it names reaches the same server, recorded as the modern era.
    assert named.code == 0, named.err
    _, manifest, _ = recorded(named)
    assert manifest["run_lock"]["negotiated_protocol_era"] == "modern"
    assert [entry["method"] for entry in log][:2] == ["initialize", "server/discover"]


# --- surface-run: the MCP Apps host -------------------------------------------------------------------

@dataclass
class Surface:
    child: Child
    path: Path | None
    origin: str


@pytest.fixture
def surface_run(workspace):
    """surface-run with the shipped example pointed at the fixture on a free port, plus `edit`. Its workspace
    is the repository (tooling and browsers live there); the runs it writes under runs/ are removed after."""
    if not (ROOT / "tooling/payment-ui/node_modules/playwright").exists() \
            or not list((ROOT / ".cache/ms-playwright").glob("chromium*")):
        pytest.skip("Workspace-local Playwright tooling/browser is not installed")
    written = []

    async def run(mode, edit=None) -> Surface:
        service = await asyncio.create_subprocess_exec(
            "node", "scripts/surface_fixture.mjs", "0", mode, cwd=ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            line = await asyncio.wait_for(service.stdout.readline(), 20)
            assert line, (await service.stderr.read()).decode()
            origin = json.loads(line)["endpoint"].removesuffix("/mcp")
            data = yaml.safe_load((ROOT / "examples/surface-preferences.yaml").read_text().replace(
                "http://127.0.0.1:8765", origin))
            if edit:
                edit(data)
            scenario = workspace / f"surface-{len(written)}.yaml"
            scenario.write_text(yaml.safe_dump(data))
            child = await cli("surface-run", scenario.relative_to(ROOT), cwd=ROOT)
        finally:
            if service.returncode is None:
                service.terminate()
            await service.wait()
        found = re.search(r"^Surface report: (.+)/report\.html$", child.out, re.MULTILINE)
        path = Path(found.group(1)) if found else None
        written.append(path)
        return Surface(child, path, origin)
    yield run
    for path in written:
        if path and path.parent == ROOT / "runs" and path.name.startswith("surface-"):
            shutil.rmtree(path, ignore_errors=True)


async def test_surface_run_renders_the_example_in_a_sandbox_proxy_under_the_default_csp(surface_run, workspace):
    run = await surface_run("mcp-app")
    assert run.child.code == 0, run.child.out + run.child.err
    assert run.child.out.splitlines()[0] == "Surface run completed (exit 0)"
    record = playback(run.path)
    result, events = record["result"], record["events"]
    assert (result["status"], result["compatibility_pass"]) == ("completed", True)
    assert result["mcp_apps_host"]["evidence"] == "generic MCP Apps host; not Muse evidence"
    assert {check["id"]: check["status"] for check in result["checks"]} == {
        "mcp-apps-resource-link": "passed", "embedded": "passed", "saved": "passed", "once": "passed"}
    [sandbox] = kinds(events, "ui_sandbox")
    # The resource declares no _meta.ui: the spec's restrictive default, in a proxy on an origin of its own.
    assert sandbox["metadata"] == "none" and sandbox["csp"] == {
        "source": "restrictive-default", "policy": DEFAULT_CSP, "domains": None, "rejected": []}
    assert (sandbox["renderer"], sandbox["view_origin"]) == ("native", APP_ORIGIN)
    assert sandbox["proxy"] == {"origin": APP_ORIGIN, "host_origin": HOST_ORIGIN}
    assert sandbox["permissions"] == {"granted": [], "allow": None}
    [connected] = kinds(events, "capabilities")
    assert (connected["tool_visibility"], connected["protocol_era"]) == ({}, "legacy")
    assert result["run_lock"]["server"]["protocol_era"] == "legacy"
    assert [row["reason"] for row in kinds(events, "ui_teardown")] == ["The UI step completed"]
    assert "fixture-access" not in public_text(run.path)  # The captured host input stays out of evidence.
    exported = await cli("export", run.path, "--output", workspace / "exports" / run.path.name, cwd=ROOT)
    assert exported.code == 0, exported.err
    copy = playback(exported.printed())
    assert kinds(copy["events"], "ui_sandbox")[0]["csp"]["policy"] == DEFAULT_CSP
    assert "fixture-access" not in public_text(exported.printed())


def host_contract(data):
    """The example against the host-contract server, where status is model-only and poll app-only. Its view
    shows its own origin, whether a proxy surrounds it, how calling each of those tools ended, and which of
    two fetches its CSP blocked (it declares connectDomains for its own origin, not for localhost)."""
    data["tools"]["poll"] = {"effect": "read"}
    ui = data["steps"][2]["ui"]
    ui["callback_tools"] = ["save", "poll", "status"]
    other = data["allowed_origins"][0].replace("127.0.0.1", "localhost")
    ui["actions"][0:0] = [{"kind": "expect_text", "selector": "#sandbox",
                           "value": f"{APP_ORIGIN} proxied clipboard-write=true camera=false"},
                          {"kind": "expect_text", "selector": "#visibility", "value": "status:rejected poll:allowed"},
                          {"kind": "expect_text", "selector": "#csp", "value": f"violations:connect-src {other}"}]


async def test_surface_run_enforces_ui_visibility_in_both_directions(surface_run):
    run = await surface_run("host-contract", host_contract)
    assert run.child.code == 0, run.child.out + run.child.err
    record = playback(run.path)
    result, events = record["result"], record["events"]
    assert (result["status"], result["compatibility_pass"]) == ("completed", True)
    [connected] = kinds(events, "capabilities")
    assert connected["tool_visibility"] == {"status": {"model": True, "app": False, "declared": ["model"]},
                                            "poll": {"model": False, "app": True, "declared": ["app"]}}
    # The view's model-only status call never left the host; its app-only poll did.
    assert [(row["tool"], row["reason"]) for row in kinds(events, "ui_callback_denied")] == [
        ("status", "visibility excludes app")]
    assert [row["tool"] for row in kinds(events, "ui_callback")] == ["poll", "save"]
    [sandbox] = kinds(events, "ui_sandbox")
    assert sandbox["proxy"] == {"origin": APP_ORIGIN, "host_origin": HOST_ORIGIN}
    assert (sandbox["csp"]["source"], sandbox["csp"]["domains"]["connectDomains"]) == ("declared", [run.origin])

    # A tool step never calls an app-only tool: refused at connect (setup), before any service call.
    def app_only_step(data):
        host_contract(data)
        data["steps"][3]["tool"] = "poll"
    run = await surface_run("host-contract", app_only_step)
    assert run.child.code == 2, run.child.out + run.child.err
    assert run.child.out.startswith("Surface run failed (exit 2) at connect [setup/infrastructure]: Tool poll "
                                    "is not visible to the model")
    record = playback(run.path)
    assert (record["result"]["failure_stage"], record["result"]["progress"]["completed"]) == ("connect", 0)
    assert not kinds(record["events"], "host_action")


async def test_surface_run_auto_negotiation_reaches_a_2026_07_28_only_server(surface_run):
    run = await surface_run("modern", lambda data: data.update(version_negotiation="auto"))
    assert run.child.code == 0, run.child.out + run.child.err
    record = playback(run.path)
    server = record["result"]["run_lock"]["server"]
    assert (server["protocol_version"], server["protocol_era"]) == ("2026-07-28", "modern")
    assert record["result"]["mcp_apps_host"]["version_negotiation"] == "auto"
    assert kinds(record["events"], "ui_sandbox")[0]["csp"]["policy"] == DEFAULT_CSP
    # The default handshake cannot reach it: blocked with the remedy, not a failure of the server.
    run = await surface_run("modern")
    assert run.child.code == 3 and "set version_negotiation: auto" in run.child.out
