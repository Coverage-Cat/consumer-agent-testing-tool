"""Reproductions of substantive findings from the first committed Claude review."""
import copy
import json

import pytest
from test_ci_plan import suite_document, write_inputs
from test_connector_support import scenario
from test_scripted_portable import portable_script

from consumer_agent_testing_tool.ci_suite import PlanError, prepare_suite


@pytest.mark.parametrize("name, first, second", [
    ("card_number", "4242424242424242", "4000000000000002"),
    ("email", "first.public@example.com", "second.public@example.com"),
    ("payment_token", {"$ref": "/responses/0/structuredContent/token"},
     {"$ref": "/responses/1/structuredContent/token"}),
])
def test_public_payment_values_and_symbolic_references_have_distinct_identity(tmp_path, name, first, second):
    steps = portable_script()
    steps[2]["arguments"][name] = first
    path = write_inputs(tmp_path, steps=steps)
    before = prepare_suite(path, root=tmp_path).plan
    steps[2]["arguments"][name] = second
    (tmp_path / "calls.json").write_text(json.dumps(steps))
    assert prepare_suite(path, root=tmp_path).plan.suite_sha256 != before.suite_sha256


def test_each_reference_is_checked_against_the_actual_portable_step_and_projection(tmp_path):
    for pointer in ("/results/2/result/structuredContent/booking_id", "/responses/0/structuredContent/booking_id"):
        steps = portable_script()
        if pointer.startswith("/responses"):
            steps[0] = {"idle": True}
        steps[-1] = {"answer": [{"$ref": pointer}]}
        with pytest.raises(PlanError):
            prepare_suite(write_inputs(tmp_path, steps=steps), root=tmp_path)


@pytest.mark.parametrize("tool", ["unknown_tool", "exec"])
def test_provably_unavailable_surface_is_rejected_before_any_case_runs(tmp_path, tool):
    steps = portable_script()
    steps[0].pop("connector")
    steps[0]["tool"] = tool
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, steps=steps), root=tmp_path)


def test_portable_non_allowlisted_tool_is_rejected_offline(tmp_path):
    steps = portable_script()
    steps[0]["tool"] = "not-in-allowlist"
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, steps=steps), root=tmp_path)


@pytest.mark.parametrize("expect", [
    {"exit_code": 1, "checks": {"booking-confirmd": "failed"}},
    {"exit_code": 3, "checks": {"one-confirmed-booking": "failed"}},
    {"exit_code": 1, "checks": {"one-confirmed-booking": "blocked"}},
])
def test_typoed_and_contradictory_negative_expectations_are_rejected(tmp_path, expect):
    suite = suite_document()
    suite["cases"][0]["expect"] = expect
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, suite=suite, steps=portable_script()), root=tmp_path)


def test_uppercase_remote_scheme_cannot_bypass_loopback_declaration(tmp_path):
    doc = json.loads(json.dumps(scenario()).replace("http://127.0.0.1:8765", "HTTPS://staging.example"))
    with pytest.raises(PlanError, match="remote"):
        prepare_suite(write_inputs(tmp_path, doc=doc, steps=portable_script()), root=tmp_path)


def test_owned_service_cannot_probe_or_call_an_unrelated_fixed_port(tmp_path):
    suite = suite_document()
    fixed = json.loads(json.dumps(scenario()).replace(":8765", ":8080"))
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, suite=suite, doc=fixed, steps=portable_script()), root=tmp_path)
    suite["services"]["hotel"]["ready_url"] = "http://127.0.0.1:8080/{port}"
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, suite=suite, steps=portable_script()), root=tmp_path)


def test_existing_service_uses_scenario_defaults_instead_of_the_planning_port(tmp_path):
    suite = suite_document()
    suite["services"]["hotel"] = {"state": "shared"}
    doc = json.loads(json.dumps(scenario()).replace(":8765", ":${PORT:-8765}"))
    prepared = prepare_suite(write_inputs(tmp_path, suite=suite, doc=doc, steps=portable_script()), root=tmp_path)
    assert all(row.loaded.scenario.connectors[0].endpoint == "http://127.0.0.1:8765/mcp"
               for row in prepared.cases.values())


def test_a_check_id_that_looks_like_a_field_name_is_not_a_private_value(tmp_path):
    doc = scenario()
    doc["checks"].append({**copy.deepcopy(doc["checks"][0]), "id": "payment-token"})
    suite = suite_document()
    suite["cases"][0]["expect"] = {"exit_code": 1, "checks": {"payment-token": "failed"}}
    assert prepare_suite(write_inputs(tmp_path, suite=suite, doc=doc, steps=portable_script()), root=tmp_path)


def custom_doc():
    doc = scenario()
    doc.setdefault("extensions", {})["custom_connector"] = {
        "service": "rest-api", "url": "http://127.0.0.1:${PORT}/api",
        "docs": "bundle", "credential": {"scheme": "api_key", "env": "CATT_CUSTOM_TEST_KEY", "placement": "bearer"}}
    return doc


def test_custom_docs_are_validated_before_effects_and_bound_into_identity(tmp_path):
    path = write_inputs(tmp_path, doc=custom_doc(), steps=portable_script())
    with pytest.raises(PlanError):
        prepare_suite(path, root=tmp_path)
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "README.md").write_text("Version one of the public API at {{service_url}}.")
    first = prepare_suite(path, root=tmp_path).plan
    (directory / "README.md").write_text("Version two of the public API at {{service_url}}.")
    assert prepare_suite(path, root=tmp_path).plan.suite_sha256 != first.suite_sha256


def test_custom_docs_snapshot_keeps_the_validated_content_and_runtime_url_placeholder(tmp_path):
    from consumer_agent_testing_tool.ci_run import snapshot

    path = write_inputs(tmp_path, doc=custom_doc(), steps=portable_script())
    directory = tmp_path / "bundle"
    directory.mkdir()
    text = "Validated public API at {{service_url}}."
    (directory / "README.md").write_text(text)
    prepared = prepare_suite(path, root=tmp_path)
    (directory / "README.md").write_text("This changed after validation.")
    snapshot(next(iter(prepared.cases.values())), tmp_path / "worker", 45678, "ci-namespace")
    assert (tmp_path / "worker/input/extension-docs/custom_connector/README.md").read_text() == text


def test_known_environment_credentials_cannot_be_hashed_as_public_input(tmp_path, monkeypatch):
    value = "synthetic-inline-credential-test-62be95"
    monkeypatch.setenv("CATT_TEST_TOKEN", value)
    steps = portable_script()
    steps[2]["arguments"]["arbitrary_field"] = value
    with pytest.raises(PlanError, match="environment credential") as caught:
        prepare_suite(write_inputs(tmp_path, steps=steps), root=tmp_path)
    assert value not in str(caught.value)


def test_fixed_port_cannot_match_the_offline_placeholder_by_coincidence(tmp_path):
    doc = json.loads(json.dumps(scenario()).replace(":8765", ":49152"))
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, doc=doc), root=tmp_path)


@pytest.mark.parametrize("projection,tool,envelope", [("native", "search_rooms", "stdout"), ("cli", "exec", "result")])
def test_nonportable_references_also_require_the_actual_projection(tmp_path, projection, tool, envelope):
    suite = suite_document()
    suite["cases"][0]["matrix"]["projection"] = [projection]
    steps = [{"tool": tool, "arguments": {"command": "cedar-stays list-tools"} if tool == "exec" else {}},
             {"answer": [{"$ref": f"/results/0/{envelope}/structuredContent"}]}]
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, suite=suite, steps=steps), root=tmp_path)


def test_malformed_script_url_returns_a_static_plan_error(tmp_path):
    steps = portable_script()
    steps[0]["arguments"]["callback"] = "http://127.0.0.1:bad/path"
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path, steps=steps), root=tmp_path)
