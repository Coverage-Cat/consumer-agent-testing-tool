"""Attach a completed fresh-install proof; never attest broader isolation or live gates."""
import json
from datetime import UTC, datetime, timedelta

from distribution_support import ROOT, sha, write_record
from local_distribution import verify

from consumer_agent_testing_tool.release import EVIDENCE_DIR, results_ledger


def main():
    report_path = ROOT / "dist/verification.json"
    report = json.loads(report_path.read_text())
    checks = verify(ROOT, report)
    record = {"gate": "distribution", "basis": "fresh-install", "status": "passed",
        "release_scope": report.get("release_scope"),
        "source_sha256": report["source_sha256"], "verification_sha256": sha(report_path),
        "recorded_at": datetime.now(UTC).isoformat(), "checks": checks,
        "wheel_sha256": report["wheel_sha256"], "source_archive_sha256": report["source_archive_sha256"],
        "limits": "macOS fresh installation and project notices; not a complete native-binary subcomponent "
                  "SBOM, OS browser isolation, live provider qualification, or real-Muse calibration"}
    artifact = ROOT / EVIDENCE_DIR / "distribution.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    write_record(artifact, record)
    ledger_path = results_ledger(ROOT)
    ledger = json.loads(ledger_path.read_text())
    ledger["gates"]["distribution"] = {"status": "passed", "basis": "fresh-install",
        "artifact": str(artifact.relative_to(ROOT)), "sha256": sha(artifact),
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat()}
    write_record(ledger_path, ledger)
    print("Recorded the fresh-install distribution gate; broader release gates were not modified.")


if __name__ == "__main__":
    main()
