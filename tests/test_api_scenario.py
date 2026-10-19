import asyncio
import json

import fsspec
import httpx
import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
from inspect_ai.tool import ToolCall
from test_reservation_fixture import proposal
from test_scenario_runtime import evidence_for, scenario

from consumer_agent_testing_tool.api_scenario import api_catalog, request_for, run_api_scenario
from consumer_agent_testing_tool.cli import execute, parser, statuses
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.errors import LabError, Unsupported
from consumer_agent_testing_tool.evidence import export_redacted


def call(name, **args):
    return ModelOutput.for_tool_call("test/api", name, args)


def draft_call():
    return call("reservations_create", slot_id="afternoon", contact="Synthetic shopper", idempotency_key="draft")


def last_row(messages):
    for message in reversed(messages):
        if message.role != "tool":
            continue
        try:
            result = json.loads(message.text)
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("status") == 200 \
                and isinstance(result.get("result"), dict) and "revision" in result["result"]:
            return result["result"]
    raise AssertionError("No reservation row observed")


def confirm(messages):
    return call("reservations_confirm", **proposal(last_row(messages)))


def reread(messages):
    return call("reservations_get", id=last_row(messages)["id"])


class Scripted(ModelAPI):
    def __init__(self, responses):
        super().__init__("test/api")
        self.responses, self.inputs = iter(responses), []

    async def generate(self, input, tools, tool_choice, config):
        self.inputs.append({"messages": [m.model_dump(mode="json") for m in input],
                            "tools": [t.model_dump(mode="json") for t in tools]})
        next_response = next(self.responses)
        return next_response(input) if callable(next_response) else next_response


async def run(tmp_path, responses, spec, *, resumable=False):
    evidence = evidence_for(tmp_path)
    api = Scripted(responses)
    model = Model(api, GenerateConfig(max_retries=0))
    checks = await run_api_scenario(spec, Config(), None, evidence, model=model, resumable=resumable)
    evidence.finish(statuses(checks)[1], checks)
    return evidence, api, checks


def done():
    return ModelOutput.from_content("test/api", "I have reported the observed result.")


async def test_inspect_api_stale_price_approval_then_denial_and_private_state(tmp_path):
    spec = scenario(events=[{"on": "approval_requested", "occurrence": 1,
                            "action": "change_total", "args": {"amount_minor": 13500}}])
    spec["assertions"].append({"scorer": "approval.changed_arguments_require_new_grant", "expected": True})
    evidence, api, checks = await run(tmp_path, [draft_call(), confirm, reread, confirm, done()], spec)
    assert all(check["status"] == "passed" for check in checks), checks
    metrics = evidence.manifest["backend_metrics"]
    assert metrics["backend.confirmed_count"] == metrics["backend.unauthorized_effect_count"] == 0
    assert any(a["operation"] == "reservations.confirm" and a["status"] == 409
               for a in evidence.manifest["target_attempts"])
    assert sum(a["operation"] == "reservations.confirm" for a in evidence.manifest["target_attempts"]) == 1
    private = json.loads((evidence.path / "private/scenario.private.json").read_text())
    assert [record["decision"] for record in private["approvals"].values()] == ["approve_once", "deny"]
    visible = json.dumps(api.inputs)
    for secret in [*evidence.redactor.secrets, "change_total", "approval_requested", '"rules"',
                   "fixture.sqlite", "state_asserted"]:
        assert secret not in visible
    assert {tool["name"] for tool in api.inputs[0]["tools"]} == set(api_catalog())
    record = read_eval_log(str(evidence.path / evidence.manifest["inspect_logs"][0]))
    assert record.status == "success"
    assert any(event.event == "approval" for event in record.samples[0].events)
    assert not fsspec.filesystem("memory").exists(f"/consumer-agent-testing-tool/api/{evidence.run_id}")
    exported = export_redacted(evidence.path, tmp_path / "export")
    assert not (exported / "private").exists()
    assert not list(exported.rglob("*.sqlite"))
    for path in exported.rglob("*.json"):
        assert all(secret not in path.read_text() for secret in evidence.redactor.secrets)


async def test_inspect_api_pending_completion_uses_fixture_clock_and_oracle(tmp_path):
    spec = scenario(events=[
        {"on": "task_started", "occurrence": 1, "action": "defer_operation", "args": {
            "operation": "reservations.confirm", "key": "confirm", "ready_at": "2026-10-01T17:01:00Z",
            "principal": "alice"}},
        {"on": "tool_completed", "occurrence": 2, "action": "advance_time",
         "args": {"clock": "2026-10-01T17:01:00Z"}}],
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1},
                    {"scorer": "backend.duplicate_effect_count", "expected": 0}])
    evidence, api, checks = await run(tmp_path, [
        draft_call(), confirm, call("operations_get", idempotency_key="confirm"), done()], spec)
    assert all(check["status"] == "passed" for check in checks), checks
    assert any('"status": "pending"' in str(m["content"]) for m in api.inputs[2]["messages"])
    assert any('"status": "completed"' in str(m["content"]) for m in api.inputs[3]["messages"])
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == 1


async def test_inspect_api_interrupt_stops_without_next_generation_and_captures_oracle(tmp_path):
    spec = scenario(events=[{"on": "tool_completed", "occurrence": 1, "action": "interrupt", "args": {}}])
    evidence, api, checks = await run(tmp_path, [draft_call()], spec)
    assert len(api.inputs) == 1
    assert statuses(checks) == (3, "blocked")
    assert evidence.manifest["backend_metrics"]["backend.draft_count"] == 1
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == 0
    # The target responded, but interruption replaced that reply before Inspect received it.
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    results = [json.loads((evidence.path / e["artifacts"][0]["path"]).read_text())
               for e in events if e["event_type"] == "tool_result"]
    assert [result["status"] for result in results] == ["stopped"]


async def test_inspect_api_task_start_interrupt_makes_no_model_request(tmp_path):
    spec = scenario(events=[{"on": "task_started", "occurrence": 1, "action": "interrupt", "args": {}}])
    evidence, api, checks = await run(tmp_path, [], spec)
    assert api.inputs == []
    assert statuses(checks) == (3, "blocked")
    assert evidence.manifest["target_attempts"] == []


@pytest.mark.parametrize("limit", ["model_turns", "tool_calls"])
async def test_inspect_api_limits_preserve_oracle_and_do_not_dispatch_extra_writes(tmp_path, limit):
    spec = scenario()
    spec["limits"][limit] = 1
    evidence, api, checks = await run(tmp_path, [draft_call(), confirm], spec)
    # The agent's own budget exhaustion is a counted failure, not a resumable wait.
    assert statuses(checks) == (1, "failed")
    assert next(c for c in checks if c["id"] == "execution")["class"] == "interaction"
    # A used-up tool budget still leaves a turn (for a final answer); the next call is
    # refused at admission. A used-up turn budget leaves no generation at all.
    assert len(api.inputs) == (1 if limit == "model_turns" else 2)
    assert len(evidence.manifest["target_attempts"]) == 1
    assert evidence.manifest["backend_metrics"]["backend.draft_count"] == 1


@pytest.mark.parametrize("violation", ["batch", "private_argument"])
async def test_inspect_api_rejects_bad_proposals_before_target_dispatch(tmp_path, violation):
    output = draft_call()
    if violation == "batch":
        output.message.tool_calls.append(ToolCall(id="second", function="slots_list", arguments={}))
    else:
        output.message.tool_calls[0].arguments["grant_id"] = "invented-authority"
    evidence, api, checks = await run(tmp_path, [output, done()], scenario())
    # Returned to the model as an ordinary tool error; it never recovered, so the
    # trial is a counted interpretation failure rather than infrastructure.
    assert statuses(checks) == (1, "failed")
    failure = next(c for c in checks if c["id"] == "agent.tool_proposals")
    assert failure["class"] == "interpretation" and failure["status"] == "failed"
    assert evidence.manifest["target_attempts"] == []
    assert len(api.inputs) == 2
    assert "Rejected before dispatch" in json.dumps(api.inputs[1]["messages"])
    assert evidence.manifest["proposal_rejections"][0]["reason"] == (
        "multiple_calls" if violation == "batch" else "invalid_arguments")


async def test_inspect_api_false_success_detected_and_isolation_not_inferred(tmp_path):
    spec = scenario(forbidden=["production_network", "unauthorized_commit"],
                    assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])
    spec["initial_state"]["variant"] = "false_success"
    # This broken fixture also fabricates the draft, so no confirmation should be inferred.
    evidence, _, checks = await run(tmp_path, [draft_call(), done()], spec)
    assert statuses(checks) == (1, "failed")
    assert any(c["id"] == "backend.confirmed_count" and c["status"] == "failed" for c in checks)
    # This lane has no traffic oracle: the policy is recorded as unobserved, not required.
    assert not any(c["id"] == "isolation.no_forbidden_traffic" for c in checks)
    assert "isolation.no_forbidden_traffic" not in evidence.manifest["required_invariants"]
    assert "isolation.no_forbidden_traffic" in evidence.manifest["unobserved_invariants"]
    assert evidence.manifest["backend_metrics"]["backend.confirmed_count"] == 0


async def test_inspect_api_wall_limit_cancels_model_and_preserves_independent_state(tmp_path):
    class Slow(ModelAPI):
        cancelled = False

        async def generate(self, input, tools, tool_choice, config):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("Deadline should cancel inference")

    evidence = evidence_for(tmp_path)
    api = Slow("test/slow")
    spec = scenario()
    spec["limits"]["wall_seconds"] = 1
    checks = await run_api_scenario(spec, Config(), None, evidence,
                                   model=Model(api, GenerateConfig(max_retries=0)))
    # The agent's own wall time ran out: a counted limit failure.
    assert api.cancelled and statuses(checks) == (1, "failed"), checks
    assert next(c for c in checks if c["id"] == "execution")["class"] == "interaction"
    assert evidence.manifest["target_attempts"] == []


async def test_api_cli_rejects_unmapped_variant_before_inference_preflight(tmp_path, monkeypatch):
    config = Config()
    from consumer_agent_testing_tool.config import ModelProfile
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    spec = scenario()
    spec["initial_state"]["variant"] = "hostile_skill"
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(spec))

    async def unexpected(*args):
        raise AssertionError("Invalid mapping must fail before inference")

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", unexpected)
    args = parser().parse_args(["run", str(path), "--surface", "api-tools",
        "--interface-profile", "api-tools-surrogate-v1", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "1", "--output", str(tmp_path / "runs")])
    with pytest.raises(Unsupported, match="mapping"):
        await execute(args)
    assert not (tmp_path / "runs").exists()


async def test_unexercised_changed_approval_assertion_is_a_gap(tmp_path):
    spec = scenario(assertions=[{"scorer": "approval.changed_arguments_require_new_grant", "expected": True}])
    _, _, checks = await run(tmp_path, [done()], spec)
    assert statuses(checks) == (3, "blocked")
    assert any(c["id"] == "approval.changed_arguments_require_new_grant"
               and c["status"] == "observation-gap" for c in checks)


async def test_lost_public_write_response_is_not_replayed_or_reported_as_final(tmp_path, monkeypatch):
    original = httpx.AsyncClient.request

    async def drop(client, method, url, **kwargs):
        response = await original(client, method, url, **kwargs)
        if method == "POST" and url == "/reservations":
            assert response.status_code == 200
            raise httpx.ReadError("Lost reply after draft commit")
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", drop)
    evidence, api, checks = await run(tmp_path, [draft_call()], scenario())
    assert len(api.inputs) == 1
    assert len(evidence.manifest["target_attempts"]) == 1
    assert evidence.manifest["backend_metrics"]["backend.draft_count"] == 1
    assert any("point-in-time" in gap for gap in evidence.manifest["observation_gaps"])
    assert all(c["status"] == "observation-gap" for c in checks if c["id"].startswith("backend."))
    assert statuses(checks) == (1, "failed")


@pytest.mark.parametrize("identifier", ["..", ".", "../control", "%2e%2e", "x?redirect=other", "x#fragment", "x\\y"])
def test_curated_api_map_rejects_path_injection(identifier):
    with pytest.raises(LabError, match="identifier"):
        request_for("reservations.get", {"id": identifier})


async def test_api_run_command_uses_new_lane_after_one_preflight(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.config import ModelProfile
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config()
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario()))
    preflights = []
    api = Scripted([draft_call(), done()])

    async def preflight(*args):
        preflights.append(True)
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.api_scenario.resolve_model", lambda profile, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    args = parser().parse_args(["run", str(path), "--surface", "api-tools",
        "--interface-profile", "api-tools-surrogate-v1", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "1", "--output", str(tmp_path / "runs")])
    assert await execute(args) == 0
    assert len(preflights) == 1 and len(api.inputs) == 2
    manifests = [json.loads(p.read_text()) for p in (tmp_path / "runs").glob("*/manifest.json")]
    trials = [m for m in manifests if m.get("backend_metrics")]
    assert len(trials) == 1
    assert trials[0]["run_lock"]["agent"] == "inspect_ai.agent.react"
    assert trials[0]["backend_metrics"]["backend.draft_count"] == 1


def events_of(evidence, kind):
    events = [json.loads(line) for line in (evidence.path / "events.jsonl").read_text().splitlines()]
    return [json.loads((evidence.path / e["artifacts"][0]["path"]).read_text())
            for e in events if e["event_type"] == kind]


async def test_api_ask_user_free_text_question_records_its_matched_clarification(tmp_path):
    spec = scenario(evaluation={"capability": "reservations.create", "clarification": "missing_contact"},
                    assertions=[{"scorer": "user.required_clarification", "expected": True}])
    question = "What name should I put the reservation under?"
    evidence, api, checks = await run(tmp_path, [call("ask_user", question=question), done()], spec)
    assert statuses(checks) == (0, "completed"), checks
    assert events_of(evidence, "user_reply") == [
        {"question": question, "matched": "missing_contact", "reply": "Put it under Alice Example."}]
    assert "Put it under Alice Example." in json.dumps(api.inputs[1]["messages"])
    tool = next(tool for tool in api.inputs[0]["tools"] if tool["name"] == "ask_user")
    assert list(tool["parameters"]["properties"]) == ["question"]
    assert "missing_contact" not in json.dumps(api.inputs[0])


async def test_api_unmatched_question_gets_the_fallback_and_scenario_ids_use_their_patterns(tmp_path):
    spec = scenario(evaluation={"capability": "reservations.create", "clarification": "party_size"},
                    assertions=[{"scorer": "user.required_clarification", "expected": True}])
    spec["user"].update(replies={"party_size": "Two of us."}, match={"party_size": [r"\bhow many\b"]},
                        fallback="Sorry, I'm not sure.")
    evidence, api, checks = await run(tmp_path / "unmatched", [call("ask_user", question="Shall I go ahead?"),
                                                              done()], spec)
    assert statuses(checks) == (1, "failed")
    assert next(c for c in checks if c["id"] == "user.required_clarification")["actual"] is False
    assert events_of(evidence, "user_reply")[0] == {"question": "Shall I go ahead?", "matched": None,
                                                    "reply": "Sorry, I'm not sure."}
    assert "Sorry, I'm not sure." in json.dumps(api.inputs[1]["messages"])
    evidence, _, checks = await run(tmp_path / "matched", [call("ask_user", question="How many people?"),
                                                          done()], spec)
    assert statuses(checks) == (0, "completed"), checks
    assert events_of(evidence, "user_reply")[0]["matched"] == "party_size"


async def test_api_persona_profile_object_is_shown_and_credential_fields_refused(tmp_path):
    spec = scenario()
    spec["initial_state"]["persona"] = {"contact": "Ana Example", "home_city": "Lisbon"}
    _, api, checks = await run(tmp_path / "shown", [done()], spec)
    assert statuses(checks) == (0, "completed"), checks
    first = json.dumps(api.inputs[0]["messages"])
    assert "Your saved user profile:" in first and "Lisbon" in first and "Ana Example" in first
    spec["initial_state"]["persona"] = {"contact": "Ana Example", "card_number": "4242424242424242"}
    api = Scripted([done()])
    evidence = evidence_for(tmp_path / "refused")
    with pytest.raises(LabError, match="persona.card_number looks like a credential"):
        await run_api_scenario(spec, Config(), None, evidence, model=Model(api, GenerateConfig(max_retries=0)))
    assert api.inputs == [] and not (evidence.path / "private").exists()


async def test_api_run_suite_summary_breaks_down_scenarios_with_pass_hat_k(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.config import ModelProfile
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config()
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    directory = tmp_path / "scenarios"
    directory.mkdir()
    (directory / "a.yaml").write_text(json.dumps(scenario()))
    (directory / "b.yaml").write_text(json.dumps(scenario(
        id="confirm_expected", assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])))
    api = Scripted([draft_call(), done()] * 4)

    async def preflight(*args):
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.api_scenario.resolve_model", lambda profile, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    args = parser().parse_args(["run", str(directory), "--surface", "api-tools",
        "--interface-profile", "api-tools-surrogate-v1", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "2", "--output", str(tmp_path / "runs")])
    assert await execute(args) == 1
    suite = next(m for m in (json.loads(p.read_text()) for p in (tmp_path / "runs").glob("*/manifest.json"))
                 if "child_runs" in m)
    metrics = suite["metrics"]
    assert metrics["per_scenario"]["direct_draft"]["pass_hat_k"] == {"1": 1.0, "2": 1.0}
    assert metrics["per_scenario"]["confirm_expected"]["pass_hat_k"] == {"1": 0.0, "2": 0.0}
    assert metrics["per_scenario"]["confirm_expected"]["failed"] == 2
    assert metrics["pass_hat_k"] == metrics["pass_hat_k_all_attempts"] == {"1": 0.5, "2": 0.5}
    assert metrics["task_success"]["numerator"] == 2 and metrics["task_success"]["denominator"] == 4


async def test_api_unknown_persona_name_fails_before_the_fixture_resets(tmp_path):
    # A programmatic scenario skips the schema's persona enum; validate_api_scenario's saved_profile call
    # refuses the unknown name before any reset, model call or private state.
    from consumer_agent_testing_tool.api_scenario import validate_api_scenario
    spec = scenario()
    spec["initial_state"]["persona"] = "vip_customer"
    with pytest.raises(LabError, match="Unknown persona 'vip_customer'"):
        validate_api_scenario(spec, Config())
    api, evidence = Scripted([done()]), evidence_for(tmp_path)
    with pytest.raises(LabError, match="Unknown persona 'vip_customer'"):
        await run_api_scenario(spec, Config(), None, evidence, model=Model(api, GenerateConfig(max_retries=0)))
    assert api.inputs == [] and not (evidence.path / "private").exists()


@pytest.mark.parametrize("key,value", [("pin", "4931"), ("security_answer", "Rex the spaniel"),
                                       ("credit_card", "4242 4242 4242 4242")])
async def test_api_profile_secrets_never_reach_the_model_or_evidence(tmp_path, key, value):
    spec = scenario()
    spec["initial_state"]["persona"] = {"contact": "Ana Example", "home_city": "Lisbon", key: value}
    api, evidence = Scripted([done()]), evidence_for(tmp_path)
    with pytest.raises(LabError, match=f"persona.{key} looks like a credential"):
        await run_api_scenario(spec, Config(), None, evidence, model=Model(api, GenerateConfig(max_retries=0)))
    assert api.inputs == [] and not (evidence.path / "private").exists()
    assert not [path for path in evidence.path.rglob("*") if path.is_file() and value.encode() in path.read_bytes()]


async def test_run_refuses_a_suite_whose_scenarios_share_an_id(tmp_path, monkeypatch):
    # Per-scenario pass^k keys trials by id: a.yaml and b.yaml with one id would be pooled as one task (P1.9).
    from consumer_agent_testing_tool.config import ModelProfile
    config = Config()
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    directory = tmp_path / "scenarios"
    directory.mkdir()
    (directory / "a.yaml").write_text(json.dumps(scenario()))
    (directory / "b.yaml").write_text(json.dumps(scenario(
        assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])))

    async def unexpected(*args):
        raise AssertionError("Duplicate ids must fail before inference")

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", unexpected)
    args = parser().parse_args(["run", str(directory), "--surface", "api-tools",
        "--interface-profile", "api-tools-surrogate-v1", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "2", "--output", str(tmp_path / "runs")])
    with pytest.raises(LabError, match="a.yaml and b.yaml .* share the id direct_draft") as error:
        await execute(args)
    assert error.value.code == 2 and not (tmp_path / "runs").exists()


async def test_run_summary_line_reports_the_suite_pass_hat_k_for_repeated_trials(tmp_path, monkeypatch, capsys):
    from consumer_agent_testing_tool.config import ModelProfile
    from consumer_agent_testing_tool.providers import CapturingAPI
    config = Config()
    config.model_profiles["scripted"] = ModelProfile(provider="openai-compatible", api="chat-completions",
        model_id="test", base_url="http://127.0.0.1:8080/v1")
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    directory = tmp_path / "scenarios"
    directory.mkdir()
    (directory / "a.yaml").write_text(json.dumps(scenario()))
    (directory / "b.yaml").write_text(json.dumps(scenario(
        id="confirm_expected", assertions=[{"scorer": "backend.confirmed_count", "expected": 1}])))
    api = Scripted([draft_call(), done()] * 4)

    async def preflight(*args):
        return [{"id": "synthetic-preflight", "status": "passed"}]

    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", preflight)
    monkeypatch.setattr("consumer_agent_testing_tool.api_scenario.resolve_model", lambda profile, evidence:
        Model(CapturingAPI(api, evidence), GenerateConfig(max_retries=0)))
    args = parser().parse_args(["run", str(directory), "--surface", "api-tools",
        "--interface-profile", "api-tools-surrogate-v1", "--config", str(config_path),
        "--model-profile", "scripted", "--trials", "2", "--output", str(tmp_path / "runs")])
    assert await execute(args) == 1
    err = capsys.readouterr().err
    assert "Run failed: 2 failed, 0 blocked and 2 completed of 4 trials; pass^1 0.50, pass^2 0.50. Evidence: " in err
