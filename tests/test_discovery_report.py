import asyncio
import json

import pytest
from pydantic import ValidationError

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.discovery_report import STAGES, DiscoveryStudy, summarize
from consumer_agent_testing_tool.errors import LabError


def study():
    return {"schema_version": 1, "study_id": "synthetic", "cases": [
        {"id": "request", "exposure": "cold", "intent": "purchase", "host": "hosted", "trials": 2}],
        "observations": [{"case_id": "request", "trial": 1, "target_seeded": False, "stages": {
            name: {"status": "observed", "source": "service" if name in ("started", "completed") else "transcript",
                   "evidence_ref": "private-evidence"} for name in STAGES}}]}


def test_reports_missing_trials_and_does_not_export_evidence():
    result = summarize(DiscoveryStudy.model_validate(study()))
    assert result["status"] == "incomplete"
    assert result["groups"][0]["unrun"] == 1
    assert result["groups"][0]["stages"]["completed"]["measurement_coverage"] == 0.5
    assert "private-evidence" not in json.dumps(result)


def test_unknown_is_not_absence_or_success_and_does_not_infer_stages():
    data = study()
    data["observations"][0]["stages"]["recommended"] = {"status": "unobserved"}
    result = summarize(DiscoveryStudy.model_validate(data))
    stages = result["groups"][0]["stages"]
    assert stages["recommended"]["observed_rate"] is None
    assert stages["completed"]["observed"] == 1


@pytest.mark.parametrize("stage", ["started", "completed"])
@pytest.mark.parametrize("status", ["observed", "absent"])
def test_rejects_assistant_claim_of_business_outcome(stage, status):
    data = study()
    data["observations"][0]["stages"][stage]["status"] = status
    data["observations"][0]["stages"][stage]["source"] = "transcript"
    with pytest.raises(ValidationError, match="service evidence"):
        DiscoveryStudy.model_validate(data)


@pytest.mark.parametrize("change", ["seeded", "duplicate", "unknown_case", "out_of_range", "no_evidence"])
def test_rejects_invalid_observations(change):
    data = study()
    row = data["observations"][0]
    if change == "seeded":
        row["target_seeded"] = True
    elif change == "duplicate":
        data["observations"].append(row)
    elif change == "unknown_case":
        row["case_id"] = "unknown"
    elif change == "out_of_range":
        row["trial"] = 3
    else:
        row["stages"]["discovered"].pop("evidence_ref")
    with pytest.raises(ValidationError):
        DiscoveryStudy.model_validate(data)


def test_separates_treatments_and_negative_activations():
    data = study()
    data["cases"].append({"id": "negative", "exposure": "connected", "intent": "negative",
                          "host": "surrogate", "trials": 1})
    data["observations"].append({**data["observations"][0], "case_id": "negative", "target_seeded": True})
    result = summarize(DiscoveryStudy.model_validate(data))
    assert len(result["groups"]) == 2
    negative = next(group for group in result["groups"] if group["intent"] == "negative")
    assert negative["unexpected_invocations"] == 1


def test_cli_runs_offline_and_returns_incomplete(tmp_path, capsys):
    path = tmp_path / "study.json"
    path.write_text(json.dumps(study()))
    assert asyncio.run(execute(parser().parse_args(["discovery-report", str(path)]))) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


def test_validation_does_not_print_submitted_secrets(tmp_path):
    path = tmp_path / "study.json"
    path.write_text('{"secret": "do-not-print"}')
    with pytest.raises(LabError) as error:
        asyncio.run(execute(parser().parse_args(["discovery-report", str(path)])))
    assert "do-not-print" not in str(error.value)
