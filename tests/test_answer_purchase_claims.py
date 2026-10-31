"""Insurance completion wording must be checked against policy and charge records."""
import pytest

from consumer_agent_testing_tool.answer import detect_claims


@pytest.mark.parametrize("text", [
    "Your policy is bound.", "Your coverage is active.", "Insurance is in force.",
    "You are now insured.", "You're insured.",
])
def test_policy_completion_is_a_confirmed_claim(text):
    assert "confirmed" in detect_claims(text)


@pytest.mark.parametrize("text", [
    "Your policy is not bound.", "Your coverage is not active.", "You are not insured.",
    "Your policy may be bound.", "Once approved, your coverage will be active.",
    "The insurer said your policy is bound.", "Is your coverage active?",
    "The payment method is on file. Coverage is still pending.",
])
def test_pending_negated_conditional_and_quoted_policy_outcomes_are_not_completion(text):
    assert "confirmed" not in detect_claims(text)


@pytest.mark.parametrize("text", ["Your premium was charged.", "The premium is paid."])
def test_premium_collection_is_a_paid_claim(text):
    assert "paid" in detect_claims(text)


def test_policy_cancellation_is_distinct_from_opening_a_request():
    assert "cancelled" in detect_claims("Your policy has been cancelled.")
    assert "cancelled" not in detect_claims("Your cancellation request is pending.")


@pytest.mark.parametrize("text", [
    "Your premium has been refunded.", "I refunded your premium.", "Your refund was issued.",
    "Your payment has been refunded.", "The charge was refunded.",
    "The refund is paid.", "Refund: complete.", "Refunded.",
])
def test_money_returned_is_a_refunded_claim_separate_from_premiums_paid(text):
    assert "refunded" in detect_claims(text)
    assert "paid" not in detect_claims(text)


@pytest.mark.parametrize("text", [
    "Your refund request has been submitted.", "Your refund has not been issued.",
    "Your premium will be refunded if approved.", "The insurer said your refund was issued.",
    "Your payment has not been refunded.", "The charge will be refunded if approved.",
    "Was your premium refunded?", "The refund is still pending.",
])
def test_requested_negated_conditional_and_quoted_refunds_are_not_money_returned(text):
    assert "refunded" not in detect_claims(text)
