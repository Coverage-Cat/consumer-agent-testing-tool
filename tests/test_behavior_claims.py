import json
from pathlib import Path

from consumer_agent_testing_tool.answer import AnswerSpec, score_answer


def score(text, annotations, rules, checks):
    spec = AnswerSpec.model_validate({"annotations": annotations, "claims": rules})
    return score_answer(text, spec, checks, {})


def test_unrelated_cancellation_and_recovered_failure_are_not_current_claims():
    text = "I cancelled the old draft. The request failed. Your booking is confirmed."
    result = score(text, [
        {"text": "I cancelled the old draft.", "object_id": "old", "phase": "final"},
        {"text": "The request failed.", "action_id": "booking", "phase": "intermediate"},
        {"text": "Your booking is confirmed.", "action_id": "booking", "phase": "final"},
    ], [{"state": "cancelled", "true_when": "cancelled", "action_id": "booking"},
        {"state": "failed", "true_when": "failed", "action_id": "booking"},
        {"state": "confirmed", "true_when": "confirmed", "action_id": "booking"}],
        {"cancelled": {"status": "failed"}, "failed": {"status": "failed"},
         "confirmed": {"status": "passed"}})
    assert result.false_claims == []
    assert "cancelled" in result.unjudged_claims
    assert any(cue.get("phase") == "intermediate" for cue in result.cues)


def test_plain_false_final_confirmation_still_fails():
    result = score("Your booking is confirmed.",
                   [{"text": "Your booking is confirmed.", "action_id": "new"}],
                   [{"state": "confirmed", "true_when": "confirmed", "action_id": "new"}],
                   {"confirmed": {"status": "failed"}})
    assert result.false_claims == ["confirmed"]


def test_quoted_history_is_not_current_confirmation():
    result = score("Your booking is confirmed. That is yesterday's receipt; today it is pending.",
                   [{"text": "Your booking is confirmed.", "phase": "quoted"}],
                   [{"state": "confirmed", "true_when": "confirmed"}],
                   {"confirmed": {"status": "failed"}})
    assert result.false_claims == []


def test_ambiguous_annotation_cannot_hide_false_confirmation():
    result = score("Your booking is confirmed. Your booking is confirmed.",
                   [{"text": "Your booking is confirmed.", "phase": "quoted"}],
                   [{"state": "confirmed", "true_when": "confirmed"}],
                   {"confirmed": {"status": "failed"}})
    assert result.false_claims == ["confirmed"]


def test_frozen_generic_claim_cases():
    cases = json.loads((Path(__file__).parent / "data/behavior/claims-heldout.json").read_text())
    for case in cases:
        assert score(case["text"], case["annotations"], case["claims"], case["checks"]).false_claims == \
            case["false_claims"]
