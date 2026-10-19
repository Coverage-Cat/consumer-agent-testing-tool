"""report.html in the DX review's order (§5 Reports): the verdict and failing step, a per-step timeline with
what the model received inline, the published, exposed, observed and diagnostic views (spec §8), metrics,
and the viewers: Inspect View and, when diagnostics exist, the Playwright trace. Offline and redacted."""
import hashlib
import html
import json
import re

from test_browser_portals import portal as portal_fixture
from test_connector_support import (
    API_KEY,
    agent,
    answer,
    booking_responses,
    call,
    run_connector,
    structured,
)
from test_connector_support import scenario as connector_scenario
from test_evidence_p0_report import LostHost, assert_offline, hrefs
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.config import Config
from consumer_agent_testing_tool.replay import (
    export_public_run,
    exported_private_values,
    playback,
    seal,
)
from consumer_agent_testing_tool.sealed_report import (
    RENDERER,
    document,
    finalize,
    report_unavailable,
    view,
)
from consumer_agent_testing_tool.surface import SurfaceRun

EMAIL = "taylor.private@example.com"  # The connector scenario's host-private value.
SECRET = "original-secret-canary"
portal = portal_fixture


def text_of(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "|", fragment))


def step_titles(page):
    return [html.unescape(title) for title in re.findall(r'id="step-\d+"><h3>Step \d+ · (.*?) <span', page)]


def step_sections(page):
    """{step title: section HTML} of the timeline (titles that repeat keep their last step)."""
    sections = re.findall(r'<section class="step[^"]*" id="step-\d+">.*?</section>', page, flags=re.DOTALL)
    return dict(zip(step_titles(page), sections, strict=True))


def kinds(section):
    return re.findall(r"<td><code>([a-z_.-]+)</code></td>", section)


async def connector_page(tmp_path):
    driver, _ = agent(booking_responses())
    run, code, _ = await run_connector(tmp_path, driver=driver)
    assert code == 0, run.summary()
    return run, (run.path / "report.html").read_text()


async def test_connector_report_follows_the_review_order_and_names_inspect_view(tmp_path):
    run, page = await connector_page(tmp_path)
    assert_offline(page)
    order = ["<h2>Verdict", "<h2>Checks", "<h2>Timeline", "<h2>Surface views", "<h3>Published surface",
             "<h3>Exposed surface", "<h3>Observed input", "<h3>Developer diagnostics", "<h2>Metrics",
             "<h2>Viewers and linked evidence", "<h2>Observation gaps"]
    assert [page.index(heading) for heading in order] == sorted(page.index(heading) for heading in order)
    agent_run = json.loads((run.path / "result.json").read_text())["agent_runs"][0]
    viewers = page[page.index("<h2>Viewers and linked evidence"):page.index("<h2>Observation gaps")]
    assert f"Inspect View: <code>inspect view --log-dir &lt;run&gt;/{agent_run}/inspect</code>" in viewers
    assert f"{agent_run}/inspect/trial-0.eval" in hrefs(viewers) and f"{agent_run}/report.html" in hrefs(viewers)
    assert all((run.path / href).is_file() for href in hrefs(page))
    assert API_KEY not in page and EMAIL not in page
    assert document(run.path, playback(run.path)["result"]) == page  # Deterministic.


async def test_connector_views_show_published_exposed_observed_and_what_changed(tmp_path):
    run, page = await connector_page(tmp_path)
    published = page[page.index("<h3>Published surface"):page.index("<h3>Exposed surface")]
    exposed = page[page.index("<h3>Exposed surface"):page.index("<h3>Observed input")]
    observed = page[page.index("<h3>Observed input"):page.index("<h3>Developer diagnostics")]
    diagnostics = page[page.index("<h3>Developer diagnostics"):page.index("<h2>Metrics")]
    assert "<td>cancel_booking</td>" in published and "<td>cancel_booking</td>" not in exposed
    assert "quote_id*, guest_name*, guest_email*, idempotency_key" in published  # As the server published it.
    assert "quote_id*, guest_name*, idempotency_key" in exposed  # The host fills guest_email.
    assert "Published but not exposed: cancel_booking (cedar-stays)" in exposed
    changes = text_of(exposed)
    assert "lost keywords: /properties/adults/exclusiveMaximum" in changes
    assert "host owned removed: guest_email" in changes
    assert observed.count("<code>tool_result</code>") == 3 and "not estimated: tokenizer unavailable" in observed
    assert "<code>service_response</code>" in diagnostics and "<code>tool_result</code>" not in diagnostics
    assert "Backend state, as last observed independently" in diagnostics and "confirmed_count" in diagnostics
    # The exported report keeps the views and is still free of the run's private values.
    exported = export_public_run(run.path, tmp_path / "export", secrets=[API_KEY, EMAIL])
    copy = (exported / "report.html").read_text()
    assert "Published but not exposed: cancel_booking (cedar-stays)" in copy and EMAIL not in copy
    assert not exported_private_values(exported, [API_KEY, EMAIL])


async def test_cli_projection_steps_and_views_follow_the_exec_commands(tmp_path):
    def run(command, **extra):
        return call("exec", command=command, **extra)

    def calling(tool, **arguments):
        return f"cedar-stays call-tool --name {tool} --arguments-json '{json.dumps(arguments)}'"
    dates = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
    responses = [run("cat cedar-stays.md"), run("cedar-stays list-tools --names-only"),
                 run(calling("search_rooms", **dates)),
                 lambda input: run(calling("get_quote", room_id=structured(input, "room_id"), **dates)),
                 lambda input: run(calling("create_booking", quote_id=structured(input, "quote_id"),
                                           guest_name="Taylor Example")),
                 answer("Booked the Garden double for $420.00.")]
    driver, _ = agent(responses)
    cli = connector_scenario(projection="muse-cli")
    cli["connectors"][0].update(doc="connectors/cedar-stays.md", client_profile="muse-template-unofficial-2026-10")
    (tmp_path / "connectors").mkdir()
    (tmp_path / "connectors" / "cedar-stays.md").write_text("---\nname: cedar-stays\ndescription: Stays.\n---\n")
    connector_run, code, _ = await run_connector(tmp_path, cli, driver=driver)
    assert code == 0, connector_run.summary()
    page = (connector_run.path / "report.html").read_text()
    # The doc read and the tool listing are host-handled commands: each result is its own step.
    assert step_titles(page) == ["Setup", "exec", "exec", "search_rooms (cedar-stays)", "get_quote (cedar-stays)",
                                 "create_booking (cedar-stays)", "Final answer"]
    exposed = page[page.index("<h3>Exposed surface"):page.index("<h3>Observed input")]
    assert "<td>exec</td>" in exposed and "<td>search_rooms</td><td>Search" not in exposed  # One exec tool.
    assert "Published but not exposed: cancel_booking (cedar-stays)" in exposed  # The CLI lists allowed tools.


async def test_timeline_has_one_section_per_step_with_the_model_input_inline(tmp_path):
    from test_surface_agent import driver
    planner, _ = driver()
    run = SurfaceRun(tmp_path, scenario(), FakeHost(), secrets={"access": SECRET}, planner=planner)
    assert await run.run() == 0
    page = (run.path / "report.html").read_text()
    sections = step_sections(page)
    assert step_titles(page) == ["Setup", "status", "prepare", "form", "verify", "Turn 5"]
    # A step holds the observation the model received, its proposal and what the host did with it.
    assert kinds(sections["status"]) == ["agent_observation", "agent_proposal", "agent_action", "step_started",
                                         "host_action", "service_response"]
    observation = next(row for row in sections["prepare"].split("<tr>") if "agent_observation" in row)
    assert "<pre>" in observation and "Prepare form" in observation
    assert "ui_callback" in kinds(sections["form"]) and kinds(sections["verify"])[-1] == "assertion"
    index = page[page.index("<h2>Timeline"):page.index('<section class="step')]
    assert "<td>verify</td><td>scenario step, model turn 4</td>" in index
    assert SECRET not in page and "rotated-secret-canary" not in page


async def test_failed_report_names_the_failing_step_and_marks_it(tmp_path):
    run = SurfaceRun(tmp_path, scenario(), LostHost(), secrets={"access": SECRET})
    assert await run.run() == 1
    page = (run.path / "report.html").read_text()
    verdict = page[page.index("<h2>Verdict"):page.index("<h2>Checks")]
    assert "<dt>Failing step</dt><dd>Step 5 · verify (scenario step); check saved failed</dd>" in verdict
    assert '<section class="step failed" id="step-5"><h3>Step 5 · verify' in page
    assert page.count('class="step failed"') == 1


def test_failing_step_comes_from_the_outcome_not_from_reporting(tmp_path):
    rows = [{"sequence": 1, "kind": "step_started", "id": "pay"},
            {"sequence": 2, "kind": "assertion", "id": "paid", "status": "failed", "expected": 1, "actual": 0},
            {"sequence": 3, "kind": "step_started", "id": "receipt"}]
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    failed = document(tmp_path, {"status": "failed"})
    assert "<dt>Failing step</dt><dd>Step 1 · pay (scenario step); check paid failed</dd>" in failed
    # Failed only to report (verification completed): no step failed.
    assert "Failing step" not in document(tmp_path, {"status": "failed", "verification_status": "completed"})
    blocked = document(tmp_path, {"status": "blocked", "failure_stage": "receipt"})
    assert "<dt>Stopped at step</dt><dd>Step 2 · receipt (scenario step)</dd>" in blocked
    assert "Failing step" not in document(tmp_path, {"status": "completed"})


async def test_inspect_report_shows_the_four_views_without_inference(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(Config().model_dump_json())
    assert await execute(parser().parse_args(["inspect", "--config", str(config), "--interface-profile",
                                              "mcp-contract-2025-11-25", "--output", str(tmp_path / "runs")])) == 0
    [run] = (tmp_path / "runs").glob("r-*")
    page = (run / "report.html").read_text()
    assert_offline(page)
    views = page[page.index("<h2>Surface views"):page.index("<details><summary>Run lock")]
    assert views.count("<h3>Published surface") == 1 and "<td>reservations_create</td>" in views
    assert "Identical to the published surface: no filtering or transformation was recorded." in views
    assert "Visibility: " in views and "invoked: no" in views
    assert "<h3>Observed input</h3><p>not generated (No inference requested)</p>" in views
    assert page.index("<h2>Timeline") < page.index("<h2>Surface views")


def test_trace_link_needs_the_private_manifest_the_result_pins(tmp_path):
    from consumer_agent_testing_tool.sealed_report import diagnostics_links
    private = tmp_path / "diagnostics.private"
    private.mkdir()
    trace = hashlib.sha256(b"synthetic trace").hexdigest() + ".zip"
    (private / trace).write_bytes(b"synthetic trace")
    (private / "index.html").write_text("<p>Synthetic index</p>")
    (private / "manifest.json").write_text(json.dumps({"trace": {"status": "captured", "path": trace}}))
    pinned = {"index_sha256": "recorded", "trace_status": "captured",
              "manifest_sha256": hashlib.sha256((private / "manifest.json").read_bytes()).hexdigest()}
    items = "".join(diagnostics_links(tmp_path, {"diagnostics": pinned}))
    assert f'<a href="diagnostics.private/{trace}">Playwright trace</a>' in items and "--trace-command" in items
    assert '<a href="diagnostics.private/index.html">' in items
    changed = "".join(diagnostics_links(tmp_path, {"diagnostics": {**pinned, "manifest_sha256": "0" * 64}}))
    # Updated (check:reports follow-up): fails closed for the command too, not only the link, as claimed.
    assert trace not in changed and "--trace-command" not in changed
    assert "Playwright trace not linked: its name or private manifest failed verification." in changed
    assert diagnostics_links(tmp_path, {"diagnostics": {"status": "excluded"}}) == []


async def test_browser_report_links_the_captured_trace_and_exports_drop_it(portal, tmp_path):
    snapshot = await portal.open("validation", diagnostics={"enabled": True, "retain_raw_trace": True})
    await portal.action(snapshot, "Submit")
    await portal.broker.close()
    portal.broker.finish("completed")
    path = portal.broker.path
    page = (path / "report.html").read_text()
    manifest = json.loads((path / "diagnostics.private" / "manifest.json").read_text())
    trace = f"diagnostics.private/{manifest['trace']['path']}"
    viewers = page[page.index("<h2>Viewers and linked evidence"):]
    assert trace in hrefs(viewers) and (path / trace).is_file() and "--trace-command" in viewers
    assert "diagnostics.private/index.html" in hrefs(viewers)
    assert {"Generation 1", "Generation 2"} <= set(step_titles(page))
    generation = step_sections(page)["Generation 1"]
    assert "<pre>" in generation and "browser_action" in kinds(generation)
    exported = export_public_run(path, tmp_path / "public")
    copy = (exported / "report.html").read_text()
    assert "diagnostics.private/" not in copy and "Private browser diagnostics: excluded from this export." in copy
    assert all((exported / href).is_file() for href in hrefs(copy))


def test_reports_from_the_previous_renderer_stay_recoverable(tmp_path):
    assert RENDERER == "consumer-agent-testing-tool-static-v3"  # The layout changed: a new renderer identity.
    finalize(tmp_path, {"status": "failed", "detail": "Synthetic failure"})
    assert json.loads((tmp_path / "result.json").read_text())["html_report"]["renderer"] == RENDERER
    result = json.loads((tmp_path / "result.json").read_text())
    result.update(html_report={"schema_version": 1, "renderer": "consumer-agent-testing-tool-static-v2",
                               "status": "failed"}, reporting_status="failed")
    (tmp_path / "report.html").unlink()
    (tmp_path / "result.json").write_text(json.dumps(result))
    seal(tmp_path)
    assert report_unavailable(result)
    copy = view(tmp_path).read_text()
    assert "<h2>Verdict" in copy and "Synthetic failure" in copy and "<h2>Timeline" in copy
