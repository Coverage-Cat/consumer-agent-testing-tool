"""The published JSON Schema of connector-v1 scenarios, including every shipped extension's configuration.

The pydantic models stay the contract: loading a scenario validates it against them, and a developer
extension validates its own configuration at load time. The schema is for editors and CI linting.
"""
import importlib
import pkgutil

from pydantic.json_schema import models_json_schema

from . import connector_run
from .connector_scenario import EXTENSION_NAME, ConnectorScenario

SCHEMA_ID = "urn:consumer-agent-testing-tool:connector:v1"


def shipped_extensions() -> list[str]:
    """The built-in clock plus every consumer_agent_testing_tool.ext_<name> module in this package."""
    modules = pkgutil.iter_modules(importlib.import_module(__package__).__path__)
    return sorted({"clock"} | {module.name.removeprefix("ext_") for module in modules
                               if module.name.startswith("ext_")})


def connector_schema() -> dict:
    factories = {name: connector_run.discover(name) for name in shipped_extensions()}
    models = [(ConnectorScenario, "validation"), *((factory.config_model, "validation")
                                                    for factory in factories.values())]
    refs, document = models_json_schema(models, by_alias=True, ref_template="#/$defs/{model}")
    definitions = document["$defs"]
    root = definitions.pop(refs[(ConnectorScenario, "validation")]["$ref"].rsplit("/", 1)[1])
    root["properties"]["extensions"] = {
        "title": "Extensions", "type": "object", "propertyNames": {"pattern": EXTENSION_NAME},
        "properties": {name: refs[(factory.config_model, "validation")] for name, factory in factories.items()},
        # A developer's own ext_<name> module validates its configuration when the scenario loads.
        "additionalProperties": {"type": "object"},
    }
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": SCHEMA_ID, **root,
            "$defs": definitions}
