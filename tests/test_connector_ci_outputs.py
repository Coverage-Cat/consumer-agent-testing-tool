"""connector-run and check CI outputs: --junit (JUnit XML from the sealed result.json) and --summary-json
(one stable JSON document, validated against schemas/summary-v1.schema.json), against the loopback fixture."""
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
import yaml
from jsonschema import Draft202012Validator
from test_connector_e2e_support import EMAIL, EXAMPLES, document, fixture

from consumer_agent_testing_tool.ci_outputs import SUMMARY_SCHEMA, CiOutputs, check_reason
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.errors import LabError

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = [ROOT / "schemas/summary-v1.schema.json",
           ROOT / "src/consumer_agent_testing_tool/data/schemas/summary-v1.schema.json"]
SCENARIO, SCRIPT = "examples/fixture-booking.yaml", "examples/fixture-booking.calls.json"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI paths stay inside the workspace.
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    return tmp_path


async def cli(*arguments):
    return await execute(parser().parse_args([str(argument) for argument in arguments]))


async def connector_run(running, *extra, scenario=SCENARIO):
    return await cli("connector-run", scenario, "--driver", "scripted", "--script", SCRIPT,
                     "--set", f"PORT={running.port}", *extra)


def validate(summary):
    schema = json.loads(SCHEMAS[1].read_text())
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(summary), key=str)
    assert not errors, [error.message for error in errors]


def suites(path):
    root = ElementTree.parse(path).getroot()
    assert root.tag == "testsuites"
    return root, root.findall("testsuite")


def cases(suite):
    return {case.get("name"): case for case in suite.findall("testcase")}


def properties(suite):
    return {item.get("name"): item.get("value") for item in suite.find("properties")}


def failing_scenario(workspace):
    """The example with one check that fails (two bookings expected) and one with nothing to observe."""
    def change(scenario):
        scenario["checks"][0]["expect"]["value"] = 2
        scenario["checks"].append({"id": "loyalty-tier", "source": "oracle",
                                   "oracle": {"url": scenario["checks"][0]["oracle"]["url"],
                                              "pointer": "/loyalty_tier"},
                                   "expect": {"op": "eq", "value": "gold"}})
    (workspace / "examples" / "failing.yaml").write_text(yaml.safe_dump(document("fixture-booking.yaml", change)))
    return "examples/failing.yaml"


def test_summary_schema_files_match_the_contract():
    for path in SCHEMAS:
        assert json.loads(path.read_text()) == SUMMARY_SCHEMA
    Draft202012Validator.check_schema(SUMMARY_SCHEMA)


async def test_junit_has_one_suite_per_trial_and_one_case_per_check(workspace):
    async with fixture() as running:
        assert await connector_run(running, "--trials", "2", "--junit", "reports/junit.xml") == 0
    root, found = suites(workspace / "reports" / "junit.xml")
    assert [suite.get("name") for suite in found] == ["fixture-booking-happy-path trial 1",
                                                      "fixture-booking-happy-path trial 2"]
    assert root.get("tests") == "16" and root.get("failures") == root.get("errors") == root.get("skipped") == "0"
    for suite in found:
        run = Path(properties(suite)["run_directory"])
        result = json.loads((run / "result.json").read_text())
        named = cases(suite)
        # Every check of result.json, the execution outcome first, in its order; nothing failed or skipped.
        assert list(named) == [check["id"] for check in result["checks"]] and next(iter(named)) == "execution"
        assert {case.get("classname") for case in named.values()} == {"fixture-booking-happy-path"}
        assert all(len(case) == 0 for case in named.values())
        assert properties(suite)["status"] == "completed" and properties(suite)["exit_code"] == "0"
    assert not list((workspace / "reports").glob("*.tmp"))  # Written atomically.


async def test_junit_reports_failed_and_unobservable_checks_from_the_sealed_result(workspace):
    scenario = failing_scenario(workspace)
    async with fixture() as running:
        assert await connector_run(running, "--junit", "junit.xml", scenario=scenario) == 1
    text = (workspace / "junit.xml").read_text()
    root, (suite,) = suites(workspace / "junit.xml")
    result = json.loads((Path(properties(suite)["run_directory"]) / "result.json").read_text())
    checks = {check["id"]: check for check in result["checks"]}
    named = cases(suite)
    failure = named["one-confirmed-booking"].find("failure")
    assert failure.get("message") == check_reason(checks["one-confirmed-booking"]) == \
        'expected {"op": "eq", "value": 2}, actual 1'
    assert failure.get("type") == "verification" and failure.text == failure.get("message")
    skipped = named["loyalty-tier"].find("skipped")
    assert skipped.get("message") == "observation-gap: nothing at /loyalty_tier"
    assert named["execution"].find("failure") is None and named["execution"].find("error") is None
    # The answer's "confirmed" claim is false too, now that its check failed: two failures, both from result.json.
    assert sorted(name for name, case in named.items() if case.find("failure") is not None) == \
        sorted(check["id"] for check in result["checks"] if check["status"] == "failed") == \
        ["answer.no_false_claims", "one-confirmed-booking"]
    assert (root.get("failures"), root.get("skipped"), root.get("errors")) == ("2", "1", "0")
    assert properties(suite)["exit_code"] == "1" and properties(suite)["status"] == "failed"
    assert EMAIL not in text  # Private values stay out, as they stay out of result.json.


def test_junit_marks_blocked_checks_skipped_and_reporting_failures_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = tmp_path / "runs" / "connector-blocked"
    run.mkdir(parents=True)
    (run / "result.json").write_text(json.dumps({
        "status": "failed", "reporting_status": "failed", "failure_class": "reporting", "failure_stage": "reporting",
        "detail": "1 registered secret value(s) appear in this run's public evidence; it is not shareable",
        "checks": [{"id": "execution", "status": "passed", "detail": "The scripted calls ran to completion"},
                   {"id": "reconcile.create_booking.1", "status": "blocked", "class": "reconciliation",
                    "detail": "create_booking may have taken effect\x01 without a response"},
                   {"id": "optional-read", "status": "observation-gap", "required": False, "detail": "no read"}]}))
    outputs = CiOutputs(SimpleNamespace(junit=Path("junit.xml"), summary_json=None), "connector-run", tmp_path)
    outputs.scenario_id = "blocked-scenario"
    outputs.add_trial(run, 2)
    outputs.write(2)
    root, (suite,) = suites(tmp_path / "junit.xml")  # The control character was replaced: the XML parses.
    named = cases(suite)
    assert named["reconcile.create_booking.1"].find("skipped").get("message") == \
        "blocked: create_booking may have taken effect� without a response"
    assert named["optional-read"].find("skipped").get("message") == "observation-gap (not required): no read"
    assert named["reporting"].find("error").get("type") == "reporting"
    assert (root.get("tests"), root.get("errors"), root.get("skipped")) == ("4", "1", "2")


async def test_summary_json_on_stdout_is_the_only_stdout_output(workspace, capsys):
    async with fixture() as running:
        assert await connector_run(running, "--trials", "2", "--summary-json", "-") == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)  # One document and nothing else.
    validate(summary)
    assert summary["command"] == "connector-run" and summary["scenario_id"] == "fixture-booking-happy-path"
    assert (summary["exit_code"], summary["status"], summary["error"], summary["service"]) == (0, "completed", None,
                                                                                                None)
    assert summary["pass_hat_k"] == {"1": 1.0, "2": 1.0} and Path(summary["summary_run"]).is_dir()
    assert summary["treatment"] == {"projection": "native", "exposure": "in-prompt", "client_profile": "generic-sdk",
                                    "output_schema": "lenient"}
    for trial in summary["trials"]:
        assert trial["run"] in captured.err and Path(trial["run"], "result.json").is_file()
        assert trial["checks"][0] == {"id": "execution", "status": "passed", "required": True}
        assert (trial["status"], trial["exit_code"], trial["failure_stage"]) == ("completed", 0, None)
    assert "Connector report:" in captured.err and "Connector trials: 2 of 2 succeeded" in captured.err


async def test_summary_json_file_names_the_failing_trial(workspace, capsys):
    scenario = failing_scenario(workspace)
    async with fixture() as running:
        assert await connector_run(running, "--summary-json", "out/summary.json", "--output-schema", "strict",
                                   scenario=scenario) == 1
    summary = json.loads((workspace / "out" / "summary.json").read_text())
    validate(summary)
    (trial,) = summary["trials"]
    assert (summary["status"], summary["exit_code"], summary["pass_hat_k"]) == ("failed", 1, None)
    assert (trial["status"], trial["exit_code"], trial["failure_stage"], trial["failure_class"]) == \
        ("failed", 1, "checks", "verification")
    assert trial["reason"] == 'one-confirmed-booking: expected {"op": "eq", "value": 2}, actual 1'
    checks = {check["id"]: check for check in trial["checks"]}
    assert checks["loyalty-tier"] == {"id": "loyalty-tier", "status": "observation-gap", "required": True}
    assert summary["treatment"]["output_schema"] == "strict"
    assert str(trial["run"]) in capsys.readouterr().out  # Without "-", stdout keeps its run paths.


async def test_ci_outputs_record_an_error_before_any_trial(workspace):
    with pytest.raises(LabError, match="needs --script"):
        await cli("connector-run", SCENARIO, "--driver", "scripted", "--junit", "junit.xml",
                  "--summary-json", "summary.json")
    summary = json.loads((workspace / "summary.json").read_text())
    validate(summary)
    assert (summary["status"], summary["exit_code"], summary["trials"]) == ("error", 2, [])
    assert summary["error"]["class"] == "setup/infrastructure" and "--script" in summary["error"]["detail"]
    root, (suite,) = suites(workspace / "junit.xml")
    assert suite.get("name") == "fixture-booking-happy-path setup" and root.get("errors") == "1"
    assert "--script" in cases(suite)["setup"].find("error").get("message")


async def test_check_muse_guidelines_writes_junit_and_summary(workspace, capsys):
    async with fixture(variant="hidden-write") as running:
        assert await cli("check", running.mcp_url, "--suite", "muse-guidelines", "--junit", "g.xml",
                         "--summary-json", "-") == 1
    summary = json.loads(capsys.readouterr().out)
    validate(summary)
    assert (summary["command"], summary["suite"], summary["exit_code"], summary["status"]) == \
        ("check", "muse-guidelines", 1, "failed")
    assert summary["treatment"]["client_profile"] == "generic-sdk"
    _, (suite,) = suites(workspace / "g.xml")
    named = cases(suite)
    failed = [name for name, case in named.items() if case.find("failure") is not None]
    assert failed and all(name.startswith("3.2-hidden-write") for name in failed)
    assert named["3.2-steering"].find("failure") is None  # "no-finding" passes.
    assert all(case.find("skipped") is not None for name, case in named.items() if name.startswith("5.5-"))


async def test_check_ci_outputs_refuse_suites_they_cannot_read(workspace):
    with pytest.raises(LabError, match="contract, static and muse-guidelines"):
        await cli("check", "device.json", "--suite", "gadget", "--junit", "g.xml")
    assert not (workspace / "g.xml").exists()
