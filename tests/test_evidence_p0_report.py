"""P0 reports: structured, escaped, offline HTML that shows the verdict first and the trajectory."""
import json
import re

from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.evidence import Evidence, Redactor, write_report
from consumer_agent_testing_tool.replay import export_public_run, playback
from consumer_agent_testing_tool.sealed_report import (
    RENDERER,
    document,
    finalize,
    report_unavailable,
    view,
)
from consumer_agent_testing_tool.surface import SurfaceRun


def hrefs(text):
    return re.findall(r'href="([^"]+)"', text)


def assert_offline(text):
    assert "<script" not in text and " src=" not in text and "<link" not in text
    assert all(not href.startswith(("http:", "https:", "javascript:", "/")) and ".." not in href
               for href in hrefs(text))


class LostHost(FakeHost):
    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        if name == "status" and self.saved:
            result["structuredContent"]["status"] = "lost"
        return result


async def test_sealed_report_shows_verdict_checks_and_step_timeline(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    text = (run.path / "report.html").read_text()
    assert_offline(text)
    assert text.index("Verdict") < text.index("<h2>Checks") < text.index("<h2>Timeline")
    assert "<td>saved</td>" in text and "<td>verify</td>" in text and "service_response" in text
    assert all(f"<td>{step}</td>" in text for step in ("status", "prepare", "form"))
    assert "<details><summary>Run lock</summary>" in text and not text.split("<h1>")[0].count("<pre>")
    assert "original-secret-canary" not in text and "rotated-secret-canary" not in text
    assert json.loads((run.path / "result.json").read_text())["html_report"]["renderer"] == RENDERER
    assert document(run.path, playback(run.path)["result"]) == text  # Deterministic.


async def test_failed_run_report_leads_with_failure_stage_and_reason(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), LostHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() != 0
    text = (run.path / "report.html").read_text()
    verdict = text[text.index("Verdict"):text.index("<h2>Checks")]
    assert '<section class="verdict s-failed">' in text
    assert "<dt>Overall status</dt><dd><strong class=\"s-failed\">failed</strong></dd>" in verdict
    assert "<dt>Failure stage</dt><dd>verify</dd><dt>Failure class</dt><dd>assertion</dd>" in verdict
    assert ("<dt>Reason</dt><dd>service assertion saved failed: expected &quot;saved&quot;, "
            "actual &quot;lost&quot;</dd>") in verdict
    # The checks table shows expected and actual side by side.
    assert ('<td>saved</td><td>service</td><td><strong class="s-failed">failed</strong></td>'
            "<td>saved</td><td>lost</td>") in text


async def test_agent_report_links_sub_run_and_inspect_log_with_model_input_inline(tmp_path):
    from test_surface_agent import driver
    planner, _ = driver()
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"},
                     planner=planner)
    assert await run.run() == 0
    agent = run.state["agent_runs"][-1]
    text = (run.path / "report.html").read_text()
    links = hrefs(text)
    assert f"{agent}/report.html" in links and f"{agent}/inspect/trial-0.eval" in links
    assert all((run.path / href).is_file() for href in links)
    # What the model saw is inline, not only behind a hash link.
    row = next(item for item in text.split("<tr>") if "agent_observation" in item)
    assert "<pre>" in row and "available_actions" in row
    child = (run.path / agent / "report.html").read_text()
    assert_offline(child)
    assert "inspect/trial-0.eval" in hrefs(child) and "observation_emitted" in child
    observation = next(item for item in child.split("<tr>") if "observation_emitted" in item)
    assert "<pre>" in observation and "original-secret-canary" not in child


def test_core_report_renders_checks_failure_metrics_and_escapes(tmp_path):
    evidence = Evidence(tmp_path / "runs", lane="contract", label="protocol-tested",
                        profile={"name": "synthetic", "surface": "mcp", "label": "synthetic fixture"},
                        lock={"model": {"id": "synthetic"}}, redactor=Redactor([]))
    evidence.event("observation_emitted", {"messages": [{"role": "user", "content": "<script>x()</script>"}]},
                   audience="model")
    evidence.manifest["execution_failure"] = {"class": "interaction", "code": 1, "detail": "Turn limit reached"}
    evidence.manifest["metrics"] = {"model_seconds": 1.5}
    evidence.finish("failed", [{"id": "backend.count", "status": "failed", "expected": 1, "actual": 0}])
    text = write_report(evidence.path).read_text()
    assert_offline(text)
    assert "<script>" not in text and "&lt;script&gt;" in text
    verdict = text[text.index("Verdict"):text.index("<h2>Checks")]
    assert "interaction" in verdict and "Turn limit reached" in verdict
    assert "<td>backend.count</td>" in text and "model_seconds" in text
    assert "<details><summary>Run lock</summary>" in text


async def test_exported_report_is_structured_and_legacy_failure_descriptors_recover(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": "original-secret-canary"})
    assert await run.run() == 0
    exported = export_public_run(run.path, tmp_path / "export")
    text = (exported / "report.html").read_text()
    assert "Verdict" in text and "<h2>Timeline" in text and "original-secret-canary" not in text
    assert all((exported / href).is_file() for href in hrefs(text))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    finalize(legacy, {"status": "completed"})
    result = json.loads((legacy / "result.json").read_text())
    result["html_report"] = {"schema_version": 1, "renderer": "surface-lab-static-v1", "status": "failed"}
    result.update(reporting_status="failed", status="failed")
    (legacy / "report.html").unlink()
    (legacy / "result.json").write_text(json.dumps(result))
    from consumer_agent_testing_tool.replay import seal
    seal(legacy)
    assert report_unavailable(result)
    assert "Verdict" in view(legacy).read_text()
