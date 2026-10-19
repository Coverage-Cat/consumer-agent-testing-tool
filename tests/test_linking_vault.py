"""linking.SurrogateVault: the agent sees surrogates; the host swaps real secrets in on approved hosts only."""
import pytest

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Redactor
from consumer_agent_testing_tool.linking import SURROGATE, SurrogateVault

SECRET = "sk_test_51Hostile0123456789"


def test_header_surrogates_are_swapped_for_registered_hosts_only():
    vault = SurrogateVault()
    surrogate = vault.issue(SECRET, hosts=["api.example.test"], placement="bearer_header")
    assert SURROGATE.fullmatch(surrogate) and SECRET not in surrogate
    url, headers, body = vault.swap("GET", "https://api.example.test/v1/rooms",
                                    {"Authorization": f"Bearer {surrogate}"}, None)
    assert url == "https://api.example.test/v1/rooms" and headers == {"Authorization": f"Bearer {SECRET}"}
    assert body is None and vault.swaps == [{"surrogate": surrogate, "method": "GET", "host": "api.example.test",
                                             "placement": "header", "swapped": True}]
    assert vault.secrets == [SECRET] and SECRET in Redactor(vault.secrets).secrets
    for url in ("https://evil.example.test/steal", "https://api.example.test.evil.test/", "https://api.example.test:8443/"):
        with pytest.raises(LabError, match="unregistered host") as raised:
            vault.swap("GET", url, {"Authorization": f"Bearer {surrogate}"}, None)
        assert raised.value.code == 1 and raised.value.category == "policy/approval"


def test_surrogates_outside_their_placement_stop_the_request():
    vault = SurrogateVault()
    surrogate = vault.issue(SECRET, hosts=["api.example.test"], placement="header")
    for url, headers, body in (
            ("https://api.example.test/notes", {}, f'{{"note": "{surrogate}"}}'.encode()),   # exfiltration via body
            (f"https://api.example.test/notes?q={surrogate}", {}, None),
            (f"https://api.example.test/{surrogate}", {}, None),
            (f"https://api.example.test/#{surrogate}", {}, None),
            ("https://api.example.test/", {surrogate: "x"}, None)):
        with pytest.raises(LabError, match="outside its header placement"):
            vault.swap("POST", url, headers, body)


def test_query_path_and_body_placements_are_encoded_for_their_location():
    vault = SurrogateVault()
    query = vault.issue("key/with+specials=1", hosts=["127.0.0.1:8123"], placement="query_param")
    path = vault.issue("path-key-0123", hosts=["api.example.test"], placement="url_path_segment")
    body = vault.issue("body-key-0123", hosts=["api.example.test"], placement="body")
    assert vault.swap("GET", f"http://127.0.0.1:8123/v1?key={query}&x=1", {}, None)[0] == \
        "http://127.0.0.1:8123/v1?key=key%2Fwith%2Bspecials%3D1&x=1"
    assert vault.swap("GET", f"https://api.example.test/hooks/{path}/run", {}, None)[0] == \
        "https://api.example.test/hooks/path-key-0123/run"
    assert vault.swap("POST", "https://api.example.test/token", {"Content-Type": "application/json"},
                      f'{{"key": "{body}"}}'.encode())[2] == b'{"key": "body-key-0123"}'
    with pytest.raises(LabError, match="JSON- and form-safe"):
        vault.issue('quote"d-secret', hosts=["api.example.test"], placement="body")


def test_a_query_surrogate_is_swapped_in_parameter_values_only():
    vault = SurrogateVault()
    query = vault.issue(SECRET, hosts=["127.0.0.1:8123"], placement="query_param")
    for url in (f"http://127.0.0.1:8123/v1/me?{query}=1&api_key={query}", f"http://127.0.0.1:8123/v1/me?{query}",
                f"http://127.0.0.1:8123/v1/me?x{query}y=2&api_key={query}"):
        with pytest.raises(LabError, match="query parameter name") as raised:
            vault.swap("GET", url, {}, None)
        assert raised.value.code == 1 and raised.value.category == "policy/approval"
    # Each pair stays as written; only the values holding the surrogate change.
    assert vault.swap("GET", f"http://127.0.0.1:8123/v1/me?q=a+b%20c&api_key={query}&flag", {}, None)[0] == \
        f"http://127.0.0.1:8123/v1/me?q=a+b%20c&api_key={SECRET}&flag"


def test_redirect_hops_are_never_swapped():
    vault = SurrogateVault()
    surrogate = vault.issue(SECRET, hosts=["api.example.test"], placement="header")
    request = ("GET", "https://api.example.test/moved", {"Authorization": f"Bearer {surrogate}"}, None)
    assert vault.swap(*request, redirect=True) == request[1:]
    assert vault.swaps[-1]["swapped"] is False
    with pytest.raises(LabError, match="unregistered host"):  # a redirect elsewhere still may not carry it
        vault.swap("GET", "https://elsewhere.example.test/", request[2], None, redirect=True)


def test_cleartext_and_host_header_tricks_are_refused():
    vault = SurrogateVault()
    surrogate = vault.issue(SECRET, hosts=["api.example.test", "api.example.test:80"], placement="header")
    with pytest.raises(LabError, match="cleartext"):
        vault.swap("GET", "http://api.example.test/", {"Authorization": surrogate}, None)
    with pytest.raises(LabError, match="Host header"):
        vault.swap("GET", "https://api.example.test/", {"Host": "evil.example.test", "Authorization": surrogate}, None)
    loopback = vault.issue(SECRET, hosts=["127.0.0.1:9000"], placement="header")
    assert vault.swap("GET", "http://127.0.0.1:9000/", {"X-Key": loopback}, None)[1] == {"X-Key": SECRET}


def test_requests_without_known_surrogates_pass_unchanged():
    vault = SurrogateVault()
    vault.issue(SECRET, hosts=["api.example.test"], placement="header")
    foreign = "catt-surr-" + "A" * 32  # not issued by this vault: carries no secret
    request = ("https://other.example.test/", {"Authorization": foreign}, b"{}")
    assert vault.swap("GET", *request) == request and vault.swaps == []


@pytest.mark.parametrize("secret, hosts, placement, message", [
    ("abc", ["api.example.test"], "header", "real secret"),
    ("two words!", ["api.example.test"], "header", "real secret"),
    (SECRET, [], "header", "at least one host"), (SECRET, ["https://api.example.test/x"], "header", "not URLs"),
    (SECRET, ["api.example.test:http"], "header", "Invalid surrogate host"),
    (SECRET, ["api.example.test"], "cookie", "placement")])
def test_issue_validates_its_inputs(secret, hosts, placement, message):
    with pytest.raises(LabError, match=message):
        SurrogateVault().issue(secret, hosts=hosts, placement=placement)


def test_a_surrogate_in_the_request_host_is_refused():
    vault = SurrogateVault()
    surrogate = vault.issue(SECRET, hosts=["api.example.test"], placement="header")
    for url in (f"https://{surrogate.lower()}.evil.example/", f"https://{surrogate}.api.example.test/",
                f"https://user@{surrogate.upper()}.evil.example:8443/x"):
        with pytest.raises(LabError, match="appeared in the request host") as raised:
            vault.swap("GET", url, {}, None)
        assert raised.value.category == "policy/approval"
    assert vault.swaps == []
