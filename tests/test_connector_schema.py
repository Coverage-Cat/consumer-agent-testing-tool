"""The published connector-v1 JSON Schema matches the pydantic contract and accepts the shipped examples."""
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from consumer_agent_testing_tool.connector_schema import connector_schema, shipped_extensions
from consumer_agent_testing_tool.serialization import load

ROOT = Path(__file__).resolve().parents[1]
COPIES = [ROOT / "schemas/connector-v1.schema.json",
          ROOT / "src/consumer_agent_testing_tool/data/schemas/connector-v1.schema.json"]
EXAMPLES = sorted(path for path in (ROOT / "examples/connector").glob("*.yaml")
                  if isinstance(document := load(path), dict) and document.get("kind") == "connector")


def validator():
    schema = load(COPIES[0])
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_the_shipped_schema_is_regenerated_from_the_models():
    # Regenerate with: python scripts/generate_schema.py
    expected = connector_schema()
    for copy in COPIES:
        assert load(copy) == expected, copy


def test_every_shipped_extension_has_a_configuration_schema():
    extensions = load(COPIES[0])["properties"]["extensions"]
    assert sorted(extensions["properties"]) == shipped_extensions()
    assert {"clock", "routes", "wallet", "browser", "scheduler", "content_trust"} <= set(shipped_extensions())


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_the_shipped_examples_validate(path):
    errors = sorted(validator().iter_errors(load(path)), key=lambda error: list(error.path))
    assert not errors, [f"{list(error.path)}: {error.message}" for error in errors]


def test_unknown_fields_and_extension_settings_are_refused():
    document = load(EXAMPLES[0])
    check = validator()
    assert not list(check.iter_errors(document))
    assert list(check.iter_errors({**document, "surprise": True}))
    assert list(check.iter_errors({**document, "extensions": {"clock": {"now": 5}}}))
    assert list(check.iter_errors({**document, "extensions": {"routes": {"no_such_setting": 1}}}))
    # A developer's own extension validates its configuration itself when the scenario loads.
    assert not list(check.iter_errors({**document, "extensions": {"my_lane": {"anything": 1}}}))
    assert list(check.iter_errors({**document, "extensions": {"Bad Name": {}}}))
