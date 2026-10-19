"""request_api_access: the user grants access on a card, the model gets a reference, never the secret;
declined schemes, mismatches and misplaced grants become auth compatibility findings."""
import json

import pytest
from test_ext_custom_connector_support import (
    DISCONNECTED,
    ENV,
    KEY,
    call,
    checks_of,
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

from consumer_agent_testing_tool.linking import SURROGATE
from consumer_agent_testing_tool.permissions import DECLINED


async def test_a_grant_gives_the_model_a_reference_and_the_service_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        text = await run.execute_extension_tool("request_api_access", {
            "service": "cedar-stays-api", "scheme": "api_key", "placement": "header", "name": "X-Cedar-Key"})
        reply = json.loads(text)
        assert reply["granted"] is True and SURROGATE.fullmatch(reply["reference"]) and KEY not in text
        assert (reply["header"], reply["origin"]) == ("X-Cedar-Key", running.base_url)
        assert "X-Cedar-Key header" in reply["how"] and "never shown to you" in reply["how"]
        # The user approved a write card (granting access changes permissions); the key is host-private.
        card = events(run, "custom_connector.approval_card")[0]
        assert (card["tool"], card["tool_class"], card["connector"]) == ("request_api_access", "write",
                                                                          "cedar-stays-api")
        assert card["disclosure"] == {"service": "Cedar Stays", "access": "an API key"}
        assert KEY in run.redactor.credentials and run.private("custom_connector_credential") == KEY
        me = await call(run, "http_request", method="GET", url=f"{running.base_url}/v1/me",
                        headers={"X-Cedar-Key": reply["reference"]})
        assert me["status"] == 200 and me["json"]["username"] == "taylor" and me["untrusted"] is True
        assert requests_seen(running)[-1] == ("GET", "/v1/me", "ok")
        metrics = extension.metrics(run)
        assert metrics["access"]["granted_as"] == {"scheme": "api_key", "placement": "header", "name": "X-Cedar-Key"}
        assert metrics["access"]["matches_service"] is True and metrics["verified_with_read"] is True
        assert KEY not in public_text(run.path)


@pytest.mark.parametrize("scheme", ["cookie", "password", "signed_request", "multi_secret", "HMAC", "basic",
                                    "client credentials"])
async def test_declined_schemes_share_nothing_and_leave_a_finding(tmp_path, monkeypatch, scheme):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        reply = await call(run, "request_api_access", service="Cedar Stays", scheme=scheme, placement="header",
                           name="X-Cedar-Key")
        assert reply["granted"] is False and "API keys and OAuth sign-in only" in reply["message"]
        assert extension.grants == [] and events(run, "custom_connector.approval_card") == []
        finding = extension.findings[0]
        # The API takes an API key, so asking for a declined scheme is the agent's misreading.
        assert finding["rule"] == "auth.declined-scheme-requested" and finding["severity"] == "warn"
        assert extension.metrics(run)["access"]["outcomes"] == {"declined-scheme": 1}


async def test_requests_name_a_known_service_scheme_and_placement(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)

        async def ask(**arguments):
            return await call(run, "request_api_access", **{"service": "cedar-stays-api", "scheme": "api_key",
                                                             "placement": "header", "name": "X-Cedar-Key",
                                                             **arguments})
        assert "not a service you can connect" in (await ask(service="pine-lodge"))["message"]
        assert "Unknown scheme" in (await ask(scheme="kerberos"))["message"]
        assert "Invalid placement" in (await ask(name=None))["message"]
        assert "Invalid placement" in (await ask(name="Cookie"))["message"]
        assert "Invalid placement" in (await ask(placement="path"))["message"]
        assert "not a parameter" in (await ask(scope="all"))["message"]
        assert "Invalid request" in (await ask(scheme=7))["message"]
        mismatch = await ask(scheme="oauth2", placement="bearer", name=None)
        assert mismatch["granted"] is False and "did not offer OAuth sign-in" in mismatch["message"]
        assert [row["rule"] for row in extension.findings] == ["auth.scheme-mismatch"]
        assert extension.grants == [] and events(run, "custom_connector.approval_card") == []
        outcomes = extension.metrics(run)["access"]["outcomes"]
        assert outcomes == {"unknown-service": 1, "unknown-scheme": 1, "invalid": 5, "scheme-mismatch": 1}
        assert (await ask(scheme="API key"))["granted"] is True  # Aliases normalize to api_key.


async def test_a_misplaced_grant_is_issued_reported_and_rejected_by_the_service(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, extension = await ready(tmp_path, running)
        reference = await grant(run, placement="bearer", name=None)
        reply = await call(run, "http_request", method="GET", url=f"{running.base_url}/v1/me",
                           headers={"Authorization": f"Bearer {reference}"})
        assert reply["status"] == 401 and "X-Cedar-Key header" in reply["json"]["message"]
        rules = {row["rule"]: row for row in extension.findings}
        assert set(rules) == {"auth.placement-mismatch", "auth.credential-rejected"}
        assert "X-Cedar-Key header" in rules["auth.placement-mismatch"]["detail"]
        assert rules["auth.credential-rejected"]["challenge"].startswith("ApiKey")
        metrics = extension.metrics(run)
        assert metrics["access"]["matches_service"] is False and metrics["first_call_success"] is False
        assert requests_seen(running)[-1] == ("GET", "/v1/me", "misplaced")
        # The real key went where it was granted (Authorization), and only to the service.
        assert running.controls.state()["auth_failures"] == {"misplaced": 1}


async def test_the_user_can_decline_access(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    def no_rule(scenario):
        scenario["permissions"]["rules"] = [rule for rule in scenario["permissions"]["rules"]
                                            if rule["id"] != "share-the-api-key"]
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(no_rule))
        reply = await call(run, "request_api_access", service="cedar-stays-api", scheme="api_key",
                           placement="header", name="X-Cedar-Key")
        assert reply == {"source": "app", "tool": "request_api_access", "message": DECLINED, "granted": False}
        assert extension.grants == [] and extension.metrics(run)["access"]["outcomes"] == {"declined-by-user": 1}
        decision = events(run, "custom_connector.approval_decision")[0]
        assert decision["allowed"] is False and decision["decision"]["kind"] == "deny"


async def test_an_edited_grant_is_what_the_user_granted_and_must_stay_valid(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    def edit(edits):
        def change(scenario):
            scenario["permissions"]["rules"][0] = {"id": "share-the-api-key", "decision": "edit", "edits": edits,
                                                   "when": {"field": "tool", "op": "eq", "value": "request_api_access"}}
        return change
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(edit({"/name": "X-Cedar-Key"})))
        reply = await call(run, "request_api_access", service="cedar-stays-api", scheme="api_key",
                           placement="header", name="X-Api-Key")
        assert reply["header"] == "X-Cedar-Key" and extension.metrics(run)["access"]["matches_service"] is True
    async with service() as running:
        run, extension = await ready(tmp_path, running, document(edit({"/scheme": "oauth"})))
        reply = await call(run, "request_api_access", service="cedar-stays-api", scheme="api_key",
                           placement="header", name="X-Cedar-Key")
        # An edit the vault cannot honor is the scenario's mistake: the run stops, nothing is issued.
        assert reply["message"] == "The app stopped this task." and "not a grant" in str(run.terminal)
        assert extension.grants == []


@pytest.mark.parametrize(("value", "kind"), [(None, "missing"), ("short", "rejected"), ("has space here", "rejected")])
async def test_a_missing_or_unsafe_credential_stops_setup_before_the_model(tmp_path, monkeypatch, value, kind):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    async with service() as running:
        run, code = await run_custom(tmp_path, running)
    execution = checks_of(run)["execution"]
    assert code == 2 and execution["class"] == "authentication" and execution["link_error"] == kind
    assert execution["before_first_model_request"] is True and execution["stage"] == "project"
    assert running.controls.state()["requests"] == []


async def test_the_service_name_must_differ_from_a_connector_name(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)

    def clash(scenario):
        # A declared connector, even one the user has not connected, that the service is named like.
        scenario["connectors"] = [dict(DISCONNECTED)]
        extension_config(scenario).update(service="cedar-stays")
    async with service() as running:
        run, code = await run_custom(tmp_path, running, document(clash))
    assert code == 2 and "also a connector name" in checks_of(run)["execution"]["detail"]


async def test_an_oauth_token_is_granted_as_a_bearer_reference(tmp_path, monkeypatch):
    token = "cedar_oauth_access_token_0123456789"
    monkeypatch.setenv("CEDAR_STAYS_TOKEN", token)

    def oauth(scenario):
        extension_config(scenario)["credential"] = {"scheme": "oauth", "env": "CEDAR_STAYS_TOKEN"}
    # The service reads a bearer token; the host holds it as the OAuth access token of a completed sign-in.
    async with service(api_key_placement="bearer", api_keys={token: "taylor"}) as running:
        run, extension = await ready(tmp_path, running, document(oauth))
        refused = await call(run, "request_api_access", service="cedar-stays-api", scheme="oauth",
                             placement="header", name="X-Token")
        assert "OAuth tokens use bearer" in refused["message"]
        reply = await call(run, "request_api_access", service="cedar-stays-api", scheme="oauth", placement="bearer")
        assert reply["granted"] is True and "Authorization: Bearer <reference>" in reply["how"]
        me = await call(run, "http_request", method="GET", url=f"{running.base_url}/v1/me",
                        headers={"Authorization": f"Bearer {reply['reference']}"})
        assert me["status"] == 200 and token not in json.dumps(me) and token not in public_text(run.path)
        assert extension.metrics(run)["access"]["matches_service"] is True and extension.findings == []
