"""linking and mcp_clients against the lane's connector fixture (connector_fixture.py) on loopback."""
from pathlib import Path

import pytest

from consumer_agent_testing_tool.connector_fixture import FixtureConfig, serve_fixture, subject_for
from consumer_agent_testing_tool.errors import Unsupported
from consumer_agent_testing_tool.linking import ApiKeyAuth, LinkError, NoCredential, OAuthAuth, link
from consumer_agent_testing_tool.mcp_clients import (
    GENERIC_PROFILE,
    MUSE_TEMPLATE_PROFILE,
    make_client,
)

STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
LOGIN = "CATT_FIXTURE_LOGIN"
STORE = Path(".cache/connector-links")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def oauth(**fields):
    return OAuthAuth(mode="oauth", consent="fixture-auto", test_login_env=LOGIN,
                     scopes=["stays:read", "stays:write"], **fields)


async def test_fixture_auto_links_the_connector_fixture_for_both_clients(workspace, monkeypatch):
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture:
        monkeypatch.setenv(LOGIN, f"taylor:{fixture.credentials['users']['taylor']}")
        credential = await link(oauth(read_only_scopes=["stays:read"], expected_subject=subject_for("taylor")),
                                fixture.mcp_url, store=STORE, http_policy=MUSE_TEMPLATE_PROFILE)
        described, results = credential.describe(), {}
        for profile in (MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE):
            async with make_client(profile, fixture.mcp_url, credential) as client:
                results[profile.name] = await client.call_tool("search_rooms", STAY)
        async with make_client(MUSE_TEMPLATE_PROFILE, fixture.mcp_url, credential) as client:
            fixture.controls.revoke_user("taylor")  # the user revokes access at the provider, mid-run
            revoked = await client.call_tool("search_rooms", STAY)
        authorization = fixture.controls.state()["authorization"]
    assert described["subject"] == subject_for("taylor")  # learned through RFC 7662 introspection
    assert described["scopes"] == ["stays:read", "stays:write"] and described["read_only_supported"] is True
    assert described["protected_resource_metadata"] and described["resource"] == fixture.mcp_url
    assert all(result.error is None for result in results.values()), results
    assert [client["registration"] for client in authorization["clients"]] == ["dcr"]
    assert revoked.error["kind"] == "revoked" and not revoked.uncertain
    assert fixture.credentials["users"]["taylor"] in credential.secrets


async def test_the_connector_fixture_exposes_a_wrong_account_link(workspace, monkeypatch):
    async with serve_fixture(FixtureConfig(auth="oauth")) as fixture:
        monkeypatch.setenv(LOGIN, f"jordan:{fixture.credentials['users']['jordan']}")
        with pytest.raises(LinkError) as raised:
            await link(oauth(expected_subject=subject_for("taylor")), fixture.mcp_url, store=STORE,
                       http_policy=GENERIC_PROFILE)
    assert raised.value.kind == "wrong-account"


@pytest.mark.parametrize("placement, auth, profiles", [
    ("header", {"placement": "custom_header", "name": "X-API-Key"}, (MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE)),
    ("bearer", {"placement": "bearer_header"}, (MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE)),
    ("query", {"placement": "query_param", "name": "X-API-Key"}, (GENERIC_PROFILE,)),
    ("path", {"placement": "url_path_segment"}, (GENERIC_PROFILE,)),
])
async def test_api_key_placements_reach_the_connector_fixture(workspace, monkeypatch, placement, auth, profiles):
    async with serve_fixture(FixtureConfig(auth="api_key", api_key_placement=placement)) as fixture:
        monkeypatch.setenv("CATT_FIXTURE_KEY", fixture.credentials["api_keys"]["taylor"])
        credential = await link(ApiKeyAuth(mode="api_key", env="CATT_FIXTURE_KEY", **auth), fixture.mcp_url,
                                store=STORE, http_policy=GENERIC_PROFILE)
        for profile in profiles:
            async with make_client(profile, fixture.mcp_url, credential) as client:
                assert (await client.call_tool("search_rooms", STAY)).error is None, profile.name
        if MUSE_TEMPLATE_PROFILE not in profiles:
            with pytest.raises(Unsupported, match="header only"):
                make_client(MUSE_TEMPLATE_PROFILE, fixture.mcp_url, credential)


async def test_the_template_flags_redirects_and_session_bound_state(workspace):
    async with serve_fixture(FixtureConfig(variant="needs-redirect")) as fixture:
        template = make_client(MUSE_TEMPLATE_PROFILE, fixture.mcp_url, NoCredential())
        with pytest.raises(Unsupported, match="follows no redirects"):
            await template.connect()
        async with make_client(GENERIC_PROFILE, fixture.mcp_url, NoCredential()) as sdk:
            assert (await sdk.call_tool("search_rooms", STAY)).error is None
    assert "redirect-refused" in [note["kind"] for note in template.observations]
    assert next(note for note in sdk.observations if note["kind"] == "redirect")["followed"] is True

    async with (serve_fixture(FixtureConfig(variant="needs-session")) as fixture,
                make_client(MUSE_TEMPLATE_PROFILE, fixture.mcp_url, NoCredential()) as client):
        rooms = (await client.call_tool("search_rooms", STAY)).result["structuredContent"]["rooms"]
        quote = (await client.call_tool("get_quote", {"room_id": rooms[0]["room_id"], **STAY})).result
        booking = await client.call_tool("create_booking", {
            "quote_id": quote["structuredContent"]["quote_id"], "guest_name": "Taylor Example",
            "guest_email": "taylor@example.com", "idempotency_key": "booking-key-0001"})
    assert booking.result["isError"] is True  # the quote belongs to an earlier invocation's session
    # The tool's text names the MCP session: a labeled hint, not the protocol evidence of needs-session.
    note = next(note for note in client.observations if note["kind"] == "session-hint")
    assert note["tool"] == "create_booking" and note["basis"] == "assumed"
    assert "needs-session" not in [note["kind"] for note in client.observations]
