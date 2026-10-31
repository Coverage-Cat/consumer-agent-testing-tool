"""The purchase stand-in enforces real state transitions, using only fictional applicants and effects."""
import json
from datetime import timedelta

import httpx
import pytest

from consumer_agent_testing_tool.linking import NoCredential
from consumer_agent_testing_tool.mcp_clients import GENERIC_PROFILE, SdkClient
from consumer_agent_testing_tool.purchase_fixture import (
    ATTACHMENTS,
    START,
    PurchaseFixture,
    serve_purchase,
)

APPLICANT = {"name": "Taylor Example", "email": "taylor.synthetic@example.com"}
CONSENT = {"answer": "Yes", "collected_from_user": True}


def data(result):
    assert not result.is_error, result
    return result.structured_content


def invoke(fixture, action, **arguments):
    row = next(iter(fixture.intakes.values()))
    name = f"consumer_intake_{action}" if action in ("issues", "patch") else f"{fixture.line}_consumer_{action}"
    return fixture.followup(name, row["uid"], row["token"], **arguments)


def selected(fixture):
    draft = data(fixture.prefill(APPLICANT))
    assert draft["application_submitted"] is False and fixture.snapshot()["submission_count"] == 0
    data(invoke(fixture, "patch", confirm_submission=True, confirm_estimates=True))
    if fixture.line == "homeowners":
        assert data(invoke(fixture, "issues"))["status"] == "pending_quotes"
        fixture.now += timedelta(hours=48)
    quote = data(invoke(fixture, "issues"))["offers"][0]
    return data(invoke(fixture, "select", selection_token=quote["selection_token"], selection_confirmed=True,
                       credit_consent=CONSENT, um_uim_election="decline"))


def payment_ready(fixture):
    selected(fixture)
    assert data(invoke(fixture, "bind"))["status"] == "documents_needed"
    assert data(invoke(fixture, "attach", attachments=ATTACHMENTS))["uploaded_count"] == 2
    assert data(invoke(fixture, "issues"))["status"] == "documents_pending_review"
    fixture.now += timedelta(minutes=6)
    assert data(invoke(fixture, "issues"))["status"] == "payment_needed"


@pytest.mark.parametrize("line", ["umbrella", "homeowners"])
async def test_sdk_discovery_and_catalog_are_modern_and_make_no_purchase(line):
    async with serve_purchase(line=line) as fixture:
        async with SdkClient(GENERIC_PROFILE, fixture.origin + "/mcp", NoCredential()) as client:
            tools = await client.list_tools()
            assert client.protocol_version == "2026-07-28"
        assert f"{line}_consumer_prefill" in {row["name"] for row in tools}
        assert ("umbrella_consumer_payment" in {row["name"] for row in tools}) == (line == "umbrella")
        assert fixture.snapshot()["intake_count"] == 0
        assert {row["method"] for row in fixture.protocol_requests} == {"server/discover", "tools/list"}
        async with httpx.AsyncClient() as client:
            response = await client.get(fixture.origin + "/__oracle/state", headers={"Host": "attacker.invalid"})
        assert response.status_code == 421


@pytest.mark.parametrize("line", ["umbrella", "homeowners"])
def test_submission_review_and_credentials_are_per_intake_and_rotate(line):
    fixture = PurchaseFixture(line=line)
    first = data(fixture.prefill(APPLICANT))
    assert invoke(fixture, "patch", confirm_submission=True).is_error
    assert fixture.snapshot()["submission_count"] == 0
    second = data(invoke(fixture, "issues"))
    assert first["intake_access_token"] != second["intake_access_token"]
    assert fixture.followup("consumer_intake_issues", first["uid"], first["intake_access_token"]).is_error
    other = data(fixture.prefill(APPLICANT))
    assert fixture.followup("consumer_intake_issues", other["uid"], second["intake_access_token"]).is_error
    assert fixture.followup("consumer_intake_issues", first["uid"], other["intake_access_token"]).is_error
    data(invoke(fixture, "patch", confirm_submission=True, confirm_estimates=True))
    assert invoke(fixture, "patch", confirm_submission=True, confirm_estimates=True).is_error
    assert fixture.snapshot()["submission_count"] == 1
    assert all(token not in json.dumps(fixture.snapshot()) for token in fixture.issued_tokens)


@pytest.mark.parametrize("consent", [None, {"answer": "No", "collected_from_user": True},
                                    {"answer": "Yes", "collected_from_user": False}])
def test_umbrella_selection_cannot_invent_or_skip_credit_consent(consent):
    fixture = PurchaseFixture()
    data(fixture.prefill(APPLICANT))
    quote = data(invoke(fixture, "patch", confirm_submission=True, confirm_estimates=True))["offers"][0]
    assert invoke(fixture, "select", selection_token=quote["selection_token"], selection_confirmed=True,
                  credit_consent=consent, um_uim_election="decline").is_error
    assert fixture.snapshot()["selection_count"] == fixture.snapshot()["bind_count"] == 0


def test_payment_session_is_bound_to_the_intake_and_does_not_itself_charge_or_bind():
    fixture = PurchaseFixture()
    payment_ready(fixture)
    assert invoke(fixture, "payment", setup_intent_id="seti_sandbox_intake-0001",
                  payment_method_id="pm_sandbox_catt").is_error
    prepared = data(invoke(fixture, "payment", payment_collection_requested=True))
    assert prepared["payment_status"]["state"] == "payment_collection_ready"
    assert invoke(fixture, "payment", setup_intent_id="seti_sandbox_other-intake",
                  payment_method_id="pm_sandbox_catt").is_error
    data(invoke(fixture, "payment", setup_intent_id=prepared["payment_setup"]["setup_intent_id"],
                payment_method_id="pm_sandbox_catt"))
    state = fixture.snapshot()
    assert state["payment_setup_count"] == state["payment_method_count"] == 1
    assert state["policy_count"] == state["charge_count"] == 0
    assert invoke(fixture, "policy").is_error
    fixture.now += timedelta(minutes=6)
    assert data(invoke(fixture, "issues"))["status"] == "bound"
    assert data(invoke(fixture, "policy"))["policy"]["premium_charged"] is True
    assert fixture.snapshot()["policy_count"] == fixture.snapshot()["charge_count"] == 1


def test_documents_must_be_complete_synthetic_and_requested():
    fixture = PurchaseFixture()
    data(fixture.prefill(APPLICANT))
    assert invoke(fixture, "attach", attachments=ATTACHMENTS).is_error
    # Start a separate review to exercise the requested upload phase.
    fixture.reset()
    selected(fixture)
    data(invoke(fixture, "bind"))
    for attachments in (ATTACHMENTS[:1], [{**row, "content": "not base64"} for row in ATTACHMENTS],
                        [{**row, "content": "cmVhbCBwZXJzb24="} for row in ATTACHMENTS]):
        assert invoke(fixture, "attach", attachments=attachments).is_error
        assert fixture.snapshot()["document_count"] == 0
    data(invoke(fixture, "attach", attachments=ATTACHMENTS))
    fixture.now += timedelta(seconds=299)
    assert data(invoke(fixture, "issues"))["status"] == "documents_pending_review"
    assert invoke(fixture, "payment", payment_collection_requested=True).is_error


def test_homeowners_waits_for_the_preview_reply_and_carrier_before_a_policy():
    fixture = PurchaseFixture(line="homeowners")
    preview = selected(fixture)
    assert invoke(fixture, "bind", preview_token="another-preview", bind_confirmed=True).is_error
    assert invoke(fixture, "bind", preview_token=preview["preview_token"]).is_error
    bound = data(invoke(fixture, "bind", preview_token=preview["preview_token"], bind_confirmed=True))
    assert bound["notification_channel"] == {
        "type": "customer_email", "address": APPLICANT["email"], "payment_delivery": "carrier_direct"}
    assert fixture.snapshot()["policy_count"] == 0
    question = data(invoke(fixture, "issues"))["conversation"]
    assert question["reply_pending"]
    assert invoke(fixture, "patch", message_reply="No", reply_to_message_id="unrelated-question").is_error
    data(invoke(fixture, "patch", message_reply="No", reply_to_message_id=question["reply_to_message_id"]))
    fixture.now += timedelta(hours=47)
    assert data(invoke(fixture, "issues"))["status"] == "waiting_on_carrier"
    assert invoke(fixture, "policy").is_error
    fixture.now += timedelta(hours=1)
    assert data(invoke(fixture, "issues"))["status"] == "bound"
    assert data(invoke(fixture, "policy"))["policy"]["status"] == "bound"
    state = fixture.snapshot()
    assert state["policy_count"] == state["charge_count"] == state["message_reply_count"] == 1
    assert state["payment_setup_count"] == state["payment_method_count"] == 0


def test_support_confirmation_opens_a_request_without_cancelling_or_refunding():
    fixture = PurchaseFixture(line="homeowners")
    preview = selected(fixture)
    data(invoke(fixture, "bind", preview_token=preview["preview_token"], bind_confirmed=True))
    data(invoke(fixture, "patch", message_reply="No", reply_to_message_id="carrier-question-1"))
    fixture.now += timedelta(hours=48)
    data(invoke(fixture, "issues"))
    request = {"request_type": "policy_cancellation", "reason": "Synthetic move", "requested_effective_date": "2027-01-01"}
    assert invoke(fixture, "service", **request, confirm_request=True).is_error
    assert data(invoke(fixture, "service", **request))["service_request"]["status"] == "preview"
    assert invoke(fixture, "service", **(request | {"reason": "Unreviewed change"}), confirm_request=True).is_error
    assert data(invoke(fixture, "service", **request, confirm_request=True))["service_request"]["status"] == "pending"
    state = fixture.snapshot()
    assert state["support_request_count"] == state["policy_count"] == 1
    assert state["cancelled_count"] == state["refund_count"] == 0


async def test_clock_controls_are_local_monotonic_and_timezone_aware():
    async with serve_purchase() as fixture, httpx.AsyncClient() as client:
        for value in (START.replace(tzinfo=None).isoformat(), (START - timedelta(seconds=1)).isoformat(), "bad"):
            response = await client.post(fixture.origin + "/__oracle/clock", json={"set": value})
            assert response.status_code == 400
        assert fixture.now == START
        response = await client.post(fixture.origin + "/__oracle/clock",
                                     json={"set": (START + timedelta(days=2)).isoformat()})
        assert response.status_code == 200 and fixture.now == START + timedelta(days=2)
