"""P3.2: check --suite poppy against the loopback draft 0.1 company in test_poppy_support.py: discovery, DPoP
Session Tokens, client metadata (CIMD) refusals, the browser join, the section 6 error codes, Account Tokens and
the operations extension's at-most-once confirm. Each deliberate defect must be named by its own rule."""
import json
from pathlib import Path

import httpx
import pytest
from test_poppy_support import ACCOUNT, company, probe_manifest

from consumer_agent_testing_tool import poppy
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.replay import playback


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the workspace.
    monkeypatch.setenv("POPPY_TEST_EMAIL", ACCOUNT["email"])
    monkeypatch.setenv("POPPY_TEST_PASSWORD", ACCOUNT["password"])
    return tmp_path


async def cli(*arguments):
    return await execute(parser().parse_args([str(argument) for argument in arguments]))


def sealed(capsys):
    captured = capsys.readouterr()
    path = Path(captured.out.splitlines()[-1])
    return playback(path)["result"], captured.err, path


async def check(workspace, base, manifest=None, *extra):
    arguments = ["check", base, "--suite", "poppy", *extra]
    if manifest is not None:
        (workspace / "probes.json").write_text(json.dumps(manifest))
        arguments += ["--manifest", "probes.json"]
    return await cli(*arguments)


def failing(result):
    return sorted({row["rule"] for row in result["findings"] if row["severity"] == "fail"})


async def test_a_conforming_company_passes_discovery_and_every_probe(workspace, capsys):
    with company() as (fixture, base):
        assert await check(workspace, base, probe_manifest(base)) == 0
        minted = [*fixture.tokens, *fixture.accounts]
        effects, fetched = fixture.effects, list(fixture.fetched)
    result, err, path = sealed(capsys)
    assert (result["kind"], result["status"], result["label"]) == ("poppy-check", "completed", poppy.LABEL)
    assert result["specification"]["commit"] == poppy.SPEC_COMMIT and result["muse"] == poppy.MUSE_EVIDENCE
    assert result["compatibility_pass"] is False and result["summary"]["fail"] == 0
    exercised = {item.split(":")[0] for item in result["probes_run"]}
    assert {"4.2-start-session", "4.2-assertion-replay", "4.3-proof-replay", "4.1-assertion-audience",
            "4.1-client-metadata", "4.3-dpop-required", "4.3-proof-binding", "4.3-token-in-url", "6-invalid-token",
            "5-browser-join", "5-browser-checks", "4.7-mediated", "6-sign-in-required", "6-insufficient-scope",
            "4.8-account-token", "4.9-sign-out", "ops-proposal", "ops-terms-changed", "ops-confirm",
            "ops-at-most-once", "ops-cancel", "ops-not-found"} <= exercised
    assert effects == 1  # two confirms, one exchange
    assert any(url.endswith("/agent.json") for url in fetched)  # the company read the client metadata
    assert all(row["status"] in ("no-finding", "info") for row in result["checks"])
    assert "draft 0.1; no production Muse evidence" in err
    # No credential, Session Token, Account Token, assertion or proof reaches evidence.
    text = "\n".join(item.read_text(errors="replace") for item in path.rglob("*") if item.is_file()
                     and item.suffix in (".json", ".jsonl"))
    assert ACCOUNT["password"] not in text and ACCOUNT["email"] not in text
    assert not [token for token in minted if token in text]
    assert "eyJ" not in text  # no JWT at all


@pytest.mark.parametrize("defect,rules", [
    ("domain-mismatch", ["3.1-organization", "3.2-metadata"]),
    ("claimed-domain", ["3.1-organization"]),  # served by one host, claiming another the issuer also lists
    ("no-es256", ["3.2-dpop-algs"]),
    ("issuer-mismatch", ["3.2-metadata"]),
    ("accepts-bearer", ["4.3-dpop-required"]),
    ("unbound-proofs", ["4.3-proof-binding"]),
    ("replayable-assertion", ["4.2-assertion-replay"]),
    ("replayable-proof", ["4.3-proof-replay"]),
    ("token-audience", ["4.1-assertion-audience"]),
    ("lax-client-metadata", ["4.1-client-metadata"]),
    ("no-metadata-fetch", ["4.1-assertion-audience", "4.1-client-metadata", "4.2-start-session"]),
    ("sign-in-error-401", ["6-sign-in-required"]),
    ("strict-cookie", ["5-cookie"]),
    ("replayable-browser", ["5-browser-checks"]),
    ("double-perform", ["ops-at-most-once"]),  # every repeated confirm performs again
    ("racy-confirm", ["ops-at-most-once"]),  # check-then-act: the concurrent confirm performs too
    ("ignores-user-approval", ["ops-user-approval"]),
    ("echo-credentials", ["4.7-mediated"]),
    ("token-in-url", ["4.3-token-in-url"]),
    ("no-terms-changed", ["ops-terms-changed"]),
    ("anonymous-mcp-metadata", ["6-mcp-metadata"]),
])
async def test_each_defect_fails_its_own_rule(workspace, capsys, defect, rules):
    with company(defect) as (fixture, base):
        assert await check(workspace, base, probe_manifest(base)) == 1
    result, err, _ = sealed(capsys)
    assert failing(result) == rules and result["status"] == "failed"
    assert "failing: " in err and all(rule in err.split("failing: ", 1)[1] for rule in rules)
    if defect in ("issuer-mismatch", "claimed-domain"):  # 3.1/3.2: the document is unusable, so nothing is probed
        assert result["probes_run"] == [] and not fixture.sessions
        assert any(row["severity"] == "info" and "Probes not run" in row["evidence"] for row in result["findings"])
    if defect in ("double-perform", "racy-confirm"):
        assert fixture.effects > 1


@pytest.mark.parametrize("defect,rule", [("no-scope-attribute", "6-insufficient-scope"),
                                         ("no-store-missing", "4.2-token-response")])
async def test_documented_status_with_a_missing_detail_is_a_warning(workspace, capsys, defect, rule):
    with company(defect) as (_, base):
        assert await check(workspace, base, probe_manifest(base)) == 0
    result, _, _ = sealed(capsys)
    assert [row["rule"] for row in result["findings"] if row["severity"] == "warn"] == [rule]


async def test_an_operation_needing_the_users_own_approval_refuses_a_standing_permission(workspace, capsys):
    with company("user-approval") as (fixture, base):
        assert await check(workspace, base, probe_manifest(base)) == 0
        assert fixture.effects == 1
    result, _, _ = sealed(capsys)
    assert "ops-user-approval:operations" in result["probes_run"] and result["summary"]["fail"] == 0


async def test_the_probes_use_the_newest_token_of_a_company_that_retires_old_ones(workspace, capsys):
    # The operation runs on the test account's Session after its Account Token exchange (4.8), whose new
    # signed-in token replaced the one from sign-in.
    with company("retires-superseded-tokens") as (fixture, base):
        manifest = probe_manifest(base)
        manifest["sign_in"]["scope"] = "poppy:read poppy:write"
        del manifest["operations"]["scope"], manifest["requests"]["insufficient_scope"]
        assert await check(workspace, base, manifest) == 0
        assert fixture.effects == 1
    result, _, _ = sealed(capsys)
    assert result["summary"]["fail"] == 0 and "ops-confirm" in result["probes_run"]
    assert "4.7-mediated:operations" not in result["probes_run"]  # it reused the account's Session


async def test_required_dpop_nonces_are_supplied(workspace, capsys):
    with company("nonce") as (_, base):
        assert await check(workspace, base, probe_manifest(base)) == 0
    result, _, _ = sealed(capsys)
    assert [row["rule"] for row in result["findings"]] == ["4.3-nonce"] and result["summary"]["fail"] == 0


async def test_a_sign_in_that_needs_a_person_blocks_instead_of_failing(workspace, capsys):
    with company("code-required") as (fixture, base):
        assert await check(workspace, base, probe_manifest(base)) == 3
        assert fixture.effects == 0
    result, err, _ = sealed(capsys)
    assert result["status"] == "blocked" and failing(result) == []
    assert {row["rule"] for row in result["findings"] if row["severity"] == "blocked"} == {"4.7-mediated",
                                                                                            "ops-proposal"}
    assert "one-time code" in result["detail"] and "blocked" in err


async def test_discovery_alone_is_read_only(workspace, capsys):
    with company() as (fixture, base):
        assert await check(workspace, base) == 0
        assert not fixture.sessions and not fixture.fetched and fixture.effects == 0
    result, _, _ = sealed(capsys)
    assert result["target"]["probes"] is False and result["probes_run"] == []
    assert result["company"]["poppy_json"]["protocol_version"] == "0.1"
    # A probe rule that never ran says so: no finding is never a pass.
    row = next(row for row in result["checks"] if row["id"] == "ops-at-most-once")
    assert row == {"id": "ops-at-most-once", "layer": "poppy", "status": "no-finding", "required": False,
                   "detail": "no finding; not an admission", "basis": "draft 0.1 §Operations 6"}


async def test_probes_without_a_test_account_still_check_signed_out_sessions(workspace, capsys):
    with company() as (_, base):
        manifest = probe_manifest(base, operations=False, sign_in=False)
        assert await check(workspace, base, manifest) == 0
    result, _, _ = sealed(capsys)
    exercised = {item.split(":")[0] for item in result["probes_run"]}
    assert "6-sign-in-required" in exercised and "4.7-mediated" not in exercised


async def test_missing_credentials_are_a_setup_error(workspace, capsys, monkeypatch):
    monkeypatch.delenv("POPPY_TEST_PASSWORD")
    with company() as (_, base):
        assert await check(workspace, base, probe_manifest(base)) == 2
    result, _, _ = sealed(capsys)
    assert "POPPY_TEST_PASSWORD" in result["detail"] and result["checks"] == []


async def test_the_cli_refuses_other_suites_options_and_paths_outside_the_workspace(workspace):
    with pytest.raises(LabError, match="--auth-env does not apply to --suite poppy"):
        await cli("check", "https://example.com", "--suite", "poppy", "--auth-env", "KEY")
    with pytest.raises(Unsupported, match="--offline does not apply"):
        await poppy.check_from_cli(parser().parse_args(["check", "https://example.com", "--suite", "poppy",
                                                        "--offline"]))
    with pytest.raises(LabError, match="outside the workspace"):
        await cli("check", "https://example.com", "--suite", "poppy", "--manifest", "../probes.json")


def shop(issuer, apis=()):
    """A non-loopback company on an httpx MockTransport whose poppy.json names `issuer` (and MCP `apis`)."""
    seen = []

    def handler(request):
        seen.append({"url": str(request.url), "host": request.headers.get("host"),
                     "sni": request.extensions.get("sni_hostname")})
        if request.url.path == "/.well-known/poppy.json":
            return httpx.Response(200, json={"protocol_version": "0.1",
                                             "organization": {"name": "Shop", "domain": "shop.example"},
                                             "apis": [{"type": "mcp", "url": url} for url in apis],
                                             "auth": {"issuer": issuer}})
        return httpx.Response(404, json={"error": "not_found"})
    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize("issuer, resolved", [
    ("https://10.0.0.7/tenant", None), ("https://[::ffff:169.254.169.254]/", None),
    ("https://auth.shop-login.example/", ["10.0.0.7"]), ("https://auth.shop-login.example/", ["93.184.216.34", "::1"])])
async def test_discovery_never_contacts_an_internal_address_a_document_names(tmp_path, issuer, resolved):
    transport, seen = shop(issuer, apis=["https://10.0.0.7/mcp"])

    async def resolver(host, port):
        return {"shop.example": ["93.184.216.34"], "auth.shop-login.example": resolved}[host]
    await poppy.run_check("https://shop.example", manifest_path=None, output=tmp_path / "out", transport=transport,
                          resolver=resolver if resolved else None)
    # Only the target, which the developer chose: neither the issuer's metadata nor the API's on an internal address.
    assert [row["url"] for row in seen] == ["https://shop.example/.well-known/poppy.json"]
    result = json.loads(next((tmp_path / "out").glob("*/result.json")).read_text())
    blocked = next(row for row in result["findings"] if row["rule"] == "3.2-metadata")
    assert blocked["severity"] == "blocked" and "internal address" in blocked["evidence"]


async def test_a_vetted_host_is_reached_at_the_address_checked_keeping_its_name(tmp_path):
    transport, seen = shop("https://auth.shop-login.example/")

    async def resolver(host, port):
        return {"shop.example": ["151.101.1.140"], "auth.shop-login.example": ["93.184.216.34"]}[host]
    await poppy.run_check("https://shop.example", manifest_path=None, output=tmp_path / "out", transport=transport,
                          resolver=resolver)
    # The target is the developer's own choice: reached as named. A host the document named: pinned, Host and SNI kept.
    assert seen[0] == {"url": "https://shop.example/.well-known/poppy.json", "host": "shop.example", "sni": None}
    assert seen[1] == {"url": "https://93.184.216.34/.well-known/oauth-authorization-server",
                       "host": "auth.shop-login.example", "sni": "auth.shop-login.example"}
    # Only the issuer's own host is reachable, not its subdomains.
    egress = poppy.Egress("https://shop.example")
    egress.allow("https://auth.shop-login.example/")
    with pytest.raises(Unsupported, match="outside the company's declared hosts"):
        egress.check("https://evil.auth.shop-login.example/x")


async def test_the_outcome_line_leads_with_what_the_company_answered(tmp_path, capsys):
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "not_found"}))
    assert await poppy.run_check("https://shop.example", manifest_path=None, output=tmp_path / "out",
                                 transport=transport) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"poppy failed: 1 fail, 0 warn, 0 info ({poppy.LABEL}); https://shop.example/.well-known/"
                          "poppy.json answered HTTP 404 without a JSON document; failing: 3-https (poppy.json)")
