"""Explicit, workspace-contained Linux VM setup for the browser isolation proof."""
import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".cache/browser-vm"
PROFILE = "b"
PLAN = {
    "purpose": "Dedicated local browser isolation test VM; no model or customer service",
    "profile": PROFILE,
    "cpus": 2, "memory_gib": 4, "data_disk_gib": 20, "root_disk_gib": 20,
    "host_mounts": [], "ssh_agent_forwarding": False, "activate_global_context": False,
    "download_policy": "Explicit setup only; Colima guest/runtime downloads are not yet cached",
}


def environment():
    env = {name: value for name, value in os.environ.items() if name in ("PATH", "LANG", "LC_ALL")}
    for variable, child in (
        ("HOME", "home"), ("COLIMA_HOME", "colima"), ("LIMA_HOME", ROOT / ".l"),
        ("DOCKER_CONFIG", "docker"), ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"), ("TMPDIR", "tmp"),
    ):
        directory = WORK / child
        directory.mkdir(parents=True, exist_ok=True)
        env[variable] = str(directory)
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "start", "status", "stop"])
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This VM setup is scoped to this macOS ARM64 proof; other platforms are unverified")
    if not shutil.which("colima") or not shutil.which("docker"):
        raise SystemExit("Install Colima and Docker CLI explicitly before this setup")
    plan = {**PLAN, "workspace": str(WORK), "free_disk_bytes": shutil.disk_usage(ROOT).free}
    print(json.dumps(plan, indent=2), flush=True)
    if args.action == "plan":
        return
    env = environment()
    marker = WORK / "owned-vm.json"
    if marker.exists():
        if json.loads(marker.read_text()) != PLAN:
            raise SystemExit("Existing VM ownership/limits differ from this setup")
    elif args.action != "start" or (WORK / "colima" / PROFILE).exists():
        raise SystemExit("No matching owned VM marker; refusing to manage another VM")
    else:
        marker.write_text(json.dumps(PLAN, indent=2) + "\n")
        marker.chmod(0o600)
    command = ["colima", args.action, PROFILE]
    if args.action == "start":
        command += ["--cpus=2", "--memory=4", "--disk=20", "--root-disk=20",
                    "--runtime=docker", "--vm-type=vz", "--mount=none",
                    "--ssh-agent=false", "--ssh-config=false", "--activate=false",
                    "--template=false", "--binfmt=false", "--network-address=false"]
    log = WORK / f"{args.action}.log"
    with log.open("w") as output:
        log.chmod(0o600)
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                                timeout=900 if args.action == "start" else 60, check=False)
    print(f"{args.action}: exit {result.returncode}; log: {log.relative_to(ROOT)}", flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
    if args.action == "start":
        connection = {"docker_config": ".cache/browser-vm/docker",
                      "docker_socket": f".cache/browser-vm/colima/{PROFILE}/docker.sock",
                      "vm_profile": PROFILE, "host_mounts": []}
        (WORK / "connection.json").write_text(json.dumps(connection, indent=2) + "\n")
        print("Owned VM started; Docker connection is explicit in .cache/browser-vm/connection.json")


if __name__ == "__main__":
    main()
