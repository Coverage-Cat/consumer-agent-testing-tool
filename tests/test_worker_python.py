"""Exercise pip and isolated environment deployment without a hosted service."""
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.worker_python import deploy, pack
from consumer_agent_testing_tool.worker_python_bootstrap import environment_identity, install


@pytest.fixture
def wheel_bundle(tmp_path, monkeypatch):
    # Synthetic wheel: this tests installation/transport, not the real harness.
    expected = {"implementation": "synthetic", "packages": {"consumer-agent-testing-tool": "0.0.1"}}
    wheel = tmp_path / "consumer_agent_testing_tool-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("consumer_agent_testing_tool/__init__.py", "")
        archive.writestr("consumer_agent_testing_tool/worker_run.py",
            "def harness_identity(provider=None):\n    return " + repr(expected) + "\n")
        archive.writestr("consumer_agent_testing_tool-0.0.1.dist-info/METADATA",
                        "Metadata-Version: 2.1\nName: consumer-agent-testing-tool\nVersion: 0.0.1\n")
        archive.writestr("consumer_agent_testing_tool-0.0.1.dist-info/WHEEL",
                        "Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("consumer_agent_testing_tool-0.0.1.dist-info/RECORD", "")
    monkeypatch.setattr("consumer_agent_testing_tool.worker_python.harness_identity", lambda provider: expected)
    specification = tmp_path / "wheels.json"
    specification.write_text(json.dumps({"wheels": [wheel.name]}))
    package = pack(tmp_path, specification, "python.zip")
    return package, expected


def test_real_pip_install_reuse_and_file_tamper_rejection(tmp_path, wheel_bundle, monkeypatch):
    package, expected = wheel_bundle
    monkeypatch.setenv("SYNTHETIC_INFERENCE_SECRET", "must-not-reach-installer")
    archive = Path(package["archive"])
    first = install(tmp_path / "host with spaces", archive, package["sha256"])
    assert first["status"] == "installed" and first["harness"] == expected
    assert install(tmp_path / "host with spaces", archive, package["sha256"]) == first
    environment = Path(first["python"]).parent.parent
    receipt = json.loads((environment.parent / "deployment.json").read_text())
    assert receipt["files"]
    assert stat.S_IMODE((environment.parent / "deployment.json").stat().st_mode) == 0o600
    module = next(environment.glob("lib/python*/site-packages/consumer_agent_testing_tool/worker_run.py"))
    module.write_text(module.read_text() + "# tampered\n")
    with pytest.raises(ValueError, match="receipt differs"):
        install(tmp_path / "host with spaces", archive, package["sha256"])


async def test_source_only_bootstrap_writes_usable_gateway_configuration(tmp_path, wheel_bundle):
    package, expected = wheel_bundle
    result = await deploy(tmp_path, package["archive"], package["sha256"], "host", "worker.json")
    assert result["harness"] == expected
    config = json.loads((tmp_path / "worker.json").read_text())
    assert config["gateway"] == [
        result["python"], "-I", "-m", "consumer_agent_testing_tool.worker_gateway", str(tmp_path / "host/jobs")]
    with pytest.raises(LabError, match="output already exists"):
        await deploy(tmp_path, package["archive"], package["sha256"], "host", "worker.json")


@pytest.mark.parametrize("fault", ["hash", "platform", "missing-wheel", "traversal"])
def test_invalid_python_bundle_cannot_install(tmp_path, wheel_bundle, fault):
    package, _ = wheel_bundle
    archive = Path(package["archive"])
    checksum = package["sha256"]
    if fault == "hash":
        checksum = "0" * 64
    else:
        with zipfile.ZipFile(archive) as source:
            members = {name: source.read(name) for name in source.namelist()}
        lock = json.loads(members["python-lock.json"])
        name = next(iter(lock["wheels"]))
        if fault == "platform":
            lock["platform"] = {**environment_identity(), "machine": "wrong-machine"}
        elif fault == "missing-wheel":
            members.pop(name)
        else:
            lock["wheels"]["wheels/../outside.whl"] = lock["wheels"].pop(name)
            members["wheels/../outside.whl"] = members.pop(name)
        members["python-lock.json"] = json.dumps(lock).encode()
        with zipfile.ZipFile(archive, "w") as target:
            for name, content in members.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                target.writestr(info, content)
        from consumer_agent_testing_tool.worker_python_bootstrap import sha
        checksum = sha(archive)
    with pytest.raises(ValueError):
        install(tmp_path / "host", archive, checksum)
    assert not (tmp_path / "host").exists()


def test_bootstrap_environment_excludes_inference_and_user_config(tmp_path, monkeypatch):
    from consumer_agent_testing_tool.worker_python_bootstrap import clean_environment
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://invalid.example")
    monkeypatch.setenv("PYTHONPATH", "/not-allowed")
    env = clean_environment(tmp_path)
    assert not {"OPENROUTER_API_KEY", "PIP_INDEX_URL", "PYTHONPATH"} & env.keys()
    assert Path(env["HOME"]).is_relative_to(tmp_path)
    assert env["PIP_NO_INDEX"] == "1" and env["PIP_CONFIG_FILE"] == os.devnull


def test_worker_selected_python_remains_an_explicit_ssh_argument(tmp_path):
    import shlex

    from consumer_agent_testing_tool.workers import WorkerConfig, ssh_command
    (tmp_path / "key").touch()
    (tmp_path / "known").touch()
    python = "/tmp/worker python/venv/bin/python"
    config = WorkerConfig(provider="ssh", ssh_target="test-host", ssh_identity_file="key",
        ssh_known_hosts_file="known", ssh_python=python)
    command = ssh_command(tmp_path, config, shlex.join([config.ssh_python, "-I", "-c", "pass"]))
    assert shlex.split(command[-1])[0] == python
    assert "StrictHostKeyChecking=yes" in command
    assert str(tmp_path / "key") in command and "-A" not in command
