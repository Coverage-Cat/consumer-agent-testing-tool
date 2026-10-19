"""Record complete regression evidence; a subset or skipped live test cannot pass."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from consumer_agent_testing_tool.regression import verify_regression, verify_run_record
from consumer_agent_testing_tool.release import EVIDENCE_DIR, results_ledger, source_digest
from consumer_agent_testing_tool.release_scope import RELEASE_SCOPE
from consumer_agent_testing_tool.serialization import digest, file_digest
from consumer_agent_testing_tool.surface import inside


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    report = inside(root, args.junit)
    before = source_digest(root)
    run_path = report.with_suffix(".run.json")
    run = json.loads(run_path.read_text())
    verify_run_record(run, before, str(report.relative_to(root)), file_digest(report),
                      file_digest(inside(root, run["log"])))
    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="regression-collection-", dir=cache))
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    for variable, name in (("HOME", "home"), ("TMPDIR", "tmp"),
                           ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
        directory = work / name
        directory.mkdir()
        env[variable] = str(directory)
    collection = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                                 "-o", "addopts=", "tests"],
        cwd=root, env=env, text=True, capture_output=True, timeout=60, check=False)
    (work / "collection.log").write_text(collection.stdout + collection.stderr)
    if collection.returncode:
        raise ValueError("Current pytest collection failed; inspect " + str(work.relative_to(root)))
    nodeids = [line.strip() for line in collection.stdout.splitlines()
               if line.startswith("tests/") and "::" in line]
    checks = verify_regression(report, nodeids)
    if source_digest(root) != before:
        raise ValueError("Regression source changed during verification")
    artifact = root / EVIDENCE_DIR / "regression.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({
        "gate": "regression", "basis": "test-report", "status": "passed",
        "release_scope": RELEASE_SCOPE,
        "source_sha256": before, "junit_sha256": file_digest(report),
        "run_record_sha256": file_digest(run_path),
        "junit": str(report.relative_to(root)), "collected_cases": len(nodeids),
        "collection_sha256": digest(sorted(nodeids)), "checks": checks,
        "recorded_at": datetime.now(UTC).isoformat(),
        "limits": "Complete default local-consumer collection; explicitly excluded development "
                  "extensions are not qualified. No live model, cloud or Muse claims.",
    }, indent=2) + "\n")
    ledger = results_ledger(root)
    entries = json.loads(ledger.read_text())
    entries["gates"]["regression"] = {
        "status": "passed", "basis": "test-report", "artifact": str(artifact.relative_to(root)),
        "sha256": file_digest(artifact),
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
    }
    ledger.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Recorded complete regression evidence: {len(checks)} cases, no failures or skips.")


if __name__ == "__main__":
    main()
