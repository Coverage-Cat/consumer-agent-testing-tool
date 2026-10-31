"""A complete CI plan is validated without network, service startup, or model inference."""
import copy
import json
import socket
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_connector_support import scenario
from test_scripted_portable import portable_script

from consumer_agent_testing_tool.ci_suite import PlanError, SuitePlan, SuiteSpec, prepare_suite
from consumer_agent_testing_tool.cli import execute, parser


def suite_document():
    return {"version": 1, "kind": "ci-suite", "id": "booking",
            "services": {"hotel": {"command": ["python", "-m", "my_service", "--port", "{port}"],
                                    "ready_url": "http://127.0.0.1:{port}/health", "state": "process-local"}},
            "cases": [{"id": "book", "scenario": "scenario.json", "script": "calls.json", "service": "hotel",
                       "tags": ["smoke"], "matrix": {"emulation_target": ["muse", "instinct"],
                                                       "projection": ["native", "cli"]}}]}


def write_inputs(root, *, suite=None, doc=None, steps=None):
    root.mkdir(parents=True, exist_ok=True)
    doc = json.loads(json.dumps(doc or scenario()).replace(":8765", ":${PORT:-8765}"))
    for name, value in (("suite.json", suite or suite_document()), ("scenario.json", doc),
                        ("calls.json", steps or portable_script())):
        (root / name).write_text(json.dumps(value))
    return root / "suite.json"


@pytest.fixture
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Offline planning reached a network or process boundary")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.resolve_model", refuse)
    monkeypatch.setattr("consumer_agent_testing_tool.providers.preflight", refuse)


def test_matrix_plan_is_deterministic_and_closed(tmp_path, offline):
    path = write_inputs(tmp_path)
    first = prepare_suite(path, root=tmp_path, jobs=4).plan
    assert len(first.cases) == len(first.selected) == len(first.scheduled) == 4
    assert first == prepare_suite(path, root=tmp_path, jobs=4).plan
    assert {(row.treatment.emulation_target, row.treatment.projection) for row in first.cases} == {
        ("muse", "native"), ("muse", "cli"), ("instinct", "native"), ("instinct", "cli")}
    assert "taylor@example.com" not in first.model_dump_json()
    Draft202012Validator(SuitePlan.model_json_schema()).validate(first.model_dump())
    Draft202012Validator(SuiteSpec.model_json_schema()).validate(suite_document())


def test_all_cases_are_validated_before_any_start_even_when_filtered(tmp_path, offline):
    suite = suite_document()
    suite["cases"].append({**copy.deepcopy(suite["cases"][0]), "id": "broken", "script": "missing.json"})
    path = write_inputs(tmp_path, suite=suite)
    with pytest.raises(PlanError, match="inside the working directory"):
        prepare_suite(path, root=tmp_path, cases=["book"])


@pytest.mark.parametrize("mutation", [
    lambda doc: doc.update(config="agent.yaml"),
    lambda doc: doc["cases"][0].update(driver="agent"),
    lambda doc: doc["cases"][0]["matrix"].pop("emulation_target"),
    lambda doc: doc["cases"][0]["matrix"].update(emulation_target=["muse", "muse"]),
    lambda doc: doc["cases"][0]["matrix"].update(projection=["muse-cli"]),
    lambda doc: doc["cases"][0].update(expect={"exit_code": 2, "checks": {"execution": "failed"}}),
    lambda doc: doc["cases"][0].update(expect={"exit_code": 1}),
    lambda doc: doc["cases"][0].update(expect={"exit_code": 1, "checks": {"*": "failed"}}),
])
def test_inference_implicit_targets_invalid_combinations_and_broad_expectations_are_rejected(
        tmp_path, offline, mutation):
    suite = suite_document()
    mutation(suite)
    path = write_inputs(tmp_path, suite=suite)
    with pytest.raises(PlanError):
        prepare_suite(path, root=tmp_path)


@pytest.mark.parametrize("extension", ["user_simulator", "unknown"])
def test_paid_and_unreviewed_extensions_are_rejected_before_factory_import(tmp_path, offline, extension):
    doc = scenario()
    doc["extensions"] = {extension: {"model_profile": "paid"}}
    with pytest.raises(PlanError, match="shared extensions"):
        prepare_suite(write_inputs(tmp_path, doc=doc), root=tmp_path)


def test_ci_requires_an_independent_check(tmp_path, offline):
    doc = scenario()
    doc.update(checks=[], answer=None)
    with pytest.raises(PlanError, match="independent"):
        prepare_suite(write_inputs(tmp_path, doc=doc), root=tmp_path)


def test_shards_partition_selection_and_keep_one_scope_identity(tmp_path, offline):
    path = write_inputs(tmp_path)
    whole = prepare_suite(path, root=tmp_path).plan
    shards = [prepare_suite(path, root=tmp_path, shard=f"{index}/3").plan for index in range(1, 4)]
    assert all(row.scope_sha256 == whole.scope_sha256 for row in shards)
    assert sorted(key for row in shards for key in row.scheduled) == whole.scheduled
    assert all(row.scheduled for row in shards)


@pytest.mark.parametrize("options", [
    {"cases": ["typo"]}, {"cases": ["book", "typo"]}, {"tags": ["typo"]},
    {"shard": "0/3"}, {"shard": "1/5"}, {"shard": "4/3"}, {"shard": "2/0"},
    {"jobs": 0}, {"jobs": 65},
])
def test_invalid_and_empty_selection_is_not_green(tmp_path, offline, options):
    with pytest.raises(PlanError):
        prepare_suite(write_inputs(tmp_path), root=tmp_path, **options)


def test_shared_state_is_explicit_and_cannot_be_parallel_or_sharded(tmp_path, offline):
    suite = suite_document()
    suite["services"]["hotel"] = {"state": "shared"}
    path = write_inputs(tmp_path, suite=suite)
    assert prepare_suite(path, root=tmp_path).plan.cases[0].state == "shared"
    for options in ({"jobs": 2}, {"shard": "1/2"}):
        with pytest.raises(PlanError, match="Shared state"):
            prepare_suite(path, root=tmp_path, **options)


def test_remote_endpoints_need_an_explicit_case_declaration(tmp_path, offline):
    doc = scenario()
    doc = json.loads(json.dumps(doc).replace("http://127.0.0.1:8765", "https://staging.example"))
    suite = suite_document()
    path = write_inputs(tmp_path, doc=doc, suite=suite)
    with pytest.raises(PlanError, match="remote"):
        prepare_suite(path, root=tmp_path)
    suite["cases"][0]["network"] = "remote"
    write_inputs(tmp_path, doc=doc, suite=suite)
    assert prepare_suite(path, root=tmp_path).plan.cases[0].network == "remote"


def test_input_paths_cannot_escape_the_repository(tmp_path, offline):
    root = tmp_path / "repository"
    suite = suite_document()
    suite["cases"][0]["scenario"] = "../outside.json"
    (tmp_path / "outside.json").write_text(json.dumps(scenario()))
    with pytest.raises(PlanError, match="working directory"):
        prepare_suite(write_inputs(root, suite=suite), root=root)


def test_private_value_changes_cannot_be_guessed_from_plan_hashes(tmp_path, offline):
    path = write_inputs(tmp_path)
    first = prepare_suite(path, root=tmp_path).plan
    doc = scenario()
    doc["private"]["email"] = "another-private-person@example.com"
    write_inputs(tmp_path, doc=doc)
    assert first == prepare_suite(path, root=tmp_path).plan


def test_changed_outcome_checks_change_case_identity(tmp_path, offline):
    path = write_inputs(tmp_path)
    first = prepare_suite(path, root=tmp_path).plan
    doc = scenario()
    doc["checks"][0]["expect"]["value"] = 2
    write_inputs(tmp_path, doc=doc)
    second = prepare_suite(path, root=tmp_path).plan
    assert first.suite_sha256 != second.suite_sha256
    assert all(a.definition_sha256 != b.definition_sha256 for a, b in zip(first.cases, second.cases, strict=True))


def test_joint_treatment_overrides_validate_a_muse_specific_source_for_instinct(tmp_path, offline):
    doc = scenario()
    doc["projection"] = "muse-cli"
    doc["connectors"][0]["client_profile"] = "muse-template-unofficial-2026-10"
    found = prepare_suite(write_inputs(tmp_path, doc=doc), root=tmp_path)
    assert len(found.plan.cases) == 4
    assert all(row.loaded.scenario.connectors[0].client_profile == "generic-sdk" for row in found.cases.values())


def test_public_identity_cannot_echo_a_registered_private_value(tmp_path, offline):
    doc = scenario()
    doc["private"]["email"] = "private-person-identifier"
    suite = suite_document()
    suite["cases"][0]["id"] = "private-person-identifier"
    with pytest.raises(PlanError, match="private value"):
        prepare_suite(write_inputs(tmp_path, doc=doc, suite=suite), root=tmp_path)


@pytest.mark.parametrize("reference", ["/private/email", "/results/2/x", "/responses/0/bad~x", 3])
def test_bad_references_are_found_before_any_effect(tmp_path, offline, reference):
    steps = [{"tool": "search_rooms", "arguments": {"x": {"$ref": reference}}}]
    with pytest.raises(PlanError, match="earlier"):
        prepare_suite(write_inputs(tmp_path, steps=steps), root=tmp_path)


async def test_cli_plan_is_one_json_document(tmp_path, offline, monkeypatch, capsys):
    path = write_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert await execute(parser().parse_args(["ci", "plan", str(path)])) == 0
    out, err = capsys.readouterr()
    assert not err
    assert json.loads(out)["kind"] == "catt-ci-plan"


@pytest.mark.parametrize("text", [
    "private: {email: extremely-private@example.com}\nx: [not: valid",
    '{"api_key": "secret-known-only-to-the-broken-document",',
    '{"secret-value-used-as-a-duplicate-key": 1, "secret-value-used-as-a-duplicate-key": 2}',
])
async def test_malformed_documents_never_echo_input_in_machine_diagnostics(tmp_path, monkeypatch, capsys, text):
    path = write_inputs(tmp_path)
    (tmp_path / "scenario.json").write_text(text)
    monkeypatch.chdir(tmp_path)
    assert await execute(parser().parse_args(["ci", "validate", str(path), "--json"])) == 2
    out, _ = capsys.readouterr()
    value = json.loads(out)
    assert value["valid"] is False and value["diagnostics"][0]["code"] == "input.document"
    assert "extremely-private" not in out and "secret-" not in out


def test_readiness_uses_the_scenarios_auth_and_validates_the_manifest_offline(tmp_path, offline):
    suite = suite_document()
    suite["cases"][0].update(kind="readiness", manifest="readiness.json")
    suite["cases"][0].pop("script")
    (tmp_path / "readiness.json").write_text(json.dumps({"allow": ["search_rooms"]}))
    prepared = prepare_suite(write_inputs(tmp_path, suite=suite), root=tmp_path)
    assert {row.readiness_connector for row in prepared.cases.values()} == {"cedar-stays"}
    (tmp_path / "readiness.json").write_text(json.dumps({"allow": []}))
    with pytest.raises(PlanError):
        prepare_suite(tmp_path / "suite.json", root=tmp_path)


def test_shipped_booking_suite_validates_without_setup_or_keys(offline):
    root = Path(__file__).resolve().parents[1]
    found = prepare_suite(root / "examples/ci/booking.suite.yaml", root=root, jobs=4)
    assert len(found.plan.cases) == 12


@pytest.mark.parametrize("value", [123456, ["private-list"], "private-scalar"])
async def test_invalid_private_container_has_a_structured_diagnostic(tmp_path, offline, monkeypatch, capsys, value):
    doc = scenario()
    doc["private"] = value
    path = write_inputs(tmp_path, doc=doc)
    monkeypatch.chdir(tmp_path)
    assert await execute(parser().parse_args(["ci", "validate", str(path), "--json"])) == 2
    text = capsys.readouterr().out
    assert json.loads(text)["valid"] is False and str(value) not in text


def test_plan_digest_is_independent_of_case_order_when_private_values_overlap(tmp_path, offline):
    first = scenario()
    first["goal"] = "Inspect shared-private-marker-8346."
    second = scenario()
    second["private"]["email"] = "shared-private-marker-8346"
    (tmp_path / "second.json").write_text(json.dumps(second).replace(":8765", ":${PORT:-8765}"))
    suite = suite_document()
    suite["cases"].append({**copy.deepcopy(suite["cases"][0]), "id": "second", "scenario": "second.json"})
    steps = portable_script()
    steps[-1] = {"answer": "Nothing booked. shared-private-marker-8346"}
    path = write_inputs(tmp_path, suite=suite, doc=first, steps=steps)
    before = prepare_suite(path, root=tmp_path).plan
    suite["cases"].reverse()
    path.write_text(json.dumps(suite))
    after = prepare_suite(path, root=tmp_path).plan
    assert before == after


@pytest.mark.parametrize("mutation", [
    lambda value: value["cases"][0]["expect"].update(exit_code=1, checks={"one-policy": "failed"}),
    lambda value: value["selected"].pop(),
    lambda value: value["scheduled"].append(value["scheduled"][0]),
    lambda value: value.update(scope_sha256="0" * 64),
    lambda value: value["cases"][0]["treatment"].update(projection="muse-cli"),
])
def test_loaded_plans_reject_inconsistent_identity_or_coverage(tmp_path, offline, mutation):
    plan = prepare_suite(write_inputs(tmp_path), root=tmp_path).plan.model_dump()
    mutation(plan)
    with pytest.raises(ValueError):
        SuitePlan.model_validate(plan)


def test_case_isolation_placeholder_is_validated_offline(tmp_path, offline):
    doc = scenario()
    doc["connectors"][0]["endpoint"] += "/${CASE}"
    suite = suite_document()
    suite["services"]["hotel"]["state"] = "case-isolated"
    found = prepare_suite(write_inputs(tmp_path, suite=suite, doc=doc), root=tmp_path)
    assert all(row.loaded.scenario.connectors[0].endpoint.endswith("/ci-case") for row in found.cases.values())
