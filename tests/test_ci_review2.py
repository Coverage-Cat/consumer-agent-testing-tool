"""Concrete reproductions from the second committed Claude review."""
import asyncio
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_ci_execution import fixture_inputs
from test_ci_plan import suite_document, write_inputs
from test_connector_support import scenario
from test_scripted_portable import portable_script

from consumer_agent_testing_tool.ci_results import SuiteReport, atomic_write
from consumer_agent_testing_tool.ci_suite import Expectation, PlanError, prepare_suite
from consumer_agent_testing_tool.ci_validation import validate_expectation
from consumer_agent_testing_tool.cli import execute, parser
from consumer_agent_testing_tool.service_start import Service


async def test_report_write_failure_returns_observed_rows_as_error_json(tmp_path, monkeypatch, capsys):
    from consumer_agent_testing_tool import ci_run

    monkeypatch.chdir(tmp_path)
    path, _ = fixture_inputs(tmp_path, count=3)
    original = ci_run.write_report

    def disk_full(report, directory):
        if any(row.outcome for row in report.cases):
            raise OSError("RAW-PRIVATE-REPORT-WRITE-ERROR")
        return original(report, directory)

    monkeypatch.setattr(ci_run, "write_report", disk_full)
    code = await execute(parser().parse_args(["ci", "run", str(path), "--summary-json", "-"]))
    text = capsys.readouterr().out
    report = SuiteReport.model_validate_json(text)
    assert code == report.exit_code == 2
    assert report.cases[0].status == "passed" and report.cases[0].outcome.exit_code == 0
    assert all(row.status == "not-run" for row in report.cases[1:])
    assert not report.compatibility_pass
    assert "RAW-PRIVATE-REPORT-WRITE-ERROR" not in text


def test_optional_failed_outcome_check_can_be_expected(tmp_path):
    document = scenario()
    document["checks"][1]["required"] = False
    suite = suite_document()
    suite["cases"][0]["expect"] = {"exit_code": 1, "checks": {"no-duplicate-charge": "failed"}}
    prepare_suite(write_inputs(tmp_path, suite=suite, doc=document), root=tmp_path)


def test_real_wallet_receipt_finding_is_an_admissible_negative_expectation():
    from consumer_agent_testing_tool.ext_wallet import WalletExtension

    wallet = object.__new__(WalletExtension)
    emitted = wallet.receipt_check({"merchant": SimpleNamespace(charges=[
        SimpleNamespace(status="captured", receipt_id=None, charge_id="synthetic-charge")])}, False)
    assert emitted["id"] == "wallet.receipt" and emitted["status"] == "failed"
    case = SimpleNamespace(kind="connector", expect=Expectation(exit_code=1, checks={emitted["id"]: emitted["status"]}))
    document = SimpleNamespace(checks=[], extensions={"wallet": {}}, connectors=[], limits=SimpleNamespace(tool_calls=4))
    validate_expectation(case, document, "case")


@pytest.mark.parametrize("extension,identifier", [
    ("presentation", "presentation.whatsapp_format"), ("presentation", "presentation.text_format"),
    ("browser", "browser.checkout_terms"), ("browser", "browser.anti_automation.captcha"),
    ("browser", "browser.anti_automation.waf-block"), ("browser", "browser.anti_automation.account-locked"),
])
def test_emitted_extension_check_ids_are_supported(extension, identifier):
    case = SimpleNamespace(kind="connector", expect=Expectation(exit_code=1, checks={identifier: "failed"}))
    document = SimpleNamespace(checks=[], extensions={extension: {}}, connectors=[], limits=SimpleNamespace(tool_calls=4))
    validate_expectation(case, document, "case")


@pytest.mark.parametrize("identifier", ["execution", "answer"])
def test_generic_host_failure_expectations_require_a_pinned_cause(identifier):
    with pytest.raises(ValueError):
        Expectation(exit_code=1, checks={identifier: "failed"})


@pytest.mark.parametrize("projection", ["native", "cli"])
def test_a_portable_search_only_call_without_discovery_is_rejected_offline(tmp_path, projection):
    suite = suite_document()
    suite["cases"][0]["matrix"].update(exposure=["search-only"], projection=[projection])
    with pytest.raises(PlanError, match="tool_search"):
        prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path)


@pytest.mark.parametrize("first,second", [("123", "456"), ("yes", "no"), ("a", "b")])
def test_short_and_common_private_values_are_rejected_before_any_digest_is_published(tmp_path, first, second):
    # Review finding 9 is already prevented by ConnectorScenario's private-value admission.
    for value in (first, second):
        document, calls = scenario(), portable_script()
        document["private"]["short_value"] = value
        calls[0]["arguments"]["pin"] = value
        with pytest.raises(PlanError):
            prepare_suite(write_inputs(tmp_path, doc=document, steps=calls), root=tmp_path)


async def test_cleanup_does_not_queue_behind_the_default_executor(tmp_path):
    loop, release = asyncio.get_running_loop(), threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    occupied = loop.run_in_executor(None, release.wait, 30)
    process = subprocess.Popen(  # noqa: ASYNC220 — do not use the executor whose saturation this test controls
                               [sys.executable, "-c", "import time; time.sleep(60)"],
                               start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup = asyncio.create_task(Service(process, [], 0, tmp_path / "unused.log", "").astop())
    try:
        done, _ = await asyncio.wait({cleanup}, timeout=1)
        assert cleanup in done, "Owned-process cleanup waited for an unrelated executor task"
        assert process.poll() is not None
    finally:
        release.set()
        await occupied
        await cleanup
        pool.shutdown(wait=True)


def test_public_artifacts_are_readable_by_ci_artifact_users(tmp_path):
    path = tmp_path / "report.json"
    atomic_write(path, "{}")
    assert path.stat().st_mode & 0o777 == 0o644


def test_abandoned_atomic_report_file_does_not_block_the_next_run(tmp_path):
    from consumer_agent_testing_tool.ci_results import output_directories

    public = tmp_path / "reports"
    public.mkdir()
    (public / ".catt-ci-ab12cd34").write_text('{"interrupted":')
    assert output_directories(tmp_path, "reports", "private")[0] == public


def test_host_expectation_pins_both_stage_and_failure_class():
    from test_ci_results import check, outcome

    from consumer_agent_testing_tool.ci_results import judge

    expected = Expectation(exit_code=1, checks={"execution": "failed"},
                           failure_class="interaction", failure_stage="checks")
    assert judge(outcome(1, failure_class="interaction"), [
        check("execution", "failed", failure_class="interaction")], expected) == "expected-failure"
    assert judge(outcome(1, failure_class="tool/API"), [
        check("execution", "failed", failure_class="tool/API")], expected) == "failed"
    changed = outcome(1, failure_class="interaction")
    changed.failure_stage = "linking"
    assert judge(changed, [check("execution", "failed", failure_class="interaction")], expected) == "failed"


def test_callback_urls_use_the_same_port_during_validation_and_execution(tmp_path):
    import json

    from consumer_agent_testing_tool.ci_run import snapshot

    calls = portable_script()
    calls[0]["arguments"]["redirect_uri"] = "http://127.0.0.1:${PORT}/callback"
    prepared = prepare_suite(write_inputs(tmp_path, steps=calls), root=tmp_path)
    snapshot(next(iter(prepared.cases.values())), tmp_path / "worker", 45678, "case-namespace")
    stored = json.loads((tmp_path / "worker/input/calls.json").read_text())
    assert stored[0]["arguments"]["redirect_uri"] == "http://127.0.0.1:45678/callback"
