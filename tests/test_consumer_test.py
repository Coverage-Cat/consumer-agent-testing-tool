import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai import Task, eval_async
from inspect_ai.agent import as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.model import GenerateConfig, Model, ModelAPI, ModelOutput
from mcp import types

from consumer_agent_testing_tool.config import interface_profile
from consumer_agent_testing_tool.consumer_test import (
    ConsumerBoundary,
    consumer_checkpoint,
    consumer_origin,
    local_url,
)
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.providers import CapturingAPI

TEST_ORIGIN = "https://consumer-fixture.invalid"


def boundary(tmp_path):
    shopper = json.loads(Path("examples/coveragecat-fake-shopper.json").read_text())
    evidence = Evidence(tmp_path, lane="contract", label="protocol-tested",
                        profile=interface_profile("mcp-contract-2025-11-25"), lock={})
    return ConsumerBoundary(shopper, evidence, stop_after_prefill=True)


def test_origin_and_tool_gate_rejects_production_and_operator_routes(tmp_path):
    value = boundary(tmp_path)
    local_url(TEST_ORIGIN + "/api/consumer/mcp", TEST_ORIGIN)
    for url in ("https://coveragecat.com/api/consumer/mcp", "https://other.invalid/mcp"):
        with pytest.raises(LabError):
            local_url(url, TEST_ORIGIN)
    with pytest.raises(LabError):
        value.payload("umbrella_operator_bind", {})
    value.state.update(uid="fake-id", intake_access_token="fake-target-token")
    for name in ("umbrella_consumer_select", "umbrella_consumer_bind", "umbrella_consumer_payment"):
        with pytest.raises(LabError, match="user's offer choice"):
            value.payload(name, {})


@pytest.mark.parametrize("value,expected", [
    (TEST_ORIGIN, TEST_ORIGIN), (TEST_ORIGIN + "/", TEST_ORIGIN),
    ("https://Consumer-Fixture.invalid:443/", TEST_ORIGIN),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ("http://[::1]:8080", "http://[::1]:8080"),
])
def test_consumer_target_origin_is_explicit_and_normalized(value, expected):
    assert consumer_origin(value) == expected


@pytest.mark.parametrize("value", [
    "", "consumer-fixture.invalid", "http://consumer-fixture.invalid",
    TEST_ORIGIN + "/api/consumer/mcp", "https://token@consumer-fixture.invalid",
    TEST_ORIGIN + "?token=private", TEST_ORIGIN + "#private", TEST_ORIGIN + "?",
    TEST_ORIGIN + "\n", "https://consumer-fixture.invalid:bad", "https://consumer-fixture.invalid\\path",
    "\x00" + TEST_ORIGIN, "https://@consumer-fixture.invalid", "https://consumer-fixture.invalid:0",
])
def test_consumer_target_rejects_ambiguous_or_credential_bearing_urls(value):
    with pytest.raises(LabError, match="--origin must be"):
        consumer_origin(value)


def test_consumer_checkpoint_is_bound_to_its_target_and_application(tmp_path):
    path = tmp_path / "checkpoint.json"
    state = {"origin": TEST_ORIGIN, "uid": "test-application", "intake_access_token": "fixture-token",
             "latest": {"uid": "test-application"}}
    path.write_text(json.dumps(state))
    assert consumer_checkpoint(path, TEST_ORIGIN) == state
    with pytest.raises(LabError, match="origin must match"):
        consumer_checkpoint(path, "https://other.invalid")
    for changed in ({**state, "origin": None}, {**state, "intake_access_token": ""},
                    {**state, "latest": {"uid": "other-application"}}, []):
        path.write_text(json.dumps(changed))
        with pytest.raises(LabError):
            consumer_checkpoint(path, TEST_ORIGIN)


def test_prefill_cannot_change_the_fake_shopper(tmp_path):
    value = boundary(tmp_path)
    payload = {"credit_consent_pending": True, "intake": value.shopper}
    assert value.payload("umbrella_consumer_prefill", payload) == payload
    with pytest.raises(LabError, match="fake intake exactly"):
        value.payload("umbrella_consumer_prefill", {**payload, "intake": {"full_name": "Other"}})


def test_documented_address_paths_preserve_facts_and_reject_conflicts(tmp_path):
    value = boundary(tmp_path)
    intake = dict(value.shopper)
    address = intake.pop("address")
    intake.update({f"address.{key}": item for key, item in address.items()})
    payload = {"credit_consent_pending": True, "intake": intake}
    assert value.payload("umbrella_consumer_prefill", payload)["intake"] == value.shopper
    intake["address"] = {"city": "Other"}
    with pytest.raises(LabError, match="Conflicting address"):
        value.payload("umbrella_consumer_prefill", payload)


def test_review_confirmation_and_selection_are_host_owned(tmp_path):
    value = boundary(tmp_path)
    value.state.update(uid="fake-id", intake_access_token="fake-target-token", review_seen=True)
    with pytest.raises(LabError, match="confirmation"):
        value.payload("consumer_intake_patch", {"confirm_submission": True})
    value.confirm_review = True
    assert value.payload("consumer_intake_patch", {"confirm_submission": True})["confirm_submission"]
    value.decision = {"selection_token": "chosen-offer", "um_uim_election": "decline"}
    with pytest.raises(LabError, match="successful user-approved"):
        value.payload("umbrella_consumer_bind", {})


async def test_existing_checkout_requires_fresh_payment_status_and_rotates_token(tmp_path):
    value = boundary(tmp_path)
    value.payment_checkout = True
    value.state.update(uid="existing-payment-id", intake_access_token="old-fixture-token")
    value.observe({"uid": "existing-payment-id", "status": "payment_needed"})
    with pytest.raises(LabError, match="fresh payment_needed"):
        value.payload("umbrella_consumer_payment", {})
    for name in ("umbrella_consumer_prefill", "consumer_intake_patch", "umbrella_consumer_select"):
        with pytest.raises(LabError, match="only status and payment"):
            value.payload(name, {})
    calls = []

    async def send_request(request, response_type):
        calls.append(request.params)
        return types.CallToolResult(content=[], structuredContent={
            "uid": "existing-payment-id", "status": "payment_needed",
            "intake_access_token": "rotated-fixture-token"})

    value.session = SimpleNamespace(send_request=send_request)
    await value.call("consumer_intake_issues", {})
    assert value.payment_ready_observed
    payload = value.payload("umbrella_consumer_payment", {"payment_collection_requested": False})
    assert payload == {"uid": "existing-payment-id", "intake_access_token": "rotated-fixture-token",
                       "payment_collection_requested": True}
    assert [call.name for call in calls] == ["consumer_intake_issues"]


async def test_native_inspect_consumer_tool_composition_and_token_injection(tmp_path):
    value = boundary(tmp_path)
    calls = []

    async def send_request(request, response_type):
        calls.append(request.params)
        return types.CallToolResult(content=[], structuredContent={"uid": "fake-id",
            "intake_access_token": "target-secret-canary", "resume_token": "resume-secret-canary",
            "polling_token": "poll-secret-canary", "review": {"ready": True},
            "compare_details_url": "https://local.example/user/otp?token=otp-secret-canary"})

    value.session = SimpleNamespace(send_request=send_request)
    remote = types.Tool(name="umbrella_consumer_prefill", description="Create the fake consumer draft",
        inputSchema={"type": "object", "properties": {
            "intake": {"type": "object", "additionalProperties": True},
            "credit_consent_pending": {"type": "boolean"}},
            "required": ["intake", "credit_consent_pending"]})

    class Scripted(ModelAPI):
        step = 0

        async def generate(self, messages, *args):
            self.step += 1
            if self.step == 1:
                return ModelOutput.for_tool_call("test/consumer", "umbrella_consumer_prefill",
                    {"intake": value.shopper, "credit_consent_pending": True}, tool_call_id="prefill")
            assert "target-secret-canary" not in messages[-1].text
            assert "resume-secret-canary" not in messages[-1].text
            assert "poll-secret-canary" not in messages[-1].text
            assert "otp-secret-canary" not in messages[-1].text
            return ModelOutput.from_content("test/consumer", "The application is not submitted yet.")

    model = Model(CapturingAPI(Scripted("test/consumer"), value.evidence), GenerateConfig(max_retries=0))
    task = Task(dataset=[Sample(input="Exercise the fake prefill")], model=model,
        solver=as_solver(react(model=model, tools=[value.tool(remote)], submit=False)),
        turn_limit=3, time_limit=15)
    logs = await eval_async(task, model=model, log_dir="memory://consumer-composition-test",
                            log_format="json", log_model_api=False)
    assert logs[0].status == "success"
    assert len(calls) == 1
    assert calls[0].arguments["intake"] == value.shopper
    assert value.paused
    assert value.state["intake_access_token"] == "target-secret-canary"
    for path in (value.evidence.path / "artifacts").glob("*.json"):
        assert "target-secret-canary" not in path.read_text()


async def test_model_copy_keeps_the_shoppers_own_data_and_masks_credentials(tmp_path):
    value = boundary(tmp_path)
    value.state.update(uid="fake-id", intake_access_token="target-secret-canary")

    async def send_request(request, response_type):
        return types.CallToolResult(content=[], structuredContent={
            "uid": "fake-id", "status": "intake", "email": "taylor@example.com", "full_name": "Taylor Example",
            "address": {"street": "1 Main St"}, "review_token": "review-secret-canary",
            "compare_details_url": "https://local.example/user/otp?token=otp-secret-canary"})

    value.session = SimpleNamespace(send_request=send_request)
    reply = json.loads(await value.call("consumer_intake_issues", {}))["response"]
    assert reply["email"] == "taylor@example.com" and reply["full_name"] == "Taylor Example"
    assert reply["address"] == {"street": "1 Main St"}
    assert reply["review_token"] == "[REDACTED]" and "otp-secret-canary" not in json.dumps(reply)
    recorded = "".join(path.read_text() for path in (value.evidence.path / "artifacts").glob("*.json"))
    assert not any(item in recorded for item in ("taylor@example.com", "Taylor Example", "1 Main St",
                                                 "review-secret-canary", "otp-secret-canary"))


async def test_the_shoppers_email_stays_visible_when_the_developer_exports_it(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL", "taylor@example.com")  # The developer tests with their own account.
    value = boundary(tmp_path)
    value.state.update(uid="fake-id", intake_access_token="target-secret-canary")

    async def send_request(request, response_type):
        return types.CallToolResult(content=[], structuredContent={
            "uid": "fake-id", "status": "intake", "email": "taylor@example.com",
            "note": "Review sent to taylor@example.com"})

    value.session = SimpleNamespace(send_request=send_request)
    reply = json.loads(await value.call("consumer_intake_issues", {}))["response"]
    assert reply["email"] == "taylor@example.com" and reply["note"] == "Review sent to taylor@example.com"
    assert "taylor@example.com" not in value.evidence.redactor.credentials  # The guard would not refuse it.
    recorded = "".join(path.read_text() for path in (value.evidence.path / "artifacts").glob("*.json"))
    assert "taylor@example.com" not in recorded
