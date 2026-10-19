"""custom_connector config, documentation bundle, operation names and request classes (no network)."""
import ipaddress
import os

import pytest
from pydantic import ValidationError
from test_ext_custom_connector_support import EXAMPLES, document, extension_config

from consumer_agent_testing_tool.connector_run import EXTENSIONS, make_extensions
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.ext_custom_connector import (
    CustomConnector,
    CustomConnectorConfig,
    DocsBundle,
    Operations,
    classify_operation,
    internal_address,
    path_words,
)

BASE = {"service": "cedar-stays-api", "url": "http://127.0.0.1:8790",
        "credential": {"scheme": "api_key", "env": "CEDAR_STAYS_API_KEY", "placement": "header", "name": "X-Cedar-Key"}}
JOIN = {"result": "/json/quote_id", "argument": "/json/quote_id"}


def config(**changes):
    return CustomConnectorConfig.model_validate({**BASE, **changes})


def refused(match, **changes):
    with pytest.raises(ValidationError, match=match):
        config(**changes)


def test_the_extension_registers_and_the_shipped_scenario_validates():
    loaded = parse_scenario(document(), base=EXAMPLES, path=EXAMPLES / "custom-booking.yaml")
    assert EXTENSIONS["custom_connector"] is CustomConnector
    names = [extension.name for extension in make_extensions(loaded.scenario)]
    assert names == ["clock", "custom_connector"]
    with pytest.raises(LabError, match="extensions.custom_connector"):
        parse_scenario(document(lambda scenario: extension_config(scenario).update(shell=True)), base=EXAMPLES)


def test_config_is_strict_and_names_operations_as_method_and_path():
    assert config().allowed_origins is None and config().limits.timeout_seconds == 30
    refused("Extra inputs", shell="bash")
    refused("METHOD /path", classes={"post /v1/quotes": "read"})
    refused("METHOD /path", classes={"POST v1/quotes": "read"})
    refused("METHOD /path", disclosure={"POST /v1/bookings?x=1": {"seller": {"value": "Cedar Stays"}}})
    refused("one line", title="Cedar\nStays")
    refused("service", service="Cedar Stays")
    refused("relative path", docs="../docs")
    refused("relative path", docs="/etc")
    refused("greater than 0", limits={"timeout_seconds": 0})


def test_urls_follow_the_lane_s_rules():
    refused("https", url="http://api.cedar.example")
    refused("query", url="http://127.0.0.1:8790/?key=1")
    refused("credentials", url="http://user:pass@127.0.0.1:8790")
    refused("outside its allowed_origins", allowed_origins=["http://127.0.0.1:8791"])
    refused("exact origin", allowed_origins=["http://127.0.0.1:8790/v1"])
    assert config(url="https://api.cedar.example/v1", allowed_origins=["https://API.cedar.example:443"]).url


@pytest.mark.parametrize(("credential", "match"), [
    ({"scheme": "api_key", "placement": "header", "name": "X-Cedar-Key"}, "env"),
    ({"scheme": "api_key", "env": "cedar_key", "placement": "header", "name": "X-Cedar-Key"}, "uppercase"),
    ({"scheme": "api_key", "env": "KEY", "placement": "header"}, "name is required"),
    ({"scheme": "api_key", "env": "KEY", "placement": "bearer", "name": "X"}, "not used"),
    ({"scheme": "api_key", "env": "KEY"}, "placement"),
    ({"scheme": "api_key", "env": "KEY", "placement": "header", "name": "Cookie"}, "header name"),
    ({"scheme": "api_key", "env": "KEY", "placement": "header", "name": "Authorization"}, "header name"),
    ({"scheme": "api_key", "env": "KEY", "placement": "query", "name": "api key"}, "query parameter"),
    ({"scheme": "oauth", "env": "TOKEN", "placement": "query", "name": "access_token"}, "Bearer"),
    ({"scheme": "signed_request", "env": "SECRET"}, "declined scheme names none"),
    ({"scheme": "cookie", "placement": "header"}, "apply to api_key and oauth"),
    ({"scheme": "basic"}, "scheme"),
])
def test_credential_names_a_variable_and_a_placement(credential, match):
    refused(match, credential=credential)


def test_supported_and_declined_credentials():
    assert config(credential={"scheme": "oauth", "env": "CEDAR_TOKEN"}).credential.placement == "bearer"
    assert config(credential={"scheme": "multi_secret"}).credential.env is None
    assert config(credential={"scheme": "signed-request"}).credential.scheme == "signed_request"
    assert config(credential={"scheme": "api_key", "env": "K", "placement": "query", "name": "api_key"})


def test_a_write_card_reads_another_response_only_through_a_match_join():
    pick = {"from": "POST /v1/quotes", "pointer": "/json/total_minor"}
    refused("add match", disclosure={"POST /v1/bookings": {"total": pick}})
    refused("operation", disclosure={"POST /v1/bookings": {"total": {**pick, "from": "quotes", "match": JOIN}}})
    refused("lowercase identifier", disclosure={"POST /v1/bookings": {"Total": {**pick, "match": JOIN}}})
    assert config(disclosure={"POST /v1/bookings": {"total": {**pick, "match": JOIN}}})
    # A read, by method or declared, may show the latest response; so may a pick from the request itself.
    assert config(disclosure={"GET /v1/bookings/{booking_id}": {"total": pick}})
    assert config(classes={"POST /v1/search": "read"}, disclosure={"POST /v1/search": {"total": pick}})
    assert config(disclosure={"POST /v1/bookings": {"guest": {"from": "arguments", "pointer": "/json/guest_name"},
                                                    "seller": {"value": "Cedar Stays"}}})


def test_docs_bundle_reads_text_files_once_and_finds_nothing_outside(tmp_path):
    bundle = tmp_path / "docs"
    (bundle / "docs").mkdir(parents=True)
    (bundle / "llms.txt").write_text("# API\n\nBase URL: {{service_url}}\n")
    (bundle / "docs" / "api.md").write_text("Reference")
    (bundle / ".DS_Store").write_bytes(b"\0\1")
    loaded = DocsBundle.load(tmp_path, "docs", service_url="http://127.0.0.1:8790/")
    assert loaded.files == {"docs/api.md": "Reference", "llms.txt": "# API\n\nBase URL: http://127.0.0.1:8790\n"}
    assert [row["path"] for row in loaded.listing()] == ["docs/api.md", "llms.txt"]
    same = DocsBundle.load(tmp_path, "docs", service_url="http://127.0.0.1:9999")
    assert same.sha256 == loaded.sha256  # One bundle, one digest, whatever port fills it.
    find = loaded.find
    assert [find(path, "http://127.0.0.1:8790") for path in (
        "", ".", "llms.txt", "./docs/api.md", "/llms.txt", "http://127.0.0.1:8790/docs/api.md?x#y",
        "http://127.0.0.1:9999/llms.txt", "../docs/llms.txt", "/etc/passwd", "docs")] == [
        "", "", "llms.txt", "docs/api.md", "llms.txt", "docs/api.md", None, None, None, None]
    (bundle / "binary.bin").write_bytes(b"\xff\xfe")
    with pytest.raises(LabError, match="UTF-8"):
        DocsBundle.load(tmp_path, "docs", service_url="http://x")
    (bundle / "binary.bin").unlink()
    os.symlink(tmp_path / "elsewhere.txt", bundle / "link.txt")
    with pytest.raises(LabError, match="symbolic link"):
        DocsBundle.load(tmp_path, "docs", service_url="http://x")
    with pytest.raises(LabError, match="inside the scenario directory"):
        DocsBundle.load(bundle / "docs", "..", service_url="http://x")  # The parent of the scenario's directory.
    (tmp_path / "empty").mkdir()
    with pytest.raises(LabError, match="no documentation"):
        DocsBundle.load(tmp_path, "empty", service_url="http://x")


def test_operations_take_the_openapi_path_template():
    bundle = DocsBundle.load(EXAMPLES, "docs/cedar-stays", service_url="http://127.0.0.1:8790")
    operations = Operations.from_bundle(bundle)
    assert operations.match("GET", "/v1/bookings/b-0001") == ("GET /v1/bookings/{booking_id}",
                                                              "/v1/bookings/{booking_id}", True)
    assert operations.match("HEAD", "/v1/rooms") == ("HEAD /v1/rooms", "/v1/rooms", True)
    assert operations.match("POST", "/v1/bookings/b-0001/cancel")[0] == "POST /v1/bookings/{booking_id}/cancel"
    assert operations.match("POST", "/v1/bookings")[0] == "POST /v1/bookings"
    assert operations.match("DELETE", "/v1/bookings/b-0001") == ("DELETE /v1/bookings/b-0001",
                                                                 "/v1/bookings/b-0001", False)
    assert operations.match("GET", "/v1/bookings/") == ("GET /v1/bookings/", "/v1/bookings/", False)
    assert Operations.from_bundle(None).match("GET", "") == ("GET /", "/", False)


def test_operations_prefer_the_most_literal_template_and_the_server_path(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "openapi.yaml").write_text(
        "openapi: 3.1.0\nservers: [{url: 'https://api.example.com/v2'}]\n"
        "paths:\n  /items/{id}: {get: {}}\n  /items/latest: {get: {}}\n")
    operations = Operations.from_bundle(DocsBundle.load(tmp_path, "docs", service_url="https://api.example.com"))
    assert operations.match("GET", "/v2/items/latest")[0] == "GET /v2/items/latest"
    assert operations.match("GET", "/v2/items/42")[0] == "GET /v2/items/{id}"


@pytest.mark.parametrize(("method", "template", "declared", "expected", "basis"), [
    ("GET", "/v1/bookings/{booking_id}", None, "read", "heuristic"),
    ("HEAD", "/v1/payments", None, "read", "heuristic"),
    ("POST", "/v1/quotes", None, "write", "heuristic"),
    ("PATCH", "/v1/profile", None, "write", "heuristic"),
    ("POST", "/v1/bookings", None, "sensitive-write", "heuristic"),
    ("POST", "/v1/bookings/{booking_id}/cancel", None, "sensitive-write", "heuristic"),
    ("PUT", "/v1/checkoutSessions", None, "sensitive-write", "heuristic"),
    ("POST", "/v1/notes/{payment_id}", None, "write", "heuristic"),
    ("DELETE", "/v1/notes/{note_id}", None, "sensitive-write", "heuristic"),
    ("POST", "/v1/quotes", "read", "read", "declared"),
])
def test_requests_are_classified_by_method_and_path(method, template, declared, expected, basis):
    item = classify_operation(f"{method} {template}", method, template, declared)
    assert (item.tool_class, item.basis) == (expected, basis) and item.tool == f"{method} {template}"
    assert item.reasons


@pytest.mark.parametrize(("method", "template", "expected"), [
    ("POST", "/v1/emails/send", "sensitive-write"), ("POST", "/v1/messages", "sensitive-write"),
    ("POST", "/v1/posts", "sensitive-write"), ("POST", "/v1/shares", "sensitive-write"),
    ("POST", "/v1/invitations", "sensitive-write"), ("POST", "/v1/threads/{thread_id}/replies", "sensitive-write"),
    ("PUT", "/v1/plans/{plan_id}/upgrade", "sensitive-write"), ("POST", "/v1/notifications", "sensitive-write"),
    ("POST", "/v1/tickets", "write"), ("PATCH", "/v1/drafts/{draft_id}", "write"), ("GET", "/v1/messages", "read")])
def test_paths_that_send_or_publish_are_sensitive_writes_as_in_the_mcp_lexicon(method, template, expected):
    # As permissions.classify_tool reads send_email, send_message and create_post (guidelines 3.2: 'Sensitive
    # write (make a purchase, send an email, etc.)'); the method still decides reads.
    assert classify_operation(f"{method} {template}", method, template).tool_class == expected


def test_path_words_ignore_parameters_and_fold_plurals():
    assert path_words("/v1/bookings/{booking_id}/cancel") == {"v1", "booking", "cancel"}
    assert path_words("/v2/paymentIntents") == {"v2", "payment", "intent"}


@pytest.mark.parametrize(("address", "internal"), [
    ("10.0.0.5", True), ("127.0.0.1", True), ("169.254.169.254", True), ("100.64.0.1", True),
    ("192.168.1.1", True), ("0.0.0.0", True), ("224.0.0.1", True), ("::1", True), ("fe80::1", True),
    ("::ffff:10.0.0.1", True), ("fd00::1", True), ("93.184.216.34", False), ("2606:2800:220:1::1", False)])
def test_internal_addresses_are_everything_but_global_unicast(address, internal):
    assert internal_address(ipaddress.ip_address(address)) is internal
