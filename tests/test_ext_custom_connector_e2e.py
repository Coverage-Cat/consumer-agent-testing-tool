"""The custom-connector lane end to end: the shipped example, an agent (mockllm) that reads the docs and
books without ever seeing the key, a service whose scheme is declined, and documentation that leaves the
key's placement out."""
import json

import pytest
from inspect_ai.model import ModelOutput
from test_connector_e2e_support import FRAMING, Script, answer, call, framing, output
from test_ext_custom_connector_support import (
    ENV,
    KEY,
    QUOTE,
    SIGNED,
    SIGNED_CALLS,
    STAY,
    checks_of,
    document,
    events,
    extension_config,
    loaded,
    public_text,
    requests_seen,
    result_of,
    run_custom,
    service,
)

from consumer_agent_testing_tool.connector_run import ScriptedDriver, run_trials
from consumer_agent_testing_tool.linking import SURROGATE
from consumer_agent_testing_tool.replay import playback
from consumer_agent_testing_tool.rest_fixture import RestConfig, write_docs
from consumer_agent_testing_tool.serialization import load


@pytest.mark.parametrize("emulation_target", ["muse", "instinct"])
async def test_the_scripted_example_books_through_a_custom_connector(tmp_path, monkeypatch, emulation_target):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        run, code = await run_custom(tmp_path, running, document(lambda doc: doc.update(emulation_target=emulation_target)))
        state = running.controls.state()
    assert code == 0, run.summary()
    assert playback(run.path)["integrity"] == "verified"
    checks = checks_of(run)
    assert {check["status"] for check in checks.values()} == {"passed"}, checks
    assert {"custom_connector.dispatches_match", "custom_connector.disclosure_bound",
            "custom_connector.auth_compatibility"} <= set(checks)
    assert state["confirmed_count"] == 1 and state["bookings"][0]["idempotency_key"] == "scripted-smoke-1"
    assert [entry["auth"] for entry in state["requests"]] == ["ok"] * 4
    result = result_of(run)
    metrics, custom = result["metrics"], result["metrics"]["extensions"]["custom_connector"]
    assert metrics["task_success"] is True and metrics["dispatches"] == 0  # No MCP connector acted.
    assert custom["first_call"]["operation"] == "GET /v1/me" and custom["first_call_success"] is True
    assert custom["verified_with_read"] is True and custom["calls_before_first_success"] == 0
    assert custom["doc_fetches"] == {"read_doc": 1, "listings": 1, "http": 0, "documents": ["llms.txt"]}
    assert custom["auth_compatibility"] == {"documented_scheme": "api_key", "compatible": True, "findings": []}
    assert custom["refused_egress"]["count"] == 0 and custom["http"]["classes"] == {
        "read": 2, "write": 1, "sensitive-write": 1}
    assert custom["approval"]["prompts"] == {"read": 2, "write": 2, "sensitive-write": 1}
    assert [entry["name"] for entry in result["run_lock"]["extensions"]] == [
        "clock", "custom_connector", *(["presentation"] if emulation_target == "instinct" else [])]
    setup = events(run, "custom_connector.setup")[0]
    assert setup["docs"]["files"][1] == {"path": "llms.txt", "bytes": 846}
    assert setup["bases"]["request-api-access"]["basis"] == ("unofficial" if emulation_target == "muse" else "assumed")
    card = next(row for row in events(run, "custom_connector.approval_card") if row["tool"] == "POST /v1/bookings")
    assert card["disclosure"]["total"] == 44836 and card["disclosure_sources"]["total"]["match"]
    assert KEY not in public_text(run.path)
    # Kept privately for export re-redaction, like every host-private value.
    assert KEY in json.loads((run.path / "private" / "connector.private.json").read_text())["secrets"]


def reference(messages):
    """The access reference the model was given (it copies it into its next requests)."""
    for message in messages:
        if message.role == "tool" and (found := SURROGATE.search(str(message.content))):
            return found[0]
    raise AssertionError("no access reference in the conversation")


async def test_an_agent_reads_the_docs_and_books_without_seeing_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        base = running.base_url

        def http(method, path, **extra):
            return lambda messages: call("http_request", method=method, url=f"{base}{path}",
                                         headers={"X-Cedar-Key": reference(messages), **extra.pop("headers", {})},
                                         **extra)
        script = Script(
            call("read_doc", path="llms.txt"),
            output(ModelOutput.for_tool_call("mockllm/model", "request_api_access", {
                "service": "cedar-stays-api", "scheme": "api_key", "placement": "header", "name": "X-Cedar-Key"})),
            http("GET", f"/v1/rooms?{STAY}"),
            http("POST", "/v1/quotes", json=QUOTE),
            http("POST", "/v1/bookings", headers={"Idempotency-Key": "agent-booking-1"},
                 json={"quote_id": "q-0001", "guest_name": "Taylor Example"}),
            answer("Booked: the Garden Queen at Cedar Stays for December 4 to 6, 2026, confirmation b-0001, "
                   "$448.36 USD in total including taxes and fees."))
        run, code = await run_custom(tmp_path, running, driver=script.driver())
        state = running.controls.state()
    assert code == 0, run.summary()
    assert state["confirmed_count"] == 1
    visible = script.visible()
    assert KEY not in visible and not framing(visible), framing(visible)
    first = script.requests[0]
    assert {"read_doc", "request_api_access", "http_request", "ask_user"} <= set(first["tools"])
    system = first["messages"][0]["content"]
    assert "public HTTP API" in system and "read_doc" in system and "cedar-stays-api" in system
    assert "Connected services" not in system  # The Cedar Stays connector is not connected.
    results = script.tool_results()
    assert results[0]["path"] == "llms.txt" and "X-Cedar-Key header" in results[0]["content"]
    assert results[1]["granted"] is True and results[-1]["json"]["status"] == "confirmed"
    custom = result_of(run)["metrics"]["extensions"]["custom_connector"]
    assert custom["first_authenticated_call"]["operation"] == "GET /v1/rooms"
    assert custom["verified_with_read"] is True and custom["http"]["sent"] == 3
    assert not FRAMING.search(json.dumps([row.get("content") for row in events(run, "tool_result")]))


async def test_a_service_whose_scheme_is_declined_is_unsupported_and_the_agent_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)  # A declined scheme needs no credential.
    async with service(auth="signed") as running:
        run, code = await run_custom(tmp_path, running, load(SIGNED), steps=load(SIGNED_CALLS))
        seen = requests_seen(running)
    checks = checks_of(run)
    assert code == 3 and result_of(run)["status"] == "blocked"
    assert checks["custom_connector.auth_compatibility"]["status"] == "unsupported"
    assert checks["nothing-booked"]["status"] == "passed" and checks["answer.relay_complete"]["status"] == "passed"
    assert seen == []  # Nothing reached the API.
    custom = result_of(run)["metrics"]["extensions"]["custom_connector"]
    assert [(row["rule"], row["severity"]) for row in custom["auth_compatibility"]["findings"]] == [
        ("auth.declined-scheme", "fail"), ("auth.declined-scheme-requested", "info")]
    assert custom["auth_compatibility"]["compatible"] is False and custom["access"]["outcomes"] == {
        "declined-scheme": 1}
    reply = json.loads(next(row["content"] for row in events(run, "tool_result")
                            if row["tool"] == "request_api_access"))
    assert reply["granted"] is False and "signed requests" in reply["message"]


async def test_documentation_that_leaves_out_the_key_s_header_costs_the_first_call(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    write_docs(tmp_path / "docs" / "vague", RestConfig(docs="vague-auth"))

    def vague(scenario):
        extension_config(scenario)["docs"] = "docs/vague"
        scenario["checks"] = [{"id": "nothing-booked", "source": "oracle", "expect": {"op": "eq", "value": 0},
                               "oracle": {"url": "http://127.0.0.1:${PORT}/__oracle/state",
                                          "pointer": "/confirmed_count"}}]
        scenario["answer"] = None

    def guess(header, step):
        return [{"tool": "request_api_access", "arguments": {"service": "cedar-stays-api", "scheme": "api_key",
                                                             "placement": "header", "name": header}},
                {"tool": "http_request", "arguments": {"method": "GET", "url": "http://127.0.0.1:8790/v1/me",
                                                       "headers": {header: {"$ref": f"/results/{step}/reference"}}}}]
    # The documentation says "your API key" and never where it goes: the agent can only guess.
    steps = [{"tool": "read_doc", "arguments": {"path": "docs/api.md"}}, *guess("X-API-Key", 1), *guess("Api-Key", 3),
             {"answer": "I could not connect to Cedar Stays: its API did not accept the key."}]
    async with service(docs="vague-auth") as running:
        run, code = await run_custom(tmp_path, running, document(vague), steps=steps, base=tmp_path)
        seen = requests_seen(running)
    assert code == 0, run.summary()
    assert seen == [("GET", "/v1/me", "misplaced"), ("GET", "/v1/me", "misplaced")]
    custom = result_of(run)["metrics"]["extensions"]["custom_connector"]
    assert custom["first_call_success"] is False and custom["calls_before_first_success"] is None
    assert custom["access"]["granted"] == 2 and custom["access"]["matches_service"] is False
    rules = [row["rule"] for row in custom["auth_compatibility"]["findings"]]
    assert rules.count("auth.placement-mismatch") == 2 and "auth.credential-rejected" in rules
    gap = checks_of(run)["custom_connector.auth_compatibility"]
    assert gap["status"] == "observation-gap" and gap["required"] is False


async def test_every_trial_starts_with_a_fresh_vault_engine_and_service(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    async with service() as running:
        from test_ext_custom_connector_support import script
        code, runs, summary = await run_trials(loaded(document(), running), driver=ScriptedDriver(
            script(running.base_url)), trials=2, root=tmp_path, output=tmp_path / "runs")
    assert code == 0 and summary["pass_hat_k"] == {"1": 1.0, "2": 1.0}
    references = [next(json.loads(row["content"])["reference"] for row in events(run, "tool_result")
                       if row["tool"] == "request_api_access") for run in runs]
    assert len(set(references)) == 2  # A reference from one trial means nothing in the next.
    assert all(result_of(run)["metrics"]["extensions"]["custom_connector"]["approval"]["prompts"]["read"] == 2
               for run in runs)
