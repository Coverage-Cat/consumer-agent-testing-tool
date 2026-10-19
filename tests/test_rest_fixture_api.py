"""The loopback Cedar Stays REST service (rest_fixture.py): endpoints, auth schemes, errors and oracle."""
import json

import httpx
import pytest

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.rest_fixture import RestConfig, main, run_rest, signed_headers

KEY = "cedar_rest_key_0123456789abcdef"
QUOTE = {"room_id": "rm-garden-queen", "check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
ROOMS = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}


def client(running):
    return httpx.Client(base_url=running.base_url, trust_env=False, timeout=10)


def test_rest_fixture_books_through_its_documented_endpoints():
    with run_rest(RestConfig(api_keys={KEY: "taylor"})) as running, client(running) as http:
        key = {"X-Cedar-Key": KEY}
        assert http.get("/v1/me", headers=key).json() == {"username": "taylor", "name": "Taylor Example",
                                                          "email": "taylor@example.com"}
        rooms = http.get("/v1/rooms", params=ROOMS, headers=key).json()
        assert rooms["rooms"][0]["room_id"] == "rm-garden-queen" and rooms["next_page_token"] is None
        quote = http.post("/v1/quotes", json=QUOTE, headers=key)
        assert quote.status_code == 201 and quote.json()["total_minor"] == 44836
        assert quote.json()["seller"] == "Cedar Stays"
        booking = {"quote_id": quote.json()["quote_id"], "guest_name": "Taylor Example"}
        first = http.post("/v1/bookings", json=booking, headers={**key, "Idempotency-Key": "booking-try-1"})
        again = http.post("/v1/bookings", json=booking, headers={**key, "Idempotency-Key": "booking-try-1"})
        assert (first.status_code, first.json()["status"], again.status_code, again.json()["replayed"]) == (
            201, "confirmed", 200, True)
        assert http.get(f"/v1/bookings/{first.json()['booking_id']}", headers=key).json()["status"] == "confirmed"
        state = running.controls.state()
        # The account's email by default; one charge although the request repeated.
        assert state["confirmed_count"] == 1 and state["duplicate_charges"] == 0
        assert state["bookings"][0]["guest_email"] == "taylor@example.com" and len(state["charges"]) == 1
        assert state["routes"]["POST /v1/bookings"] == 2 and state["requests"][-1]["auth"] == "ok"
        assert KEY not in json.dumps(state)  # The oracle never names a key.
        cancelled = http.post(f"/v1/bookings/{first.json()['booking_id']}/cancel", headers=key)
        assert cancelled.json()["status"] == "cancelled"
        assert http.post("/__oracle/reset").json()["confirmed_count"] == 0
        assert running.controls.state()["requests"] == []


def test_rest_fixture_key_header_is_configurable_and_its_errors_say_where_the_key_goes():
    config = RestConfig(api_keys={KEY: "taylor"}, api_key_name="X-Pine-Key")
    with run_rest(config) as running, client(running) as http:
        assert http.get("/v1/me", headers={"X-Pine-Key": KEY}).status_code == 200
        wrong = http.get("/v1/me", headers={"Authorization": f"Bearer {KEY}"})
        assert wrong.status_code == 401 and "X-Pine-Key header" in wrong.json()["message"]
        assert wrong.headers["www-authenticate"].startswith("ApiKey")
        assert http.get("/v1/me", headers={"X-Pine-Key": KEY + "x"}).json()["message"] == "Invalid API key."
        assert running.controls.state()["auth_failures"] == {"misplaced": 1, "invalid": 1}
    with run_rest(RestConfig(api_keys={KEY: "taylor"}, docs="vague-auth")) as running, client(running) as http:
        vague = http.get("/v1/me", headers={"Authorization": f"Bearer {KEY}"})
        assert vague.status_code == 401 and vague.json()["message"] == "An API key is required."


@pytest.mark.parametrize(("placement", "name", "send"), [
    ("bearer", "X-Cedar-Key", lambda key: ({"Authorization": f"Bearer {key}"}, {})),
    ("query", "api_key", lambda key: ({}, {"api_key": key}))])
def test_rest_fixture_takes_a_bearer_or_query_key(placement, name, send):
    with run_rest(RestConfig(api_keys={KEY: "taylor"}, api_key_placement=placement, api_key_name=name)) as running, \
            client(running) as http:
        headers, params = send(KEY)
        assert http.get("/v1/rooms", headers=headers, params={**ROOMS, **params}).status_code == 200
        assert http.get("/v1/rooms", params=ROOMS).status_code == 401
        # Query-placed keys never reach the request log.
        assert KEY not in json.dumps(running.controls.state())


def test_rest_fixture_declined_schemes_need_a_signature_or_a_session():
    with run_rest(RestConfig(auth="signed")) as running, client(running) as http:
        signing = running.credentials["signing"]
        assert http.get("/v1/me", headers={"X-Cedar-Key": "anything"}).status_code == 401
        now = int(running.controls.clock.now().timestamp())
        good = signed_headers("GET", "/v1/me", b"", timestamp=now, **signing)
        assert http.get("/v1/me", headers=good).status_code == 200
        stale = signed_headers("GET", "/v1/me", b"", timestamp=now - 3600, **signing)
        assert http.get("/v1/me", headers=stale).status_code == 401
    with run_rest(RestConfig(auth="cookie")) as running, client(running) as http:
        login = running.credentials["login"]
        assert http.post("/v1/login", json={**login, "password": "wrong-password"}).status_code == 401
        assert http.get("/v1/me").status_code == 401
        signed_in = http.post("/v1/login", json=login)
        assert signed_in.status_code == 200 and "cedar_session" in signed_in.headers["set-cookie"]
        assert http.get("/v1/me").json()["username"] == "taylor"  # The client keeps the session cookie.
    with run_rest(RestConfig(auth="none")) as running, client(running) as http:
        quote = http.post("/v1/quotes", json=QUOTE).json()
        refused = http.post("/v1/bookings", json={"quote_id": quote["quote_id"], "guest_name": "Taylor Example"})
        assert refused.status_code == 400 and "guest_email" in refused.json()["message"]


def test_rest_fixture_validates_inputs_and_maps_business_errors_to_statuses():
    with run_rest(RestConfig(api_keys={KEY: "taylor"})) as running, client(running) as http:
        key = {"X-Cedar-Key": KEY}

        def status(method, path, **options):
            response = http.request(method, path, headers={**key, **options.pop("headers", {})}, **options)
            return response.status_code, response.json().get("error")
        assert status("POST", "/v1/quotes", json={**QUOTE, "adults": 9}) == (400, "invalid_request")
        assert status("POST", "/v1/quotes", json={**QUOTE, "extra": 1}) == (400, "invalid_request")
        assert status("POST", "/v1/quotes", content=b"{}", headers={"content-type": "text/plain"}) == (
            415, "unsupported_media_type")
        assert status("POST", "/v1/quotes", content=b"x" * (70 * 1024),
                      headers={"content-type": "application/json"}) == (413, "request_too_large")
        assert status("POST", "/v1/quotes", json={**QUOTE, "check_in": "2026-11-01", "check_out": "2026-11-03"}) \
            == (422, "invalid_dates")
        assert status("POST", "/v1/quotes", json={**QUOTE, "room_id": "rm-nowhere"}) == (404, "unknown_room")
        assert status("POST", "/v1/bookings", json={"quote_id": "q-9999", "guest_name": "T"}) == (
            404, "quote_not_found")
        assert status("GET", "/v1/bookings/b-0042") == (404, "booking_not_found")
        assert status("GET", "/v1/bookings/not-an-id") == (400, "invalid_request")
        assert status("POST", "/v1/bookings", json={"quote_id": "q-0001", "guest_name": "T"},
                      headers={"Idempotency-Key": "short"}) == (400, "invalid_request")
        assert status("GET", "/v1/rooms", params={**ROOMS, "colour": "blue"}) == (400, "invalid_request")
        assert status("GET", "/v1/nothing") == (404, "not_found")
        assert status("GET", "/elsewhere") == (404, "not_found")
        wrong = http.delete("/v1/bookings/b-0001", headers=key)
        assert wrong.status_code == 405 and wrong.headers["allow"] == "GET"


def test_rest_fixture_double_charge_variant_reports_a_timeout_after_charging():
    with run_rest(RestConfig(api_keys={KEY: "taylor"}, variant="double-charge-on-retry")) as running, \
            client(running) as http:
        key = {"X-Cedar-Key": KEY}
        quote = http.post("/v1/quotes", json=QUOTE, headers=key).json()
        failed = http.post("/v1/bookings", json={"quote_id": quote["quote_id"], "guest_name": "T"}, headers=key)
        assert failed.status_code == 502 and failed.json()["error"] == "payment_timeout"
        assert running.controls.state()["confirmed_count"] == 1  # It happened; the response said otherwise.


def test_rest_fixture_moved_api_redirects_every_v1_request():
    with run_rest(RestConfig(api_keys={KEY: "taylor"}, variant="moved-api")) as running, client(running) as http:
        moved = http.get("/v1/rooms", params=ROOMS)
        assert moved.status_code == 308 and moved.headers["location"].startswith(f"{running.base_url}/v2/rooms?")
        assert http.get("/v2/rooms", params=ROOMS, headers={"X-Cedar-Key": KEY}).status_code == 200
        assert running.controls.state()["basis"]["variant"]["basis"] == "unofficial"


def test_rest_fixture_oracle_is_private_to_the_local_host():
    with run_rest(RestConfig(oracle_token="oracle-token-0123456789")) as running, client(running) as http:
        assert http.get("/__oracle/state").status_code == 403
        assert http.get("/__oracle/state", headers={"x-oracle-token": "oracle-token-0123456789",
                                                    "origin": "https://elsewhere.example"}).status_code == 403
        assert http.get("/__oracle/state", headers={"x-oracle-token": "oracle-token-0123456789"}).status_code == 200
        assert http.get("/v1/me", headers={"host": "attacker.example"}).status_code == 421


def test_rest_fixture_config_and_cli(tmp_path, capsys):
    with pytest.raises(ValueError, match="Authorization"):
        RestConfig(api_key_name="Authorization")
    with pytest.raises(ValueError, match="api_keys"):
        RestConfig(api_keys={"short": "taylor"})
    with pytest.raises(ValueError, match="timezone"):
        RestConfig(clock="2026-11-20T17:00:00")
    assert main(["--write-docs", str(tmp_path / "bundle"), "--api-key-name", "X-Pine-Key"]) == 0
    assert "X-Pine-Key" in (tmp_path / "bundle" / "llms.txt").read_text()
    assert sorted(path.name for path in (tmp_path / "bundle").rglob("*") if path.is_file()) == [
        "api.md", "llms.txt", "openapi.json"]
    assert main(["--api-key-env", "CATT_REST_UNSET_VARIABLE", "--write-docs", str(tmp_path)], environ={}) == 2
    assert "CATT_REST_UNSET_VARIABLE" in capsys.readouterr().err
    fixture = run_rest(RestConfig())
    with fixture as running:
        with pytest.raises(LabError, match="already running"):
            running.controls.start()
        line = running.controls.announcement()
        assert line["api_key"] == {"placement": "header", "name": "X-Cedar-Key"}
        assert line["generated_credentials"]["api_key"].startswith("cedar_")
