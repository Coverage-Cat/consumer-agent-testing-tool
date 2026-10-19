"""report.html names where a run stopped and what the model was given (check:reports follow-ups): the attempt
a resumed run stopped at, the check that decided the step, the observed-input sizes the host counted, and the
fallbacks and views a run did not record. Synthetic, offline, loopback-only."""
import hashlib
import html
import json
import re

from test_connector_support import ORIGIN, agent, answer, booking_responses, call, run_connector
from test_connector_support import scenario as connector_scenario
from test_surface import FakeHost, scenario

from consumer_agent_testing_tool.sealed_report import diagnostics_links, document
from consumer_agent_testing_tool.surface import SurfaceRun, SurfaceScenario


def verdict_row(page):
    """(label, text) of the verdict's failing-step row."""
    verdict = page[page.index("<h2>Verdict"):page.index("<h2>Checks")]
    found = re.search(r"<dt>(Failing step|Stopped at step|Last recorded step)</dt><dd>(.*?)</dd>", verdict)
    return (found[1], html.unescape(found[2])) if found else None


def titles(page):
    return [html.unescape(title) for title in re.findall(r'id="step-\d+"><h3>Step \d+ · (.*?) <span', page)]


def write_events(path, rows):
    (path / "events.jsonl").write_text("".join(json.dumps({"sequence": index, **row}) + "\n"
                                               for index, row in enumerate(rows, 1)))


async def test_a_resumed_run_names_the_attempt_it_stopped_at(tmp_path):
    # The flow of test_surface.py::test_reconciliation_does_not_turn_negative_status_into_success.
    host = FakeHost(fail_after_save=True)
    first = SurfaceRun(tmp_path, scenario(), host, secrets={"access": "secret-value-1234"})
    assert await first.run() == 2
    host.saved = False
    resumed = SurfaceRun(tmp_path, scenario(), host, resume=first.path)
    assert await resumed.run() == 3 and resumed.path == first.path
    page = (resumed.path / "report.html").read_text()
    assert titles(page) == ["Setup", "status", "prepare", "form", "form"]  # The step, then its resumed attempt.
    assert verdict_row(page) == ("Stopped at step", "Step 5 · form (scenario step)")


async def test_a_resumed_run_that_fails_names_the_failed_attempt_and_its_check(tmp_path):
    data = scenario().model_dump()
    form = next(step for step in data["steps"] if step["id"] == "form")
    form["checks"] = [{"id": "form-done", "layer": "service", "path": "/form/result/reconciled", "expected": False}]
    contract = SurfaceScenario.model_validate(data)
    host = FakeHost(fail_after_save=True)
    first = SurfaceRun(tmp_path, contract, host, secrets={"access": "secret-value-1234"})
    assert await first.run() == 2
    resumed = SurfaceRun(tmp_path, contract, host, resume=first.path)
    assert await resumed.run() == 1
    page = (resumed.path / "report.html").read_text()
    assert verdict_row(page) == ("Failing step", "Step 5 · form (scenario step); check form-done failed")
    assert re.findall(r'<section class="step failed" id="step-(\d+)">', page) == ["5"]  # Row and marker agree.


async def test_the_failing_step_names_the_check_that_failed_not_an_earlier_gap(tmp_path):
    document_ = connector_scenario()
    # Declared first: an oracle field the fixture never reports (an observation gap), then the booking check.
    document_["checks"].insert(0, {"id": "loyalty-points-recorded", "source": "oracle",
                                   "oracle": {"url": f"{ORIGIN}/__oracle/state", "pointer": "/loyalty_points"},
                                   "expect": {"op": "eq", "value": 10}})
    dates = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
    driver, _ = agent([call("search_rooms", **dates), answer("I booked the Garden double for $420.")])
    run, code, _ = await run_connector(tmp_path, document_, driver=driver)
    assert code == 1, run.summary()
    page = (run.path / "report.html").read_text()
    reason = re.search(r"<dt>Reason</dt><dd>(.*?)</dd>", page)[1]
    assert html.unescape(reason).startswith("one-confirmed-booking: ")
    expected = "Step 3 · Final answer (final answer); check one-confirmed-booking failed"
    assert verdict_row(page) == ("Failing step", expected)


async def test_observed_input_sizes_what_the_model_was_given(tmp_path):
    driver, _ = agent(booking_responses())
    run, code, _ = await run_connector(tmp_path, driver=driver)
    assert code == 0, run.summary()
    page = (run.path / "report.html").read_text()
    observed = page[page.index("<h3>Observed input"):page.index("<h3>Developer diagnostics")]
    rows = {sequence: (kind, characters, size, basis) for sequence, kind, characters, size, basis in re.findall(
        r"<tr><td>[^<]*</td><td>(\d+)</td><td><code>([a-z_]+)</code></td><td>(.*?)</td><td>(.*?)</td><td>.*?</td>"
        r"<td>(.*?)</td></tr>", observed)}
    recorded = [json.loads(line) for line in (run.path / "events.jsonl").read_text().splitlines()]
    results = [event for event in recorded if event["kind"] == "tool_result"]
    assert len(results) == 3
    for event in results:  # The host's own count of the text the model received, not the record's size.
        _, characters, _, basis = rows[str(event["sequence"])]
        assert characters == str(event["characters"]) != str(event["artifacts"][0]["characters"])
        assert basis == "host count of the text the model received"
    projection = next(event for event in recorded if event["kind"] == "projection")
    offered = projection["system_prompt"] + json.dumps(projection["model_tools"], separators=(",", ":"))
    _, characters, _, basis = rows[str(projection["sequence"])]
    assert characters == str(len(offered)) and int(characters) < projection["artifacts"][0]["characters"] / 2
    assert basis.startswith("recorded system_prompt, model_tools")
    # The model's own proposals are not input.
    assert "<code>tool_proposed</code>" not in observed and "tool_proposed" in page


def test_without_a_failed_check_or_named_step_the_last_recorded_step_is_labeled_so(tmp_path):
    write_events(tmp_path, [{"kind": "step_started", "id": "pay"}, {"kind": "host_action", "operation": "pay"}])
    page = document(tmp_path, {"status": "failed", "failure_stage": "receipt", "detail": "Synthetic stop"})
    assert verdict_row(page) == ("Last recorded step", "Step 1 · pay (scenario step)")


def test_a_re_snapshotted_generation_stays_one_step(tmp_path):
    write_events(tmp_path, [{"kind": "snapshot", "generation": 1, "text": "Synthetic page"},
                            {"kind": "browser_action", "action": "click", "ref": "e1", "status": "refused"},
                            {"kind": "snapshot", "generation": 1, "text": "Synthetic page, continued"},
                            {"kind": "snapshot", "generation": 2, "text": "Synthetic next page"}])
    page = document(tmp_path, {"status": "completed"})
    assert titles(page) == ["Generation 1", "Generation 2"]


def test_views_a_run_did_not_record_are_named_as_absent(tmp_path):
    write_events(tmp_path, [{"kind": "step_started", "id": "pay"},
                            {"kind": "oracle_snapshot", "state": {"confirmed_count": 1}}])
    page = document(tmp_path, {"status": "completed"})
    assert "<h3>Developer diagnostics" in page
    assert "<p class=muted>Not recorded in this run: published surface, exposed surface, observed input.</p>" in page


def test_published_but_not_exposed_follows_the_projection_name_map(tmp_path):
    write_events(tmp_path, [
        {"kind": "catalog", "connector": "cedar", "tools": [{"name": "search"}, {"name": "cancel"}]},
        {"kind": "projection", "visibility": "model", "model_tools": [{"name": "cedar_search"}],
         "name_map": {"cedar_search": {"connector": "cedar", "tool": "search"}}}])
    page = document(tmp_path, {"status": "completed"})
    assert "<p>Published but not exposed: cancel (cedar)</p>" in page


async def test_the_exposed_view_shows_the_skill_text_a_cli_profile_exposes(tmp_path):
    from consumer_agent_testing_tool.cli import execute, parser
    from consumer_agent_testing_tool.config import Config
    config = tmp_path / "config.json"
    config.write_text(Config().model_dump_json())
    arguments = ["inspect", "--config", str(config), "--interface-profile", "muse-cli-skills-public-2026-09",
                 "--output", str(tmp_path / "runs")]
    assert await execute(parser().parse_args(arguments)) == 0
    [run] = (tmp_path / "runs").glob("r-*")
    page = (run / "report.html").read_text()
    exposed = page[page.index("<h3>Exposed surface"):page.index("<h3>Observed input")]
    assert "<p>Skill text:</p><pre>---\nname: booking" in exposed


def test_a_trace_name_the_harness_did_not_write_is_never_linked(tmp_path):
    private = tmp_path / "diagnostics.private"
    private.mkdir()
    (private / "trace.zip").write_bytes(b"synthetic trace")  # Not the harness's <sha256>.zip name.
    (private / "index.html").write_text("<p>Synthetic index</p>")
    (private / "manifest.json").write_text(json.dumps({"trace": {"status": "captured", "path": "trace.zip"}}))
    pinned = {"index_sha256": "recorded", "trace_status": "captured",
              "manifest_sha256": hashlib.sha256((private / "manifest.json").read_bytes()).hexdigest()}
    items = "".join(diagnostics_links(tmp_path, {"diagnostics": pinned}))
    assert "diagnostics.private/trace.zip" not in items and "--trace-command" not in items
    assert "Playwright trace not linked: its name or private manifest failed verification." in items
