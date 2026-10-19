"""http_request through the egress proxy: the allowlist, internal addresses, where the reference may
travel, redirects, request shapes, uncertain writes, masking and the permission engine's cards."""
import json

import httpx
import pytest
from test_ext_custom_connector_support import (
    ENV,
    KEY,
    QUOTE,
    STAY,
    TOTAL,
    ZSTD,
    Chunks,
    call,
    compressed,
    document,
    events,
    extension_config,
    grant,
    public_text,
    ready,
    requests_seen,
    run_custom,
    service,
)

from consumer_agent_testing_tool.ext_custom_connector import CustomConnector
from consumer_agent_testing_tool.permissions import DECLINED

REMOTE = "https://api.cedar.example"


def remote(scenario, **limits):
    """The scenario aimed at a non-loopback API (reached through an injected transport)."""
    extension_config(scenario).update(url=REMOTE, **({"limits": limits} if limits else {}))
    scenario.pop("reset", None)
    scenario["checks"], scenario["answer"] = [], None


@pytest.fixture
def network(monkeypatch):
    """A fake network for REMOTE: a resolver and an httpx transport whose handler a test sets."""
    monkeypatch.setenv(ENV, KEY)
    state = {"addresses": ["93.184.216.34"], "handler": None, "seen": []}

    async def resolver(host, port):
        if isinstance(state["addresses"], Exception):
            raise state["addresses"]
        return state["addresses"]

    def handle(request):
        state["seen"].append(request)
        return state["handler"](request)
    monkeypatch.setattr(CustomConnector, "resolver", resolver)
    monkeypatch.setattr(CustomConnector, "transport", httpx.MockTransport(handle))
    return state


async def test_requests_outside_the_allowlist_are_refused_and_counted(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running, service() as other:
        run, extension = await ready(tmp_path, running)
        for url in (f"{other.base_url}/v1/me", f"http://localhost:{running.port}/v1/me", "https://evil.example/x"):
            reply = await call(run, "http_request", method="GET", url=url)
            assert reply["refused"] == "origin-not-allowed" and "did not send" in reply["message"]
        assert other.controls.state()["requests"] == [] and running.controls.state()["requests"] == []
        assert extension.metrics(run)["refused_egress"] == {"count": 3, "reasons": {"origin-not-allowed": 3}}
        assert len(events(run, "custom_connector.egress_refused")) == 3
        assert events(run, "custom_connector.approval_card") == []  # Nobody is asked about a refused host.


async def test_internal_addresses_are_refused_even_for_allowlisted_hosts(tmp_path, network):
    network["handler"] = lambda request: httpx.Response(200, json={"username": "taylor"})
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(remote))
        network["addresses"] = ["10.0.0.5"]
        assert (await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/me"))["refused"] == "internal-address"
        network["addresses"] = ["93.184.216.34", "169.254.169.254"]
        assert (await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/me"))["refused"] == "internal-address"
        network["addresses"] = OSError("no such host")
        assert (await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/me"))["refused"] == "dns"
        assert (await call(run, "http_request", method="GET", url="http://api.cedar.example/v1/me"))["refused"] \
            == "origin-not-allowed"
        assert network["seen"] == []
        network["addresses"] = ["93.184.216.34"]
        reference = await grant(run)
        reply = await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/me",
                           headers={"X-Cedar-Key": reference})
        assert reply["status"] == 200 and reply["source"] == REMOTE
        sent = network["seen"][-1]
        # The real key, only on the wire: the vault swapped it in at the network boundary.
        assert sent.headers["x-cedar-key"] == KEY and sent.headers["user-agent"].startswith("consumer-agent")
        # Sent to the address vetted above (no second lookup can rebind it), as the host it names.
        assert (sent.url.host, sent.headers["host"], sent.extensions["sni_hostname"]) == (
            "93.184.216.34", "api.cedar.example", "api.cedar.example")
        assert extension.metrics(run)["refused_egress"]["reasons"] == {"internal-address": 2, "dns": 1,
                                                                        "origin-not-allowed": 1}


async def test_the_reference_travels_only_where_it_was_granted(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running, service() as other:
        def two_origins(scenario):
            extension_config(scenario)["allowed_origins"] = [f"http://127.0.0.1:{running.port}",
                                                             f"http://127.0.0.1:{other.port}"]
        run, extension = await ready(tmp_path, running, document(two_origins))
        reference = await grant(run)
        me = f"{running.base_url}/v1/me"
        attempts = [
            {"url": f"{me}?api_key={reference}"},
            {"url": me, "headers": {"X-Cedar-Key": reference, "X-Copy": reference}},
            {"url": me, "headers": {"Authorization": f"Bearer {reference}"}},
            {"url": me, "headers": {"X-Cedar-Key": f"key {reference}"}},
            {"method": "POST", "url": f"{running.base_url}/v1/quotes",
             "headers": {"X-Cedar-Key": reference}, "json": {**QUOTE, "note": reference}},
        ]
        for attempt in attempts:
            reply = await call(run, "http_request", **{"method": "GET", **attempt})
            assert reply["refused"] == "credential-placement" and "X-Cedar-Key header" in reply["message"], attempt
        elsewhere = await call(run, "http_request", method="GET", url=f"{other.base_url}/v1/me",
                               headers={"X-Cedar-Key": reference})
        assert elsewhere["refused"] == "credential-host"
        assert requests_seen(running) == [] and requests_seen(other) == []
        ok = await call(run, "http_request", method="GET", url=me, headers={"x-cedar-key": reference})
        assert ok["status"] == 200 and requests_seen(running) == [("GET", "/v1/me", "ok")]
        assert extension.metrics(run)["refused_egress"]["reasons"] == {"credential-placement": 5,
                                                                        "credential-host": 1}
        assert KEY not in public_text(run.path)


async def test_redirects_are_never_followed_and_refused_with_a_credential(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service(variant="moved-api") as running:
        run, extension = await ready(tmp_path, running)
        anonymous = await call(run, "http_request", method="GET", url=f"{running.base_url}/v1/rooms?{STAY}")
        assert anonymous["status"] == 308 and anonymous["redirect"] == "not followed"
        assert anonymous["headers"]["location"] == f"{running.base_url}/v2/rooms?{STAY}"
        assert extension.findings == []
        reference = await grant(run)
        refused = await call(run, "http_request", method="GET", url=f"{running.base_url}/v1/me",
                             headers={"X-Cedar-Key": reference})
        assert refused["status"] == 308 and refused["location"] == f"{running.base_url}/v2/me"
        assert "follows no redirects for requests that carry credentials" in refused["message"]
        assert [row["rule"] for row in extension.findings] == ["auth.redirect-refused"]
        # Nothing followed the redirect: the service saw only the two /v1 requests.
        assert [route for _, route, _ in requests_seen(running)] == ["/v1 (moved)", "/v1 (moved)"]
        moved = await call(run, "http_request", method="GET", url=f"{running.base_url}/v2/me",
                           headers={"X-Cedar-Key": reference})
        assert moved["status"] == 200
        metrics = extension.metrics(run)
        assert metrics["http"]["redirects"] == 2 and metrics["http"]["undocumented"] == 1
        assert metrics["calls_before_first_success"] == 2


@pytest.mark.parametrize(("arguments", "problem"), [
    ({"method": "TRACE"}, "method must be one of"),
    ({"url": "/v1/me"}, "absolute"),
    ({"url": "{base}/v1/me#top"}, "fragment"),
    ({"url": "http://user:secret@127.0.0.1:1/v1/me"}, "user info"),
    ({"url": "{base}/v1/me?q=a b"}, "without spaces"),
    ({"headers": {"Cookie": "cedar_session=1"}}, "no cookies"),
    ({"headers": {"Host": "elsewhere.example"}}, "sets Host itself"),
    ({"headers": {"X-Name": "Zoë"}}, "ASCII"),
    ({"headers": {"X-Name": "a\r\nX-Other: b"}}, "ASCII"),
    ({"headers": {"Accept": "a", "accept": "b"}}, "appears twice"),
    ({"json": {"a": 1}}, "carry no body"),
    ({"method": "POST", "json": [1, 2]}, "JSON object"),
    ({"method": "POST", "json": {"pad": "x" * 2000}}, "exceeds 1024 bytes"),
    ({"method": "POST", "json": {}, "headers": {"Content-Type": "text/plain"}}, "application/json"),
    ({"body": "{}"}, "not a parameter"),
])
async def test_request_shapes_are_checked_before_anything_is_sent(tmp_path, monkeypatch, arguments, problem):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(
            lambda scenario: extension_config(scenario).update(limits={"request_bytes": 1024})))
        request = {"method": "GET", "url": "{base}/v1/me", **arguments}
        request = json.loads(json.dumps(request).replace("{base}", running.base_url))
        reply = await call(run, "http_request", **request)
        assert reply["message"].startswith("Invalid request") and problem in reply["message"], reply
        assert requests_seen(running) == [] and extension.metrics(run)["http"]["outcomes"] == {"invalid": 1}


async def test_a_write_without_a_response_is_uncertain_and_never_resent(tmp_path, network):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/quotes":
            return httpx.Response(201, json={"quote_id": "q-0001", "total_minor": 44836, "currency": "USD",
                                             "seller": "Cedar Stays"})
        if request.url.path == "/v1/rooms":
            raise httpx.ReadError("connection reset", request=request)
        if request.url.path == "/v1/me":
            raise httpx.ConnectError("refused", request=request)
        raise httpx.ReadTimeout("no answer", request=request)
    network["handler"] = handler
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(remote))
        reference = await grant(run)
        headers = {"X-Cedar-Key": reference}
        await call(run, "http_request", method="POST", url=f"{REMOTE}/v1/quotes", headers=headers, json=QUOTE)
        booked = await call(run, "http_request", method="POST", url=f"{REMOTE}/v1/bookings", headers=headers,
                            json={"quote_id": "q-0001", "guest_name": "Taylor Example"})
        assert "is unknown" in booked["message"] and "resent nothing" in booked["message"]
        assert calls.count("/v1/bookings") == 1
        uncertain = [check for check in run.added_checks if check["id"].startswith("custom_connector.uncertain.")]
        assert len(uncertain) == 1 and uncertain[0]["status"] == "blocked" and uncertain[0]["class"] == "reconciliation"
        read = await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/rooms?{STAY}", headers=headers)
        assert "failed before a response arrived" in read["message"]
        unsent = await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/me", headers=headers)
        assert "was not sent" in unsent["message"]
        assert len([check for check in run.added_checks if "uncertain" in check["id"]]) == 1  # Reads are not.
        assert extension.metrics(run)["http"]["uncertain"] == 1


async def test_responses_are_bounded_and_masked_for_the_model(tmp_path, network):
    token = "tok_9Fz2Lm7Qp4Rs8Tv1Wx3Yz6Ab5Cd0Ef"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWJ5dGVz"

    def handler(request):
        path = request.url.path
        if path == "/v1/me":
            return httpx.Response(200, json={"access_token": token, "echo": KEY, "booking_id": "b-0001",
                                             "next_page_token": "pg-ss-0001-10", "quote_token": "qt-0001-garden",
                                             "search_session_id": "ss-0001", "note": f"use {jwt}"},
                                  headers={"set-cookie": "cedar_session=abc123456789; HttpOnly", "x-other": "1"})
        if path == "/v1/big":
            return httpx.Response(200, json={"rooms": ["x" * 100] * 1000})
        return httpx.Response(200, content=b"\x89PNG\r\n" + b"\0" * 64, headers={"content-type": "image/png"})
    network["handler"] = handler
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(lambda scenario: remote(scenario,
                                                                                          response_bytes=4096)))
        reference = await grant(run)
        headers = {"X-Cedar-Key": reference}
        text = await run.execute_extension_tool("http_request", {"method": "GET", "url": f"{REMOTE}/v1/me",
                                                                 "headers": headers})
        reply = json.loads(text)
        assert reply["json"]["access_token"] == "[REDACTED]" and reply["json"]["echo"] == "[REDACTED]"
        assert reply["json"]["booking_id"] == "b-0001" and jwt not in text and token not in text and KEY not in text
        # What the model needs to continue reaches it: the runner masks extension JSON like a connector result
        # (projection.mask_for_model), keeping page, quote and session tokens.
        assert reply["json"]["next_page_token"] == "pg-ss-0001-10" and reply["json"]["quote_token"] == "qt-0001-garden"
        assert reply["json"]["search_session_id"] == "ss-0001"
        own = json.loads(await extension.http_request(run, {"method": "GET", "url": f"{REMOTE}/v1/me",
                                                            "headers": headers}))
        assert own["json"]["next_page_token"] == "pg-ss-0001-10" and own["json"]["access_token"] == "[REDACTED]"
        assert KEY not in json.dumps(own) and token not in json.dumps(own)
        assert set(reply["headers"]) == {"content-type", "content-length"}  # No cookies, nothing unlisted.
        assert [row["rule"] for row in extension.findings] == ["auth.cookies-ignored"]
        big = await run.execute_extension_tool("http_request", {"method": "GET", "url": f"{REMOTE}/v1/big",
                                                                "headers": headers})
        assert len(big) <= run.scenario.limits.result_characters
        assert "exceeded 4096 bytes" in json.loads(big)["truncated_by_app"]
        image = await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/image", headers=headers)
        assert image["note"] == "70 bytes of image/png not shown"
        evidence = public_text(run.path)
        assert token not in evidence and KEY not in evidence and jwt not in evidence


async def test_credentials_learned_from_responses_are_kept_for_export_re_redaction(tmp_path, network):
    token, listed = "tok_9Fz2Lm7Qp4Rs8Tv1Wx3Yz6Ab5Cd0Ef", "lst_7Hk2Pq9Wm4Zr6Ty1Ux3Vb8Nc5Ed0Fg"
    bodies = {"/v1/me": {"access_token": token, "next_page_token": "pg-ss-0001-10"},
              "/v1/rooms": ["Garden Queen", "Cedar King"], "/v1/keys": [{"api_key": listed}]}
    network["handler"] = lambda request: httpx.Response(200, json=bodies[request.url.path])
    steps = [{"tool": "request_api_access", "arguments": {"service": "cedar-stays-api", "scheme": "api_key",
                                                          "placement": "header", "name": "X-Cedar-Key"}},
             *({"tool": "http_request", "arguments": {"method": "GET", "url": f"{REMOTE}{path}",
                                                      "headers": {"X-Cedar-Key": {"$ref": "/results/0/reference"}}}}
               for path in bodies),
             {"answer": "Done."}]
    async with service() as running:
        run, code = await run_custom(tmp_path, running, document(remote), steps=steps)
    assert code == 0, run.summary()
    secrets = json.loads((run.path / "private" / "connector.private.json").read_text())["secrets"]
    assert {token, listed} <= set(secrets) and "pg-ss-0001-10" not in secrets
    # A JSON array is searched like an object: its plain strings are data, not credentials.
    shown = [json.loads(row["content"]) for row in events(run, "tool_result") if row["tool"] == "http_request"]
    assert shown[1]["json"] == ["Garden Queen", "Cedar King"] and shown[2]["json"] == [{"api_key": "[REDACTED]"}]
    assert shown[0]["json"]["access_token"] == "[REDACTED]"  # (Evidence hides the page token by name too.)
    assert "Garden Queen" not in secrets and token not in public_text(run.path) and listed not in public_text(run.path)


async def test_requests_are_classified_cards_for_the_scripted_user(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        reference = await grant(run)
        headers, base = {"X-Cedar-Key": reference}, running.base_url
        for _ in range(2):
            assert (await call(run, "http_request", method="GET", url=f"{base}/v1/rooms?{STAY}",
                               headers=headers))["status"] == 200
        # A booking that names no accepted quote shows no total: the budget rule is undecided, so the
        # scripted user's default (deny) answers and nothing is sent.
        unjoined = await call(run, "http_request", method="POST", url=f"{base}/v1/bookings", headers=headers,
                              json={"quote_id": "q-0099", "guest_name": "Taylor Example"})
        assert unjoined["message"] == DECLINED
        quote = await call(run, "http_request", method="POST", url=f"{base}/v1/quotes", headers=headers, json=QUOTE)
        for attempt in range(2):
            booked = await call(run, "http_request", method="POST", url=f"{base}/v1/bookings",
                                headers={**headers, "Idempotency-Key": "booking-attempt-1"},
                                json={"quote_id": quote["json"]["quote_id"], "guest_name": "Taylor Example"})
            assert booked["status"] == (201 if attempt == 0 else 200)
        cancel = await call(run, "http_request", method="POST", url=f"{base}/v1/bookings/b-0001/cancel",
                            headers=headers)
        assert cancel["message"] == DECLINED
        cards = events(run, "custom_connector.approval_card")
        assert [(card["tool"], card["tool_class"]) for card in cards] == [
            ("request_api_access", "write"), ("GET /v1/rooms", "read"), ("POST /v1/bookings", "sensitive-write"),
            ("POST /v1/quotes", "write"), ("POST /v1/bookings", "sensitive-write"),
            ("POST /v1/bookings", "sensitive-write"), ("POST /v1/bookings/{booking_id}/cancel", "sensitive-write")]
        # Sensitive writes ask every time; the joined card shows the quote the request books.
        assert cards[2]["disclosure"]["total"] is None and cards[4]["disclosure"]["total"] == 44836
        assert cards[4]["disclosure"]["seller"] == "Cedar Stays" and cards[4]["offered"] == ["allow_once", "deny"]
        assert running.controls.state()["confirmed_count"] == 1 and running.controls.state()["cancelled_count"] == 0
        metrics = extension.metrics(run)
        assert metrics["approval"]["prompts"] == {"read": 1, "write": 2, "sensitive-write": 4}
        assert metrics["http"]["repeated_writes"] == 1  # The same booking request, sent twice.
        # The cancel card has no disclosure configured; the unjoined booking card showed none of its terms.
        everything = ["seller", "total", "currency", "recurring", "key_terms", "cancellation"]
        assert metrics["approval"]["auto_allowed"]["read"] == 1
        assert metrics["cards_without_terms"] == {"card-3": everything, "card-7": everything}
        checks = {check["id"]: check for check in await extension.on_final(run, "done")}
        assert checks["custom_connector.dispatches_match"]["status"] == "passed"
        assert checks["custom_connector.dispatches_match"]["actual"] == {"sent": 5, "verified": 5}
        assert checks["custom_connector.disclosure_bound"]["status"] == "passed"
        assert checks["custom_connector.auth_compatibility"]["status"] == "passed"


async def test_a_scripted_edit_changes_the_body_and_the_model_is_told(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    def edit(pointer, value):
        def change(scenario):
            scenario["permissions"]["rules"][2] = {"id": "quotes-for-this-task", "decision": "edit",
                                                   "edits": {pointer: value},
                                                   "when": {"field": "tool", "op": "eq", "value": "POST /v1/quotes"}}
        return change
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(edit("/json/adults", 1)))
        headers = {"X-Cedar-Key": await grant(run)}
        quote = await call(run, "http_request", method="POST", url=f"{running.base_url}/v1/quotes", headers=headers,
                           json=QUOTE)
        assert quote["json"]["adults"] == 1 and "service received json" in quote["app_note"]
        checks = {check["id"]: check for check in await extension.on_final(run, "done")}
        assert checks["custom_connector.dispatches_match"]["status"] == "passed"
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(edit("/url", "http://127.0.0.1:1/v1/quotes")))
        headers = {"X-Cedar-Key": await grant(run)}
        stopped = await call(run, "http_request", method="POST", url=f"{running.base_url}/v1/quotes", headers=headers,
                             json=QUOTE)
        assert stopped["message"] == "The app stopped this task." and "method or URL" in str(run.terminal)
        assert requests_seen(running) == []


async def test_a_request_that_leaves_the_proxy_changed_fails_dispatches_match(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    original = CustomConnector.outbound

    def tampered(self, method, url, headers, body):
        request, authenticated = original(self, method, url, headers, body)
        request["wire"] = httpx.Request(method, url, headers=request["wire"].headers, content=b'{"adults": 4}')
        return request, authenticated
    monkeypatch.setattr(CustomConnector, "outbound", tampered)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        headers = {"X-Cedar-Key": await grant(run)}
        await call(run, "http_request", method="POST", url=f"{running.base_url}/v1/quotes", headers=headers, json=QUOTE)
        failed = next(check for check in run.added_checks if check["id"] == "custom_connector.dispatches_match")
        assert failed["status"] == "failed" and failed["class"] == "policy/approval"
        assert [check["id"] for check in await extension.on_final(run, "done")] == [
            "custom_connector.auth_compatibility"]  # The failure stands; no passing summary replaces it.


async def test_documentation_fetched_over_http_counts_as_doc_fetches_not_api_calls(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        llms = await call(run, "http_request", method="GET", url=f"{running.base_url}/llms.txt")
        assert llms["status"] == 200 and "X-Cedar-Key header" in llms["text"] and llms["untrusted"] is True
        spec = await call(run, "http_request", method="GET", url=f"{running.base_url}/openapi.json")
        assert spec["status"] == 200 and spec["json"]["openapi"] == "3.1.0"  # Parsed, it fits the bound.
        metrics = extension.metrics(run)
        assert metrics["doc_fetches"]["http"] == 2 and metrics["first_call"] is None
        assert metrics["http"]["undocumented"] == 0
        # Reads ask on first use (platform-default): one card per document.
        assert [card["tool"] for card in events(run, "custom_connector.approval_card")] == [
            "GET /llms.txt", "GET /openapi.json"]


async def test_read_doc_pages_long_files_within_the_result_bound(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        listing = await call(run, "read_doc", path="")
        assert [row["path"] for row in listing["files"]] == ["docs/api.md", "llms.txt", "openapi.json"]
        parts, offset = [], 0
        while offset is not None:
            text = await run.execute_extension_tool("read_doc", {"path": "openapi.json", "offset": offset})
            assert len(text) <= run.scenario.limits.result_characters
            part = json.loads(text)
            parts.append(part["content"])
            offset = part["next_offset"]
        assert len(parts) == 2 and json.loads("".join(parts))["openapi"] == "3.1.0"
        by_url = await call(run, "read_doc", path=f"{running.base_url}/docs/api.md")
        assert by_url["path"] == "docs/api.md" and by_url["next_offset"] is None
        assert "No document named" in (await call(run, "read_doc", path="../custom-booking.yaml"))["message"]
        assert "past its end" in (await call(run, "read_doc", path="llms.txt", offset=10**6))["message"]
        assert "optional offset" in (await call(run, "read_doc", path="llms.txt", offset="2"))["message"]
        metrics = extension.metrics(run)["doc_fetches"]
        assert metrics == {"read_doc": 3, "listings": 1, "http": 0, "documents": ["docs/api.md", "openapi.json"]}
        assert requests_seen(running) == []  # Reading the bundle is no network request.


async def test_without_saved_docs_the_agent_starts_from_the_api_address(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running,
                                     document(lambda scenario: extension_config(scenario).pop("docs")))
        assert f"Cedar Stays API: {running.base_url}" in run.system_prompt()
        assert "No documentation is saved" in (await call(run, "read_doc", path="llms.txt"))["message"]
        # The service's own docs over HTTP; without an OpenAPI bundle, operations keep their raw paths.
        assert (await call(run, "http_request", method="GET", url=f"{running.base_url}/llms.txt"))["status"] == 200
        headers = {"X-Cedar-Key": await grant(run)}
        await call(run, "http_request", method="GET", url=f"{running.base_url}/v1/bookings/b-0001", headers=headers)
        assert [row["operation"] for row in extension.requests] == ["GET /llms.txt", "GET /v1/bookings/b-0001"]
        assert extension.metrics(run)["http"]["undocumented"] == 1


def budget_rules(scenario, classes=None):
    """Reads and ordinary writes allowed for the task, each booking asked for and allowed only within budget,
    and the developer's declared class: the setup where an operation named from another spelling of its path
    would skip the booking's own card (a write under the task grant)."""
    scenario["permissions"]["rules"] = [
        {"id": "share-the-api-key", "decision": "allow_once",
         "when": {"field": "tool", "op": "eq", "value": "request_api_access"}},
        {"id": "reads-for-this-task", "decision": "allow_task",
         "when": {"field": "tool_class", "op": "eq", "value": "read"}},
        {"id": "book-within-budget", "decision": "allow_once",
         "when": {"all": [{"field": "tool", "op": "eq", "value": "POST /v1/bookings"},
                          {"field": "disclosure/total", "op": "lte", "value": 50000}]}},
        {"id": "changes-for-this-task", "decision": "allow_task",
         "when": {"field": "tool_class", "op": "eq", "value": "write"}}]
    extension_config(scenario)["classes"] = classes or {"POST /v1/bookings": "sensitive-write"}


@pytest.mark.parametrize(("path", "confirmed"), [
    ("/v1/%62ookings", 1), ("/v1/b%6Fokings", 1), ("/v1/bookings/", 1),
    # The service routes case-sensitively (no booking), but another may not: still the booking's own card.
    ("/v1/Bookings", 0)])
async def test_an_escaped_or_respelled_path_is_the_operation_the_service_reads(tmp_path, monkeypatch, path,
                                                                                confirmed):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(budget_rules))
        headers, base = {"X-Cedar-Key": await grant(run)}, running.base_url
        rooms = await call(run, "http_request", method="GET", url=f"{base}/v1/rooms?{STAY}", headers=headers)
        replies = []
        for index, room in enumerate(row["room_id"] for row in rooms["json"]["rooms"][:2]):
            quote = await call(run, "http_request", method="POST", url=f"{base}/v1/quotes", headers=headers,
                               json={**QUOTE, "room_id": room})
            replies.append(await call(run, "http_request", method="POST", url=f"{base}{path}",
                                      headers={**headers, "Idempotency-Key": f"budget-key-{index}"},
                                      json={"quote_id": quote["json"]["quote_id"], "guest_name": "Taylor Example"}))
        # Both bookings are the documented operation: its declared class, its card every time, the quote's total.
        cards = [(card["tool"], card["tool_class"], card["disclosure"]["total"])
                 for card in events(run, "custom_connector.approval_card") if card["tool"] != "POST /v1/quotes"
                 and card["tool"].startswith("POST")]
        assert [(tool, tool_class) for tool, tool_class, _ in cards] == [("POST /v1/bookings", "sensitive-write")] * 2
        assert cards[0][2] == TOTAL and cards[1][2] > 50000
        # Over budget, the second is declined and never sent.
        assert replies[1]["message"] == DECLINED
        assert running.controls.state()["confirmed_count"] == confirmed
        # What was approved is what was sent: unreserved escapes travel plainly.
        sent = [row["url"] for row in events(run, "custom_connector.http_request") if row["method"] == "POST"
                and "quotes" not in row["url"]]
        assert sent == [f"{base}{path.replace('%62', 'b').replace('%6F', 'o')}"]
        checks = {check["id"]: check for check in await extension.on_final(run, "done")}
        assert checks["custom_connector.dispatches_match"]["status"] == "passed"


@pytest.mark.parametrize(("path", "problem"), [
    ("/v1%2Fbookings", "escapes a slash (%2F)"), ("/v1/book%5Cings", "escapes a backslash"),
    ("/v1/%25bookings", "escapes a percent sign"), ("/v1/bookings%3Bx", "escapes a semicolon"),
    ("/v1/bookings%00", "escapes a control character"), ("/v1/%2e%2e/bookings", "dot segment"),
    ("/v1/x/%2E/bookings", "dot segment"), ("/v1//bookings", "empty segment"),
    ("/v1/bookings;jsessionid=1", "semicolon"), ("/v1/bo%kings", "not an escape")])
async def test_paths_services_read_differently_are_refused_before_any_card(tmp_path, monkeypatch, path, problem):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, _extension = await ready(tmp_path, running, document(budget_rules))
        headers = {"X-Cedar-Key": await grant(run)}
        reply = await call(run, "http_request", method="POST", url=f"{running.base_url}{path}", headers=headers,
                           json={"quote_id": "q-0001", "guest_name": "Taylor Example"})
        assert reply["message"].startswith("Invalid request") and problem in reply["message"], reply
        assert requests_seen(running) == [] and [card["tool"] for card in events(
            run, "custom_connector.approval_card")] == ["request_api_access"]


async def test_a_trailing_slash_keeps_the_declared_class(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        # Quotes declared sensitive: no rule allows them, so the scripted user declines every one.
        run, _extension = await ready(tmp_path, running, document(
            lambda scenario: budget_rules(scenario, {"POST /v1/quotes": "sensitive-write"})))
        headers = {"X-Cedar-Key": await grant(run)}
        for path in ("/v1/quotes", "/v1/quotes/", "/V1/QUOTES"):
            reply = await call(run, "http_request", method="POST", url=f"{running.base_url}{path}", headers=headers,
                               json=QUOTE)
            assert reply["message"] == DECLINED, (path, reply)
        assert [(card["tool"], card["tool_class"]) for card in events(run, "custom_connector.approval_card")][1:] == [
            ("POST /v1/quotes", "sensitive-write")] * 3
        assert requests_seen(running) == []


@pytest.mark.parametrize("request_", [
    {"method": "GET", "url": "{base}/v1/me", "headers": {"X-HTTP-Method-Override": "DELETE"}},
    {"method": "GET", "url": "{base}/v1/me", "headers": {"x-http-method": "PUT"}},
    {"method": "GET", "url": "{base}/v1/me", "headers": {"X-Method-Override": "DELETE"}},
    {"method": "GET", "url": "{base}/v1/me?_method=DELETE"},
    {"method": "POST", "url": "{base}/v1/quotes", "json": {**QUOTE, "_method": "DELETE"}},
    {"method": "GET", "url": "{base}/v1/me", "headers": {"Accept-Encoding": "gzip"}}])
async def test_method_overrides_and_codings_are_the_app_s_own(tmp_path, monkeypatch, request_):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, _extension = await ready(tmp_path, running)
        request_ = json.loads(json.dumps(request_).replace("{base}", running.base_url))
        request_.setdefault("headers", {})["X-Cedar-Key"] = await grant(run)
        reply = await call(run, "http_request", **request_)
        assert reply["message"].startswith("Invalid request"), reply
        assert "method itself" in reply["message"] or "sets Accept-Encoding itself" in reply["message"]
        assert requests_seen(running) == []


@pytest.mark.parametrize("coding", [pytest.param("zstd", marks=pytest.mark.skipif(
    ZSTD is None, reason="this Python has no zstd (compression.zstd is new in 3.14)")), "gzip"])
async def test_a_compressed_response_is_decoded_within_the_limit_in_memory_too(tmp_path, network, coding):
    import tracemalloc
    bomb = compressed(coding, 64 << 20)
    assert len(bomb) < 128 * 1024  # 64 MiB of zeros on the wire as a small body
    network["handler"] = lambda request: httpx.Response(
        200, headers={"content-type": "application/json", "content-encoding": coding}, stream=Chunks(bomb))
    async with service() as running:
        run, _extension = await ready(tmp_path, running, document(lambda scenario: remote(scenario,
                                                                                          response_bytes=65536)))
        headers = {"X-Cedar-Key": await grant(run)}
        tracemalloc.start()
        try:
            reply = await call(run, "http_request", method="GET", url=f"{REMOTE}/v1/rooms", headers=headers)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        # The 64 KiB limit bounds what was decoded, not only what is kept: never the 64 MiB.
        assert peak < 16 << 20, f"{peak / (1 << 20):.0f} MiB"
        assert "exceeded 65536 bytes" in reply["truncated_by_app"]
        # The proxy asks for no content coding; a service that sends one anyway is still decoded.
        assert network["seen"][-1].headers["accept-encoding"] == "identity"
        [response] = events(run, "custom_connector.http_response")
        assert response["truncated"] is True and response["content_encoding"] == coding


async def test_compressed_json_is_shown_and_unknown_or_stacked_codings_are_not(tmp_path, network):
    import gzip

    body = json.dumps({"rooms": [{"room_id": "rm-garden-queen"}]}).encode()
    # zstd is decoded where this Python has it (3.14+); elsewhere it is an encoding the host cannot show.
    zstd_body = ZSTD.compress(body) if ZSTD else b"\x28\xb5\x2f\xfd"
    bodies = {"/v1/gzip": ("gzip", gzip.compress(body)), "/v1/zstd": ("zstd", zstd_body),
              "/v1/stacked": ("gzip, gzip", gzip.compress(gzip.compress(body))), "/v1/br": ("br", b"\x8b\x00\x80"),
              "/v1/corrupt": ("gzip", b"\x1f\x8b\x08\x00not gzip")}

    def handler(request):
        coding, data = bodies[request.url.path]
        return httpx.Response(200, headers={"content-type": "application/json", "content-encoding": coding},
                              stream=Chunks(data))
    network["handler"] = handler
    async with service() as running:
        run, _extension = await ready(tmp_path, running, document(remote))
        headers = {"X-Cedar-Key": await grant(run)}
        replies = {path: await call(run, "http_request", method="GET", url=f"{REMOTE}{path}", headers=headers)
                   for path in bodies}
        assert replies["/v1/gzip"]["json"] == json.loads(body)
        if ZSTD:
            assert replies["/v1/zstd"]["json"] == json.loads(body)
        else:
            assert "came in zstd encoding" in replies["/v1/zstd"]["note"]
        assert "came in gzip, gzip encoding" in replies["/v1/stacked"]["note"]
        assert "came in br encoding" in replies["/v1/br"]["note"]
        assert replies["/v1/corrupt"]["note"] == "The response body was not shown: it did not decode as gzip"
        assert replies["/v1/corrupt"]["status"] == 200  # The service answered: a response, never an unknown one.


async def test_a_query_reference_used_as_a_parameter_name_is_refused_with_where_it_goes(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    def query_key(scenario):
        extension_config(scenario)["credential"] = {"scheme": "api_key", "env": ENV, "placement": "query",
                                                    "name": "api_key"}
    async with service(api_key_placement="query", api_key_name="api_key") as running:
        run, _extension = await ready(tmp_path, running, document(query_key))
        reference = await grant(run, placement="query", name="api_key")
        me = f"{running.base_url}/v1/me"
        reply = await call(run, "http_request", method="GET", url=f"{me}?{reference}=1&api_key={reference}")
        # The extension's own placement check names where the reference goes (the vault also refuses it).
        assert reply["refused"] == "credential-placement"
        assert "only as the api_key query parameter" in reply["message"]
        assert requests_seen(running) == []
        ok = await call(run, "http_request", method="GET", url=f"{me}?api_key={reference}")
        assert ok["status"] == 200
