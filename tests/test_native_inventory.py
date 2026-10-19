"""Native artifact observation and review are separate from package notice counts."""
import copy
import importlib
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from consumer_agent_testing_tool.release import native_inventory_review
from consumer_agent_testing_tool.serialization import file_digest


@pytest.fixture
def native(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("native_inventory")


def image(kind):
    if kind == "webassembly":
        return b"\0asm\x01\0\0\0" + b"\0" * 20
    if kind == "elf":
        return b"\x7fELF\x02\x01" + b"\0" * 58
    if kind == "mach-o":
        return b"\xcf\xfa\xed\xfe" + b"\0" * 60
    if kind == "mach-o-universal":
        return b"\xca\xfe\xba\xbe" + struct.pack(">I", 1) + struct.pack(">IIIII", 1, 2, 28, 64, 0) + image("mach-o")
    if kind == "pe":
        return b"MZ" + b"\0" * 58 + struct.pack("<I", 64) + b"PE\0\0" + b"\0" * 20
    return {"truetype": b"\0\x01\0\0", "opentype": b"OTTO",
            "font-collection": b"ttcf", "woff": b"wOFF", "woff2": b"wOF2"}[kind] + b"\0" * 20


@pytest.mark.parametrize("kind", [
    "elf", "mach-o", "mach-o-universal", "pe", "webassembly",
    "truetype", "opentype", "font-collection", "woff", "woff2"])
def test_payload_inventory_uses_headers_not_names(tmp_path, native, kind):
    path = tmp_path / "no-extension"
    path.write_bytes(image(kind))
    observed = native.observe(tmp_path, path)
    assert observed["format"] == kind and observed["sha256"] == file_digest(path)
    assert observed["bytes"] == path.stat().st_size


@pytest.mark.parametrize("content", [
    b"#!/bin/sh\nexit 99\n", b"ordinary text", b"\xca\xfe\xba\xbe\0\0\0A" + b"\0" * 100,
    b"MZ" + b"\0" * 62, b"\xca\xfe\xba\xbe" + struct.pack(">I", 1) + b"\0" * 80,
])
def test_non_payload_and_java_headers_are_not_native_components(native, content):
    assert native.payload_format(io.BytesIO(content), len(content)) is None


def test_changed_native_header_cannot_hide_a_locked_payload(tmp_path, native):
    path = tmp_path / "binding.node"
    path.write_bytes(image("mach-o"))
    before = file_digest(path)
    path.write_text("replaced native payload")
    with pytest.raises(ValueError, match="installed-file inventory"):
        native.observe(tmp_path, path, expected=before)


def test_inventory_rejects_escaping_alias_without_reading_it(tmp_path, native, monkeypatch):
    root = tmp_path / "selected"
    root.mkdir()
    outside = tmp_path / "not-selected"
    outside.write_bytes(image("elf"))
    alias = root / "alias"
    alias.symlink_to(outside)
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != outside, "Escaping file was opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ValueError, match="workspace"):
        native.observe(root, alias)


def test_observation_retains_versions_and_upstream_docs_without_claiming_review(tmp_path, native):
    path = tmp_path / "binary"
    path.write_bytes(image("elf"))
    inventory = native.NativeInventory(tmp_path)
    document = {"path": "retained-sbom.json", "sha256": "a" * 64}
    inventory.add("core-python", "fixture@1", {"binary": (path, file_digest(path))},
                  documents=[document], package={"name": "fixture", "version": "1"})
    result = inventory.result()
    component = result["components"]["fixture@1"]
    assert result["status"] == "unverified" and result["gaps"]
    assert component["package"] == {"name": "fixture", "version": "1"}
    assert component["upstream_documents"] == [document]
    assert component["dependency_coverage"]["status"] == "unverified"
    assert component["notice_coverage"]["status"] == "unverified"
    assert {row["scope"] for row in result["gaps"]} >= {"worker-images", "host-runtime", "browser-runtime"}


def test_missing_installed_python_payload_is_not_silently_omitted(tmp_path, native):
    missing = tmp_path / "binding.so"
    installation = {"prefix": str(tmp_path), "packages": [
        {"name": "fixture", "version": "1", "files": [str(missing)]}]}
    components = [{"name": "fixture", "bom-ref": "fixture@1", "properties": [
        {"name": "consumer-agent-testing-tool:upstream-sboms", "value": "[]"}]}]
    with pytest.raises(ValueError, match="file is missing"):
        native.NativeInventory(tmp_path).python("core-python", installation, components)
    missing.write_bytes(image("elf"))
    installation["packages"][0]["files"].append(str(tmp_path / "disposable.pyc"))
    collector = native.NativeInventory(tmp_path)
    collector.python("core-python", installation, components)
    assert collector.result()["summary"]["artifact_paths"] == 1


@pytest.mark.parametrize("fault", [
    None, "gap", "unreviewed", "missing-scope", "missing-evidence", "changed-document", "changed-inventory",
    "empty-artifacts", "bad-artifact-hash", "omitted-known-component", "omitted-known-payload",
    "changed-known-payload", "changed-package", "dropped-upstream-doc", "changed-observation",
    "missing-observation", "unbound-observation", "changed-sealed-observation",
    "missing-archive-member", "duplicate-archive-member", "invalid-archive",
    "omitted-null-metadata",
])
def test_review_gate_checks_scope_and_linked_evidence(tmp_path, fault):
    # Synthetic reviewed fixture tests the checker; it is not actual runtime evidence.
    document = tmp_path / "review.txt"
    document.write_text("Synthetic reviewer evidence fixture")
    reference = {"path": document.name, "sha256": file_digest(document)}
    scopes = ["core-python", "optional-api-python", "node-protocol", "node-browser", "node-api",
              "browser-runtime", "host-runtime", "worker-images"]
    inventory = {"schema_version": 1, "status": "reviewed", "observed_groups": scopes, "gaps": [],
                 "components": {scope: {"scope": scope,
                     "artifacts": {"binary": {"kind": "binary", "bytes": 64, "sha256": "b" * 64}},
                     "dependency_coverage": {"status": "reviewed", "evidence": [reference]},
                     "notice_coverage": {"status": "reviewed", "evidence": [reference]}}
                     for scope in scopes}}
    inventory["components"]["core-python"]["package"] = {"name": "synthetic", "version": "1"}
    inventory["components"]["core-python"]["upstream_documents"] = [reference]
    inventory["components"]["browser-runtime"]["package"] = None
    inventory["components"]["core-python"]["artifacts"]["other-binary"] = {
        "kind": "binary", "bytes": 32, "sha256": "c" * 64}
    observation = copy.deepcopy(inventory)
    observation["status"] = "unverified"
    observation["gaps"] = [{"reason": "Synthetic observation needs review"}]
    observation["observed_groups"] = scopes[:6]
    observation["components"] = {name: value for name, value in observation["components"].items()
                                 if name in observation["observed_groups"]}
    for component in observation["components"].values():
        for field in ("dependency_coverage", "notice_coverage"):
            component[field] = {"status": "unverified", "reason": "Synthetic unreviewed fixture"}
    directory = tmp_path / "third_party"
    directory.mkdir()
    observed_path = directory / "native-runtime.json"
    observed_path.write_text(json.dumps(observation))
    with zipfile.ZipFile(directory / "notices.zip", "w") as archive:
        archive.write(observed_path, "native-runtime.json")
    observed_reference = {"path": "third_party/native-runtime.json", "sha256": file_digest(observed_path)}
    if fault == "gap":
        inventory["gaps"] = [{"reason": "Unreviewed dependency"}]
    elif fault == "unreviewed":
        inventory["components"]["node-api"]["dependency_coverage"]["status"] = "unverified"
    elif fault == "missing-scope":
        inventory["observed_groups"].remove("worker-images")
    elif fault == "missing-evidence":
        inventory["components"]["node-api"]["notice_coverage"]["evidence"] = []
    elif fault == "empty-artifacts":
        inventory["components"]["host-runtime"]["artifacts"] = {}
    elif fault == "bad-artifact-hash":
        inventory["components"]["node-api"]["artifacts"]["binary"]["sha256"] = "not-a-digest"
    elif fault == "omitted-known-component":
        del inventory["components"]["core-python"]
    elif fault == "omitted-known-payload":
        del inventory["components"]["core-python"]["artifacts"]["other-binary"]
    elif fault == "changed-known-payload":
        inventory["components"]["core-python"]["artifacts"]["binary"]["sha256"] = "d" * 64
    elif fault == "changed-package":
        inventory["components"]["core-python"]["package"]["version"] = "2"
    elif fault == "dropped-upstream-doc":
        inventory["components"]["core-python"]["upstream_documents"] = []
    elif fault == "omitted-null-metadata":
        del inventory["components"]["browser-runtime"]["package"]
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory))
    evidence = {"inventory": {"path": path.name, "sha256": file_digest(path)},
                "observation": observed_reference}
    if fault == "changed-inventory":
        path.write_text("{}")
    elif fault == "changed-document":
        document.write_text("Changed after review")
    elif fault == "changed-observation":
        observed_path.write_text("{}")
    elif fault == "missing-observation":
        evidence.pop("observation")
    elif fault == "unbound-observation":
        alternative = tmp_path / "unbound.json"
        alternative.write_bytes(observed_path.read_bytes())
        evidence["observation"] = {"path": alternative.name, "sha256": file_digest(alternative)}
    elif fault == "changed-sealed-observation":
        observation["components"]["core-python"]["package"]["version"] = "unsealed"
        observed_path.write_text(json.dumps(observation))
        evidence["observation"]["sha256"] = file_digest(observed_path)
    elif fault == "missing-archive-member":
        with zipfile.ZipFile(directory / "notices.zip", "w") as archive:
            archive.writestr("different.json", observed_path.read_bytes())
    elif fault == "duplicate-archive-member":
        with zipfile.ZipFile(directory / "notices.zip", "a") as archive, pytest.warns(UserWarning):
            archive.write(observed_path, "native-runtime.json")
    elif fault == "invalid-archive":
        (directory / "notices.zip").write_bytes(b"corrupted archive")
    assert native_inventory_review(tmp_path, evidence) is (fault is None)
