"""Container deployment integrity, private connection authority and real worker use."""
import io
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path

import pytest

from consumer_agent_testing_tool.config import BrowserWorkerSettings, Execution, WorkerPlacement
from consumer_agent_testing_tool.errors import LabError
from consumer_agent_testing_tool.serialization import digest, file_digest
from consumer_agent_testing_tool.worker_container import (
    deploy,
    host_identity,
    pack,
    select,
    verify_connections,
    worker_docker_connections,
)
from consumer_agent_testing_tool.worker_container_bootstrap import (
    FORMAT,
    Docker,
    inspect_bundle,
    sha,
    verify_tar,
)
from consumer_agent_testing_tool.workers import WorkerConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def short_connection():
    (ROOT / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dc-", dir=ROOT / ".cache") as directory:
        base = Path(directory)
        endpoint, config = base / "s", base / "c"
        config.mkdir()
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(endpoint))
            yield endpoint, config
        finally:
            sock.close()


def container_bundle(root, fault=None):
    import hashlib

    def blob(value):
        content = json.dumps(value).encode()
        return "sha256:" + hashlib.sha256(content).hexdigest(), content

    config_id, config = blob({"architecture": "arm64", "os": "linux",
                             "rootfs": {"type": "layers", "diff_ids": []}})
    image_id, manifest = blob({"schemaVersion": 2, "config": {
        "digest": config_id, "size": len(config)}, "layers": []})
    image = {"id": image_id, "os": "linux", "arch": "arm64", "variant": ""}
    prefix = "blobs/sha256/"
    members = {
        prefix + config_id[7:]: config,
        prefix + image_id[7:]: manifest,
        "index.json": json.dumps({"schemaVersion": 2, "manifests": [{
            "digest": image_id, "size": len(manifest)}]}).encode(),
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "manifest.json": json.dumps([{
            "Config": prefix + config_id[7:], "RepoTags": None, "Layers": []}]).encode(),
    }
    if fault == "blob":
        members[prefix + config_id[7:]] = b"changed"
    elif fault == "tag":
        row = json.loads(members["manifest.json"])
        row[0]["RepoTags"] = ["unrelated:existing-tag"]
        members["manifest.json"] = json.dumps(row).encode()
    elif fault == "extra":
        extra, content = blob({"not": "referenced"})
        members[prefix + extra[7:]] = content
    elif fault == "identity":
        image["id"] = "sha256:" + "f" * 64
    archive = root / "image.tar"
    with tarfile.open(archive, "w") as output:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))
        if fault == "link":
            info = tarfile.TarInfo("outside")
            info.type, info.linkname = tarfile.SYMTYPE, "/outside"
            output.addfile(info)
        if fault == "duplicate":
            info = tarfile.TarInfo("index.json")
            info.size = len(members["index.json"])
            output.addfile(info, io.BytesIO(members["index.json"]))
    lock = {"format": FORMAT, "image": image, "tar_sha256": sha(archive), "tar_bytes": archive.stat().st_size}
    target = root / "container.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as output:
        output.writestr("container-lock.json", json.dumps(lock))
        output.write(archive, "image.tar")
    return target, sha(target), image


def test_container_inventory_checks_graph_and_payload(tmp_path):
    bundle, checksum, image = container_bundle(tmp_path)
    assert inspect_bundle(bundle, checksum, tmp_path / "unpacked.tar")["image"] == image
    verify_tar(tmp_path / "unpacked.tar", image)
    with pytest.raises(ValueError, match="integrity"):
        inspect_bundle(bundle, "0" * 64)


@pytest.mark.parametrize("fault", ["blob", "tag", "extra", "identity", "link", "duplicate"])
async def test_invalid_container_never_reaches_setup_transport(tmp_path, monkeypatch, fault):
    bundle, checksum, _ = container_bundle(tmp_path, fault)

    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid container reached setup transport")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    with pytest.raises(ValueError):
        await deploy(tmp_path, bundle, checksum, "missing.socket", "host", "receipt.json")
    assert not (tmp_path / "receipt.json").exists()


def test_container_receipt_is_bound_to_host_hash_and_explicit_role(tmp_path):
    receipt = {"format": FORMAT, "status": "installed", "host_identity": host_identity(tmp_path),
        "bundle_sha256": "c"*64, "requested_socket": "/selected/socket",
        "daemon": {"id": "test-daemon", "version": "test", "os": "linux", "arch": "arm64"},
        "image": {"id": "sha256:" + "a" * 64, "os": "linux", "arch": "arm64", "variant": ""},
        "environment": {"DOCKER_HOST": "unix:///selected/socket", "DOCKER_CONFIG": "/selected/docker-config"}}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    placement = WorkerPlacement(gateway_config="gateway.json",
        container_images={"sandbox": {"receipt": str(path), "sha256": file_digest(path)}})
    gateway = WorkerConfig(provider="local-process-reference", gateway=["test-command"])
    execution = Execution()
    assert select(tmp_path, placement, gateway, execution) == {"sandbox": receipt}
    assert execution.sandbox_image == receipt["image"]["id"]
    assert placement.environment == receipt["environment"]
    path.write_text(json.dumps({**receipt, "host_identity": "d"*64}))
    with pytest.raises(LabError, match="receipt changed"):
        select(tmp_path, placement, gateway, Execution())
    placement.container_images["sandbox"].sha256 = file_digest(path)
    with pytest.raises(LabError, match="different worker host"):
        select(tmp_path, placement, gateway, Execution())
    with pytest.raises(ValueError):
        WorkerPlacement(gateway_config="gateway.json", container_images={"other": {"receipt": "x", "sha256": "a"*64}})


def test_container_setup_excludes_ambient_credentials_and_context(tmp_path, monkeypatch, short_connection):
    endpoint, _ = short_connection
    for name in ("OPENROUTER_API_KEY", "AWS_SECRET_ACCESS_KEY", "DOCKER_AUTH_CONFIG", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(name, "synthetic-must-not-forward")
    monkeypatch.setenv("DOCKER_HOST", "tcp://not-selected.invalid:2375")
    docker = Docker(endpoint, tmp_path / "private")
    assert docker.env["DOCKER_HOST"] == "unix://" + str(endpoint)
    assert not {"OPENROUTER_API_KEY", "AWS_SECRET_ACCESS_KEY", "DOCKER_AUTH_CONFIG", "SSH_AUTH_SOCK"} & docker.env.keys()
    assert (docker.config / "config.json").read_text() == '{"auths":{}}\n'
    (docker.config / "config.json").write_text('{"credsStore":"not-allowed"}')
    with pytest.raises(ValueError, match="configuration changed"):
        Docker(endpoint, tmp_path / "private")


@pytest.mark.parametrize("fault", [None, "daemon", "image", "environment", "configuration", "missing-config"])
def test_worker_rechecks_actual_image_daemon_and_environment(tmp_path, monkeypatch, short_connection, fault):
    endpoint, _ = short_connection
    docker = Docker(endpoint, tmp_path / "private")
    image = {"id": "sha256:" + "a"*64, "os": "linux", "arch": "arm64", "variant": ""}
    daemon = {"id": "selected-daemon", "version": "test", "os": "linux", "arch": "arm64"}
    selected = {"DOCKER_HOST": "unix://" + str(endpoint), "DOCKER_CONFIG": str(docker.config)}
    receipt = {"format": FORMAT, "status": "installed", "bundle_sha256": "b"*64,
               "host_identity": host_identity(tmp_path), "image": image, "daemon": daemon,
               "environment": selected, "requested_socket": str(endpoint)}
    environment = {**selected, "AWS_EC2_METADATA_DISABLED": "true"}
    configuration = {"container_images": {"sandbox": digest(receipt), "browser": digest(receipt)},
                     "runtime_environment": dict(environment)}
    monkeypatch.setattr(Docker, "daemon", lambda self: {**daemon, "id": "changed"} if fault == "daemon" else daemon)
    monkeypatch.setattr(Docker, "inspect", lambda self, value:
        {**image, "id": "sha256:" + "c"*64} if fault == "image" else image)
    if fault == "environment":
        environment["DOCKER_HOST"] = "unix:///not-selected/socket"
    elif fault == "configuration":
        configuration["container_images"]["sandbox"] = "changed"
    elif fault == "missing-config":
        (docker.config / "config.json").unlink()
    if fault:
        with pytest.raises((LabError, ValueError)):
            verify_connections(tmp_path, {"sandbox": receipt, "browser": receipt}, configuration, environment)
    else:
        connections = verify_connections(tmp_path, {"sandbox": receipt, "browser": receipt}, configuration, environment)
        assert len(connections) == 2 and connections[0]["socket"] == str(endpoint)


def test_browser_external_connection_requires_exact_private_authority(tmp_path, monkeypatch, short_connection):
    from types import SimpleNamespace

    from consumer_agent_testing_tool.browser_execution import validate_worker
    job = tmp_path / "job"
    job.mkdir()
    endpoint, config = short_connection
    image = "sha256:" + "a" * 64
    selected = {"socket": str(endpoint), "config": str(config), "image": image}
    worker = BrowserWorkerSettings(image=image, docker_socket=str(endpoint), docker_config=str(config))
    monkeypatch.setenv("CONSUMER_AGENT_TESTING_TOOL_WORKER_DOCKER", json.dumps([selected]))
    monkeypatch.setattr("consumer_agent_testing_tool.browser_execution.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"Id": image, "Os": "linux", "Architecture": "arm64"})))
    try:
        with pytest.raises(LabError, match="escapes"):
            validate_worker(worker, job)
        token = worker_docker_connections.set((selected,))
        try:
            validate_worker(worker, job)
            changed = worker.model_copy(update={"image": "sha256:" + "b"*64})
            with pytest.raises(LabError, match="escapes"):
                validate_worker(changed, job)
        finally:
            worker_docker_connections.reset(token)
        with pytest.raises(LabError, match="escapes"):
            validate_worker(worker, job)
    finally:
        assert worker_docker_connections.get() == ()


@pytest.mark.parametrize("authority", ["none", "wrong-image", "selected"])
def test_browser_sidecar_connection_authority_is_not_a_global_path_bypass(tmp_path, authority, short_connection):
    job = tmp_path / "job"
    job.mkdir()
    endpoint, config = short_connection
    image = "sha256:" + "a"*64
    selected = {"socket": str(endpoint), "config": str(config), "image": image}
    environment = {"PATH": os.environ["PATH"]}
    if authority != "none":
        environment["CONSUMER_AGENT_TESTING_TOOL_WORKER_DOCKER"] = json.dumps([
            {**selected, "image": image if authority == "selected" else "sha256:" + "b"*64}])
    program = """
const {ContainerBrowser}=await import(process.argv[1]);
const worker=new ContainerBrowser(process.argv[2],JSON.parse(process.argv[3]));
if ('CONSUMER_AGENT_TESTING_TOOL_WORKER_DOCKER' in worker.env) throw Error('Authority leaked into Docker environment');
console.log('accepted');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", program,
        str(ROOT / "src/consumer_agent_testing_tool/data/surface_host/browser_worker.mjs"), str(job),
        json.dumps({"image": image, "docker_socket": str(endpoint), "docker_config": str(config)})],
        env=environment, capture_output=True, text=True, timeout=10, check=False)
    assert (result.returncode == 0) == (authority == "selected")


@pytest.fixture(scope="module")
def browser_assets(tmp_path_factory):
    from test_worker_runtime import browser_runtime
    return browser_runtime.__wrapped__(tmp_path_factory)


@pytest.mark.skipif(os.environ.get("CONSUMER_AGENT_TESTING_TOOL_TEST_DOCKER") != "1", reason="Requires actual Docker boundary")
@pytest.mark.parametrize("role", ["sandbox", pytest.param("browser", marks=pytest.mark.skipif(
    os.environ.get("SURFACE_BROWSER_WORKER_TEST") != "1", reason="Requires actual browser worker"))])
async def test_deployed_missing_image_runs_native_worker_and_collects_evidence(
        tmp_path, monkeypatch, browser_assets, role):
    import test_worker_run
    import test_worker_runtime

    endpoint = os.environ["DOCKER_HOST"].removeprefix("unix://")
    docker = Docker(endpoint, tmp_path / "docker-test")
    base = ("consumer-agent-testing-tool-sandbox:0.1.0" if role == "sandbox" else
            json.loads((ROOT / ".cache/browser-vm/worker-image.json").read_text())["image"])
    base_id = docker.call("image", "inspect", base, "--format", "{{.Id}}").strip()
    context = tmp_path / "build"
    context.mkdir()
    (context / "Dockerfile").write_text(f"FROM {base_id}\nLABEL surface.lab-test={uuid.uuid4().hex}\n")
    iid = tmp_path / "image-id"
    docker.call("build", "--pull=false", "--network=none", "--quiet", "--iidfile", str(iid), str(context))
    image = docker.inspect(iid.read_text().strip())["id"]
    assert image != base_id
    present = True
    try:
        bundle = pack(ROOT, image, endpoint, tmp_path / "container.zip")
        if role == "sandbox":
            repeated = pack(ROOT, image, endpoint, tmp_path / "repeated.zip")
            assert repeated["sha256"] == bundle["sha256"]
        docker.call("image", "rm", "--no-prune", image)
        present = False
        with pytest.raises(ValueError, match="Docker operation failed"):
            docker.inspect(image)
        result = await deploy(ROOT, bundle["archive"], bundle["sha256"], endpoint,
                              tmp_path / "installed", tmp_path / "deployment.json")
        present = True
        assert result["status"] == "installed" and docker.inspect(image) == result["image"]
        with pytest.raises(LabError, match="already exists"):
            await deploy(ROOT, bundle["archive"], bundle["sha256"], endpoint,
                         tmp_path / "installed", tmp_path / "deployment.json")
        # Re-import is safe after an acknowledgement loss; no tag is created.
        retry = await deploy(ROOT, bundle["archive"], bundle["sha256"], endpoint,
                             tmp_path / "installed", tmp_path / "retry.json")
        assert retry["image"] == result["image"]
        module = test_worker_run if role == "sandbox" else test_worker_runtime
        original = module.configuration

        def configuration(root, **options):
            path, config = original(root, **options)
            config["execution"]["worker"]["container_images"] = {role: {
                "receipt": str(tmp_path / "deployment.json"), "sha256": result["sha256"]}}
            return path, config

        monkeypatch.setattr(module, "configuration", configuration)
        for name, value in result["environment"].items():
            monkeypatch.setenv(name, value)
        if role == "sandbox":
            await test_worker_run.test_worker_runs_native_inspect_scenario_and_independent_oracle(
                tmp_path, monkeypatch, "good", 0, "cli-skill")
        else:
            await test_worker_runtime.test_real_chromium_and_sdk_scenario_from_deployed_assets(
                tmp_path, monkeypatch, browser_assets, True)
        journals = list((tmp_path / ("selected-output" if role == "sandbox" else "runs")).glob("worker-*/worker.json"))
        assert journals
        for path in journals:
            record = json.loads(path.read_text())
            assert record["cleanup"] == "confirmed" and record["collection"] == "complete"
            selected = record["invocation"]["configuration"]
            assert selected["container_images"] == {role: digest(json.loads(
                (tmp_path / "deployment.json").read_text()))}
            field = selected["sandbox_image"] if role == "sandbox" else selected["browser_worker"]["image"]
            assert field == image
    finally:
        try:
            docker.inspect(image)
        except ValueError:
            if present:
                raise
        else:
            docker.call("image", "rm", "--no-prune", image)
        assert docker.inspect(base_id)["id"] == base_id
