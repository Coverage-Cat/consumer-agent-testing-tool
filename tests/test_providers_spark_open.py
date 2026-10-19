"""Muse Spark Open's lineage (P1.8): a profile serving it is labeled, in its evidence and the connector run's lock, as
derived from the August Muse Spark release (dev.meta.ai), never as the model Muse runs."""
import pytest
from test_connector_support import scenario

from consumer_agent_testing_tool.config import ModelProfile, interface_profile
from consumer_agent_testing_tool.connector_run import AgentDriver, ConnectorRun
from consumer_agent_testing_tool.connector_scenario import parse_scenario
from consumer_agent_testing_tool.evidence import Evidence
from consumer_agent_testing_tool.providers import (
    SPARK_OPEN_GAP,
    SPARK_OPEN_LINEAGE,
    model_lineage,
    record_model_context,
)

AUTH = {"mode": "api_key", "env": "OPENROUTER_API_KEY"}


def profile(model_id, provider="openrouter"):
    return ModelProfile(provider=provider, api="chat-completions", model_id=model_id, auth=AUTH)


def test_the_lineage_quotes_meta_s_model_page():
    assert SPARK_OPEN_LINEAGE["basis"] == "documented"
    assert SPARK_OPEN_LINEAGE["source"] == "https://dev.meta.ai/models/muse-spark-open"
    assert SPARK_OPEN_LINEAGE["quote"].startswith("Muse Spark Open is derived from the Muse Spark model we released "
                                                  "in August")
    assert "not the Muse Spark 1.3 model Muse launched on" in SPARK_OPEN_LINEAGE["detail"]


@pytest.mark.parametrize("model_id, labeled", [
    ("meta/muse-spark-open", True), ("meta/muse-spark-open-20261015", True), ("Muse-Spark-Open-816B-A42B", True),
    ("meta/muse-spark-1.3", False), ("example/user-sim-1", False), ("meta/muse-spark-opener", False)])
def test_only_spark_open_profiles_carry_the_lineage(model_id, labeled):
    assert (model_lineage(profile(model_id)) == SPARK_OPEN_LINEAGE) is labeled


def test_an_agent_sub_run_records_the_lineage_and_an_observation_gap(tmp_path):
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("api-tools-surrogate-v1"), lock={})
    record_model_context(profile("meta/muse-spark-open"), evidence)
    assert evidence.manifest["run_lock"]["model_lineage"] == SPARK_OPEN_LINEAGE
    assert evidence.manifest["observation_gaps"].count(SPARK_OPEN_GAP) == 1
    record_model_context(profile("meta/muse-spark-open"), evidence)  # Recorded once per run.
    assert evidence.manifest["observation_gaps"].count(SPARK_OPEN_GAP) == 1
    other = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                     profile=interface_profile("api-tools-surrogate-v1"), lock={})
    record_model_context(profile("meta/muse-spark-1.3", provider="meta"), other)
    assert "model_lineage" not in other.manifest["run_lock"] and SPARK_OPEN_GAP not in other.manifest[
        "observation_gaps"]


def test_a_connector_run_on_spark_open_says_so_in_its_lock_and_gaps(tmp_path):
    run = ConnectorRun(parse_scenario(scenario()), driver=AgentDriver(model_profile=profile("meta/muse-spark-open")),
                       root=tmp_path, output=tmp_path / "runs")
    assert run.run_lock["model_lineage"] == SPARK_OPEN_LINEAGE and SPARK_OPEN_GAP in run.observation_gaps
    plain = ConnectorRun(parse_scenario(scenario()), driver=AgentDriver(model_profile=profile("meta/muse-spark-1.3")),
                         root=tmp_path, output=tmp_path / "runs")
    assert "model_lineage" not in plain.run_lock and SPARK_OPEN_GAP not in plain.observation_gaps
