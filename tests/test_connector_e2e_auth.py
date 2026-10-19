"""Connector lane end to end: host-owned credentials through real linking against the fixture.

OAuth 2.1 with fixture-auto consent (DCR, PKCE, introspected subject), a token refreshed after it
expired mid-run, a wrong-account link, a link revoked mid-run, and API keys in a custom header and
a query parameter that never reach events, model requests or exports.
"""
import secrets
import zipfile
from pathlib import Path

import pytest
from test_connector_e2e_support import (
    NATIVE,
    STAY,
    Script,
    answer,
    call,
    checks_of,
    connector,
    document,
    fixture,
    happy_path,
    oracle,
    public_text,
    result_of,
    run_scenario,
)

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_fixture import User, subject_for
from consumer_agent_testing_tool.replay import playback

LOGIN, KEY_ENV = "CEDAR_STAYS_LOGIN", "CEDAR_STAYS_KEY"
TAYLOR, JORDAN = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
USERS = [User(username="taylor", password=TAYLOR, name="Taylor Example", email="taylor@example.com"),
         User(username="jordan", password=JORDAN, name="Jordan Sample", email="jordan@example.com")]
TEMPLATE = "muse-template-unofficial-2026-10"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # The token store is workspace-local (.cache/connector-links).
    return tmp_path


def oauth(scenario, profile=TEMPLATE):
    connector(scenario).update(client_profile=profile, auth={
        "mode": "oauth", "registration": "dcr", "consent": "fixture-auto", "test_login_env": LOGIN,
        "scopes": ["stays:read", "stays:write"], "read_only_scopes": ["stays:read"],
        "expected_subject": subject_for("taylor")})


def every_text(path):
    """Public evidence as text, including the members of zipped Inspect logs."""
    found = [public_text(path)]
    for item in Path(path).rglob("*"):
        if item.suffix in (".eval", ".zip") and "private" not in item.relative_to(path).parts:
            with zipfile.ZipFile(item) as archive:
                found += [archive.read(name).decode(errors="ignore") for name in archive.namelist()]
    return "".join(found)


def tokens(running):
    return [token for token in running.controls.auth.grants]


async def test_oauth_links_with_fixture_auto_consent_and_refreshes_a_token_that_expired_mid_run(
        workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, f"taylor:{TAYLOR}")
    async with fixture(auth="oauth", users=USERS, access_token_seconds=300) as running:
        def expire_then_search(input):
            running.controls.advance_clock(301)  # The access token expires on the fixture's clock.
            return call("search_rooms", **STAY)
        steps = happy_path()
        script = Script(expire_then_search, *steps[1:])
        run, code = await run_scenario(workspace, running, document(NATIVE, oauth), driver=script.driver())
        state, issued = oracle(running), tokens(running)
    assert code == 0, run.summary()
    assert state["confirmed_count"] == 1
    authorization = state["authorization"]
    assert [client["registration"] for client in authorization["clients"]] == ["dcr"]
    assert [event["event"] for event in authorization["events"]].count("refreshed") == 1
    result = result_of(run)
    linked = result["connector"]["link"]
    assert linked["mode"] == "oauth" and linked["registration"] == "dcr" and linked["consent"] == "fixture-auto"
    assert linked["subject"] == subject_for("taylor") and linked["pkce"] == "S256"  # From RFC 7662 introspection.
    assert linked["scopes"] == ["stays:read", "stays:write"] and linked["read_only_supported"] is True
    # One host-side refresh after the 401, recorded and labeled (the template itself retries nothing);
    # never a silent re-authorization.
    refreshed = next(note for note in result["connector"]["client_observations"]
                     if note["kind"] == "credential-refreshed")
    assert (refreshed["status"], refreshed["count"], refreshed["basis"]) == (401, 1, "assumed")
    assert "retries nothing" in refreshed["source"] and result["run_lock"]["bases"]["host-credential-refresh"]
    assert {"kind": "token-refreshed"} in result["connector"]["credential_observations"]
    text = every_text(run.path)
    assert len(issued) == 4 and not any(token in text for token in issued)  # Old and refreshed tokens.
    assert TAYLOR not in text and not any(token in script.visible() for token in issued)
    store = list((workspace / ".cache" / "connector-links").iterdir())
    assert len(store) == 1 and store[0].stat().st_mode & 0o777 == 0o600


async def test_a_wrong_account_link_is_a_setup_error_before_any_model_request(workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, f"jordan:{JORDAN}")
    script = Script(answer("unused"))
    async with fixture(auth="oauth", users=USERS) as running:
        run, code = await run_scenario(workspace, running, document(NATIVE, oauth), driver=script.driver())
        state = oracle(running)
    assert code == 2 and script.requests == []
    execution = checks_of(run)["execution"]
    assert execution["link_error"] == "wrong-account" and execution["stage"] == "link"
    assert execution["remediation"] == "link the account auth.expected_subject names"
    assert execution["before_first_model_request"] is True
    result = result_of(run)
    assert result["failure_class"] == "authentication" and result["failure_stage"] == "link"
    assert "rpc" in state and "tools/call" not in state["rpc"]  # Linking only: no tool ran.
    assert JORDAN not in every_text(run.path)


async def test_a_link_revoked_mid_run_is_reported_with_its_remedy_never_reauthorized(workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, f"taylor:{TAYLOR}")
    async with fixture(auth="oauth", users=USERS) as running:
        def revoke_then_search(input):
            running.controls.revoke_user("taylor")  # The user revokes access at the provider.
            return call("search_rooms", **STAY)
        script = Script(revoke_then_search, answer("I can't reach Cedar Stays: please reconnect it, then I'll "
                                                   "look for a room again."))
        run, code = await run_scenario(workspace, running, document(NATIVE, oauth), driver=script.driver())
        state = oracle(running)
    assert code == 1  # Nothing was booked.
    told = script.tool_results()[-1]
    assert told["untrusted"] is True and "credential" in told["error"]["message"]
    result = result_of(run)
    # The report and the CLI line lead with the lapsed link and its remedy, not the outcome check.
    link = checks_of(run)["connector.link"]
    assert link["status"] == "failed" and link["class"] == "authentication" and link["link_error"] == "revoked"
    assert link["remediation"] == "relink the connector: the authorization was revoked"
    assert [check["id"] for check in result["checks"]][:2] == ["execution", "connector.link"]
    assert result["detail"].startswith("connector.link: search_rooms: authentication error (revoked)")
    assert result["metrics"]["service_errors"] == {"authentication": 1}
    gap = next(gap for gap in result["observation_gaps"] if gap.startswith("search_rooms: authentication error"))
    assert "(revoked)" in gap and "relink the connector" in gap
    assert [event["event"] for event in state["authorization"]["events"]].count("code_issued") == 1


@pytest.mark.parametrize("placement, fixture_placement, name, profile", [
    ("custom_header", "header", "X-Cedar-Key", TEMPLATE),
    ("query_param", "query", "api_key", "generic-sdk"),
])
async def test_an_api_key_reaches_the_connector_but_never_evidence_model_requests_or_exports(
        workspace, monkeypatch, capsys, placement, fixture_placement, name, profile):
    key = "csk_" + secrets.token_urlsafe(24)
    monkeypatch.setenv(KEY_ENV, key)

    def keyed(scenario):
        connector(scenario).update(client_profile=profile, auth={"mode": "api_key", "env": KEY_ENV,
                                                                 "placement": placement, "name": name})
    script = Script(*happy_path())
    async with fixture(auth="api_key", api_key_placement=fixture_placement, api_key_name=name,
                       api_keys={key: "taylor"}) as running:
        run, code = await run_scenario(workspace, running, document(NATIVE, keyed), driver=script.driver())
        state = oracle(running)
    assert code == 0, run.summary()
    mcp = [entry for entry in state["requests"] if entry["path"].startswith("/mcp")]
    assert mcp and {entry["auth"] for entry in mcp} == {"ok"}  # Every MCP request carried the key.
    assert state["confirmed_count"] == 1
    assert key not in script.visible()  # Model requests.
    assert key not in every_text(run.path)  # Events, the sub-run's model requests and Inspect logs.
    assert key not in (run.path / "private" / "connector.private.json").read_text()
    linked = result_of(run)["connector"]["link"]
    assert linked == {"mode": "api_key", "placement": placement, "name": name, "env": KEY_ENV, "scopes": [],
                      "subject": None, "expires_at": None}
    monkeypatch.delenv(KEY_ENV)  # The export must be clean on its own, without the environment's help.
    capsys.readouterr()  # Inspect's own console display from the agent run.
    destination = workspace / "exports" / run.path.name
    assert await execute(parser().parse_args(["export", str(run.path), "--output", str(destination)])) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(destination)
    assert playback(destination)["integrity"] == "verified" and not (destination / "private").exists()
    assert key not in every_text(destination)


async def test_the_shipped_oauth_example_links_and_books_with_the_scripted_calls(workspace, monkeypatch):
    from test_connector_e2e_support import EXAMPLES

    from consumer_agent_testing_tool.serialization import load
    monkeypatch.setenv(LOGIN, f"taylor:{TAYLOR}")
    async with fixture(auth="oauth", users=USERS) as running:
        run, code = await run_scenario(workspace, running, load(EXAMPLES / "fixture-booking-oauth.yaml"),
                                       script=load(EXAMPLES / "fixture-booking.calls.json"))
        state = oracle(running)
    assert code == 0, run.summary()
    linked = result_of(run)["connector"]["link"]
    assert (linked["mode"], linked["subject"], linked["read_only_supported"]) == ("oauth", subject_for("taylor"), True)
    assert state["confirmed_count"] == 1 and TAYLOR not in every_text(run.path)
