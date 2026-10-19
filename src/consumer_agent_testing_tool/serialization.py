"""Strict data loading: no executable YAML, duplicate keys, or non-finite numbers."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

from .errors import LabError


class UniqueLoader(yaml.SafeLoader):
    pass


# YAML 1.1 treats "on" as True, breaking the scenario event contract.
UniqueLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in rules
          if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")]
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
import re

UniqueLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise LabError("Object keys must be strings")
        if key in result:
            raise LabError(f"Duplicate key: {key}")
        result[key] = value
    return result


def _mapping(loader, node):
    return unique_pairs(
        (loader.construct_object(key), loader.construct_object(value))
        for key, value in node.value
    )


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


# Redaction truncates deeper evidence to this depth, so every sealed document reloads.
MAX_DOCUMENT_DEPTH = 64


def check_finite(value: Any, depth: int = 0):
    if depth > MAX_DOCUMENT_DEPTH:
        raise LabError(f"Document nesting exceeds {MAX_DOCUMENT_DEPTH} levels")
    if isinstance(value, float) and not math.isfinite(value):
        raise LabError("Non-finite numbers are forbidden")
    if isinstance(value, dict):
        for item in value.values():
            check_finite(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_finite(item, depth + 1)


def recordable(value: Any, depth: int = 0):
    """A copy of model-proposed arguments that evidence can record: canonical JSON forbids NaN and
    Infinity (Python's json.loads accepts them), so they become "NaN", "Infinity" and "-Infinity".
    Approvers still see the original numbers and reject them as invalid arguments."""
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if depth > MAX_DOCUMENT_DEPTH:
        return value  # Redaction replaces deeper content with its nesting marker.
    if isinstance(value, dict):
        return {key: recordable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [recordable(item, depth + 1) for item in value]
    return value


def loads(text: str, *, json_only: bool = False):
    """Preserve JSON value semantics; use YAML only for non-JSON syntax."""
    try:
        try:
            value = json.loads(text, object_pairs_hook=unique_pairs)
        except json.JSONDecodeError:
            if json_only:
                raise
            value = yaml.load(text, Loader=UniqueLoader)
        check_finite(value)
        return value
    except (ValueError, yaml.YAMLError, TypeError, RecursionError) as exc:
        raise LabError(f"Invalid document: {exc}") from exc


def load(path: Path):
    return loads(path.read_text(), json_only=path.suffix.lower() == ".json")


def canonical_bytes(value: Any) -> bytes:
    """Stable internal serialization, NOT an RFC8785 approval canonicalizer."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def implementation_lock(workspace: Path) -> dict:
    """Pin installed harness code and the task's declared dependency locks."""
    package = Path(__file__).parent
    sources = {str(path.relative_to(package)): file_digest(path)
               for path in package.rglob("*")
               if path.suffix in (".py", ".mjs", ".cjs", ".yaml", ".json", ".html")
               and not {"__pycache__", "third_party"}.intersection(path.parts) and path.is_file()}
    dependencies = {name: file_digest(workspace / name)
                    for name in ("uv.lock", "tooling/package-lock.json", "tooling/payment-ui/package-lock.json",
                                 "tooling/api/package-lock.json")
                    if (workspace / name).is_file()}
    return {"implementation": digest(sources), "dependency_locks": dependencies}
