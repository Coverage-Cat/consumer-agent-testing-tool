"""Snapshot installed OS packages and original notices from the tested image."""
import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--image", default="consumer-agent-testing-tool-sandbox:0.1.0")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
destination = root / "third_party/container"
destination.mkdir(parents=True, exist_ok=True)
image = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image]))[0]
probe = r"""
import base64, json, pathlib, subprocess
rows = subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\t${Architecture}\n']).decode()
notices = {}
roots = ['/usr/share/doc','/usr/share/common-licenses','/usr/local/lib/python3.14']
for directory in roots:
    for path in pathlib.Path(directory).rglob('*'):
        if not path.is_file():
            continue
        name = path.name.lower()
        if directory.endswith('common-licenses') or name.startswith(
                ('copyright','license','licence','notice','copying')):
            notices[str(path)] = base64.b64encode(path.read_bytes()).decode()
print(json.dumps({'packages':[row.split('\t') for row in rows.splitlines()], 'notices':notices}))
"""
raw = subprocess.check_output(["docker", "run", "--rm", "--pull=never", "--network=none",
    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
    "--entrypoint", "python", image["Id"], "-c", probe], timeout=60)
inventory = json.loads(raw)
references = []
for name, encoded in inventory["notices"].items():
    data = base64.b64decode(encoded)
    digest = hashlib.sha256(data).hexdigest()
    path = destination / "notices" / name.removeprefix("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    references.append({"source_path": name, "sha256": digest, "path": str(path.relative_to(root))})
document = {
    "image_id": image["Id"], "os": image["Os"], "architecture": image["Architecture"],
    "dockerfile_sha256": hashlib.sha256((root / "containers/Dockerfile").read_bytes()).hexdigest(),
    "packages": [{"name": name, "version": version, "architecture": architecture}
                 for name, version, architecture in inventory["packages"]],
    "original_notices": references,
    "scope": "Installed OS package inventory and original notices in the tested image; "
             "service terms and model artifacts are separate",
}
(destination / "inventory.json").write_text(json.dumps(document, indent=2) + "\n")
print(f"Inventoried {len(document['packages'])} OS packages and {len(references)} original notices")
