"""Check staged repository paths and blob sizes without reading private or untracked files."""
from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BLOB_BYTES = 10 * 1024 * 1024
PRIVATE_PARTS = {
    ".private", ".venv", ".cache", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", ".consumer-agent-testing-tool-worker-claims",
}
OUTPUT_ROOTS = {"runs", "release", "exports", "output", "dist", "build", "reports"}
LOCAL_FILES = {
    "consumer-agent-testing-tool.yaml", ".claude/settings.local.json", ".claude/scheduled_tasks.lock",
}


def path_problem(name: str) -> str | None:
    path = PurePosixPath(name)
    if PRIVATE_PARTS.intersection(path.parts) or path.parts[0] in OUTPUT_ROOTS:
        return "private state, cache, or generated output"
    if name in LOCAL_FILES:
        return "local configuration"
    if path.name != ".env.example" and (path.name == ".env" or path.name.startswith(".env.")):
        return "environment credentials"
    if path.suffix in {".pyc", ".sqlite", ".sqlite3"} or path.name == ".DS_Store":
        return "local runtime state"
    return None


def check(root: Path) -> list[str]:
    entries = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root).split(b"\0")
    findings = []
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8", errors="surrogateescape")
        if problem := path_problem(name):
            findings.append(f"{name!r}: {problem}")
        if stage != "0" or mode not in {"100644", "100755"}:
            findings.append(f"{name!r}: unresolved conflict, symlink, or submodule")
            continue
        size = int(subprocess.check_output(["git", "cat-file", "-s", object_id], cwd=root))
        if size > MAX_BLOB_BYTES:
            findings.append(f"{name!r}: blob exceeds {MAX_BLOB_BYTES // (1024 * 1024)} MiB")
    return findings


def main() -> int:
    findings = check(ROOT)
    for finding in findings:
        print(finding)
    if not findings:
        print("Repository paths and blob sizes passed.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
