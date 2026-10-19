"""Saved JSON must retain the values used by configuration, locks and replay."""
import json

import pytest

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.replay import contract_replay, export_public_run, playback, seal
from consumer_agent_testing_tool.serialization import digest, load, loads
from consumer_agent_testing_tool.surface import atomic_json


@pytest.mark.parametrize("value", [
    {"name": "𝜋thon", "😀": ["é", "e\u0301"]},
    {"small": 1e-6, "large": 1e20, "integer": 2, "boolean": False, "null": None},
    [{"𝜋": 1e-6}, "😀", 1e20],
])
def test_json_roundtrip_preserves_unicode_and_numeric_types(tmp_path, value):
    path = tmp_path / "values.json"
    atomic_json(path, value)
    for decoded in (load(path), loads(path.read_text()), loads(path.read_text(), json_only=True)):
        assert decoded == value
        assert digest(decoded) == digest(value)


@pytest.mark.parametrize("document", ['{"value": 1,}', '{value: 1}', 'value: 1'])
def test_explicit_json_files_do_not_accept_yaml_syntax(tmp_path, document):
    path = tmp_path / "values.json"
    path.write_text(document)
    with pytest.raises(LabError, match="Invalid document"):
        load(path)
    # YAML configuration remains supported when its format permits this syntax.
    assert loads(document) == {"value": 1}


@pytest.mark.parametrize("document", [
    '{"\\ud835\\udf0b": 1, "𝜋": 2}', '{"value": NaN}', '{"value": 1e999}',
])
def test_json_semantic_errors_do_not_fall_back_to_yaml(document):
    with pytest.raises(LabError):
        loads(document)


def test_yaml_documents_keep_scenario_semantics(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("on: approval_requested\nallowed: true\nlabel: 2026-10-13\nitems:\n  - draft\n")
    assert load(path) == {
        "on": "approval_requested", "allowed": True, "label": "2026-10-13", "items": ["draft"],
    }


@pytest.mark.parametrize("suffix", [".json", ".JSON", ".yaml"])
def test_initializer_writes_the_selected_configuration_format(tmp_path, suffix):
    from consumer_agent_testing_tool.cli import initialize, parser
    from consumer_agent_testing_tool.config import read_config

    path = tmp_path / ("config" + suffix)
    initialize(parser().parse_args(["init", "--provider", "none", "--config", str(path)]))
    config = read_config(path)
    document = (json.loads(path.read_text()) if suffix.lower() == ".json" else load(path))
    assert document == config.model_dump(mode="json", exclude_none=True)


def test_contract_replay_preserves_saved_json_arguments(tmp_path):
    arguments = {"name": "𝜋thon", "threshold": 1e-6}
    request = {"step": 1, "operation": "read", "arguments": arguments}
    requests, tape = tmp_path / "requests.json", tmp_path / "tape.json"
    atomic_json(requests, [request])
    atomic_json(tape, {"fixture_version": "synthetic-v1", "entries": [{
        "request": {**request, "headers": {}, "body_sha256": digest(arguments),
                    "fixture_version": "synthetic-v1"},
        "response": {"status": "ok"},
    }]})
    result = contract_replay(tape, requests)
    assert result["results"] == [{"status": "ok"}] and result["live_fallback"] is False
    atomic_json(requests, [{**request, "arguments": {**arguments, "threshold": "1e-06"}}])
    with pytest.raises(LabError, match="Replay request mismatch"):
        contract_replay(tape, requests)


def test_sealed_playback_export_preserves_unicode_and_numbers(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    result = {"status": "completed", "checks": [],
              "run_lock": {"model": "synthetic-😀", "threshold": 1e-6}}
    atomic_json(source / "result.json", result)
    (source / "events.jsonl").write_text("")
    (source / "report.html").write_text("<p>Synthetic report</p>")
    seal(source)
    assert playback(source)["result"] == result
    exported = export_public_run(source, tmp_path / "export")
    replayed = playback(exported)["result"]
    assert replayed["run_lock"] == result["run_lock"]
    assert digest(replayed["run_lock"]) == digest(result["run_lock"])
    assert json.loads((source / "result.json").read_text()) == result


def test_deeply_nested_values_are_saved_without_recursion(tmp_path):
    deep = value = {}
    for _ in range(3000):
        value["a"] = {}
        value = value["a"]
    path = tmp_path / "deep.json"
    atomic_json(path, {"result": deep})  # Raw service results reach private checkpoints unbounded.
    assert json.loads(path.read_text()) == {"result": deep}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_value_that_cannot_be_encoded_leaves_the_saved_file_and_no_temporary(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"step": 1})
    with pytest.raises(TypeError):
        atomic_json(path, {"step": object()})
    assert json.loads(path.read_text()) == {"step": 1} and not list(tmp_path.glob("*.tmp"))


def test_recordable_arguments_name_non_finite_numbers():
    from consumer_agent_testing_tool.serialization import canonical_bytes, recordable
    proposed = {"price": float("nan"), "limits": [float("inf"), -float("inf"), 2.5], "label": "x"}
    recorded = recordable(proposed)
    assert recorded == {"price": "NaN", "limits": ["Infinity", "-Infinity", 2.5], "label": "x"}
    assert canonical_bytes(recorded) and proposed["limits"][0] == float("inf")  # The proposal is untouched.
