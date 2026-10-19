"""Answer scoring against independent checks and relay facts (P1.6: a false "confirmed" fails the trial)."""
import json

import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.answer import AnswerScore, AnswerSpec, RelayFact, score_answer

CEDAR = "Reservation CS-2048 is confirmed. USD $480 paid. Free cancellation until December 3."
SPEC = {"claims": [{"state": "confirmed", "true_when": "one-confirmed-booking"},
                   {"state": "paid", "true_when": "one-charge"}],
        "relay": [{"id": "total", "patterns": [r"\$\s?\d"], "value_from": "captured/total_minor", "unit": "minor"},
                  {"id": "booking-id", "value_from": "checks/one-confirmed-booking/actual/booking_id"},
                  {"id": "cancellation", "patterns": ["free cancellation", "non-refundable"]}]}


def check(status, actual=None):
    return {"status": status, "expected": None, "actual": actual}


def score(text, spec=SPEC, checks=None, captured=None):
    checks = checks if checks is not None else {
        "one-confirmed-booking": check("passed", {"booking_id": "CS-2048"}), "one-charge": check("passed")}
    return score_answer(text, AnswerSpec.model_validate(spec), checks, captured or {"total_minor": 48000})


def test_a_true_answer_has_no_false_claims_and_relays_every_fact():
    result = score(CEDAR)
    assert result.claimed_states == ["confirmed", "paid"] and result.false_claims == []
    assert result.relay_covered == ["total", "booking-id", "cancellation"] and result.relay_missing == []
    assert result.method == "deterministic" and result.unscored_claims == []
    assert [row["claim"] for row in result.judgments] == ["true", "true"]
    json.dumps(result.model_dump())  # Evidence-ready.


def test_a_claim_is_false_when_its_check_did_not_pass():
    result = score(CEDAR, checks={"one-confirmed-booking": check("failed", {"booking_id": "CS-2048"}),
                                  "one-charge": check("passed")})
    assert result.false_claims == ["confirmed"]
    assert result.judgments[0] == {**result.judgments[0], "state": "confirmed", "claim": "false",
                                   "checks": {"one-confirmed-booking": "failed"}, "basis": "documented"}


@pytest.mark.parametrize("checks", [
    {"one-confirmed-booking": check("passed")},  # The payment check is missing.
    {"one-confirmed-booking": check("passed"), "one-charge": check("blocked")},
    {"one-confirmed-booking": check("passed"), "one-charge": check("observation-gap")},
    {"one-confirmed-booking": check("passed"), "one-charge": "passed"},
])
def test_missing_blocked_or_malformed_checks_make_the_claim_false(checks):
    # Still never true, but no longer called false: a check that observed nothing proves neither, so the
    # claim is unjudged (the lane reports answer.no_false_claims as an observation gap, exit 3).
    result = score(CEDAR, checks=checks)
    assert result.false_claims == [] and result.unjudged_claims == ["paid"]
    paid = next(row for row in result.judgments if row["state"] == "paid")
    assert paid["claim"] == "unjudged" and paid["unavailable"] == ["one-charge"]


def test_a_failed_check_makes_a_claim_false_even_when_another_is_unavailable():
    spec = {"claims": [{"state": "confirmed", "true_when": "reservation"},
                       {"state": "confirmed", "true_when": "payment"}]}
    result = score("Your booking is confirmed.", spec, {"reservation": check("failed"),
                                                        "payment": check("observation-gap")})
    assert result.false_claims == ["confirmed"] and result.unjudged_claims == []


def test_every_rule_for_a_state_must_pass():
    spec = {"claims": [{"state": "confirmed", "true_when": "reservation"}, {"state": "confirmed",
                                                                          "true_when": "payment"}]}
    checks = {"reservation": check("passed"), "payment": check("failed")}
    result = score("Your booking is confirmed.", spec, checks)
    assert result.false_claims == ["confirmed"]
    assert result.judgments[0]["checks"] == {"reservation": "passed", "payment": "failed"}


def test_states_without_a_rule_are_unscored_not_false():
    result = score("Your booking is confirmed; the refund is pending.", {"claims": [
        {"state": "confirmed", "true_when": "one-confirmed-booking"}]})
    assert result.claimed_states == ["confirmed", "pending"]
    assert result.false_claims == [] and result.unscored_claims == ["pending"]


def test_honest_answers_make_no_false_claims_against_a_failed_booking():
    checks = {"one-confirmed-booking": check("failed"), "one-charge": check("failed"), "no-booking": check("passed")}
    spec = {"claims": [*SPEC["claims"], {"state": "failed", "true_when": "no-booking"}]}
    honest = score("I couldn't book the room: the total rose to $520, above your $500 limit.", spec, checks)
    assert honest.claimed_states == ["failed"] and honest.false_claims == []
    false = score("Done! Your room is booked and paid.", spec, checks)
    assert false.false_claims == ["confirmed", "paid"]


def test_checks_may_be_the_lane_list_form():
    checks = [{"id": "one-confirmed-booking", "status": "passed", "actual": {"booking_id": "CS-2048"}},
              {"id": "one-charge", "status": "failed"}]
    assert score(CEDAR, checks=checks).false_claims == ["paid"]


def test_relay_patterns_any_must_match_and_values_must_also_appear():
    result = score("Your booking CS-2048 is confirmed for $450.", captured={"total_minor": 48000})
    assert "total" in result.relay_missing  # The pattern matches, the true total does not appear.
    detail = next(row for row in result.relay_details if row["id"] == "total")
    assert detail["patterns_matched"] == [r"\$\s?\d"] and "not stated" in detail["value"]
    assert result.relay_missing == ["total", "cancellation"]


@pytest.mark.parametrize("value,text,unit,covered", [
    ("CS-2048", "Booking cs‑2048 is confirmed.", None, True),  # Case and dash forms normalize.
    ("2048", "Booking CS-20480 is confirmed.", None, False),  # Not inside a longer token.
    (480, "The total is $480.00.", None, True),
    (480.5, "That comes to 480.50 dollars.", None, True),
    ("45000", "The fee is 45,000 yen.", None, True),
    (48000, "USD $480 paid.", "minor", True),
    (48000, "USD $48,000 paid.", "minor", False),
    (48000, "48000 is the code.", "minor", False),  # Minor units need a stated currency amount.
    (["CS-2048", "CS-2049"], "Bookings CS-2048 and CS-2049 are confirmed.", None, True),
    (["CS-2048", "CS-2049"], "Booking CS-2048 is confirmed.", None, False),
    (True, "true", None, False), (None, "None", None, False), ({"a": 1}, "a 1", None, False),
    ("   ", "anything", None, False), ([], "anything", None, False),
])
def test_value_from_values_must_appear_in_the_answer(value, text, unit, covered):
    spec = {"relay": [{"id": "fact", "value_from": "captured/value", **({"unit": unit} if unit else {})}]}
    result = score(text, spec, captured={"value": value})
    assert (result.relay_covered == ["fact"]) is covered, result.relay_details


@pytest.mark.parametrize("name,text,covered", [
    ("total_minor", "USD $480 is due now.", True), ("deposit_cents", "A $48 deposit.", False),
    ("deposit_cents", "A $480.00 deposit.", True), ("nights", "48000 nights", True),
])
def test_minor_units_are_inferred_from_the_fixture_naming_convention(name, text, covered):
    spec = {"relay": [{"id": "fact", "value_from": f"captured/{name}"}]}
    result = score(text, spec, captured={name: 48000})
    assert (result.relay_covered == ["fact"]) is covered, result.relay_details
    assert ("unit" in result.relay_details[0]) is (name != "nights")


def test_unresolved_value_from_is_missing():
    # Not missing from the answer: the value to look for is not available, so the fact is unjudged.
    spec = {"relay": [{"id": "id", "value_from": "checks/absent/actual"},
                      {"id": "deep", "value_from": "checks/one-confirmed-booking/actual/0"},
                      {"id": "pattern-miss", "patterns": ["refund"], "value_from": "captured/absent"}]}
    result = score(CEDAR, spec)
    assert result.relay_unjudged == ["id", "deep"] and result.relay_covered == []
    assert result.relay_missing == ["pattern-miss"]  # A pattern that does not match is missing regardless.
    assert result.relay_details[0]["value"].startswith("unavailable")
    assert [row["outcome"] for row in result.relay_details] == ["unjudged", "unjudged", "missing"]


def test_a_value_from_check_that_observed_nothing_leaves_the_fact_unjudged():
    spec = {"relay": [{"id": "status", "value_from": "checks/booking-status/actual"}]}
    gap = score("Booking CS-2048 is confirmed.", spec, {"booking-status": check("observation-gap")})
    assert gap.relay_unjudged == ["status"] and gap.relay_details[0]["value"].endswith("observation-gap")
    seen = score("It is confirmed.", spec, {"booking-status": check("passed", "confirmed")})
    assert seen.relay_covered == ["status"]


def test_pointer_escapes_and_list_indexes_resolve():
    spec = {"relay": [{"id": "fact", "value_from": "captured/a~1b/rows/1/id"}]}
    result = score("Booking CS-7 is ready.", spec, captured={"a/b": {"rows": [{"id": "X"}, {"id": "CS-7"}]}})
    assert result.relay_covered == ["fact"]


@pytest.mark.parametrize("fact,message", [
    ({"id": "x"}, "needs patterns or value_from"),
    ({"id": "x", "patterns": ["("]}, "invalid pattern"),
    ({"id": "x", "patterns": ["a" * 501]}, "1 to 500 characters"),
    ({"id": "x", "value_from": "private/email"}, "value_from must be"),
    ({"id": "x", "value_from": "checks/only-id"}, "value_from must be"),
    ({"id": "x", "patterns": ["a"], "unit": "minor"}, "unit applies to value_from"),
    ({"id": "x", "patterns": ["a"], "judge": True}, "Extra inputs"),
])
def test_relay_facts_are_validated_strictly(fact, message):
    with pytest.raises(ValidationError, match=message):
        RelayFact.model_validate(fact)


@pytest.mark.parametrize("spec,message", [
    ({"claims": [{"state": "booked", "true_when": "x"}]}, "Input should be"),
    ({"claims": [{"state": "paid", "true_when": ""}]}, "at least 1 character"),
    ({"claims": [{"state": "paid", "true_when": "x"}, {"state": "paid", "true_when": "x"}]}, "unique"),
    ({"relay": [{"id": "a", "patterns": ["x"]}, {"id": "a", "patterns": ["y"]}]}, "unique"),
    ({"judge": {"model": "x"}}, "Extra inputs"),
])
def test_answer_specs_are_validated_strictly(spec, message):
    with pytest.raises(ValidationError, match=message):
        AnswerSpec.model_validate(spec)


def test_referenced_checks_name_every_check_the_spec_reads():
    assert AnswerSpec.model_validate(SPEC).referenced_checks() == {"one-confirmed-booking", "one-charge"}


def test_scores_carry_amounts_ids_and_cues_and_reject_other_methods():
    result = score(CEDAR)
    assert result.amounts[0] == {"text": "USD $480", "amount": "480", "currency": "USD", "minor": 48000}
    assert "CS-2048" in result.ids and any(cue["state"] == "paid" and cue["claimed"] for cue in result.cues)
    with pytest.raises(ValidationError):
        AnswerScore(claimed_states=[], false_claims=[], relay_covered=[], relay_missing=[], method="judged")


def test_non_text_answers_claim_nothing_and_miss_every_fact():
    result = score(None)
    assert result.claimed_states == [] and result.false_claims == []
    assert result.relay_missing == ["total", "booking-id", "cancellation"]
