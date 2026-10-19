"""Connector fixture brands: two instances as two hotels in one test (names, rooms, URL prefix)."""
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_connector_fixture_auth import authorize, link, pkce, register, sdk_provider
from test_connector_fixture_process import fixture_process, stop
from test_connector_fixture_tools import (
    GUEST,
    STAY,
    book,
    call,
    has,
    mcp_session,
    raw_call,
    tool_map,
)

from consumer_agent_testing_tool.connector_fixture import (
    ACCOUNT_HINT,
    DESCRIPTIONS,
    STEERING,
    STEERING_NOTICE,
    Brand,
    FixtureConfig,
    serve_fixture,
    subject_for,
)
from consumer_agent_testing_tool.linking import OAuthAuth
from consumer_agent_testing_tool.linking import link as host_link
from consumer_agent_testing_tool.mcp_clients import GENERIC_PROFILE, make_client

DATA = Path(__file__).parent / "data" / "connector"
ROOMS = {"rm-garden-queen": "Harbor Queen", "rm-cedar-king": "Harbor King",
         "rm-family-bunk": "Harbor Family Room", "rm-premier-suite": "Harbor Grand Suite"}
HARBOR = Brand(name="Harbor Inns", room_names=ROOMS, path_prefix="/harbor-inns")


def harbor(text):
    """Cedar Stays text as the Harbor Inns brand writes it."""
    return text.replace("Cedar Premier Suite", "Harbor Grand Suite").replace("Cedar Stays",
                                                                            "Harbor Inns")


def origin(fixture):
    return f"http://127.0.0.1:{fixture.port}"


async def test_two_brands_serve_two_hotels_side_by_side():
    cedar_config = FixtureConfig(brand=Brand(path_prefix="/cedar-stays"))
    async with serve_fixture(cedar_config) as cedar, \
            serve_fixture(FixtureConfig(brand=HARBOR)) as inns:
        assert inns.base_url == origin(inns) + "/harbor-inns"
        assert inns.mcp_url == origin(inns) + "/harbor-inns/mcp"
        assert inns.oracle_url == origin(inns) + "/harbor-inns/__oracle/state"
        assert cedar.mcp_url == origin(cedar) + "/cedar-stays/mcp"
        async with mcp_session(inns.mcp_url) as session:
            init = session.init_result
            listed = (await session.list_tools()).tools
            error, search = await call(session, "search_rooms", **STAY)
            quote, error, booking = await book(session)
        async with mcp_session(cedar.mcp_url) as session:
            cedar_init = session.init_result
            cedar_quote, _, _ = await book(session, room_id="rm-cedar-king")
        async with httpx.AsyncClient(trust_env=False) as http:
            unprefixed = await http.get(origin(inns) + "/__oracle/state")
            state = (await http.get(inns.oracle_url)).json()
            cedar_state = (await http.get(cedar.oracle_url)).json()
    assert unprefixed.status_code == 404  # Everything the brand serves lives under its prefix.
    assert (init.server_info.name, init.server_info.title) == ("harbor-inns", "Harbor Inns")
    assert (cedar_init.server_info.name, cedar_init.server_info.title) == ("cedar-stays",
                                                                          "Cedar Stays")
    tools = tool_map(listed)
    assert tools["search_rooms"].description == harbor(DESCRIPTIONS["search_rooms"][1])
    visible = json.dumps([[tool.title, tool.description, tool.input_schema, tool.output_schema]
                          for tool in listed]) + init.instructions
    assert "Cedar" not in visible and "Harbor Inns" in init.instructions
    assert [room["name"] for room in search["rooms"]] == list(ROOMS.values())
    assert not error and has(quote, {"seller": "Harbor Inns", "room_name": "Harbor Queen",
                                     "total_minor": 44836})
    assert booking["room_name"] == "Harbor Queen"
    assert has(cedar_quote, {"seller": "Cedar Stays", "room_name": "Cedar King"})
    assert state["fixture"] == "harbor-inns" and state["brand"] == {
        "name": "Harbor Inns", "server": "harbor-inns", "path_prefix": "/harbor-inns",
        "rooms": ROOMS}
    # Each hotel keeps its own books, and each brand its own subjects for the same person.
    assert [b["room_id"] for b in state["bookings"]] == ["rm-garden-queen"]
    assert [b["room_id"] for b in cedar_state["bookings"]] == ["rm-cedar-king"]
    assert state["accounts"][0]["subject"] == subject_for("taylor", "harbor-inns")
    assert cedar_state["accounts"][0]["subject"] == subject_for("taylor")
    assert subject_for("taylor", "harbor-inns") != subject_for("taylor")
    assert cedar_state["brand"]["rooms"]["rm-premier-suite"] == "Cedar Premier Suite"


async def test_a_brand_rewords_steering_notices_and_the_transfer_tool():
    steering = FixtureConfig(variant="steering-description", brand=HARBOR)
    async with serve_fixture(steering) as fixture, mcp_session(fixture.mcp_url) as session:
        init = session.init_result
        described = tool_map((await session.list_tools()).tools)["search_rooms"].description
        error, search = await call(session, "search_rooms", **STAY)
    assert described.endswith(harbor(STEERING)) and init.instructions.endswith(harbor(STEERING))
    assert not error and search["notice"] == harbor(STEERING_NOTICE)
    money = FixtureConfig(variant="money-movement", brand=HARBOR)
    async with serve_fixture(money) as fixture, mcp_session(fixture.mcp_url) as session:
        transfer = tool_map((await session.list_tools()).tools)["transfer_funds"]
    assert transfer.description == harbor(DESCRIPTIONS["transfer_funds"][1])
    account = transfer.input_schema["properties"]["from_account"]
    assert account["description"] == ACCOUNT_HINT.replace("cedar-wallet", "harbor-inns-wallet")
    assert account["pattern"] == "^[a-z0-9-]{3,40}$"


async def test_a_brand_renames_only_the_rooms_it_names():
    config = FixtureConfig(brand=Brand(name="Cedar Stays", room_names={"rm-family-bunk": "Loft"}))
    async with serve_fixture(config) as fixture, mcp_session(fixture.mcp_url) as session:
        init = session.init_result
        error, search = await call(session, "search_rooms", **(STAY | {"adults": 4}))
    assert init.server_info.name == "cedar-stays" and fixture.mcp_url == origin(fixture) + "/mcp"
    assert not error and [room["name"] for room in search["rooms"]] == ["Loft",
                                                                         "Cedar Premier Suite"]


async def test_a_prefixed_brand_links_with_path_aware_oauth_discovery():
    async with serve_fixture(FixtureConfig(auth="oauth", brand=HARBOR)) as fixture:
        root = origin(fixture)
        prm_url = root + "/.well-known/oauth-protected-resource/harbor-inns/mcp"
        async with httpx.AsyncClient(trust_env=False) as http:
            prm = (await http.get(prm_url)).json()
            metadata = (await http.get(root + "/.well-known/oauth-authorization-server/harbor-inns")
                        ).json()
            unprefixed = await http.get(root + "/.well-known/oauth-authorization-server")
            missing, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY)
            client = await register(http, fixture)
            started = await authorize(http, fixture, client["client_id"], pkce()[1])
            page = await http.get(started.headers["location"])
            _, tokens = await link(http, fixture)
        provider = sdk_provider(fixture)  # The official SDK client discovers everything itself.
        async with mcp_session(fixture.mcp_url, auth=provider) as session:
            _, error, booking = await book(session)
        state = fixture.controls.state()
    assert prm == {"resource": fixture.mcp_url, "authorization_servers": [fixture.base_url],
                   "scopes_supported": ["stays:read", "stays:write"],
                   "bearer_methods_supported": ["header"], "resource_name": "Harbor Inns"}
    assert has(metadata, {"issuer": fixture.base_url,
                          "authorization_endpoint": fixture.base_url + "/authorize",
                          "token_endpoint": fixture.base_url + "/token",
                          "registration_endpoint": fixture.base_url + "/register",
                          "revocation_endpoint": fixture.base_url + "/revoke",
                          "introspection_endpoint": fixture.base_url + "/introspect"})
    assert unprefixed.status_code == 404  # RFC 8414: a path issuer's document carries its path.
    assert f'resource_metadata="{prm_url}"' in missing.headers["www-authenticate"]
    assert started.headers["location"].startswith(fixture.base_url + "/login?txn=")
    assert "Sign in to Harbor Inns" in page.text and 'action="/harbor-inns/login"' in page.text
    assert tokens["access_token"].startswith("csat_")
    assert not error and booking["status"] == "confirmed"
    assert has(state["bookings"][0], {"account": "taylor",
                                      "subject": subject_for("taylor", "harbor-inns")})


async def test_the_host_links_a_prefixed_brand_with_its_test_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with serve_fixture(FixtureConfig(auth="oauth", brand=HARBOR)) as fixture:
        monkeypatch.setenv("CATT_FIXTURE_LOGIN", f"taylor:{fixture.credentials['users']['taylor']}")
        auth = OAuthAuth(mode="oauth", consent="fixture-auto", test_login_env="CATT_FIXTURE_LOGIN",
                         scopes=["stays:read", "stays:write"],
                         expected_subject=subject_for("taylor", "harbor-inns"))
        credential = await host_link(auth, fixture.mcp_url, store=Path(".cache/connector-links"),
                                     http_policy=GENERIC_PROFILE)
        async with make_client(GENERIC_PROFILE, fixture.mcp_url, credential) as client:
            result = await client.call_tool("search_rooms", STAY)
        described = credential.describe()
    assert result.error is None
    assert described["issuer"] == fixture.base_url and described["resource"] == fixture.mcp_url
    assert described["subject"] == subject_for("taylor", "harbor-inns")


async def test_prefixed_brands_keep_path_keys_and_redirects_under_the_prefix():
    config = FixtureConfig(auth="api_key", api_key_placement="path",
                           brand=Brand(path_prefix="/cedar"))
    async with serve_fixture(config) as fixture:
        key = fixture.credentials["api_keys"]["taylor"]
        denied, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY)
        accepted, result = await raw_call(f"{fixture.mcp_url}/{key}", "search_rooms", STAY)
    assert denied.status_code == 401 and denied.headers["www-authenticate"] == \
        'ApiKey realm="cedar-stays", in="path"'
    assert accepted.status_code == 200 and result["structuredContent"]["rooms"]
    state = fixture.controls.state()
    assert key not in json.dumps(state)
    assert {entry["path"] for entry in state["requests"]} == {"/cedar/mcp", "/cedar/mcp/{api_key}"}
    redirecting = FixtureConfig(variant="needs-redirect", brand=Brand(path_prefix="/cedar"))
    async with serve_fixture(redirecting) as fixture:
        refused, _ = await raw_call(fixture.mcp_url, "search_rooms", STAY)
        async with mcp_session(fixture.mcp_url) as session:
            error, search = await call(session, "search_rooms", **STAY)
    assert refused.status_code == 307 and refused.headers["location"] == fixture.mcp_url + "/"
    assert not error and search["rooms"]


async def test_a_branded_checkout_names_its_merchant_and_declines_another_brands_token():
    config = FixtureConfig(variant="checkout", challenge_code="975310", brand=HARBOR)
    token = {"token": "tok_test_harbor_0001", "amount_minor": 44836, "currency": "USD",
             "expires_at": "2026-11-20T18:00:00Z"}
    async with serve_fixture(config) as fixture, mcp_session(fixture.mcp_url) as session:
        fixture.controls.register_payment_token(**token, merchant="Cedar Stays")
        fixture.controls.register_payment_token(**(token | {"token": "tok_test_harbor_0002"}),
                                                merchant="Harbor Inns")
        error, quote = await call(session, "get_quote", room_id="rm-garden-queen", **STAY)
        assert quote["add_on"]["name"] == "Harbor Inns Club"
        error, foreign = await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST,
                                    payment_token="tok_test_harbor_0001", decline_add_on=True)
        assert error and foreign["reason"] == "wrong_merchant"
        assert "Cedar Stays, not Harbor Inns" in foreign["message"]
        error, booking = await call(session, "create_booking", quote_id=quote["quote_id"], **GUEST,
                                    payment_token="tok_test_harbor_0002", decline_add_on=True)
    assert not error and booking["status"] == "confirmed"
    assert fixture.controls.announcement()["wallet"]["merchant"] == "Harbor Inns"
    assert fixture.controls.state()["wallet"]["merchant"] == "Harbor Inns"


async def test_subprocess_serves_a_branded_checkout_from_a_config_file():
    async with fixture_process("--config", str(DATA / "fixture-brand-checkout.yaml"),
                               "--port", "0") as (process, info):
        assert (info["server"], info["variant"]) == ("pine-lodge", "checkout")
        assert info["mcp_url"] == info["base_url"] + "/mcp"
        assert info["base_url"] == f"http://127.0.0.1:{info['port']}/pine-lodge"
        assert info["brand"]["rooms"]["rm-cedar-king"] == "Pine King"
        assert info["wallet"] == {"register_url": info["base_url"] + "/__wallet/register",
                                  "merchant": "Pine Lodge"}
        assert "test_credentials" not in info  # The verification code came from the file.
        async with httpx.AsyncClient(trust_env=False) as http:
            registered = await http.post(info["wallet"]["register_url"], json={
                "token": "tok_test_pine_00042", "merchant": "Pine Lodge", "amount_minor": 51556,
                "currency": "USD", "expires_at": "2026-11-20T18:00:00Z"})
            assert registered.status_code == 201
        async with mcp_session(info["mcp_url"]) as session:
            error, quote = await call(session, "get_quote", room_id="rm-cedar-king", **STAY)
            assert has(quote, {"room_name": "Pine King", "seller": "Pine Lodge"})
            error, pending = await call(session, "create_booking", quote_id=quote["quote_id"],
                                        **GUEST, payment_token="tok_test_pine_00042",
                                        decline_add_on=True)
            assert pending["status"] == "requires_action"
            error, confirmed = await call(session, "confirm_payment",
                                          challenge_id=pending["challenge_id"], code="975310")
            assert not error and confirmed["receipt"]["amount_minor"] == 51556
        code, _, errors = await stop(process)
    assert code == 0 and b"Traceback" not in errors
    async with fixture_process("--port", "0", "--variant", "checkout") as (process, info):
        generated = info["test_credentials"]["challenge_code"]  # For the launcher's scripted user.
        assert len(generated) == 6 and generated.isdigit()
        await stop(process)


@pytest.mark.parametrize("brand,problem", [
    ({"room_names": {"rm-penthouse": "Penthouse"}}, "unknown rooms"),
    ({"room_names": {"rm-garden-queen": "Cedar King"}}, "distinct"),
    ({"room_names": {"rm-garden-queen": ""}}, "pattern"),
    ({"path_prefix": "harbor"}, "pattern"),
    ({"path_prefix": "/harbor/"}, "pattern"),
    ({"path_prefix": "/.well-known"}, "pattern"),
    ({"name": ""}, "pattern"),
    ({"name": "Harbor <Inns>"}, "pattern"),
    ({"slogan": "Stay by the sea"}, "Extra inputs"),
])
def test_brand_validation_fails_closed(brand, problem):
    with pytest.raises(ValidationError, match=problem):
        FixtureConfig.model_validate({"brand": brand})
