"""Readiness evaluates CATT's declared contract for either target; it never calls a service tool."""
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from test_connector_support import API_KEY, ORIGIN, Hotel, services
from test_surrogate_parity import TREATMENTS

from consumer_agent_testing_tool import connector_run
from consumer_agent_testing_tool.ci_outputs import SUMMARY_SCHEMA
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_run import TARGET_AUTH
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.surrogate_readiness import catalog_checks, run_check


async def test_targets_command_lists_both_supported_surrogates(capsys):
    assert await execute(parser().parse_args(["targets", "--json"])) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == 1
    assert document["default_target"] is None
    assert {row["emulation_target"] for row in document["targets"]} == {"muse", "instinct"}
    assert {row["status"] for row in document["targets"]} == {"supported"}


@pytest.mark.parametrize(("target", "projection"), TREATMENTS)
async def test_surrogate_readiness_seals_catalog_checks_without_calling_a_tool(
        tmp_path, monkeypatch, capsys, target, projection):
    monkeypatch.chdir(tmp_path)
    hotel = Hotel()
    made = services(hotel)
    monkeypatch.setattr(connector_run, "default_services", lambda: made)
    manifest = {"version": 1, "kind": "surrogate-readiness",
                "allow": ["get_quote", "create_booking"], "classes": {"create_booking": "sensitive-write"},
                "host_arguments": {"create_booking": ["guest_email"]}}
    Path("readiness.yaml").write_text(yaml.safe_dump(manifest))
    args = parser().parse_args([
        "check", f"{ORIGIN}/mcp", "--suite", "surrogate", "--emulation-target", target,
        "--projection", projection, "--manifest", "readiness.yaml", "--summary-json", "-",
        "--junit", "reports/readiness.xml"])
    assert await execute(args) == 0
    summary = json.loads(capsys.readouterr().out)
    Draft202012Validator(SUMMARY_SCHEMA).validate(summary)
    assert summary["treatment"]["emulation_target"] == target
    assert summary["treatment"]["projection"] == projection
    result = playback(Path(summary["trials"][0]["run"]))["result"]
    assert hotel.calls == []
    assert made.linked[0]["store"] == tmp_path / ".cache" / "connector-links"
    assert made.linked[0]["store"].stat().st_mode & 0o077 == 0
    assert result["emulation_target"] == target and result["kind"] == "surrogate-readiness-check"
    assert result["run_lock"]["surrogate_contract"]["status"] == "supported"
    assert result["compatibility_pass"] is False and "surrogate" in result["evidence_label"]
    assert all(row["status"] != "failed" for row in result["checks"])
    assert all(row["basis"] == "assumed: CATT connector-surrogate-v1" for row in result["checks"])
    assert "guideline" not in json.dumps(result["checks"])
    assert "emulation_target" in Path("reports/readiness.xml").read_text()


@pytest.mark.parametrize("target", ["muse", "instinct"])
@pytest.mark.parametrize("case, check", [
    ("schema", "catalog.schemas"), ("duplicate", "catalog.names"),
    ("credential", "catalog.credentials"), ("unknown-allowlist", "catalog.allowlist"),
], ids=["schema", "duplicate", "credential", "unknown-allowlist"])
async def test_surrogate_readiness_reports_service_findings_and_redacts_secrets(
        tmp_path, monkeypatch, capsys, target, case, check):
    monkeypatch.chdir(tmp_path)
    hotel = Hotel()
    if case == "schema":
        hotel.catalog[0]["inputSchema"]["required"] = "not-an-array"
    elif case == "duplicate":
        hotel.catalog.append(hotel.catalog[0])
    elif case == "credential":
        hotel.catalog[0]["description"] += f" Debug: {API_KEY}"
    monkeypatch.setattr(connector_run, "default_services", lambda: services(hotel))
    Path("readiness.yaml").write_text(yaml.safe_dump({
        "kind": "surrogate-readiness", "allow": ["missing"] if case == "unknown-allowlist" else ["search_rooms"]}))
    args = parser().parse_args([
        "check", f"{ORIGIN}/mcp", "--suite", "surrogate", "--emulation-target", target,
        "--manifest", "readiness.yaml", "--summary-json", "-"])
    assert await execute(args) == 1
    summary = json.loads(capsys.readouterr().out)
    path = Path(summary["trials"][0]["run"])
    result = playback(path)["result"]
    assert next(row for row in result["checks"] if row["id"] == check)["status"] == "failed"
    assert hotel.calls == []
    assert all(API_KEY not in file.read_text() for file in path.iterdir() if file.is_file())


@pytest.mark.parametrize("target", ["muse", "instinct"])
@pytest.mark.parametrize(("secret", "location"), [
    ('catt-review-"secret-72143', "description"),
    ("catt-review-\\secret-72143", "schema-key"),
    ("catt-review-\nsecret-72143", "instructions"),
], ids=["quote", "backslash", "newline"])
async def test_surrogate_readiness_detects_escaped_linked_secrets(tmp_path, target, secret, location):
    hotel = Hotel()
    if location == "description":
        hotel.catalog[0]["description"] += f" Debug: {secret}"
    elif location == "schema-key":
        hotel.catalog[0]["inputSchema"]["properties"][secret] = {"type": "string"}
    made = services(hotel, secrets=[secret])
    if location == "instructions":
        make_client = made.make_client

        def with_instructions(*args, **kwargs):
            client = make_client(*args, **kwargs)
            client.instructions = f"Debug: {secret}"
            return client

        made.make_client = with_instructions
    store = tmp_path / "linked"
    code = await run_check(
        f"{ORIGIN}/mcp", auth=TARGET_AUTH.validate_python({"mode": "none"}),
        manifest_path=None, output=tmp_path / "runs", profile=made.client_profile("generic-sdk"),
        emulation_target=target, services=made, store=store)
    assert code == 1
    assert made.linked[0]["store"] == store and hotel.calls == []
    path, = (tmp_path / "runs").iterdir()
    result = playback(path)["result"]
    assert next(row for row in result["checks"] if row["id"] == "catalog.credentials")["status"] == "failed"
    public = "".join(file.read_text() for file in path.iterdir() if file.is_file())
    assert secret not in public and json.dumps(secret)[1:-1] not in public


def test_surrogate_readiness_does_not_match_credentials_across_json_boundaries():
    hotel = Hotel()
    hotel.catalog[0]["example"] = {"first": "part-one", "second": "part-two"}
    checks, _ = catalog_checks(
        hotel.catalog, None, target="instinct", projection="native",
        secrets=['part-one", "second": "part-two'])
    assert next(row for row in checks if row["id"] == "catalog.credentials")["status"] == "passed"


@pytest.mark.parametrize("extra", [
    ["--suite", "muse-guidelines", "--emulation-target", "instinct"],
    ["--suite", "surrogate", "--emulation-target", "instinct", "--projection", "muse-cli"],
    ["--suite", "surrogate", "--emulation-target", "instinct",
     "--client-profile", "muse-template-unofficial-2026-10"],
    ["--suite", "surrogate", "--emulation-target", "muse", "--drift-check"],
    ["--suite", "contract", "--emulation-target", "instinct"],
])
async def test_incompatible_readiness_flags_fail_before_authentication(monkeypatch, extra):
    def forbidden():
        pytest.fail("Invalid target treatments must be rejected before service or credential setup")
    monkeypatch.setattr(connector_run, "default_services", forbidden)
    with pytest.raises(LabError):
        await execute(parser().parse_args(["check", f"{ORIGIN}/mcp", *extra]))


async def test_surrogate_readiness_keeps_the_assistant_target_when_resolving_a_configured_service(
        tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from consumer_agent_testing_tool import config

    monkeypatch.chdir(tmp_path)
    Path("config.yaml").write_text("# read_config is replaced with a local service declaration")
    service = SimpleNamespace(base_url=f"{ORIGIN}/mcp", auth=SimpleNamespace(mode="none"))
    monkeypatch.setattr(config, "read_config", lambda path: SimpleNamespace(targets={"hotel": service}))
    hotel = Hotel()
    monkeypatch.setattr(connector_run, "default_services", lambda: services(hotel))
    assert await execute(parser().parse_args([
        "check", "hotel", "--config", "config.yaml", "--suite", "surrogate",
        "--emulation-target", "instinct", "--summary-json", "-"])) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["treatment"]["emulation_target"] == "instinct"
    assert playback(Path(summary["trials"][0]["run"]))["result"]["emulation_target"] == "instinct"
    assert hotel.calls == []


@pytest.mark.parametrize("target", ["muse", "instinct"])
@pytest.mark.parametrize("code", [2, 3], ids=["setup-error", "unsupported"])
async def test_surrogate_readiness_errors_never_become_service_passes(tmp_path, monkeypatch, capsys, target, code):
    monkeypatch.chdir(tmp_path)
    made = services(Hotel(), link_error=LabError("Synthetic connection failure", code=code))
    monkeypatch.setattr(connector_run, "default_services", lambda: made)
    assert await execute(parser().parse_args([
        "check", f"{ORIGIN}/mcp", "--suite", "surrogate", "--emulation-target", target,
        "--summary-json", "-"])) == code
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == ("error" if code == 2 else "blocked")
    assert summary["trials"][0]["checks"] == []
    result = playback(Path(summary["trials"][0]["run"]))["result"]
    assert result["failure_stage"] == "link" and result["compatibility_pass"] is False
