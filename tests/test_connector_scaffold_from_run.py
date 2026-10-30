"""Record to scenario: scaffold --from-run turns a sealed, shareable agent trial's model tool calls into a
--driver scripted calls file and a checks skeleton, never copying a value the run's evidence hid."""
import pytest
import yaml
from test_connector_support import (
    API_KEY,
    BaseExtension,
    agent,
    answer,
    booking_responses,
    call,
    result_of,
    run_connector,
    scenario,
)

from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.connector_run import (
    EXTENSIONS,
    NoConfig,
    ScriptedDriver,
    checks_path,
)
from consumer_agent_testing_tool.connector_scenario import SCAFFOLD_MARKER
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import load

EMAIL = "taylor.private@example.com"  # The support scenario's private value.


async def recorded(workspace, emulation_target="muse"):
    """An agent trial: search, quote, book, look the booking up by its (captured, so hidden) ID, then report it."""
    steps = booking_responses()[:3] + [call("ask_user", question="Shall I check the booking status too?"),
                                       call("get_booking", booking_id="BK-1000"),
                                       answer("Booked: Garden double, $420.00 total, confirmation BK-1000.")]
    driver, _ = agent(steps)
    run, code, _ = await run_connector(workspace, scenario(emulation_target=emulation_target), driver=driver)
    assert code == 0, run.summary()
    return run


async def scaffold(*arguments):
    return await execute(parser().parse_args(["scaffold", *map(str, arguments)]))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the directory the command runs from.
    return tmp_path


@pytest.mark.parametrize("emulation_target", ["muse", "instinct"])
async def test_a_recorded_agent_trial_becomes_a_script_and_a_checks_skeleton(
        workspace, capsys, emulation_target):
    run = await recorded(workspace, emulation_target)
    output = workspace / "booking.calls.yaml"
    assert await scaffold("--from-run", run.path, "--output", output) == 0
    checks = checks_path(output)
    assert checks == workspace / "booking.checks.yaml"
    assert capsys.readouterr().out.splitlines()[-2:] == [str(output), str(checks)]
    steps = yaml.safe_load(output.read_text())
    assert [step.get("tool", "answer" if "answer" in step else None) for step in steps] == [
        "search_rooms", "get_quote", "create_booking", "ask_user", "get_booking", "answer"]
    assert steps[2]["arguments"] == {"quote_id": "q-1-garden-2", "guest_name": "Taylor Example"}
    # The booking ID was captured, so the run's evidence hides it: a placeholder, never a copy or a marker.
    assert steps[4]["arguments"]["booking_id"].startswith(f"{SCAFFOLD_MARKER}: a value this run's evidence hides")
    assert any(isinstance(part, str) and part.startswith(SCAFFOLD_MARKER) for part in steps[5]["answer"])
    text = output.read_text() + checks.read_text()
    assert "BK-1000" not in text and EMAIL not in text and API_KEY not in text and "[REDACTED]" not in text
    skeleton = yaml.safe_load(checks.read_text())
    assert skeleton["emulation_target"] == emulation_target
    assert [item["id"] for item in skeleton["checks"]] == ["create_booking-outcome"]
    assert skeleton["answer"]["claims"][0]["true_when"] == "create_booking-outcome"
    assert "one-confirmed-booking passed" in checks.read_text() and "search_rooms, get_quote" in checks.read_text()
    with pytest.raises(LabError, match="TODO\\(scaffold\\) placeholders"):
        ScriptedDriver(steps)  # connector-run --script refuses it until the placeholders are replaced.
    # With the placeholders written as references to earlier results, the script replays the trial.
    steps[4]["arguments"]["booking_id"] = {"$ref": "/results/2/result/structuredContent/booking_id"}
    steps[5]["answer"] = "Booked: Garden double, $420.00 total."
    replay, code, hotel = await run_connector(workspace, driver=ScriptedDriver(steps))
    assert code == 0, replay.summary()
    assert [name for name, _ in hotel.calls] == ["search_rooms", "get_quote", "create_booking", "get_booking"]


async def test_only_a_sealed_shareable_agent_trial_is_read(workspace):
    run = await recorded(workspace)
    scripted, code, _ = await run_connector(workspace)
    assert code == 0
    with pytest.raises(LabError, match="is a scripted replay"):
        await scaffold("--from-run", scripted.path, "--output", "a.calls.yaml")
    with pytest.raises(LabError, match="does not apply to --from-run"):
        await scaffold("http://127.0.0.1:8765/mcp", "--from-run", run.path, "--output", "b.calls.yaml")
    for target in ("muse", "instinct"):
        with pytest.raises(LabError, match="--emulation-target does not apply to --from-run"):
            await scaffold("--from-run", run.path, "--emulation-target", target, "--output", "target.calls.yaml")
    assert not (workspace / "target.calls.yaml").exists()
    (workspace / "c.calls.yaml").write_text("[]")
    with pytest.raises(LabError, match="already exists"):
        await scaffold("--from-run", run.path, "--output", "c.calls.yaml")
    with pytest.raises(LabError, match="needs the MCP endpoint"):
        await scaffold("--output", "d.yaml")
    result = run.path / "result.json"
    sealed = result.read_text()
    result.write_text(sealed.replace('"reporting_status": "completed"', '"reporting_status": "failed"'))
    with pytest.raises(LabError, match="Evidence integrity check failed"):  # Tampered evidence: never read.
        await scaffold("--from-run", run.path, "--output", "e.calls.yaml")
    assert not (workspace / "e.calls.yaml").exists() and not (workspace / "e.checks.yaml").exists()


class Leaker(BaseExtension):
    """Records a value in an event, then registers it as private: the run's leak scan finds it."""
    config_model = NoConfig

    async def on_final(self, run, final_text):
        run.event("probe.note", {"text": "reference v-leak-5521"})
        run.register_private("late_reference", "v-leak-5521")
        return []


async def test_an_unshareable_trial_is_refused(workspace, monkeypatch):
    monkeypatch.setitem(EXTENSIONS, "probe-leaker", Leaker)
    driver, _ = agent(booking_responses())
    run, code, _ = await run_connector(workspace, scenario(extensions={"probe-leaker": {}}), driver=driver)
    assert code == 2 and result_of(run)["reporting_status"] == "failed"
    with pytest.raises(LabError, match="is not shareable"):
        await scaffold("--from-run", run.path, "--output", "f.calls.yaml")


async def test_a_json_calls_file_is_written_as_json_that_connector_run_reads(workspace):
    # connector-run --script loads a .json file as JSON only (serialization.load), like the repo's .calls.json files.
    run = await recorded(workspace)
    assert await scaffold("--from-run", run.path, "--output", "booking.calls.yaml") == 0
    assert await scaffold("--from-run", run.path, "--output", "other.calls.json") == 0
    steps = load(workspace / "other.calls.json")
    assert steps == yaml.safe_load((workspace / "booking.calls.yaml").read_text())
    assert (workspace / "other.checks.yaml").read_text().startswith("# Checks skeleton for")
    with pytest.raises(LabError, match="TODO\\(scaffold\\) placeholders"):
        ScriptedDriver(steps)
    steps[4]["arguments"]["booking_id"] = {"$ref": "/results/2/result/structuredContent/booking_id"}
    steps[5]["answer"] = "Booked: Garden double, $420.00 total."
    replay, code, _ = await run_connector(workspace, driver=ScriptedDriver(steps))
    assert code == 0, replay.summary()
