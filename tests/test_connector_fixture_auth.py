"""Connector fixture auth modes: none, API keys and the in-memory OAuth authorization server."""
import base64
import hashlib
import json
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from test_connector_fixture_tools import STAY, book, call, has, mcp_session, raw_call

from consumer_agent_testing_tool.connector_fixture import (
    FixtureConfig,
    PreregisteredClient,
    serve_fixture,
    subject_for,
)

REDIRECT = "http://127.0.0.1:47321/callback"
CIMD = json.loads((Path(__file__).parent / "data/connector/fixture-cimd-client.json").read_text())


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def query(location):
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


async def register(http, fixture, **metadata):
    response = await http.post(fixture.base_url + "/register", json={
        "redirect_uris": [REDIRECT], "client_name": "Linking test",
        "token_endpoint_auth_method": "none", **metadata})
    assert response.status_code == 201, response.text
    return response.json()


async def authorize(http, fixture, client_id, challenge, *, scope=None, method="S256",
                    redirect=REDIRECT):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": method, "state": "st-1",
              "resource": fixture.mcp_url} | ({"scope": scope} if scope else {})
    return await http.get(fixture.base_url + "/authorize", params=params)


async def sign_in(http, fixture, location, username, password=None, action="approve"):
    password = fixture.credentials["users"][username] if password is None else password
    return await http.post(fixture.base_url + "/login", data={
        "txn": query(location)["txn"], "username": username, "password": password,
        "action": action})


async def exchange(http, fixture, **form):
    return await http.post(fixture.base_url + "/token", data=form)


async def link(http, fixture, username="taylor", *, scope=None):
    client = await register(http, fixture)
    verifier, challenge = pkce()
    started = await authorize(http, fixture, client["client_id"], challenge, scope=scope)
    approved = await sign_in(http, fixture, started.headers["location"], username)
    code = query(approved.headers["location"])["code"]
    tokens = await exchange(http, fixture, grant_type="authorization_code", code=code,
                            redirect_uri=REDIRECT, client_id=client["client_id"],
                            code_verifier=verifier, resource=fixture.mcp_url)
    assert tokens.status_code == 200, tokens.text
    return client, tokens.json()


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_none_mode_is_guest_checkout_without_an_authorization_server():
    async with serve_fixture() as fixture, mcp_session(fixture.mcp_url) as session:
        _, error, _ = await book(session)
        async with httpx.AsyncClient(trust_env=False) as http:
            metadata = await http.get(fixture.base_url + "/.well-known/oauth-protected-resource/mcp")
    state = fixture.controls.state()
    assert not error and state["bookings"][0]["account"] == "guest" and metadata.status_code == 404
    # Guest checkout (guidelines 1.1) is paying without logging in, not an unauthenticated endpoint.
    assert state["authorization"] is None and state["basis"]["auth"]["basis"] == "assumed"


async def test_api_key_header_rejects_missing_wrong_and_misplaced_keys():
    async with serve_fixture(FixtureConfig(auth="api_key")) as fixture:
        key = fixture.credentials["api_keys"]["taylor"]
        missing, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY)
        assert missing.status_code == 401 and missing.json()["error"] == "missing_api_key"
        assert missing.headers["www-authenticate"] == 'ApiKey realm="cedar-stays", header="X-API-Key"'
        wrong, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY,
                                  headers={"X-API-Key": key[:-1] + "x"})
        assert wrong.status_code == 401 and wrong.json()["error"] == "invalid_api_key"
        misplaced, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY,
                                      headers={"Authorization": f"Bearer {key}"})
        assert misplaced.status_code == 401
        async with mcp_session(fixture.mcp_url, headers={"X-API-Key": key}) as session:
            _, error, _ = await book(session)
    state = fixture.controls.state()
    assert not error and has(state["bookings"][0], {"account": "taylor",
                                                    "subject": subject_for("taylor")})
    assert [entry["auth"] for entry in state["requests"][:3]] == ["missing", "invalid", "missing"]
    assert state["http"]["unauthorized"] == 3 and key not in json.dumps(state)


@pytest.mark.parametrize("placement,name", [("bearer", "X-API-Key"), ("query", "api_key"),
                                            ("path", "X-API-Key"), ("header", "X-Cedar-Key")])
async def test_api_key_placements_and_challenges(placement, name):
    config = FixtureConfig(auth="api_key", api_key_placement=placement, api_key_name=name)
    async with serve_fixture(config) as fixture:
        key = fixture.credentials["api_keys"]["taylor"]
        url, headers = fixture.mcp_url, {}
        if placement == "bearer":
            headers = {"Authorization": f"Bearer {key}"}
        elif placement == "query":
            url = f"{fixture.mcp_url}?{name}={key}"
        elif placement == "path":
            url = f"{fixture.mcp_url}/{key}"
        else:
            headers = {name: key}
        denied, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY)
        accepted, result = await raw_call(url, "search_rooms", STAY, headers=headers)
    assert denied.status_code == 401 and accepted.status_code == 200
    assert result["structuredContent"]["rooms"]
    challenge = denied.headers["www-authenticate"]
    expected = {"bearer": 'Bearer realm="cedar-stays"', "query": 'param="api_key"',
                "path": 'in="path"', "header": 'header="X-Cedar-Key"'}[placement]
    assert expected in challenge
    state = json.dumps(fixture.controls.state())
    assert key not in state  # Paths and query strings that carry keys are never logged.


async def test_api_keys_map_to_accounts_and_sessions_stay_bound_to_their_key():
    keys = {"cs-key-taylor-0123456789": "taylor", "cs-key-jordan-0123456789": "jordan"}
    async with serve_fixture(FixtureConfig(auth="api_key", api_keys=keys)) as fixture:
        taylor, jordan = ({"X-API-Key": key} for key in keys)
        async with mcp_session(fixture.mcp_url, headers=taylor) as session:
            _, error, booking = await book(session)
        async with mcp_session(fixture.mcp_url, headers=jordan) as session:
            error, hidden = await call(session, "get_booking", booking_id=booking["booking_id"])
        assert error and hidden["error"] == "booking_not_found"
        opened, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=taylor)
        assert opened.status_code == 200
        async with httpx.AsyncClient(trust_env=False) as http:
            hijack = await http.post(fixture.mcp_url, headers={
                **jordan, "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "mcp-session-id": opened.request.headers["mcp-session-id"],
                "mcp-protocol-version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    assert hijack.status_code == 404  # The SDK refuses a session opened by another account.
    assert fixture.credentials == {}


async def test_oauth_discovery_metadata_and_bearer_challenges():
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture:
        prm_url = fixture.base_url + "/.well-known/oauth-protected-resource/mcp"
        missing, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY)
        bogus, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY,
                                  headers={"Authorization": "Bearer csat_not-a-token"})
        async with httpx.AsyncClient(trust_env=False) as http:
            prm = (await http.get(prm_url)).json()
            issuer = (await http.get(fixture.base_url
                                     + "/.well-known/oauth-authorization-server")).json()
            rebound = await http.post(fixture.base_url + "/register", json={},
                                      headers={"Host": "rebind.example:80"})
    assert missing.status_code == 401 and bogus.status_code == 401
    assert missing.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{prm_url}", scope="stays:read stays:write"')
    assert 'error="invalid_token"' in bogus.headers["www-authenticate"]
    assert f'resource_metadata="{prm_url}"' in bogus.headers["www-authenticate"]
    assert prm == {"resource": fixture.mcp_url, "authorization_servers": [fixture.base_url],
                   "scopes_supported": ["stays:read", "stays:write"],
                   "bearer_methods_supported": ["header"], "resource_name": "Cedar Stays"}
    assert has(issuer, {
        "issuer": fixture.base_url, "authorization_endpoint": fixture.base_url + "/authorize",
        "token_endpoint": fixture.base_url + "/token",
        "registration_endpoint": fixture.base_url + "/register",
        "revocation_endpoint": fixture.base_url + "/revoke",
        "code_challenge_methods_supported": ["S256"], "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "introspection_endpoint": fixture.base_url + "/introspect",
        "scopes_supported": ["stays:read", "stays:write"]})
    assert "none" in issuer["token_endpoint_auth_methods_supported"]
    assert rebound.status_code == 421  # DNS rebinding cannot reach the authorization server.


async def test_oauth_code_flow_enforces_pkce_and_login_and_issues_working_tokens():
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture, \
            httpx.AsyncClient(trust_env=False) as http:
        client = await register(http, fixture)
        verifier, challenge = pkce()
        plain = await authorize(http, fixture, client["client_id"], verifier, method="plain")
        assert plain.status_code == 302 and query(plain.headers["location"])["error"] == \
            "invalid_request"
        started = await authorize(http, fixture, client["client_id"], challenge)
        assert started.status_code == 302
        location = started.headers["location"]
        assert location.startswith(fixture.base_url + "/login?txn=")
        form = await http.get(location)
        assert form.status_code == 200 and "Sign in to Cedar Stays" in form.text
        assert "Linking test" in form.text and "stays:write" in form.text
        refused = await sign_in(http, fixture, location, "taylor", password="wrong-password")
        assert refused.status_code == 401 and "Incorrect" in refused.text
        approved = await sign_in(http, fixture, location, "taylor")
        assert approved.status_code == 302
        callback = query(approved.headers["location"])
        assert approved.headers["location"].startswith(REDIRECT)
        assert callback["state"] == "st-1" and callback["iss"] == fixture.base_url
        common = {"grant_type": "authorization_code", "code": callback["code"],
                  "redirect_uri": REDIRECT, "client_id": client["client_id"],
                  "resource": fixture.mcp_url}
        wrong = await exchange(http, fixture, **common, code_verifier=pkce()[0])
        assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
        issued = await exchange(http, fixture, **common, code_verifier=verifier)
        assert issued.status_code == 200
        tokens = issued.json()
        replayed = await exchange(http, fixture, **common, code_verifier=verifier)
        assert replayed.status_code == 400 and replayed.json()["error"] == "invalid_grant"
        whoami = (await http.get(fixture.base_url + "/userinfo", headers=bearer(tokens))).json()
        async with mcp_session(fixture.mcp_url, headers=bearer(tokens)) as session:
            _, error, _ = await book(session)
    assert tokens["access_token"].startswith("csat_") and tokens["refresh_token"].startswith("csrt_")
    assert has(tokens, {"token_type": "Bearer", "expires_in": 3600,
                        "scope": "stays:read stays:write"})
    assert whoami == {"sub": subject_for("taylor"), "preferred_username": "taylor",
                      "name": "Taylor Example"}
    state = fixture.controls.state()
    assert not error and has(state["bookings"][0], {"account": "taylor",
                                                    "subject": subject_for("taylor")})
    grants = state["authorization"]["grants"]
    assert [(grant["kind"], grant["status"]) for grant in grants] == [("access", "active"),
                                                                      ("refresh", "active")]
    assert tokens["access_token"] not in json.dumps(state)
    events = [event["event"] for event in state["authorization"]["events"]]
    assert events == ["registered", "login_failed", "login", "code_issued", "tokens_issued"]


async def test_oauth_refresh_rotates_detects_replay_and_honors_revocation():
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture, \
            httpx.AsyncClient(trust_env=False) as http:
        client, first = await link(http, fixture)
        refresh = {"grant_type": "refresh_token", "client_id": client["client_id"],
                   "resource": fixture.mcp_url}
        rotated = await exchange(http, fixture, **refresh, refresh_token=first["refresh_token"])
        assert rotated.status_code == 200
        second = rotated.json()
        old, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(first))
        assert old.status_code == 401 and "revoked" in old.headers["www-authenticate"]
        fresh, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(second))
        assert fresh.status_code == 200
        replay = await exchange(http, fixture, **refresh, refresh_token=first["refresh_token"])
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
        family, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(second))
        assert family.status_code == 401  # Replaying a rotated token revokes its family.
        client, third = await link(http, fixture)
        revoked = await http.post(fixture.base_url + "/revoke", data={
            "token": third["access_token"], "client_id": client["client_id"]})
        assert revoked.status_code == 200
        after, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(third))
        assert after.status_code == 401
        dead = await exchange(http, fixture, grant_type="refresh_token",
                              client_id=client["client_id"], refresh_token=third["refresh_token"])
        assert dead.status_code == 400
    reasons = [event.get("reason") for event in fixture.controls.state()["authorization"]["events"]
               if event["event"] == "revoked"]
    assert reasons == ["refresh_replay", "client_revocation"]


async def test_oauth_expiry_runs_on_the_fixture_clock_and_users_can_revoke_access():
    async with serve_fixture(FixtureConfig(auth="oauth", access_token_seconds=60)) as fixture, \
            httpx.AsyncClient(trust_env=False) as http:
        client, tokens = await link(http, fixture)
        assert tokens["expires_in"] == 60
        fixture.controls.advance_clock(61)
        expired, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(tokens))
        assert expired.status_code == 401
        assert 'error_description="The access token expired"' in expired.headers[
            "www-authenticate"]
        renewed = await exchange(http, fixture, grant_type="refresh_token",
                                 client_id=client["client_id"],
                                 refresh_token=tokens["refresh_token"])
        tokens = renewed.json()
        works, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(tokens))
        assert works.status_code == 200
        revoke = await http.post(fixture.base_url + "/__oracle/revoke", json={"username": "taylor"})
        assert revoke.json() == {"revoked_families": 1}
        inactive = await http.post(fixture.base_url + "/introspect", data={
            "token": tokens["access_token"], "client_id": client["client_id"]})
        assert inactive.json() == {"active": False}
        gone, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY, headers=bearer(tokens))
        assert gone.status_code == 401 and "was revoked" in gone.headers["www-authenticate"]
        dead = await exchange(http, fixture, grant_type="refresh_token",
                              client_id=client["client_id"],
                              refresh_token=tokens["refresh_token"])
        assert dead.status_code == 400


async def test_oauth_read_only_link_gets_an_insufficient_scope_challenge_for_writes():
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture, \
            httpx.AsyncClient(trust_env=False) as http:
        _, tokens = await link(http, fixture, scope="stays:read")
        assert tokens["scope"] == "stays:read"
        read, result = await raw_call(fixture.mcp_url, "get_quote",
                                      STAY | {"room_id": "rm-garden-queen"}, headers=bearer(tokens))
        assert read.status_code == 200 and not result.get("isError")
        write, _ = await raw_call(fixture.mcp_url, "create_booking", {
            "quote_id": result["structuredContent"]["quote_id"], "guest_name": "Taylor Example",
            "guest_email": "taylor@example.com"}, headers=bearer(tokens))
    assert write.status_code == 403 and write.json()["error"] == "insufficient_scope"
    challenge = write.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge and 'scope="stays:read stays:write"' in challenge
    state = fixture.controls.state()
    assert state["bookings"] == [] and state["http"]["forbidden"] == 1


async def test_oauth_scope_check_refuses_bodies_two_parsers_could_read_differently():
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture, \
            httpx.AsyncClient(trust_env=False) as http:
        _, tokens = await link(http, fixture, scope="stays:read")
        _, quote = await raw_call(fixture.mcp_url, "get_quote",
                                  STAY | {"room_id": "rm-garden-queen"}, headers=bearer(tokens))
        arguments = json.dumps({"quote_id": quote["structuredContent"]["quote_id"],
                                "guest_name": "Taylor Example",
                                "guest_email": "taylor@example.com"})
        # A read tool name, then the write: JSON parsers need not agree on which one wins.
        smuggled = ('{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": '
                    f'{{"name": "get_booking", "name": "create_booking", "arguments": {arguments}}}}}')
        duplicate, _ = await raw_call(fixture.mcp_url, None, None, headers=bearer(tokens),
                                      raw_body=smuggled)
        batch, _ = await raw_call(fixture.mcp_url, None, None, headers=bearer(tokens),
                                  raw_body=json.dumps([{
                                      "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "create_booking",
                                                 "arguments": json.loads(arguments)}}]))
        # Invalid UTF-8: a body only another parser might accept never reaches dispatch.
        garbled, _ = await raw_call(fixture.mcp_url, None, None, headers=bearer(tokens),
                                    raw_body=json.dumps({
                                        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "create_booking",
                                                   "arguments": json.loads(arguments)}}
                                    ).encode().replace(b"Taylor", b"Taylor \xff"))
        anonymous = await http.post(fixture.mcp_url, content=smuggled, headers={
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    assert anonymous.status_code == 401  # Credentials are judged before the body.
    assert duplicate.status_code == 400 and "Duplicate" in duplicate.json()["error"]["message"]
    assert batch.status_code == 400 and "batches" in batch.json()["error"]["message"]
    assert garbled.status_code == 400 and garbled.json()["error"]["code"] == -32700
    state = fixture.controls.state()
    assert state["bookings"] == [] and state["calls"]["create_booking"] == 0


async def test_read_only_oauth_scope_cannot_stop_a_hidden_write():
    config = FixtureConfig(auth="oauth", variant="hidden-write")
    async with serve_fixture(config) as fixture, httpx.AsyncClient(trust_env=False) as http:
        _, tokens = await link(http, fixture, scope="stays:read")
        quoted, _ = await raw_call(fixture.mcp_url, "get_quote",
                                   STAY | {"room_id": "rm-cedar-king"}, headers=bearer(tokens))
    assert quoted.status_code == 200
    state = fixture.controls.state()
    assert [(c["created_by"], c["account"]) for c in state["charges"]] == [("get_quote", "taylor")]


async def test_oauth_subjects_reveal_a_wrong_account_link():
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture, \
            httpx.AsyncClient(trust_env=False) as http:
        taylor_client, taylor = await link(http, fixture, "taylor")
        jordan_client, jordan = await link(http, fixture, "jordan")
        who = (await http.get(fixture.base_url + "/userinfo", headers=bearer(jordan))).json()

        async def introspect(client, token):
            response = await http.post(fixture.base_url + "/introspect", data={
                "token": token, "client_id": client["client_id"]})
            return response.json()
        own = await introspect(jordan_client, jordan["access_token"])
        foreign = await introspect(jordan_client, taylor["access_token"])
        refresh = await introspect(taylor_client, taylor["refresh_token"])
        async with mcp_session(fixture.mcp_url, headers=bearer(jordan)) as session:
            _, _, booking = await book(session)
        async with mcp_session(fixture.mcp_url, headers=bearer(taylor)) as session:
            error, hidden = await call(session, "get_booking", booking_id=booking["booking_id"])
    assert who["sub"] == subject_for("jordan") != subject_for("taylor")
    assert has(own, {"active": True, "sub": subject_for("jordan"), "username": "jordan",
                     "client_id": jordan_client["client_id"], "token_type": "Bearer",
                     "aud": fixture.mcp_url, "iss": fixture.base_url})
    assert foreign == {"active": False}  # A client cannot probe another client's tokens.
    assert refresh["active"] and refresh["token_type"] == "refresh_token"
    assert error and hidden["error"] == "booking_not_found"
    state = fixture.controls.state()
    assert state["bookings"][0]["account"] == "jordan"
    assert {account["subject"] for account in state["accounts"]} == {
        subject_for("taylor"), subject_for("jordan")}


async def test_oauth_cimd_clients_resolve_from_configuration_never_the_network():
    config = FixtureConfig(auth="oauth", cimd_documents={CIMD["client_id"]: CIMD})
    async with serve_fixture(config) as fixture, httpx.AsyncClient(trust_env=False) as http:
        verifier, challenge = pkce()
        unknown = await authorize(http, fixture, "https://other.example.com/app.json", challenge)
        assert unknown.status_code == 400 and "not found" in unknown.json()["error_description"]
        started = await authorize(http, fixture, CIMD["client_id"], challenge,
                                  redirect=CIMD["redirect_uris"][0])
        approved = await sign_in(http, fixture, started.headers["location"], "taylor")
        callback = query(approved.headers["location"])
        tokens = await exchange(http, fixture, grant_type="authorization_code",
                                code=callback["code"], redirect_uri=CIMD["redirect_uris"][0],
                                client_id=CIMD["client_id"], code_verifier=verifier)
    assert tokens.status_code == 200
    authorization = fixture.controls.state()["authorization"]
    lookups = [(event["client_id"], event["resolved"]) for event in authorization["events"]
               if event["event"] == "cimd_lookup"]
    assert ("https://other.example.com/app.json", False) in lookups
    assert (CIMD["client_id"], True) in lookups
    assert {"client_id": CIMD["client_id"], "registration": "cimd"}.items() <= next(
        client for client in authorization["clients"]
        if client["client_id"] == CIMD["client_id"]).items()


async def test_oauth_consent_denial_disabled_dcr_and_confidential_preregistered_clients():
    partner = PreregisteredClient(client_id="cedar-partner-app", redirect_uris=[REDIRECT],
                                  client_secret="partner-secret-0123456789", client_name="Partner")
    config = FixtureConfig(auth="oauth", dcr=False, preregistered_clients=[partner])
    async with serve_fixture(config) as fixture, httpx.AsyncClient(trust_env=False) as http:
        metadata = (await http.get(fixture.base_url
                                   + "/.well-known/oauth-authorization-server")).json()
        assert "registration_endpoint" not in metadata
        assert (await http.post(fixture.base_url + "/register", json={})).status_code == 404
        verifier, challenge = pkce()
        started = await authorize(http, fixture, partner.client_id, challenge)
        denied = await sign_in(http, fixture, started.headers["location"], "taylor",
                               action="deny")
        assert query(denied.headers["location"]) | {"iss": None} == {
            "error": "access_denied", "state": "st-1", "iss": None}
        started = await authorize(http, fixture, partner.client_id, challenge)
        approved = await sign_in(http, fixture, started.headers["location"], "taylor")
        common = {"grant_type": "authorization_code", "code": query(
            approved.headers["location"])["code"], "redirect_uri": REDIRECT,
            "client_id": partner.client_id, "code_verifier": verifier}
        anonymous = await exchange(http, fixture, **common)
        assert anonymous.status_code == 401 and anonymous.json()["error"] == "invalid_client"
        issued = await exchange(http, fixture, **common, client_secret=partner.client_secret)
        assert issued.status_code == 200


class MemoryStorage(TokenStorage):
    def __init__(self):
        self.tokens = self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info


def sdk_provider(fixture, *, client_metadata_url=None, redirect=REDIRECT):
    """The official SDK OAuth client, with a scripted user who signs in on the test login."""
    callback = {}

    async def redirect_handler(url):
        async with httpx.AsyncClient(trust_env=False) as http:
            started = await http.get(url)
            approved = await sign_in(http, fixture, started.headers["location"], "taylor")
            callback.update(query(approved.headers["location"]))

    async def callback_handler():
        return AuthorizationCodeResult(code=callback["code"], state=callback["state"],
                                       iss=callback.get("iss"))
    metadata = OAuthClientMetadata(redirect_uris=[redirect], client_name="SDK linking test",
                                   token_endpoint_auth_method="none")
    return OAuthClientProvider(server_url=fixture.mcp_url, client_metadata=metadata,
                               storage=MemoryStorage(), redirect_handler=redirect_handler,
                               callback_handler=callback_handler,
                               client_metadata_url=client_metadata_url)


@pytest.mark.parametrize("registration", ["dcr", "cimd"])
async def test_official_sdk_oauth_client_discovers_registers_and_calls_tools(registration):
    config = FixtureConfig(auth="oauth", cimd_documents={CIMD["client_id"]: CIMD})
    async with serve_fixture(config) as fixture:
        provider = sdk_provider(fixture, **({"client_metadata_url": CIMD["client_id"],
                                             "redirect": CIMD["redirect_uris"][0]}
                                            if registration == "cimd" else {}))
        async with mcp_session(fixture.mcp_url, auth=provider) as session:
            _, error, booking = await book(session)
    assert not error and booking["status"] == "confirmed"
    state = fixture.controls.state()
    clients = state["authorization"]["clients"]
    assert [client["registration"] for client in clients] == [registration]
    assert state["bookings"][0]["account"] == "taylor"
    assert [entry["auth"] for entry in state["requests"]][:1] == ["missing"]
