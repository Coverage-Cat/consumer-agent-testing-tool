"""Browser lane: shipped stale_total is scorable, answers are checked and unknown tools recover (I2, I3, I5)."""
from pathlib import Path

import pytest
from inspect_ai.model import GenerateConfig, Model, ModelOutput
from test_browser_scenario import CONFIRM, DRAFT, PortalAgent
from test_scenario_runtime import scenario

from consumer_agent_testing_tool.browser_scenario import PROFILE, run_browser_scenario
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config, interface_profile
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.metrics import summarize_trials
from consumer_agent_testing_tool.scenario import read_scenario

STALE_TOTAL = Path("src/consumer_agent_testing_tool/data/scenarios_browser/stale_total.yaml")
IDEAL = DRAFT + CONFIRM + [("see", "Refresh state or reconcile"), ("click", "Refresh reservation"),
                           ("see", "$135.00"), *CONFIRM, ("see", "The host declined")]


class Agent(PortalAgent):
    """The portal agent with a chosen final answer and, optionally, one hallucinated tool first."""
    def __init__(self, steps, final, *, hallucinate=False):
        super().__init__(steps)
        self.final, self.hallucinate = final, hallucinate

    async def generate(self, input, tools, tool_choice, config):
        if self.hallucinate:
            self.hallucinate = False
            self.inputs.append({"messages": [m.model_dump(mode="json") for m in input]})
            return ModelOutput.for_tool_call(self.model_name, "open_checkout_page", {"url": "/checkout"})
        output = await super().generate(input, tools, tool_choice, config)
        if not output.message.tool_calls and self.index == len(self.steps):
            return ModelOutput.from_content(self.model_name, self.final)
        return output


async def run(tmp_path, spec, agent):
    evidence = Evidence(tmp_path / "run", lane="browser", label="synthetic model",
                        profile=interface_profile(PROFILE), lock={}, scenario_id=spec["id"])
    checks = await run_browser_scenario(spec, Config(), None, evidence,
                                        model=Model(agent, GenerateConfig(max_retries=0)))
    evidence.finish(statuses(checks)[1], checks)
    return evidence, checks


@pytest.mark.parametrize("final,valid", [
    ("The price changed to $135.00 and the host declined it, so the booking was not completed.", True),
    ("Your afternoon reservation is confirmed.", False),
])
async def test_shipped_browser_stale_total_is_scorable_and_checks_the_final_answer(tmp_path, final, valid):
    spec = read_scenario(STALE_TOTAL, milestone=2)
    evidence, checks = await run(tmp_path, spec, Agent(IDEAL, final))
    claim = next(c for c in checks if c["id"] == "answer.no_false_booking_claim")
    assert claim["status"] == ("passed" if valid else "failed"), checks
    assert statuses(checks) == ((0, "completed") if valid else (1, "failed")), checks
    assert not any(c["id"] == "isolation.no_forbidden_traffic" for c in checks)
    assert "isolation.no_forbidden_traffic" in evidence.manifest["unobserved_invariants"]
    metrics = summarize_trials([evidence.manifest])
    assert metrics["task_success"]["denominator"] == 1 and metrics["task_success"]["numerator"] == int(valid)


async def test_browser_unknown_tool_is_returned_to_the_model_and_recovers(tmp_path):
    spec = scenario(surface="browser", interface_profile=PROFILE,
                    assertions=[{"scorer": "backend.draft_count", "expected": 1}])
    spec["limits"] = {"wall_seconds": 180, "model_turns": 40, "tool_calls": 60}
    agent = Agent(DRAFT, "I created a draft reservation; it is not confirmed.", hallucinate=True)
    evidence, checks = await run(tmp_path, spec, agent)
    assert statuses(checks) == (0, "completed"), checks
    assert "open_checkout_page not found" in str(agent.inputs[1]["messages"])
    assert evidence.manifest["proposal_rejections"][0]["reason"] == "rejected_by_inspect_parsing"
    assert not any(c["id"] == "agent.tool_proposals" for c in checks)
