"""linking: auth models, no-auth and API-key credentials (placements, redaction, logging)."""
import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import TypeAdapter, ValidationError
from test_mcp_clients_support import FakeMcp

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Redactor
from consumer_agent_testing_tool.linking import (
    ApiKeyAuth,
    Credential,
    LinkError,
    NoAuth,
    NoCredential,
    TargetAuth,
    link,
)
from consumer_agent_testing_tool.mcp_clients import (
    GENERIC_PROFILE,
    MUSE_TEMPLATE_PROFILE,
    make_client,
)

KEY = "catt-test-key-0123456789"
AUTH = TypeAdapter(TargetAuth)


def test_target_auth_is_a_strict_discriminated_union():
    assert isinstance(AUTH.validate_python({"mode": "none"}), NoAuth)
    assert AUTH.validate_python({"mode": "api_key", "env": "CATT_KEY"}).placement == "bearer_header"
    oauth = AUTH.validate_python({"mode": "oauth"})
    assert (oauth.registration, oauth.consent, oauth.scopes) == ("dcr", "interactive", [])
    for invalid in (
        {"mode": "api_key", "env": "CATT_KEY", "key": "inline-secret"},          # no credentials in scenarios
        {"mode": "api_key", "env": "sk-live-not-a-name"},
        {"mode": "api_key", "env": "CATT_KEY", "placement": "custom_header"},    # needs a header name
        {"mode": "api_key", "env": "CATT_KEY", "placement": "custom_header", "name": "Mcp-Session-Id"},
        {"mode": "api_key", "env": "CATT_KEY", "placement": "custom_header", "name": "Cookie"},
        {"mode": "api_key", "env": "CATT_KEY", "placement": "query_param", "name": "a&b"},
        {"mode": "api_key", "env": "CATT_KEY", "name": "X-Key"},                 # bearer takes no name
        {"mode": "oauth", "registration": "preregistered"},
        {"mode": "oauth", "client_id_env": "CATT_ID"},
        {"mode": "oauth", "registration": "cimd"},
        {"mode": "oauth", "registration": "cimd", "client_metadata_url": "http://client.example.test/c.json"},
        {"mode": "oauth", "registration": "cimd", "client_metadata_url": "https://client.example.test/"},
        {"mode": "oauth", "consent": "fixture-auto"},
        {"mode": "oauth", "test_login_env": "CATT_LOGIN"},
        {"mode": "oauth", "scopes": ["read", "read"]},
        {"mode": "oauth", "scopes": ["two words"]},
        {"mode": "oauth", "expected_subject": " "},
        {"mode": "oauth", "redirect_port": 80},
        {"mode": "basic"},
    ):
        with pytest.raises(ValidationError):
            AUTH.validate_python(invalid)


def test_no_auth_is_immediate_and_inert():
    credential = NoCredential()
    assert isinstance(credential, Credential)
    assert credential.apply("https://mcp.example.test/mcp", {"A": "1"}) == ("https://mcp.example.test/mcp", {"A": "1"})
    assert credential.describe() == {"mode": "none", "placement": None, "scopes": [], "subject": None,
                                     "expires_at": None}
    assert credential.secrets == []


async def test_link_none_validates_the_endpoint(tmp_path):
    assert isinstance(await link({"mode": "none"}, "https://mcp.example.test/mcp", store=tmp_path,
                                 http_policy=GENERIC_PROFILE), NoCredential)
    for endpoint, message in (("http://mcp.example.test/mcp", "HTTPS"),
                              ("https://u:p@mcp.example.test/mcp", "user info"),
                              ("mcp.example.test/mcp", "absolute"),
                              ("https://mcp.example.test/{api_key}/mcp", "placeholder")):
        with pytest.raises(LabError, match=message):
            await link(NoAuth(), endpoint, store=tmp_path, http_policy=GENERIC_PROFILE)


async def api_key(monkeypatch, tmp_path, value=KEY, endpoint="https://mcp.example.test/mcp", **auth):
    monkeypatch.setenv("CATT_KEY", value)
    return await link(ApiKeyAuth(mode="api_key", env="CATT_KEY", **auth), endpoint, store=tmp_path,
                      http_policy=GENERIC_PROFILE)


@pytest.mark.parametrize("value, kind", [("", "missing"), ("k3y9", "rejected"), ("true-ish\nkey", "rejected"),
                                         ("has space in key", "rejected")])
async def test_api_keys_must_be_present_and_redactable(monkeypatch, tmp_path, value, kind):
    with pytest.raises(LinkError) as raised:
        await api_key(monkeypatch, tmp_path, value)
    assert raised.value.kind == kind and raised.value.category == "authentication"
    assert value.strip() == "" or value not in str(raised.value)


async def test_api_keys_must_not_reuse_an_inference_credential(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with pytest.raises(LinkError, match="inference-provider") as raised:
        await api_key(monkeypatch, tmp_path)
    assert raised.value.kind == "rejected"


async def test_api_key_credentials_are_registered_and_described_without_the_key(monkeypatch, tmp_path):
    redactor = Redactor()
    monkeypatch.setenv("CATT_KEY", "catt/key+with=specials")
    credential = await link(ApiKeyAuth(mode="api_key", env="CATT_KEY", placement="query_param", name="api_key"),
                            "https://mcp.example.test/mcp", store=tmp_path, http_policy=GENERIC_PROFILE,
                            redactor=redactor)
    assert credential.describe() == {"mode": "api_key", "placement": "query_param", "name": "api_key",
                                     "env": "CATT_KEY", "scopes": [], "subject": None, "expires_at": None}
    assert set(credential.secrets) == {"catt/key+with=specials", "catt%2Fkey%2Bwith%3Dspecials"}
    url, _ = credential.apply("https://mcp.example.test/mcp?tenant=a", {})
    assert parse_qs(urlsplit(url).query) == {"tenant": ["a"], "api_key": ["catt/key+with=specials"]}
    assert "catt" not in redactor.clean({"url": url})["url"].split("api_key=")[1]
    # Never for another origin, such as a redirect target.
    assert credential.apply("https://elsewhere.example.test/mcp", {}) == ("https://elsewhere.example.test/mcp", {})
    assert credential.apply("http://mcp.example.test/mcp", {}) == ("http://mcp.example.test/mcp", {})


@pytest.mark.parametrize("endpoint, expected", [
    ("https://mcp.example.test/mcp", "/mcp/" + KEY), ("https://mcp.example.test/{api_key}/mcp", f"/{KEY}/mcp")])
async def test_path_keys_fill_the_placeholder_or_the_last_segment(monkeypatch, tmp_path, endpoint, expected):
    credential = await api_key(monkeypatch, tmp_path, endpoint=endpoint, placement="url_path_segment")
    assert urlsplit(credential.apply(endpoint, {})[0]).path == expected
    encoded = endpoint.replace("{api_key}", "%7Bapi_key%7D")  # how httpx2 spells the placeholder
    assert urlsplit(credential.apply(encoded, {})[0]).path == expected
    with pytest.raises(LabError, match="whole path segment"):
        await api_key(monkeypatch, tmp_path, endpoint="https://mcp.example.test/x{api_key}", placement="url_path_segment")


def accepts(placement):
    def check(handler):
        parts = urlsplit(handler.path)
        return {"bearer_header": handler.headers.get("Authorization") == f"Bearer {KEY}",
                "custom_header": handler.headers.get("X-API-Key") == KEY,
                "query_param": parse_qs(parts.query).get("api_key") == [KEY],
                "url_path_segment": parts.path == f"/mcp/{KEY}"}[placement]
    return check


@pytest.mark.parametrize("placement, name, profiles", [
    ("bearer_header", None, (MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE)),
    ("custom_header", "X-API-Key", (MUSE_TEMPLATE_PROFILE, GENERIC_PROFILE)),
    ("query_param", "api_key", (GENERIC_PROFILE,)),
    ("url_path_segment", None, (GENERIC_PROFILE,)),
])
async def test_each_key_placement_reaches_the_connector(monkeypatch, tmp_path, placement, name, profiles):
    with FakeMcp(require=accepts(placement)) as fake:
        credential = await api_key(monkeypatch, tmp_path, endpoint=fake.url, placement=placement, name=name)
        for profile in profiles:
            async with make_client(profile, fake.url, credential) as client:
                result = await client.call_tool("search_rooms", {"text": "x"})
            assert result.error is None, (profile.name, result.error)
            assert all(KEY not in str(note) for note in client.observations)
    assert len(fake.calls) == len(profiles)


async def test_a_rejected_key_is_reported_not_retried(monkeypatch, tmp_path):
    with FakeMcp(require=lambda handler: False) as fake:
        credential = await api_key(monkeypatch, tmp_path, endpoint=fake.url)
        with pytest.raises(LinkError) as raised:
            async with make_client(MUSE_TEMPLATE_PROFILE, fake.url, credential):
                pass
    assert raised.value.kind == "rejected" and KEY not in str(raised.value)
    assert fake.methods() == [("POST", "initialize")]  # a static key cannot refresh, so nothing is resent


async def test_url_credentials_never_reach_http_logs(monkeypatch, tmp_path, caplog):
    with FakeMcp(require=accepts("query_param")) as fake:
        credential = await api_key(monkeypatch, tmp_path, endpoint=fake.url, placement="query_param", name="api_key")
        with caplog.at_level(logging.INFO, logger="httpx2"):
            async with make_client(GENERIC_PROFILE, fake.url, credential) as client:
                assert (await client.call_tool("search_rooms", {})).error is None
    requests = [record.getMessage() for record in caplog.records if record.name == "httpx2"]
    assert requests and all(KEY not in message for message in requests)
    assert any("api_key=[REDACTED]" in message for message in requests)


async def test_connector_keys_must_not_reuse_the_selected_model_profiles_key(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setenv("CATT_KEY", KEY)
    monkeypatch.setenv("MODEL_GATEWAY_TOKEN", KEY)  # A custom model auth variable no provider list names.
    model = SimpleNamespace(auth=SimpleNamespace(env="MODEL_GATEWAY_TOKEN"))
    with pytest.raises(LinkError, match="must not reuse an inference-provider credential") as raised:
        await link(ApiKeyAuth(mode="api_key", env="CATT_KEY"), "https://mcp.example.test/mcp", store=tmp_path,
                   http_policy=GENERIC_PROFILE, model=model)
    assert raised.value.kind == "rejected"
    # Without the model profile the same key links: separation needs the selected profile.
    assert (await link(ApiKeyAuth(mode="api_key", env="CATT_KEY"), "https://mcp.example.test/mcp", store=tmp_path,
                       http_policy=GENERIC_PROFILE)).secrets[0] == KEY


async def test_credentials_are_scrubbed_from_any_logger_and_from_exception_text(monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("CATT_KEY", KEY)
    await link(ApiKeyAuth(mode="api_key", env="CATT_KEY", placement="query_param", name="api_key"),
               "https://mcp.example.test/mcp", store=tmp_path, http_policy=GENERIC_PROFILE)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    logger = logging.getLogger("mcp.client.streamable_http.catt-test")  # Not an httpx logger.
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.debug("GET https://mcp.example.test/mcp?api_key=%s failed", KEY)
        try:
            raise RuntimeError(f"Client error '405' for url 'https://mcp.example.test/mcp?api_key={KEY}'")
        except RuntimeError:
            logger.exception("GET stream failed")
        logger.warning("stack", stack_info=True)
    finally:
        logger.removeHandler(handler)
    text = stream.getvalue()
    assert KEY not in text and text.count("[REDACTED]") >= 2 and "RuntimeError" in text
