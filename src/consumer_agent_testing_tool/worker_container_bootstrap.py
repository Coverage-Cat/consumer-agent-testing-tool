"""Standard-library installation of an explicitly pinned, untagged Docker export."""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

FORMAT = "consumer-agent-testing-tool-container-v1"
MAX_BYTES = 2 * 1024**3
MAX_JSON = 1024**2


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate container manifest key")
        result[key] = value
    return result


def read_json(stream, limit=MAX_JSON):
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Container metadata exceeds limit")
    return json.loads(raw, object_pairs_hook=unique)


def image_identity(value):
    image = {"id": value["Id"], "os": value["Os"], "arch": value["Architecture"],
             "variant": value.get("Variant", "")}
    validate_image(image)
    return image


def validate_image(image):
    if set(image) != {"id", "os", "arch", "variant"} \
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image["id"]) \
            or image["os"] != "linux" or image["arch"] not in {"amd64", "arm64"} \
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{0,32}", image["variant"]):
        raise ValueError("Select an immutable Linux amd64/arm64 image")


def verify_tar(path, image):
    """Check the selected OCI graph without extracting layers on the host."""
    validate_image(image)
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        if len(members) > 32768 or len({row.name for row in members}) != len(members):
            raise ValueError("Duplicate or oversized container inventory")
        blobs = {}
        for item in members:
            if item.isdir() and item.name in {"blobs", "blobs/sha256"}:
                continue
            if not item.isfile() or not (item.name in {"index.json", "manifest.json", "oci-layout"}
                    or re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", item.name)):
                raise ValueError("Unexpected container archive member")
            if item.name.startswith("blobs/"):
                with archive.extractfile(item) as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != item.name.rsplit("/", 1)[1]:
                    raise ValueError("Container blob digest differs")
                blobs["sha256:" + actual] = item

        def document(name):
            with archive.extractfile(name) as stream:
                return read_json(stream)

        def descriptor(value):
            item = blobs.get(value["digest"])
            if item is None or type(value["size"]) is not int or value["size"] != item.size:
                raise ValueError("Container descriptor differs from blob")
            return item.name

        if document("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Unsupported container archive layout")
        index = document("index.json")
        if index["schemaVersion"] != 2 or len(index["manifests"]) != 1:
            raise ValueError("Container export must select one image")
        selected = index["manifests"][0]
        if selected["digest"] != image["id"] or selected.get("annotations"):
            raise ValueError("Container export identity differs or contains a named reference")
        manifest = document(descriptor(selected))
        config_name = descriptor(manifest["config"])
        layers = [descriptor(value) for value in manifest["layers"]]
        if manifest["schemaVersion"] != 2 or set(blobs) != {
                image["id"], manifest["config"]["digest"], *(row["digest"] for row in manifest["layers"])}:
            raise ValueError("Container export contains unreferenced or invalid content")
        legacy = document("manifest.json")
        if len(legacy) != 1 or legacy[0].get("RepoTags") not in (None, []) \
                or legacy[0]["Config"] != config_name or legacy[0]["Layers"] != layers:
            raise ValueError("Container export has tags or conflicting manifests")
        config = document(config_name)
        if (config["os"], config["architecture"], config.get("variant", "")) != (
                image["os"], image["arch"], image["variant"]):
            raise ValueError("Container image platform differs")


def inspect_bundle(path, expected, destination=None):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES \
            or sha(path) != expected:
        raise ValueError("Container bundle integrity failed")
    with zipfile.ZipFile(path) as archive:
        if sorted(archive.namelist()) != ["container-lock.json", "image.tar"]:
            raise ValueError("Container bundle inventory differs")
        with archive.open("container-lock.json") as stream:
            lock = read_json(stream)
        if set(lock) != {"format", "image", "tar_sha256", "tar_bytes"} or lock["format"] != FORMAT:
            raise ValueError("Invalid container bundle manifest")
        validate_image(lock["image"])
        info = archive.getinfo("image.tar")
        if info.compress_type != zipfile.ZIP_STORED or type(lock["tar_bytes"]) is not int \
                or not 0 < info.file_size == lock["tar_bytes"] <= MAX_BYTES \
                or not re.fullmatch(r"[a-f0-9]{64}", lock["tar_sha256"]):
            raise ValueError("Invalid container payload")
        with archive.open(info) as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != lock["tar_sha256"]:
                raise ValueError("Container payload digest differs")
        if destination:
            with archive.open(info) as stream, destination.open("xb") as output:
                shutil.copyfileobj(stream, output, 1024**2)
            destination.chmod(0o600)
            verify_tar(destination, lock["image"])
        return lock


class Docker:
    """Fixed Docker CLI operations; no default context, registry or credential helper."""
    def __init__(self, socket, directory, *, require_existing=False):
        self.socket = str(Path(socket).resolve(strict=True))
        if not stat.S_ISSOCK(Path(self.socket).stat().st_mode):
            raise ValueError("Select an existing Unix Docker socket")
        self.root = Path(directory).absolute()
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError("Linked container setup directories are forbidden")
        if require_existing and not self.root.is_dir():
            raise ValueError("Container deployment directory is missing")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config = self.root / "docker-config"
        if self.config.is_symlink():
            raise ValueError("Linked Docker configuration is forbidden")
        if require_existing and not (self.config / "config.json").is_file():
            raise ValueError("Deployed Docker configuration is missing")
        self.config.mkdir(mode=0o700, exist_ok=True)
        if self.config.stat().st_uid != os.getuid() or self.config.stat().st_mode & 0o077:
            raise ValueError("Docker configuration requires a private owner-only directory")
        configuration = self.config / "config.json"
        if configuration.exists() or configuration.is_symlink():
            if configuration.is_symlink() or configuration.read_text() != '{"auths":{}}\n':
                raise ValueError("Docker setup configuration changed")
        else:
            configuration.write_text('{"auths":{}}\n')
            configuration.chmod(0o600)
        self.env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        self.env.update(HOME=str(self.root), TMPDIR=str(self.root), DOCKER_CONFIG=str(self.config),
                        DOCKER_HOST="unix://" + self.socket)

    def call(self, *arguments, timeout=180):
        # Save/load output may contain arbitrary image metadata; retain it privately.
        with tempfile.TemporaryFile(dir=self.root) as output, tempfile.TemporaryFile(dir=self.root) as error:
            result = subprocess.run(["docker", "--config", str(self.config), "--host",
                "unix://" + self.socket, *arguments], env=self.env, stdout=output, stderr=error,
                timeout=timeout, check=False)
            output.seek(0)
            error.seek(0)
            if result.returncode:
                log = self.root / "docker-error.log"
                log.write_bytes(error.read(MAX_JSON))
                log.chmod(0o600)
                raise ValueError("Docker operation failed; inspect private docker-error.log")
            content = output.read(MAX_JSON + 1)
            if len(content) > MAX_JSON:
                raise ValueError("Docker response exceeds limit")
            return content.decode()

    def daemon(self):
        version = json.loads(self.call("version", "--format", "{{json .Server}}"))
        identifier = self.call("info", "--format", "{{.ID}}").strip()
        if not re.fullmatch(r"[a-zA-Z0-9:._-]{1,256}", identifier):
            raise ValueError("Docker daemon identity unavailable")
        return {"id": identifier, "version": version["Version"], "os": version["Os"], "arch": version["Arch"]}

    def inspect(self, image):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Docker image selection requires an immutable image ID")
        return image_identity(json.loads(self.call("image", "inspect", image, "--format", "{{json .}}")))


def install(bundle, expected, socket, directory):
    lock = inspect_bundle(bundle, expected)
    root = Path(directory).absolute()
    docker = Docker(socket, root)
    daemon = docker.daemon()
    if (daemon["os"], daemon["arch"]) != (lock["image"]["os"], lock["image"]["arch"]):
        raise ValueError("Container image and daemon platform differ")
    with tempfile.TemporaryDirectory(prefix="image-", dir=root) as temporary:
        image = Path(temporary) / "image.tar"
        inspect_bundle(bundle, expected, image)
        platform = "/".join(filter(None, (lock["image"]["os"], lock["image"]["arch"], lock["image"]["variant"])))
        docker.call("image", "load", "--quiet", "--platform", platform, "--input", str(image))
    if docker.inspect(lock["image"]["id"]) != lock["image"] or docker.daemon() != daemon:
        raise ValueError("Docker did not confirm the selected image and daemon")
    return {"format": FORMAT, "status": "installed", "bundle_sha256": expected,
            "image": lock["image"], "daemon": daemon, "requested_socket": str(socket),
            "environment": {"DOCKER_HOST": "unix://" + docker.socket, "DOCKER_CONFIG": str(docker.config)}}


def main():
    request = json.loads(sys.argv[1], object_pairs_hook=unique)
    root = Path(request["directory"]).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Linked container setup directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="upload-", dir=root) as temporary:
        archive = Path(temporary) / "container.zip"
        size = 0
        with archive.open("xb") as output:
            while chunk := sys.stdin.buffer.read(1024**2):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Container upload exceeds 2 GiB")
                output.write(chunk)
        archive.chmod(0o600)
        print(json.dumps(install(archive, request["sha256"], request["socket"], root)))


if __name__ == "__main__":
    main()
