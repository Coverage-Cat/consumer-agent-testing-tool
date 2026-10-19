"""The transient_error 429 is a retryable service response the agent can recover from (I13)."""
import json

import pytest
from inspect_ai.model import GenerateConfig, Model
from test_api_scenario import Scripted, done, draft_call
from test_scenario_runtime import evidence_for, scenario

from consumer_agent_testing_tool.api_scenario import run_api_scenario
from consumer_agent_testing_tool.cli import statuses
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.metrics import summarize_trials


def transient():
    spec = scenario(assertions=[{"scorer": "backend.draft_count", "expected": 1}],
                    evaluation={"capability": "reservations.create", "recovery": "transient-draft-retry"})
    spec["initial_state"]["variant"] = "transient_error"
    return spec


async def run(tmp_path, responses):
    spec = transient()
    evidence = evidence_for(tmp_path)
    evidence.scenario_id = evidence.manifest["scenario_id"] = spec["id"]
    api = Scripted(responses)
    checks = await run_api_scenario(spec, Config(), None, evidence, model=Model(api, GenerateConfig(max_retries=0)))
    evidence.finish(statuses(checks)[1], checks)
    return evidence, api, checks


@pytest.mark.parametrize("retry", [True, False])
async def test_rate_limit_is_returned_to_the_model_and_recovery_is_scored(tmp_path, retry):
    responses = [draft_call(), draft_call(), done()] if retry else [draft_call(), done()]
    evidence, api, checks = await run(tmp_path, responses)
    tool_results = [json.loads(m["content"]) for m in api.inputs[1]["messages"] if m["role"] == "tool"]
    assert tool_results[-1]["status"] == 429 and tool_results[-1]["retry_after"] == "1"
    assert not any(c.get("class") == "setup/infrastructure" for c in checks), checks
    assert [a["status"] for a in evidence.manifest["target_attempts"]] == ([429, 200] if retry else [429])
    assert statuses(checks) == ((0, "completed") if retry else (1, "failed")), checks
    recovery = evidence.manifest["metrics"]["recovery_success"]
    assert (recovery["numerator"], recovery["denominator"]) == (int(retry), 1)
    summary = summarize_trials([evidence.manifest])
    assert summary["infrastructure"] == 0 and summary["task_success"]["denominator"] == 1
    assert summary["recovery_success"]["numerator"] == int(retry)
