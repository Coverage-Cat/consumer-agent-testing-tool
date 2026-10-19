"""The readiness manifest: strict, safe YAML or JSON, presence-only declarations, never credentials."""
import json

import pytest
import yaml
from test_guidelines_support import MANIFEST

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.guidelines import DOC_TOPICS, ReadinessManifest, load_manifest


def write(tmp_path, document, name="readiness.yaml"):
    path = tmp_path / name
    path.write_text(document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False))
    return path


def test_a_complete_manifest_loads_from_yaml_and_json(tmp_path):
    from_yaml = load_manifest(write(tmp_path, MANIFEST))
    from_json = load_manifest(write(tmp_path, json.dumps(MANIFEST), "readiness.json"))
    assert from_yaml == from_json and from_yaml.documentation.covers == list(DOC_TOPICS)
    assert from_yaml.classes["create_booking"] == "sensitive-write"
    assert ReadinessManifest().version == 1 and ReadinessManifest().kind == "muse-readiness"


@pytest.mark.parametrize("change,where", [
    ({"test_account": {**MANIFEST["test_account"], "password": "hunter2-secret"}}, "test_account.password"),
    ({"documentation": {"covers": ["setup-instructions", "pricing"]}}, "documentation.covers.1"),
    ({"classes": {"create_booking": "payment"}}, "classes.create_booking"),
    ({"authentication": {"method": "cookies"}}, "authentication.method"),
    ({"transactions": {"no_charge_test_path": "test mode"}}, "transactions.has_transaction_tools"),
    ({"sample_responses": [{"tool": "", "result": {}}]}, "sample_responses.0.tool"),
    ({"version": 2}, "version"),
    ({"judge": True}, "judge"),
])
def test_invalid_manifests_name_the_field_without_echoing_values(tmp_path, change, where):
    with pytest.raises(LabError, match="Invalid readiness manifest") as caught:
        load_manifest(write(tmp_path, {**MANIFEST, **change}))
    assert where in str(caught.value) and "hunter2-secret" not in str(caught.value)


@pytest.mark.parametrize("text", [
    "documentation: !!python/object/apply:os.system ['true']\n",  # No executable YAML.
    "connector: a\nconnector: b\n",  # Duplicate keys are ambiguous, not last-wins.
])
def test_unsafe_or_ambiguous_yaml_is_rejected(tmp_path, text):
    with pytest.raises(LabError):
        load_manifest(write(tmp_path, text))


def test_a_missing_manifest_is_a_clear_setup_error(tmp_path):
    with pytest.raises(LabError, match="Readiness manifest not found"):
        load_manifest(tmp_path / "absent.yaml")
