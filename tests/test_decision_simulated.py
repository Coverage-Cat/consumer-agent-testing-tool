"""Decision contracts count deterministic trials only: a simulated user's trial is refused (spec section 14)."""
import pytest
from test_decision import contract, trial

from consumer_agent_testing_tool.decision import evaluate_contract
from consumer_agent_testing_tool.errors import Unsupported


def test_decision_contracts_refuse_trials_a_simulated_user_answered():
    trials = [trial() for _ in range(30)]
    trials[3]["run_lock"]["user_simulator"] = {"label": "simulated user", "model_profile": "user-sim"}
    with pytest.raises(Unsupported, match=rf"cannot count trials with a simulated user \({trials[3]['run_id']}\)"):
        evaluate_contract(contract(.8), trials)
    # Only the extension's object counts: anything else under the name is not a simulated user's lock.
    trials[3]["run_lock"]["user_simulator"] = "forged"
    trials[4]["run_lock"] = "not a lock"
    assert evaluate_contract(contract(.8), trials)["behavioral_decision"] == "inconclusive"
