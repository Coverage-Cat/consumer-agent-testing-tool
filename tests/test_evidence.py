import json
import math

import pytest

from consumer_agent_testing_tool.config import interface_profile
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.evidence import Evidence, Redactor, export_redacted, wilson


def test_redaction_content_addresses_export_and_html(tmp_path):
    evidence = Evidence(tmp_path / "runs", lane="static", label="documented constraint",
                        profile=interface_profile("muse-cli-skills-public-2026-09"),
                        lock={}, redactor=Redactor(["sensitive-value"]))
    evidence.event("tool_result", {"authorization": "Bearer sensitive-value",
        "content": "sensitive-value alice@example.test <script>alert('bad')</script>"})
    evidence.finish("completed")
    artifact = next((evidence.path / "artifacts").glob("*.json"))
    assert "sensitive-value" not in artifact.read_text()
    assert "alice@example.test" not in artifact.read_text()
    (evidence.path / "raw-secret.txt").write_text("NEVER EXPORT")
    output = export_redacted(evidence.path, tmp_path / "export")
    assert not (output / "raw-secret.txt").exists()
    assert "<script>" not in (output / "report.html").read_text()
    assert json.loads((output / "manifest.json").read_text())["evidence_label"] == "documented constraint"


def test_export_tampering_rejected(tmp_path):
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("mcp-contract-2025-11-25"), lock={})
    evidence.event("tool_result", {"result": 1})
    next((evidence.path / "artifacts").glob("*.json")).write_text('{"result":2}')
    with pytest.raises(LabError):
        export_redacted(evidence.path, tmp_path / "export")
    assert not (tmp_path / "export").exists()
    assert not list(tmp_path.glob(".surface-export-*"))


@pytest.mark.parametrize("failure", ["metrics", "html"])
def test_reporting_failure_preserves_execution_result(tmp_path, monkeypatch, failure):
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile=interface_profile("muse-cli-skills-public-2026-09"),
                        lock={}, scenario_id="report-fixture")
    checks = [{"id": "backend.draft_count", "status": "passed", "actual": 1}]

    def fail(*args):
        raise OSError("Owned reporting failure")

    monkeypatch.setattr("consumer_agent_testing_tool.metrics.summarize_run" if failure == "metrics"
                        else "consumer_agent_testing_tool.evidence.write_report", fail)
    with pytest.raises(OSError, match="reporting failure"):
        evidence.finish("completed", checks)
    persisted = json.loads((evidence.path / "manifest.json").read_text())
    assert persisted["execution_status"] == "completed"
    assert persisted["reporting_status"] == "failed"
    assert persisted["checks"] == checks
    assert persisted["finished_at"]


async def test_cli_report_failure_does_not_reclassify_a_successful_check(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.cli import execute, initialize, parser

    config_path = tmp_path / "config.yaml"
    initialize(parser().parse_args(["init", "--config", str(config_path), "--provider", "none"]))

    async def passing_check(*args):
        return [{"id": "owned-static-fixture", "status": "passed"}]

    def fail(*args):
        raise OSError("Owned HTML failure")

    monkeypatch.setattr("consumer_agent_testing_tool.inspection.inspect_surface", passing_check)
    monkeypatch.setattr("consumer_agent_testing_tool.evidence.write_report", fail)
    with pytest.raises(OSError, match="HTML failure"):
        await execute(parser().parse_args(["check", "--suite", "static", "--config",
            str(config_path), "--output", str(tmp_path / "runs")]))
    persisted = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert persisted["execution_status"] == "completed"
    assert persisted["reporting_status"] == "failed"
    assert persisted["checks"] == [{"id": "owned-static-fixture", "status": "passed"}]


def test_export_recalculates_lengths_after_new_redaction(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.serialization import file_digest

    value = "later-recognized-secret-" + "a" * 120
    evidence = Evidence(tmp_path / "runs", lane="static", label="documented constraint",
                        profile=interface_profile("muse-cli-skills-public-2026-09"),
                        lock={}, redactor=Redactor([]))
    evidence.event("tool_result", {"content": value})
    evidence.finish("completed")
    monkeypatch.setenv("OPENAI_API_KEY", value)
    output = export_redacted(evidence.path, tmp_path / "export")
    event = json.loads((output / "events.jsonl").read_text().splitlines()[0])
    reference = event["artifacts"][0]
    artifact = output / reference["path"]
    assert value not in artifact.read_text()
    assert reference["sha256"] == file_digest(artifact)
    assert reference["bytes"] == len(artifact.read_bytes())
    assert reference["characters"] == len(artifact.read_text())


def test_failed_export_can_be_retried_without_changing_original_run(tmp_path, monkeypatch):
    import consumer_agent_testing_tool.evidence as module

    evidence = Evidence(tmp_path / "runs", lane="static", label="documented constraint",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    evidence.finish("completed")
    before = (evidence.path / "manifest.json").read_bytes()
    destination = tmp_path / "export"
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("Owned export report failure")
        patch.setattr(module, "write_report", fail)
        with pytest.raises(OSError):
            export_redacted(evidence.path, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".surface-export-*"))
    assert (evidence.path / "manifest.json").read_bytes() == before
    assert export_redacted(evidence.path, destination) == destination


async def test_cli_accepts_run_id_and_default_redacted_export(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.cli import execute, parser

    monkeypatch.chdir(tmp_path)
    evidence = Evidence(tmp_path / "runs", lane="static", label="documented constraint",
                        profile=interface_profile("muse-cli-skills-public-2026-09"), lock={})
    evidence.finish("completed")
    assert await execute(parser().parse_args(["report", evidence.run_id])) == 0
    assert await execute(parser().parse_args(["export", evidence.run_id, "--redact"])) == 0
    assert (tmp_path / "exports" / evidence.run_id / "report.html").is_file()


def test_wilson_denominators_and_lower_bounds():
    assert wilson(0, 0) is None
    assert math.isclose(wilson(30, 30)[0], .8864866, abs_tol=1e-6)
    assert math.isclose(wilson(10, 10)[0], .7224672, abs_tol=1e-6)
    with pytest.raises(ValueError):
        wilson(11, 10)
