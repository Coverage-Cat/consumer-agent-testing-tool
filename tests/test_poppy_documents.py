"""P3.2: the poppy suite's document rules (poppy.json, RFC 8414 metadata, client metadata documents), its header
and URL helpers, and discovery's redirect and egress rules against mocked HTTPS companies (no network)."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from consumer_agent_testing_tool import poppy
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.poppy import (
    Key,
    challenges,
    metadata_url,
    origin_of,
    private_members,
    validate_as_metadata,
    validate_client_metadata,
    validate_poppy_json,
)
from consumer_agent_testing_tool.replay import playback

DOCUMENT = {
    "protocol_version": "0.1",
    "organization": {"name": "Example Company", "domain": "example.com"},
    "auth": {"issuer": "https://auth.example.com", "direct": {"scopes": ["poppy:read", "poppy:write", "addresses"]},
             "mediated": {"endpoint": "https://auth.example.com/poppy/sign-in",
                          "fields": [{"name": "email", "label": "Email", "secret": False},
                                     {"name": "password", "label": "Password", "secret": True}],
                          "scopes": ["poppy:read"]},
             "custom_scopes": {"addresses": "Manage saved shipping addresses"}},
    "agent": {"protocols": [{"type": "poppy", "endpoint": "https://api.example.com/poppy/conversations"}]},
    "web": {"browser_session_endpoint": "https://example.com/poppy/browser-session"},
    "apis": [{"type": "openapi", "url": "https://api.example.com/openapi.json", "description": "Orders"},
             {"type": "mcp", "url": "https://mcp.example.com/mcp", "description": "Product search"}],
    "extensions": {"operations": {"version": "1", "endpoint": "https://api.example.com/poppy/operations"}},
}
METADATA = {"issuer": "https://auth.example.com", "token_endpoint": "https://auth.example.com/oauth/token",
            "revocation_endpoint": "https://auth.example.com/oauth/revoke",
            "authorization_endpoint": "https://auth.example.com/oauth/authorize",
            "dpop_signing_alg_values_supported": ["ES256", "RS256"], "poppy_domains": ["example.com", "example.co.uk"]}


def document(**changes):
    value = copy.deepcopy(DOCUMENT)
    for path, change in changes.items():
        *parents, last = path.split("__")
        target = value
        for parent in parents:
            target = target[parent]
        if change is None:
            target.pop(last)
        else:
            target[last] = change
    return value


def rules(rows, severity=None):
    return sorted({row.rule for row in rows if severity is None or row.severity == severity})


def test_the_draft_example_document_has_no_findings():
    assert validate_poppy_json(DOCUMENT, served_url="https://example.com/.well-known/poppy.json",
                               loopback_ok=False) == []
    assert validate_as_metadata(METADATA, DOCUMENT, loopback_ok=False) == []


@pytest.mark.parametrize("changes,rule,severity", [
    ({"protocol_version": "1.0"}, "3.1-protocol-version", "blocked"),  # MUST NOT use an unsupported major
    ({"protocol_version": "0.2"}, "3.1-protocol-version", "info"),  # a newer minor still works
    ({"protocol_version": "draft"}, "3.1-protocol-version", "fail"),
    ({"organization__domain": "example.org"}, "3.1-organization", "fail"),
    ({"auth": None}, "3.1-auth", "fail"),  # agent, apis and the browser endpoint need auth
    ({"auth__issuer": "http://auth.example.com"}, "3.1-auth", "fail"),
    ({"auth__direct__scopes": ["poppy:read", "poppy:admin"]}, "4.4-scopes", "fail"),  # reserved prefix
    ({"auth__custom_scopes": None}, "4.4-scopes", "warn"),
    ({"auth__mediated__fields": [{"name": "pin"}]}, "3.1-sign-in", "fail"),
    ({"agent__protocols": []}, "3.1-agent", "fail"),
    ({"agent__protocols": [{"type": "a2a", "endpoint": "https://api.example.com/a2a"}]}, "3.1-agent", "info"),
    ({"apis": [{"type": "graphql", "url": "https://api.example.com/graphql"}]}, "6-apis", "info"),
    ({"apis": [{"type": "mcp", "url": "https://mcp.example.com/mcp"}]}, "6-apis", "warn"),  # no description
    ({"extensions": {"gift-wrap": {"version": "1"}}}, "3.3-extensions", "warn"),  # plain name, not the draft's
    ({"extensions": {"example.com/gift-wrap": {}}}, "3.3-extensions", "fail"),  # no version
    ({"web__browser_session_endpoint": "http://example.com/join"}, "3.1-web", "fail"),
])
def test_poppy_json_rules(changes, rule, severity):
    rows = validate_poppy_json(document(**changes), served_url="https://example.com/.well-known/poppy.json",
                               loopback_ok=False)
    assert rule in rules(rows, severity), rows


def test_surfaces_and_domains_compare_as_the_draft_says():
    bare = {"protocol_version": "0.1", "organization": {"name": "Example", "domain": "Example.COM"}}
    rows = validate_poppy_json(bare, served_url="https://example.com/.well-known/poppy.json", loopback_ok=False)
    assert rules(rows) == ["3.1-surfaces"]  # case-insensitive domain; none of agent, apis or web
    browse_only = {**bare, "web": {}}  # a website without the join endpoint needs no auth
    assert validate_poppy_json(browse_only, served_url="https://example.com/x", loopback_ok=False) == []


@pytest.mark.parametrize("changes,rule", [
    ({"issuer": "https://auth.example.com/"}, "3.2-metadata"),  # exactly, not after normalizing
    ({"poppy_domains": ["example.co.uk"]}, "3.2-metadata"),
    ({"revocation_endpoint": None}, "3.2-endpoints"),
    ({"authorization_endpoint": None}, "3.2-endpoints"),  # auth.direct is offered
    ({"dpop_signing_alg_values_supported": ["RS256"]}, "3.2-dpop-algs"),
])
def test_authorization_server_metadata_rules(changes, rule):
    metadata = {key: value for key, value in {**METADATA, **changes}.items() if value is not None}
    assert rules(validate_as_metadata(metadata, DOCUMENT, loopback_ok=False)) == [rule]
    upper = {**METADATA, "poppy_domains": ["EXAMPLE.com"]}
    assert validate_as_metadata(upper, DOCUMENT, loopback_ok=False) == []  # ignoring case


def test_client_metadata_documents_follow_section_4_1():
    url = "https://agent.example/agent.json"
    good = {"client_id": url, "client_name": "Example Agent", "jwks_uri": "https://agent.example/jwks.json",
            "redirect_uris": ["https://agent.example/oauth/callback"], "token_endpoint_auth_method": "private_key_jwt"}
    assert validate_client_metadata(good, fetched_url=url) == []
    problems = validate_client_metadata({**good, "client_id": "https://agent.example/other.json",
                                         "jwks_uri": "https://keys.elsewhere.example/jwks.json",
                                         "token_endpoint_auth_method": "client_secret_basic"}, fetched_url=url)
    assert len([row for row in problems if row.severity == "fail"]) == 3
    loopback = {**good, "client_id": "http://127.0.0.1:9/agent.json", "jwks_uri": "http://127.0.0.1:9/jwks.json",
                "redirect_uris": []}
    assert rules(validate_client_metadata(loopback, fetched_url="http://127.0.0.1:9/agent.json"), "fail") \
        == ["4.1-client-metadata"]  # http only for a loopback test identity
    assert validate_client_metadata(loopback, fetched_url="http://127.0.0.1:9/agent.json", loopback_ok=True) == []
    key = Key("k1")
    assert private_members({"keys": [key.jwk]}) == [] and private_members({"keys": [{**key.jwk, "d": "x"}]}) == ["k1"]


def test_headers_urls_and_proofs():
    header = 'DPoP error="insufficient_scope", scope="poppy:read poppy:write", algs="ES256", Bearer realm="mcp"'
    assert challenges(header) == [("DPoP", {"error": "insufficient_scope", "scope": "poppy:read poppy:write",
                                            "algs": "ES256"}), ("Bearer", {"realm": "mcp"})]
    assert metadata_url("https://auth.example.com") == "https://auth.example.com/.well-known/oauth-authorization-server"
    assert metadata_url("https://example.com/tenant/") == \
        "https://example.com/.well-known/oauth-authorization-server/tenant"  # RFC 8414 §3.1
    assert origin_of("example.com") == "https://example.com" and origin_of("http://127.0.0.1:8080/") == \
        "http://127.0.0.1:8080"
    for bad in ("http://example.com", "https://example.com/shop", "https://user@example.com"):
        with pytest.raises(LabError):
            origin_of(bad)
    import jwt
    key = Key("dpop")
    token = "session-token-value"
    value = poppy.proof(key, "GET", "https://api.example.com/orders?page=2", token=token, nonce="n1")
    header = jwt.get_unverified_header(value)
    claims = jwt.decode(value, options={"verify_signature": False})
    assert header["typ"] == "dpop+jwt" and header["jwk"] == key.public and "d" not in header["jwk"]
    assert claims["htu"] == "https://api.example.com/orders" and claims["htm"] == "GET"  # no query (4.3)
    assert claims["ath"] == poppy.ath(token) and claims["nonce"] == "n1" and len(claims["jti"]) >= 22


def company(routes):
    """A mocked HTTPS company: routes maps URL to (status, JSON body, headers)."""
    seen = []

    def handle(request):
        seen.append(str(request.url))
        status, body, headers = routes.get(str(request.url), (404, {"error": "not_found"}, {}))
        return httpx.Response(status, json=body, headers=headers)
    return httpx.MockTransport(handle), seen


async def run(tmp_path, target, transport, manifest=None):
    path = None
    if manifest is not None:
        path = tmp_path / "probes.json"
        path.write_text(json.dumps(manifest))
    return await poppy.run_check(target, manifest_path=path, output=tmp_path / "runs", transport=transport)


def result_of(capsys):
    return playback(Path(capsys.readouterr().out.splitlines()[-1]))["result"]


def published(document_url="https://www.example.com/.well-known/poppy.json"):
    host = document_url.split("/")[2]
    return {document_url: (200, {**DOCUMENT, "organization": {"name": "Example", "domain": host}}, {}),
            "https://auth.example.com/.well-known/oauth-authorization-server": (
                200, {**METADATA, "poppy_domains": [host]}, {}),
            "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": (
                200, {"resource": "https://mcp.example.com/mcp", "authorization_servers": ["https://auth.example.com"]},
                {})}


async def test_discovery_follows_an_https_redirect_within_the_site(tmp_path, capsys):
    routes = {"https://example.com/.well-known/poppy.json": (301, {}, {
        "Location": "https://www.example.com/.well-known/poppy.json"}), **published()}
    transport, seen = company(routes)
    assert await run(tmp_path, "example.com", transport) == 0
    result = result_of(capsys)
    assert result["status"] == "completed" and result["company"]["poppy_json"]["organization"]["domain"] == \
        "www.example.com"
    assert "https://mcp.example.com/.well-known/oauth-protected-resource/mcp" in seen


async def test_a_redirect_off_https_fails_and_is_not_followed(tmp_path, capsys):
    transport, seen = company({"https://example.com/.well-known/poppy.json": (302, {}, {
        "Location": "http://example.com/.well-known/poppy.json"})})
    assert await run(tmp_path, "https://example.com", transport) == 1
    assert [row["rule"] for row in result_of(capsys)["findings"]] == ["3-redirects"] and len(seen) == 1


async def test_hosts_outside_the_company_are_never_contacted(tmp_path, capsys):
    routes = published("https://example.com/.well-known/poppy.json")
    routes["https://example.com/.well-known/poppy.json"][1]["apis"][1]["url"] = "https://tracker.invalid/mcp"
    transport, seen = company(routes)
    assert await run(tmp_path, "https://example.com", transport) == 3
    result = result_of(capsys)
    assert [row["rule"] for row in result["findings"] if row["severity"] == "blocked"] == ["6-mcp-metadata"]
    assert not any("tracker.invalid" in url for url in seen)


async def test_a_claimed_domain_widens_no_egress(tmp_path, capsys):
    # Served by shop.example.com, the document claims victim.example, whose issuer lists it too (so 3.2 passes).
    # Only the serving host speaks for a document (section 3): victim.example's other hosts stay uncontacted.
    claimed = {**DOCUMENT, "organization": {"name": "Example", "domain": "victim.example"},
               "auth": {**DOCUMENT["auth"], "issuer": "https://auth.victim.example"},
               "apis": [{"type": "mcp", "url": "https://mcp.victim.example/mcp", "description": "Product search"}]}
    transport, seen = company({
        "https://shop.example.com/.well-known/poppy.json": (200, claimed, {}),
        "https://auth.victim.example/.well-known/oauth-authorization-server": (
            200, {**METADATA, "issuer": "https://auth.victim.example", "poppy_domains": ["victim.example"]}, {})})
    assert await run(tmp_path, "https://shop.example.com", transport) == 1
    findings = {(row["rule"], row["severity"]) for row in result_of(capsys)["findings"]}
    assert {("3.1-organization", "fail"), ("6-mcp-metadata", "blocked")} <= findings
    assert ("3.2-metadata", "fail") not in findings and not any("mcp.victim.example" in url for url in seen)


async def test_a_loopback_identity_cannot_probe_a_remote_company(tmp_path, capsys):
    transport, seen = company(published("https://example.com/.well-known/poppy.json"))
    manifest = {"version": 1, "kind": "poppy-probes", "agent": {"identity": "loopback"}}
    assert await run(tmp_path, "https://example.com", transport, manifest) == 3
    result = result_of(capsys)
    assert result["status"] == "blocked" and "loopback test deployment only" in result["detail"]
    assert not any("/oauth/token" in url for url in seen)


async def test_a_hosted_identity_must_publish_only_the_public_half_of_its_key(tmp_path, capsys, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    key = Key("agent-1")
    monkeypatch.setenv("POPPY_AGENT_KEY", key.private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    routes = published("https://example.com/.well-known/poppy.json")
    client = "https://agent.example.com/agent.json"
    routes[client] = (200, {"client_id": client, "client_name": "Agent", "jwks_uri": "https://agent.example.com/jwks",
                            "redirect_uris": [], "token_endpoint_auth_method": "private_key_jwt"}, {})
    routes["https://agent.example.com/jwks"] = (200, {"keys": [{**key.jwk, "d": "leaked"}]}, {})
    transport, seen = company(routes)
    manifest = {"version": 1, "kind": "poppy-probes",
                "agent": {"identity": "hosted", "client_id": client, "private_key_env": "POPPY_AGENT_KEY",
                          "key_id": "agent-1"}}
    assert await run(tmp_path, "https://example.com", transport, manifest) == 2
    result = result_of(capsys)
    assert "only public keys" in result["detail"] and not any("/oauth/token" in url for url in seen)
    assert "BEGIN PRIVATE KEY" not in json.dumps(result)


def test_probe_manifests_are_strict():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):  # Credentials come from the environment, never the manifest.
        poppy.ProbeManifest.model_validate({"version": 1, "kind": "poppy-probes", "password": "never here"})


def test_probe_manifest_loading_names_the_problem(tmp_path):
    path = tmp_path / "probes.json"
    path.write_text(json.dumps({"version": 1, "kind": "poppy-probes", "sign_in": {"credentials_env": {}}}))
    with pytest.raises(LabError, match="sign_in"):
        poppy.load_manifest(path)


async def test_answers_are_read_decoded_and_bounded(tmp_path, capsys):
    import gzip
    body = json.dumps(published("https://example.com/.well-known/poppy.json")[
        "https://example.com/.well-known/poppy.json"][1]).encode()
    routes = published("https://example.com/.well-known/poppy.json")

    def handle(request):
        url = str(request.url)
        if url == "https://example.com/.well-known/poppy.json":
            return httpx.Response(200, content=gzip.compress(body), headers={"content-encoding": "gzip",
                                                                            "content-type": "application/json"})
        if url == "https://auth.example.com/.well-known/oauth-authorization-server":
            return httpx.Response(200, content=b" " * (poppy.MAX_BODY + 1))
        status, value, headers = routes.get(url, (404, {}, {}))
        return httpx.Response(status, json=value, headers=headers)
    assert await run(tmp_path, "https://example.com", httpx.MockTransport(handle)) == 2
    result = result_of(capsys)
    assert "answered more than" in result["detail"]  # the gzip document itself was read and validated


def test_a_configured_target_name_is_no_company_origin():
    with pytest.raises(LabError, match="origin as TARGET"):
        origin_of("booking-service")  # the CLI's default TARGET
    assert origin_of("http://localhost:8080") == "http://localhost:8080"
