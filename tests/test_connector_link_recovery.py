# ruff: noqa: F811 — the imported pytest fixture
"""A stored OAuth client the authorization server no longer knows (the loopback fixture restarted): linking discards
it and registers again once, records that, and never loops; connector-run and scaffold --auth-from both recover."""
import json

from test_connector_e2e_auth import (  # noqa: F401 — the workspace fixture
    LOGIN,
    TAYLOR,
    USERS,
    oauth,
    workspace,
)
from test_connector_e2e_support import (
    EXAMPLES,
    NATIVE,
    checks_of,
    document,
    fixture,
    result_of,
    run_scenario,
)

from consumer_agent_testing_tool import linking
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.serialization import load

CALLS = load(EXAMPLES / "fixture-booking.calls.json")


def scenario():
    return document(NATIVE, lambda item: oauth(item, profile="generic-sdk"))


def registrations(running):
    return [event for event in running.controls.state()["authorization"]["events"] if event["event"] == "registered"]


async def linked_once(workspace, port=0):
    """A first trial: its link stores the client it registered and its tokens in the workspace's store."""
    async with fixture(auth="oauth", users=USERS, port=port) as running:
        run, code = await run_scenario(workspace, running, scenario(), script=CALLS)
        assert code == 0, run.summary()
        return running.port


async def test_a_restarted_fixture_s_forgotten_client_is_registered_again_once(workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, f"taylor:{TAYLOR}")
    port = await linked_once(workspace)
    # The same fixture on the same port, restarted: its authorization server knows no client.
    async with fixture(auth="oauth", users=USERS, port=port) as running:
        run, code = await run_scenario(workspace, running, scenario(), script=CALLS)
        registered, state = registrations(running), running.controls.state()
    assert code == 0, run.summary()
    assert len(registered) == 1 and state["confirmed_count"] == 1
    connector = result_of(run)["connector"]
    assert connector["link"]["client_registered_again"] is True
    assert {"kind": "client-registered-again", "registration": "dcr",
            "reason": "the authorization server no longer knew the stored client"} in connector["credential_observations"]
    # The store now holds the new client, never the forgotten one.
    stored = [json.loads(path.read_text()) for path in (workspace / ".cache" / "connector-links").iterdir()]
    assert len(stored) == 1 and stored[0]["client_info"]["client_id"] == registered[0]["client_id"]


async def test_scaffold_auth_from_recovers_from_a_forgotten_client_too(workspace, monkeypatch, capsys):
    monkeypatch.setenv(LOGIN, f"taylor:{TAYLOR}")
    port = await linked_once(workspace)
    source = workspace / "oauth.json"
    source.write_text(json.dumps(scenario()).replace("${PORT}", str(port)).replace("${PORT:-8765}", str(port)))
    async with fixture(auth="oauth", users=USERS, port=port) as running:
        code = await execute(parser().parse_args(["scaffold", f"http://127.0.0.1:{port}/mcp", "--auth-from",
                                                  str(source), "--output", str(workspace / "scaffolded.yaml")]))
        registered = registrations(running)
    assert code == 0 and (workspace / "scaffolded.yaml").is_file()
    assert len(registered) == 1


async def test_a_server_that_refuses_the_new_client_too_is_a_registration_error_never_a_loop(workspace, monkeypatch):
    monkeypatch.setenv(LOGIN, f"taylor:{TAYLOR}")
    port = await linked_once(workspace)
    monkeypatch.setattr(linking, "_unknown_client", lambda response: True)  # It forgets every client at once.
    async with fixture(auth="oauth", users=USERS, port=port) as running:
        run, code = await run_scenario(workspace, running, scenario(), script=CALLS)
        registered = registrations(running)
    assert code == 2
    execution = checks_of(run)["execution"]
    assert execution["link_error"] == "registration" and execution["stage"] == "link"
    assert "though this link registered it just now" in execution["detail"]
    assert len(registered) == 1  # Registered again once, then stopped.


def test_only_an_unknown_client_answer_counts():
    import httpx2

    def answer(status, body):
        return httpx2.Response(status, json=body)
    assert linking._unknown_client(answer(400, {"error": "invalid_request",
                                                "error_description": "Client ID 'abc' not found"}))
    assert linking._unknown_client(answer(401, {"error": "invalid_client"}))
    assert not linking._unknown_client(answer(400, {"error": "invalid_request", "error_description": "Missing state"}))
    assert not linking._unknown_client(answer(400, {"error": "invalid_scope", "error_description": "unknown client"}))
    assert not linking._unknown_client(answer(302, {"error": "invalid_client"}))
    assert not linking._unknown_client(httpx2.Response(400, text="<p>Unknown client</p>"))
