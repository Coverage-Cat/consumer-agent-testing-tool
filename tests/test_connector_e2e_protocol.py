"""Connector lane end to end: the unofficial Muse template client against servers it cannot drive.

needs-redirect (the published endpoint answers 307) is protocol evidence and blocks the trial;
needs-session (quotes live in an MCP session) shows only in tool text, so it gets a labeled,
non-blocking hint, and a domain "session" (a yoga class) gets none. The official SDK baseline
(generic-sdk) follows the same-origin redirect and keeps one session, and books.
"""
import pytest
from test_connector_e2e_support import (
    NATIVE,
    Script,
    answer,
    checks_of,
    document,
    fixture,
    happy_path,
    oracle,
    result_of,
    run_scenario,
)

from consumer_agent_testing_tool.connector_run import ConnectorRun, ScriptedDriver
from consumer_agent_testing_tool.connector_scenario import parse_scenario

TEMPLATE = "muse-template-unofficial-2026-10"


def notes(run, kind):
    return [note for note in result_of(run)["connector"]["client_observations"] if note["kind"] == kind]


async def test_the_template_refuses_a_redirecting_endpoint_before_any_model_turn(tmp_path):
    script = Script(answer("unused"))
    async with fixture(variant="needs-redirect") as running:
        run, code = await run_scenario(tmp_path, running, document(NATIVE), driver=script.driver(),
                                       client_profile=TEMPLATE)
        state = oracle(running)
    assert code == 3 and script.requests == []
    checks = checks_of(run)
    assert checks["execution"]["stage"] == "connect" and "follows no redirects" in checks["execution"]["detail"]
    flagged = checks["client.compatibility"]
    assert flagged["status"] == "unsupported" and flagged["observed"] == ["redirect-refused"]
    assert "follows no redirects" in flagged["detail"] and "unofficial mirror" in flagged["detail"]
    refused = notes(run, "redirect-refused")[0]
    assert refused["status"] == 307 and refused["basis"] == "unofficial" and refused["location"].endswith("/mcp/")
    assert [check["id"] for check in result_of(run)["checks"]][:2] == ["execution", "client.compatibility"]
    assert state["http"]["redirected"] >= 1 and "tools/call" not in state["rpc"]


async def test_the_template_loses_session_bound_state_and_the_trial_says_why(tmp_path):
    final = "I could not book the room: Cedar Stays said the quote belongs to an earlier session."
    script = Script(*happy_path(final))
    async with fixture(variant="needs-session") as running:
        run, code = await run_scenario(tmp_path, running, document(NATIVE), driver=script.driver(),
                                       client_profile=TEMPLATE)
        state = oracle(running)
    assert code == 1  # Nothing was booked.
    refused = script.tool_results()[-1]["result"]
    assert refused["isError"] is True and "session_required" in refused["content"][0]
    checks = checks_of(run)
    # Tool text is no protocol evidence: a labeled, non-blocking hint says why, beside the failed outcome.
    assert "client.compatibility" not in checks
    hint = checks["client.session_hint"]
    assert hint["status"] == "observation-gap" and hint["required"] is False and hint["observed"] == ["session-hint"]
    assert "new session for every call" in hint["detail"] and hint["basis"].startswith("assumed")
    assert checks["one-confirmed-booking"]["status"] == "failed"
    assert result_of(run)["detail"].startswith("one-confirmed-booking: expected")  # The CLI line's own reason.
    note = notes(run, "session-hint")[0]
    assert note["tool"] == "create_booking" and note["basis"] == "assumed"  # A labeled heuristic.
    assert state["confirmed_count"] == 0 and state["sessions"] >= 4


async def test_a_domain_session_error_is_not_an_mcp_session_incompatibility(tmp_path):
    from test_mcp_clients_support import FakeMcp

    with FakeMcp(pages=[["domain_session"]]) as fake:
        origin = fake.base
        scenario = {"version": 1, "kind": "connector", "emulation_target": "muse", "id": "yoga", "goal": "Book me into the morning class.",
                    "connectors": [{"name": "lotus-yoga", "endpoint": fake.url, "allowed_origins": [origin],
                                    "client_profile": TEMPLATE, "tools": {"allow": ["domain_session"]}}],
                    "permissions": {"rules": [{"id": "reads", "decision": "allow_once",
                                               "when": {"field": "tool", "op": "eq", "value": "domain_session"}}]}}
        script = [{"tool": "domain_session", "arguments": {"text": "s-100"}},
                  {"answer": "The class is full, so nothing was booked."}]
        run = ConnectorRun(parse_scenario(scenario, base=tmp_path), driver=ScriptedDriver(script), root=tmp_path,
                           output=tmp_path / "runs")
        code = await run.run()
    # "That session is full." is a yoga class, not MCP state: no hint and nothing blocking.
    assert code == 0, run.summary()
    checks = checks_of(run)
    assert "client.compatibility" not in checks and "client.session_hint" not in checks
    assert result_of(run)["connector"]["hints"] == [] and result_of(run)["connector"]["incompatible"] == []


@pytest.mark.parametrize("profile", [TEMPLATE, "generic-sdk"])
async def test_a_stateless_server_s_http_400_session_error_is_not_an_mcp_session_requirement(tmp_path, profile):
    from test_mcp_clients_support import FakeMcp

    # A stateless server (no Mcp-Session-Id, ever) answers a full yoga session with HTTP 400 and a JSON-RPC
    # error, as some web frameworks do: the template never sent a session, so this is no protocol evidence.
    with FakeMcp(pages=[["list_sessions", "domain_session_400"]], session=False) as fake:
        scenario = {"version": 1, "kind": "connector", "emulation_target": "muse", "id": "yoga", "goal": "Book me into the morning class.",
                    "connectors": [{"name": "lotus-yoga", "endpoint": fake.url, "allowed_origins": [fake.base],
                                    "client_profile": profile,
                                    "tools": {"allow": ["list_sessions", "domain_session_400"],
                                              "classes": {"domain_session_400": "write"}}}],
                    "permissions": {"rules": [{"id": "all", "decision": "allow_once",
                                               "when": {"field": "tool", "op": "exists", "value": True}}]},
                    "checks": [{"id": "studio-reachable", "source": "tool",
                                "tool": {"name": "list_sessions", "pointer": "/structuredContent/echo"},
                                "expect": {"op": "exists", "value": True}}]}
        script = [{"tool": "list_sessions", "arguments": {}},
                  {"tool": "domain_session_400", "arguments": {"text": "s-100"}},
                  {"answer": "The morning class is full, so nothing was booked."}]
        run = ConnectorRun(parse_scenario(scenario, base=tmp_path), driver=ScriptedDriver(script), root=tmp_path,
                           output=tmp_path / "runs")
        code = await run.run()
    assert code == 0, run.summary()
    checks = checks_of(run)
    assert "client.compatibility" not in checks and "client.session_hint" not in checks
    assert result_of(run)["connector"]["incompatible"] == [] and not notes(run, "needs-session")
    assert [call["name"] for call in fake.calls].count("domain_session_400") == 1 and fake.sessions == []


@pytest.mark.parametrize("variant", ["needs-redirect", "needs-session"])
async def test_the_generic_sdk_baseline_books_on_the_same_servers(tmp_path, variant):
    script = Script(*happy_path())
    async with fixture(variant=variant) as running:
        run, code = await run_scenario(tmp_path, running, document(NATIVE), driver=script.driver())
        state = oracle(running)
    assert code == 0, run.summary()
    assert state["confirmed_count"] == 1 and "client.compatibility" not in checks_of(run)
    result = result_of(run)
    assert result["treatment"] == "native/in-prompt/generic-sdk" and result["connector"]["incompatible"] == []
    if variant == "needs-redirect":
        assert notes(run, "redirect")[0]["followed"] is True
    else:
        # A session-dependent server is a handshake-era server: the SDK falls back to initialize.
        assert result["run_lock"]["server"]["protocol_version"] == "2025-11-25"
        assert notes(run, "negotiated")[0]["era"] == "handshake" and notes(run, "session-issued")


async def test_a_server_request_the_template_never_answers_is_a_compatibility_problem(tmp_path):
    from test_mcp_clients_support import FakeMcp

    from consumer_agent_testing_tool.connector_run import Services
    from consumer_agent_testing_tool.mcp_clients import PROFILES
    with FakeMcp(pages=[["elicit"]], delay=2.0) as fake:
        scenario = {"version": 1, "kind": "connector", "emulation_target": "muse", "id": "notes", "goal": "Send my note to Sam.",
                    "connectors": [{"name": "notes", "endpoint": fake.url, "allowed_origins": [fake.base],
                                    "client_profile": TEMPLATE,
                                    "tools": {"allow": ["elicit"], "classes": {"elicit": "write"}}}],
                    "permissions": {"rules": [{"id": "send", "decision": "allow_once",
                                               "when": {"field": "tool", "op": "eq", "value": "elicit"}}]}}
        services = Services(client_profile=lambda name: PROFILES[name].model_copy(update={"timeout_seconds": 0.5}))
        run = ConnectorRun(parse_scenario(scenario, base=tmp_path), services=services, root=tmp_path,
                           driver=ScriptedDriver([{"tool": "elicit", "arguments": {"text": "hi"}},
                                                  {"answer": "I could not tell whether the note was sent."}]),
                           output=tmp_path / "runs")
        code = await run.run()
    checks = checks_of(run)
    # The server waited for an elicitation answer the POST-only template never sends: the profile's limit.
    assert code == 3 and checks["client.compatibility"]["observed"] == ["server-request"]
    assert "never answers" in checks["client.compatibility"]["detail"]
    assert checks["reconcile.elicit.1"]["status"] == "blocked"  # The write may still have happened.
