from xml.etree import ElementTree

import pytest

from consumer_agent_testing_tool.regression import (
    junit_identity,
    verify_regression,
    verify_run_record,
)
from consumer_agent_testing_tool.release import source_digest


def report(tmp_path, cases):
    suite = ElementTree.Element("testsuite")
    for classname, name, outcome in cases:
        case = ElementTree.SubElement(suite, "testcase", classname=classname, name=name)
        if outcome:
            ElementTree.SubElement(case, outcome)
    path = tmp_path / "report.xml"
    ElementTree.ElementTree(suite).write(path)
    return path


def test_regression_collection_identity_preserves_parameter_syntax():
    assert junit_identity("tests/test_gate.py::Group::test_one[url::value]") == (
        "tests.test_gate.Group", "test_one[url::value]")


def test_regression_requires_complete_unique_collection(tmp_path):
    expected = ["tests/test_gate.py::test_one", "tests/test_gate.py::test_two[case]"]
    rows = [("tests.test_gate", "test_one", None), ("tests.test_gate", "test_two[case]", None)]
    assert len(verify_regression(report(tmp_path, rows), expected)) == 2
    for broken in (rows[:1], rows + rows[:1], rows + [("tests.test_extra", "test_extra", None)]):
        with pytest.raises(ValueError, match="complete"):
            verify_regression(report(tmp_path, broken), expected)
    with pytest.raises(ValueError, match="duplicate"):
        verify_regression(report(tmp_path, rows), expected + expected[:1])
    with pytest.raises(ValueError, match="empty"):
        verify_regression(report(tmp_path, []), [])


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_regression_cannot_hide_a_nonpassing_case(tmp_path, outcome):
    with pytest.raises(ValueError, match="no failures"):
        verify_regression(report(tmp_path, [("tests.test_gate", "test_one", outcome)]),
                          ["tests/test_gate.py::test_one"])


def test_regression_rejects_suite_error_even_when_cases_pass(tmp_path):
    path = report(tmp_path, [("tests.test_gate", "test_one", None)])
    tree = ElementTree.parse(path)
    tree.getroot().set("errors", "1")
    tree.write(path)
    with pytest.raises(ValueError, match="no failures"):
        verify_regression(path, ["tests/test_gate.py::test_one"])


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"returncode": False}, {"returncode": 1}, {"status": "running"},
    {"source_before": "old"}, {"source_after": "changed"}, {"junit": "other.xml"},
    {"junit_sha256": "changed"}, {"log_sha256": "changed"},
])
def test_regression_exit_and_source_record_cannot_be_substituted(change):
    run = {"schema_version": 1, "returncode": 0, "status": "passed",
           "source_before": "source", "source_after": "source", "junit": "test.xml",
           "junit_sha256": "junit", "log_sha256": "log"}
    verify_run_record(run, "source", "test.xml", "junit", "log")
    with pytest.raises(ValueError, match="unchanged-source"):
        verify_run_record({**run, **change}, "source", "test.xml", "junit", "log")


@pytest.mark.parametrize("file", ["tests/probe.mjs", "scripts/probe.mjs", "containers/entrypoint.sh"])
def test_release_evidence_tracks_test_and_container_enforcement_changes(tmp_path, file):
    before = source_digest(tmp_path)
    path = tmp_path / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("initial")
    added = source_digest(tmp_path)
    assert before != added
    path.write_text("changed")
    assert source_digest(tmp_path) != added
