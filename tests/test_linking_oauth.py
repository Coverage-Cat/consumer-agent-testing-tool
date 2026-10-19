"""linking: OAuth 2.1 through the SDK provider against a loopback authorization server (fixture-auto)."""
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_mcp_clients_support import FakeMcp, OAuthFixture, serve_asgi

from consumer_agent_testing_tool import linking
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Redactor
from consumer_agent_testing_tool.linking import LinkError, OAuthAuth, OAuthCredential, link
from consumer_agent_testing_tool.mcp_clients import (
    GENERIC_PROFILE,
    MUSE_TEMPLATE_PROFILE,
    TEMPLATE_BEHAVIORS,
    make_client,
)

LOGIN = "CATT_TEST_LOGIN"
STORE = Path(".cache/connector-tokens")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the token store must be workspace-local
    monkeypatch.setenv(LOGIN, "taylor:correct-horse")
    return tmp_path


@contextmanager
def authorization_server(**options):
    fixture = OAuthFixture(**options)
    with serve_asgi(fixture.app) as base:
        fixture.endpoint = f"{base}/mcp"
        yield fixture


def oauth(**fields):
    return OAuthAuth(mode="oauth", consent="fixture-auto", test_login_env=LOGIN, **fields)


async def linked(fixture, auth=None, **kwargs):
    return await link(auth or oauth(scopes=["read"]), fixture.endpoint, store=STORE,
                      http_policy=MUSE_TEMPLATE_PROFILE, **kwargs)


async def call(profile, fixture, credential, name="search_rooms"):
    async with make_client(profile, fixture.endpoint, credential) as client:
        return await client.call_tool(name, {}), client


async def test_dcr_pkce_resource_indicator_and_a_private_token_store(workspace):
    redactor = Redactor()
    with authorization_server() as fixture:
        credential = await linked(fixture, oauth(scopes=["read"], expected_subject="taylor"), redactor=redactor)
        template, template_client = await call(MUSE_TEMPLATE_PROFILE, fixture, credential)
        sdk, _ = await call(GENERIC_PROFILE, fixture, credential)
    assert isinstance(credential, OAuthCredential)
    described = credential.describe()
    assert described | {"expires_at": None, "issuer": None} == {
        "mode": "oauth", "placement": "bearer_header", "registration": "dcr", "consent": "fixture-auto",
        "scopes": ["read"], "requested_scopes": ["read"], "read_only_scopes": [], "read_only_supported": None,
        "subject": "taylor", "expires_at": None, "issuer": None, "resource": fixture.endpoint,
        "protected_resource_metadata": True, "pkce": "S256", "refreshable": True, "reused_stored_token": False}
    assert described["issuer"].startswith(fixture.base) and described["expires_at"]
    # PRM -> AS metadata -> DCR -> authorization code with PKCE -> a token bound to this resource.
    assert fixture.registrations == 1 and fixture.logins == ["taylor"]
    assert fixture.authorizations == [{"client_id": fixture.authorizations[0]["client_id"],
                                       "resource": fixture.endpoint, "scopes": ["read"], "challenge": True}]
    assert [token.resource for token in fixture.access.values()] == [fixture.endpoint]
    assert template.error is None and sdk.error is None
    assert template.result["structuredContent"] == {"rooms": 2}
    # Every credential value is a registered secret; the store is private and outside runs/.
    access = next(iter(fixture.access))
    assert access in credential.secrets and access in redactor.secrets
    assert redactor.clean({"headers": f"Bearer {access}"}) == {"headers": "Bearer [REDACTED]"}
    assert all(access not in json.dumps(note) for note in template_client.observations)
    assert "correct-horse" in credential.secrets  # the fixture password is a secret too
    files = list((workspace / STORE).iterdir())
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert (workspace / STORE).stat().st_mode & 0o777 == 0o700
    record = json.loads(files[0].read_text())
    assert record["tokens"]["access_token"] == access and record["subject"] == "taylor"


async def test_stored_tokens_are_reused_without_a_new_authorization(workspace):
    with authorization_server() as fixture:
        await linked(fixture)
        again = await linked(fixture)
        result, _ = await call(MUSE_TEMPLATE_PROFILE, fixture, again)
    assert len(fixture.authorizations) == 1 and again.describe()["reused_stored_token"] is True
    assert result.error is None


async def test_the_template_refreshes_once_after_a_401(workspace):
    redactor = Redactor()
    with authorization_server() as fixture:
        credential = await linked(fixture, redactor=redactor)
        fixture.expire_all()
        result, client = await call(MUSE_TEMPLATE_PROFILE, fixture, credential)
    assert result.error is None and fixture.token_requests == ["authorization_code", "refresh_token"]
    refreshed = next(note for note in client.observations if note["kind"] == "credential-refreshed")
    # Host credential upkeep, labeled: the unofficial template itself retries nothing.
    assert refreshed == {"kind": "credential-refreshed", "status": 401, "count": 1, "basis": "assumed",
                         "source": TEMPLATE_BEHAVIORS["host-credential-refresh"][1]}
    fresh = next(iter(fixture.access))
    assert fresh in credential.secrets and fresh in redactor.secrets
    assert len(fixture.authorizations) == 1  # refreshed, never silently re-authorized


async def test_the_sdk_refreshes_an_expired_token_before_sending(workspace):
    with authorization_server() as fixture:
        credential = await linked(fixture)
        credential.provider.context.token_expiry_time = 1.0  # in the past
        result, client = await call(GENERIC_PROFILE, fixture, credential)
    assert result.error is None and fixture.token_requests == ["authorization_code", "refresh_token"]
    assert "credential-refreshed" not in [note["kind"] for note in client.observations]
    assert next(iter(fixture.access)) in credential.secrets


@pytest.mark.parametrize("profile", [MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE])
async def test_a_revoked_link_is_reported_never_reauthorized(workspace, profile):
    with authorization_server() as fixture:
        credential = await linked(fixture)
        async with make_client(profile, fixture.endpoint, credential) as client:
            fixture.revoke_all()  # mid-run
            result = await client.call_tool("search_rooms", {})
        with pytest.raises(LinkError) as raised:  # a later connect fails the same way
            async with make_client(profile, fixture.endpoint, credential):
                pass
    assert result.error["category"] == "authentication" and result.error["kind"] == "revoked"
    assert not result.uncertain and raised.value.kind == "revoked"
    assert len(fixture.authorizations) == 1 and credential.failure in (None, "revoked")


async def test_a_refused_refresh_reports_the_servers_reason(workspace):
    with authorization_server() as fixture:
        credential = await linked(fixture)
        async with make_client(MUSE_TEMPLATE_PROFILE, fixture.endpoint, credential) as client:
            fixture.expire_all(refresh=True)  # the AS answers "refresh token has expired"
            result = await client.call_tool("search_rooms", {})
    assert result.error["kind"] == "expired" and credential.failure == "expired"
    assert {"kind": "refresh-refused", "status": 400} in credential.observations
    stored = json.loads(next((workspace / STORE).iterdir()).read_text())
    assert "tokens" not in stored and "client_info" in stored  # the dead grant is dropped, not the client


async def test_an_unreachable_token_endpoint_keeps_the_grant(workspace):
    from pydantic import AnyHttpUrl
    from test_mcp_clients_template import unused_port
    with authorization_server() as fixture:
        credential = await linked(fixture)
        context = credential.provider.context
        context.oauth_metadata = context.oauth_metadata.model_copy(
            update={"token_endpoint": AnyHttpUrl(f"http://127.0.0.1:{unused_port()}/token")})
        assert await credential.refresh() is False
    assert credential.failure == "revoked" and {"kind": "refresh-unreachable"} in credential.observations
    stored = json.loads(next((workspace / STORE).iterdir()).read_text())
    assert stored["tokens"]["refresh_token"] in credential.secrets  # kept for a later attempt


@pytest.mark.parametrize("profile", [MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE])
async def test_an_expired_link_without_a_refresh_token_is_reported(workspace, profile):
    with authorization_server(refresh=False) as fixture:
        credential = await linked(fixture)
        async with make_client(profile, fixture.endpoint, credential) as client:
            fixture.expire_all()
            credential.provider.context.token_expiry_time = 1.0  # in the past
            result = await client.call_tool("search_rooms", {})
        assert await credential.refresh() is False
    assert result.error["kind"] == "expired" and credential.failure == "expired"
    assert not result.uncertain and len(fixture.authorizations) == 1


async def test_the_wrong_account_is_refused_and_forgotten(workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, "alex:correct-horse")
    with (authorization_server() as fixture,
          pytest.raises(LinkError, match="differs from expected_subject") as raised):
        await linked(fixture, oauth(scopes=["read"], expected_subject="taylor"))
    assert raised.value.kind == "wrong-account" and "alex" not in str(raised.value)
    record = json.loads(next((workspace / STORE).iterdir()).read_text())
    assert "tokens" not in record  # the wrong account's tokens are never reused


async def test_an_unverifiable_subject_fails_closed(workspace):
    with (authorization_server(subject_in_token=False) as fixture,
          pytest.raises(LinkError, match="reported no subject") as raised):
        await linked(fixture, oauth(expected_subject="taylor"))
    assert raised.value.kind == "wrong-account"


async def test_declared_scopes_win_and_missing_scopes_fail(workspace):
    with (authorization_server(required=("read", "book")) as fixture,
          pytest.raises(LinkError) as raised):
        await linked(fixture, oauth(scopes=["read"]))
    assert raised.value.kind == "insufficient-scope"
    # PRM advertises read and book; the authorization request asked for exactly the declared scopes.
    assert [request["scopes"] for request in fixture.authorizations] == [["read"]]


async def test_a_scope_the_server_does_not_offer_is_refused(workspace, monkeypatch):
    with authorization_server() as fixture:
        with pytest.raises(LinkError, match="admin") as unknown:  # declared scopes are registered too
            await linked(fixture, oauth(scopes=["admin"]))
        monkeypatch.setenv("CATT_CLIENT_ID", "catt-read-only")
        fixture.preregister("catt-read-only", None, scopes=["read"])
        with pytest.raises(LinkError, match="invalid_scope") as narrow:
            await linked(fixture, oauth(registration="preregistered", client_id_env="CATT_CLIENT_ID",
                                        scopes=["read", "book"]))
    assert unknown.value.kind == "registration" and narrow.value.kind == "insufficient-scope"


async def test_read_only_linking_support_is_described(workspace):
    with authorization_server() as fixture:
        offered = await linked(fixture, oauth(scopes=["read"], read_only_scopes=["read"]))
        absent = await linked(fixture, oauth(scopes=["read", "book"], read_only_scopes=["bookings.read"]))
    assert offered.describe()["read_only_supported"] is True
    assert absent.describe()["read_only_supported"] is False


async def test_missing_metadata_is_a_discovery_error_without_guessing(workspace):
    with (authorization_server(metadata=False) as fixture,
          pytest.raises(LinkError, match="RFC 8414") as raised):
        await linked(fixture)
    assert raised.value.kind == "discovery" and fixture.registrations == 0


async def test_pkce_s256_must_be_advertised(workspace):
    with (authorization_server(pkce=False) as fixture,
          pytest.raises(LinkError, match="PKCE S256") as raised):
        await linked(fixture)
    assert raised.value.kind == "discovery" and fixture.authorizations == []


async def test_cimd_uses_the_metadata_url_as_client_id(workspace):
    url = "https://client.example.test/catt-client.json"
    with authorization_server(cimd=True) as fixture:
        credential = await linked(fixture, oauth(registration="cimd", client_metadata_url=url, scopes=["read"]))
        result, _ = await call(MUSE_TEMPLATE_PROFILE, fixture, credential)
    assert fixture.registrations == 0 and fixture.authorizations[0]["client_id"] == url
    assert result.error is None


async def test_cimd_never_falls_back_to_dynamic_registration(workspace):
    url = "https://client.example.test/catt-client.json"
    with (authorization_server(cimd=False) as fixture,
          pytest.raises(LinkError, match="client_id_metadata_document_supported") as raised):
        await linked(fixture, oauth(registration="cimd", client_metadata_url=url))
    assert raised.value.kind == "registration" and fixture.registrations == 0


async def test_a_preregistered_confidential_client(workspace, monkeypatch):
    monkeypatch.setenv("CATT_CLIENT_ID", "catt-preregistered")
    monkeypatch.setenv("CATT_CLIENT_SECRET", "preregistered-secret-value")
    with authorization_server() as fixture:
        fixture.preregister("catt-preregistered", "preregistered-secret-value")
        credential = await linked(fixture, oauth(registration="preregistered", client_id_env="CATT_CLIENT_ID",
                                                 client_secret_env="CATT_CLIENT_SECRET", scopes=["read"]))
        result, _ = await call(GENERIC_PROFILE, fixture, credential)
    assert fixture.registrations == 0 and fixture.authorizations[0]["client_id"] == "catt-preregistered"
    assert "preregistered-secret-value" in credential.secrets and result.error is None
    stored = json.loads(next((workspace / STORE).iterdir()).read_text())
    assert "preregistered-secret-value" not in json.dumps(stored)  # env-held secrets never reach the store


async def test_fixture_auto_only_signs_in_on_loopback(workspace):
    with (authorization_server(authorization_endpoint="https://login.example.test/authorize") as fixture,
          pytest.raises(LinkError, match="loopback") as raised):
        await linked(fixture)
    assert raised.value.kind == "authorization" and fixture.logins == []


async def test_fixture_auto_never_links_a_remote_endpoint(workspace):
    with pytest.raises(LinkError, match="loopback") as raised:
        await link(oauth(), "https://mcp.example.test/mcp", store=STORE, http_policy=GENERIC_PROFILE)
    assert raised.value.kind == "authorization"
    assert not (workspace / STORE).exists()  # refused before any traffic or store


async def test_a_rejected_test_login_is_an_authorization_error(workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, "taylor:wrong-password")
    with (authorization_server() as fixture,
          pytest.raises(LinkError, match="rejected the test login") as raised):
        await linked(fixture)
    assert raised.value.kind == "authorization" and fixture.logins == ["taylor"]  # never resubmitted


async def test_interactive_consent_prints_the_url_and_waits(workspace, monkeypatch, capsys):
    monkeypatch.setattr(linking, "INTERACTIVE_TIMEOUT", 0.3)
    with (authorization_server() as fixture,
          pytest.raises(LinkError, match="No authorization redirect") as raised):
        await linked(fixture, OAuthAuth(mode="oauth", scopes=["read"]))
    assert raised.value.kind == "authorization"
    printed = capsys.readouterr().err
    assert f"{fixture.base}/authorize?" in printed and "code_challenge_method=S256" in printed


async def test_an_endpoint_without_an_oauth_challenge_is_not_oauth(workspace):
    with FakeMcp() as fake, pytest.raises(LinkError, match="does not use OAuth") as raised:
        await link(oauth(), fake.url, store=STORE, http_policy=GENERIC_PROFILE)
    assert raised.value.kind == "discovery"


async def test_the_test_login_must_be_present(workspace, monkeypatch):
    monkeypatch.delenv(LOGIN)
    with pytest.raises(LinkError) as raised:
        await link(oauth(), "http://127.0.0.1:9/mcp", store=STORE, http_policy=GENERIC_PROFILE)
    assert raised.value.kind == "missing" and LOGIN in str(raised.value)


@pytest.mark.parametrize("store, message", [
    ("runs/connector-1/tokens", "runs/ or exports/"), ("exports/x", "runs/ or exports/"),
    ("../outside", "inside the workspace"), ("evidence/tokens", "run directory")])
async def test_the_token_store_stays_private_and_out_of_evidence(workspace, store, message):
    (workspace / "evidence").mkdir()
    (workspace / "evidence/result.json").write_text("{}")
    with pytest.raises(LabError, match=message):
        await link(oauth(), "http://127.0.0.1:9/mcp", store=Path(store), http_policy=GENERIC_PROFILE)


async def test_a_readable_token_file_is_refused(workspace):
    with authorization_server() as fixture:
        await linked(fixture)
        stored = next((workspace / STORE).iterdir())
        os.chmod(stored, 0o644)
        with pytest.raises(LabError, match="readable by others"):
            await linked(fixture)


async def test_the_sdk_provider_cannot_take_the_refresh_token_to_a_new_origin_during_a_run(workspace):
    import httpx2

    from consumer_agent_testing_tool.mcp_clients import SdkClient
    with authorization_server() as fixture:
        credential = await linked(fixture)
        hooks = credential.event_hooks()
        guard = hooks["request"][0]
        # The origins this link used stay reachable: the resource and its authorization server.
        await guard(httpx2.Request("POST", fixture.endpoint))
        await guard(httpx2.Request("POST", f"{fixture.base}/token"))
        # Metadata a 401 names later cannot send a token request anywhere else (or in cleartext off loopback).
        for url in ("https://attacker.example/token", "http://203.0.113.9/token"):
            with pytest.raises(LinkError) as raised:
                await guard(httpx2.Request("POST", url))
            assert raised.value.kind in ("discovery", "authorization")
        # The SDK client installs the same hooks on its own HTTP client.
        client = SdkClient(GENERIC_PROFILE, fixture.endpoint, credential)
        assert client._hooks["request"] == hooks["request"] and client._hooks["response"] == hooks["response"]
