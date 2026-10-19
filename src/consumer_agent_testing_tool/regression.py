"""Require the complete pytest collection, not a selected green JUnit subset."""
from collections import Counter
from pathlib import PurePosixPath
from xml.etree import ElementTree


def verify_run_record(run, source_sha, report_path, junit_sha, log_sha):
    if type(run.get("schema_version")) is not int or run["schema_version"] != 1 \
            or run.get("status") != "passed" or type(run.get("returncode")) is not int \
            or run["returncode"] != 0 or run.get("source_before") != source_sha \
            or run.get("source_after") != source_sha or run.get("junit") != report_path \
            or run.get("junit_sha256") != junit_sha or run.get("log_sha256") != log_sha:
        raise ValueError("A successful, unchanged-source run record is required")


def junit_identity(nodeid):
    path, qualified = nodeid.split("::", 1)
    if not path.startswith("tests/") or not path.endswith(".py"):
        raise ValueError("Unsupported regression collection item")
    before_parameters = qualified.split("[", 1)[0]
    parts = before_parameters.split("::")
    if not all(parts):
        raise ValueError("Invalid regression collection identity")
    classname = str(PurePosixPath(path).with_suffix("")).replace("/", ".")
    if len(parts) > 1:
        classname += "." + ".".join(parts[:-1])
    name = parts[-1] + qualified[len(before_parameters):]
    return classname, name


def verify_regression(report, nodeids):
    expected = Counter(junit_identity(nodeid) for nodeid in nodeids)
    if not expected or any(count != 1 for count in expected.values()):
        raise ValueError("Regression collection is empty or has duplicate identities")
    root = ElementTree.parse(report).getroot()
    for element in root.iter():
        if element.tag in ("testsuite", "testsuites") and any(
                int(element.get(name, "0")) != 0 for name in ("failures", "errors", "skipped")):
            raise ValueError("Regression qualification requires no failures, errors or skips")
    cases = root.findall(".//testcase")
    observed = Counter((case.get("classname"), case.get("name")) for case in cases)
    if observed != expected:
        raise ValueError("JUnit does not cover exactly the complete current pytest collection")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("Regression qualification requires no failures, errors or skips")
    return [{"id": f"{case.get('classname')}::{case.get('name')}", "status": "passed"}
            for case in cases]
